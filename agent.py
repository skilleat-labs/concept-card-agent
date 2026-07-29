#!/usr/bin/env python3
"""AI 개념카드 자동발행 에이전트
흐름: 주제선택 → 문안생성 → 렌더링 → 자가검증(최대3회) → 업로드 → 인스타발행 → 이력기록
"""

import argparse
import base64
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

# 환경변수 로드 (python-dotenv)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv 없어도 환경변수가 직접 설정되면 동작

import anthropic
import requests
from jinja2 import Environment, FileSystemLoader

# ── 상수 ──────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"
TOPICS_FILE = BASE_DIR / "topics.json"
PUBLISHED_FILE = BASE_DIR / "published.json"
TEMPLATE_DIR = BASE_DIR
TEMPLATE_FILE = "template.html"
PROMPTS_DIR = BASE_DIR / "prompts"

MODEL = "claude-sonnet-4-6"
MAX_RETRIES = 3
CARD_WIDTH = 1080
CARD_HEIGHT = 1080  # 1:1 정사각형

KST = timezone(timedelta(hours=9))


# ── 유틸리티 ──────────────────────────────────────────────────────────

def log(step: str, message: str) -> None:
    """단계별 진행 상황 출력 (색상 없이 텍스트)."""
    now = datetime.now(KST).strftime("%H:%M:%S")
    print(f"[{now}] [{step}] {message}", flush=True)


def load_json(path: Path) -> list | dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: list | dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_prompt(name: str) -> str:
    prompt_path = PROMPTS_DIR / name
    with open(prompt_path, "r", encoding="utf-8") as f:
        return f.read()


def format_prompt(template: str, **kwargs) -> str:
    """안전한 프롬프트 치환. {key} → 값. JSON 예시의 {{ }} 와 충돌 없음."""
    result = template
    for key, value in kwargs.items():
        result = result.replace("{" + key + "}", str(value))
    return result


def strip_markdown_json(text: str) -> str:
    """마크다운 코드블록(```json ... ```) 제거 후 JSON 문자열 반환."""
    text = text.strip()
    # ```json ... ``` 또는 ``` ... ``` 형태 제거
    pattern = r"^```(?:json)?\s*([\s\S]*?)\s*```$"
    match = re.match(pattern, text, re.MULTILINE)
    if match:
        return match.group(1).strip()
    return text


def parse_json_response(text: str) -> dict:
    """Claude 응답에서 JSON 파싱 (마크다운 코드블록, 배열 래핑 등 처리)."""
    cleaned = strip_markdown_json(text)
    try:
        result = json.loads(cleaned)
        # 배열로 감싸진 경우 첫 번째 요소 사용
        if isinstance(result, list):
            return result[0]
        return result
    except json.JSONDecodeError:
        pass

    # 중첩된 JSON 객체만 추출 시도
    obj_match = re.search(r"\{[\s\S]*\}", cleaned)
    if obj_match:
        try:
            return json.loads(obj_match.group(0))
        except json.JSONDecodeError:
            pass

    # 배열 추출 시도
    arr_match = re.search(r"\[[\s\S]*\]", cleaned)
    if arr_match:
        try:
            result = json.loads(arr_match.group(0))
            if isinstance(result, list) and result:
                return result[0]
        except json.JSONDecodeError:
            pass

    raise ValueError(f"JSON 파싱 실패\n원문: {text[:300]}")


def convert_story_markup(text: str) -> str:
    """[blue]...[/blue] 등의 마커를 HTML <em> 태그로 변환."""
    for color in ("blue", "green", "orange", "red"):
        text = text.replace(f"[{color}]", f"<em class='{color}'>")
        text = text.replace(f"[/{color}]", "</em>")
    return text


def shorten_text(text: str, ratio: float) -> str:
    """텍스트를 ratio 비율(0~1)로 축약."""
    max_len = max(1, int(len(text) * ratio))
    if len(text) <= max_len:
        return text
    # 문장 중간 자르기 (한국어 어절 단위 고려)
    truncated = text[:max_len].rsplit(" ", 1)[0] if " " in text[:max_len] else text[:max_len]
    return truncated


# ── Claude API 호출 ────────────────────────────────────────────────────

def call_claude_text(client: anthropic.Anthropic, prompt: str, max_tokens: int = 1024) -> str:
    """텍스트 프롬프트로 Claude 호출, 텍스트 응답 반환."""
    message = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def call_claude_vision(client: anthropic.Anthropic, prompt: str, image_path: Path) -> str:
    """이미지 + 텍스트 프롬프트로 Claude 호출 (멀티모달)."""
    with open(image_path, "rb") as f:
        image_data = base64.standard_b64encode(f.read()).decode("utf-8")

    message = client.messages.create(
        model=MODEL,
        max_tokens=512,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": image_data,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    )
    return message.content[0].text


# ── 단계 1: 주제 선택 ─────────────────────────────────────────────────

def select_topic(client: anthropic.Anthropic, forced_topic: str | None = None) -> str:
    """발행할 주제 선택. forced_topic 지정 시 그대로 사용."""
    if forced_topic:
        log("SELECT", f"주제 강제 지정: {forced_topic}")
        return forced_topic

    log("SELECT", "주제 선택 중...")
    topics = load_json(TOPICS_FILE)
    published = load_json(PUBLISHED_FILE)
    published_topics = [entry["topic"] for entry in published] if published else []

    prompt_template = load_prompt("select_topic.txt")
    prompt = format_prompt(
        prompt_template,
        published=json.dumps(published_topics, ensure_ascii=False),
        topics=json.dumps(topics, ensure_ascii=False),
    )

    response_text = call_claude_text(client, prompt)
    result = parse_json_response(response_text)
    topic = result["topic"]
    reason = result.get("reason", "")

    log("SELECT", f"선택된 주제: {topic}")
    log("SELECT", f"선택 이유: {reason}")
    return topic


# ── 단계 2: 웹 리서치 (최신 Copilot 참고자료 수집) ───────────────────

SEARCH_SOURCES = [
    "https://techcommunity.microsoft.com/category/microsoft365copilot",
    "https://support.microsoft.com/ko-kr/copilot",
    "https://blogs.microsoft.com/blog/category/copilot/",
]

def fetch_topic_references(topic: str) -> str:
    """Microsoft 공식 블로그 등에서 주제 관련 최신 내용을 가져와 참고자료 문자열로 반환."""
    log("RESEARCH", f"참고자료 수집 중: {topic}")

    # 검색 쿼리 구성 (영문으로 더 잘 나옴)
    query_map = {
        "Copilot": "Microsoft Copilot tips productivity 2025 2026",
        "이메일": "Microsoft Copilot email writing tips Outlook",
        "회의": "Microsoft Copilot Teams meeting summary tips",
        "Excel": "Microsoft Copilot Excel data analysis tips",
        "Word": "Microsoft Copilot Word document writing tips",
        "PPT": "Microsoft Copilot PowerPoint presentation tips",
        "보고서": "Microsoft Copilot report writing tips Word",
        "프롬프트": "Microsoft Copilot prompt tips best practices",
    }

    # 주제에 맞는 쿼리 선택
    query = f"Microsoft Copilot {topic} tips 2025 2026 site:techcommunity.microsoft.com OR site:blogs.microsoft.com"
    for keyword, specific_query in query_map.items():
        if keyword in topic:
            query = specific_query
            break

    references = []

    # Microsoft Tech Community RSS 시도
    rss_urls = [
        "https://techcommunity.microsoft.com/plugins/custom/microsoft/o365/blog-rss?board=MicrosoftCopilotBlog",
        "https://www.microsoft.com/en-us/microsoft-365/blog/feed/",
    ]

    for url in rss_urls:
        try:
            resp = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code == 200:
                # 간단한 텍스트 추출 (RSS XML에서 title/description)
                titles = re.findall(r"<title><!\[CDATA\[(.*?)\]\]></title>", resp.text)
                descs = re.findall(r"<description><!\[CDATA\[(.*?)\]\]></description>", resp.text[:8000])
                if titles:
                    snippet = "\n".join(f"- {t}" for t in titles[:5])
                    references.append(f"[Microsoft 공식 블로그 최신 글]\n{snippet}")
                    break
        except Exception:
            continue

    # 폴백: Microsoft Learn Copilot 페이지
    if not references:
        try:
            resp = requests.get(
                "https://learn.microsoft.com/ko-kr/copilot/microsoft-365/",
                timeout=8,
                headers={"User-Agent": "Mozilla/5.0"}
            )
            if resp.status_code == 200:
                # h2, h3 태그에서 섹션 제목 추출
                headings = re.findall(r"<h[23][^>]*>(.*?)</h[23]>", resp.text)
                clean = [re.sub(r"<[^>]+>", "", h).strip() for h in headings if h.strip()][:8]
                if clean:
                    references.append(f"[Microsoft Learn - Copilot 공식 문서 섹션]\n" + "\n".join(f"- {h}" for h in clean))
        except Exception:
            pass

    if references:
        result = "\n\n".join(references)
        log("RESEARCH", f"참고자료 {len(references)}건 수집 완료")
        return result
    else:
        log("RESEARCH", "참고자료 수집 실패 — 프롬프트 기본값 사용")
        return ""


# ── 단계 3: 문안 생성 ─────────────────────────────────────────────────

def write_card_content(client: anthropic.Anthropic, topic: str, references: str = "") -> dict:
    """카드 문안 생성 — 텍스트와 SVG 다이어그램을 분리 호출."""
    # ── 1단계: 텍스트 콘텐츠 ──
    log("WRITE", f"문안 생성 중: {topic}")
    prompt_template = load_prompt("write_card.txt")
    ref_section = f"\n[참고자료 — 아래 내용을 반영해 작성할 것]\n{references}\n" if references else ""
    prompt = format_prompt(prompt_template, topic=topic) + ref_section

    response_text = call_claude_text(client, prompt)
    log("WRITE", f"응답 원문 (앞300자): {response_text[:300]!r}")
    content = parse_json_response(response_text)

    required_keys = ["title", "hook", "category", "summary", "definition", "tags"]
    for key in required_keys:
        if key not in content:
            raise ValueError(f"문안 응답에 '{key}' 키 누락: {content}")

    log("WRITE", f"제목: {content['title']} | 카테고리: {content['category']}")
    log("WRITE", f"정의: {content['definition']}")

    # ── 3단계: 스토리 별도 생성 ──
    log("WRITE", "스토리 생성 중...")
    story_prompt = format_prompt(
        load_prompt("write_story.txt"),
        topic=content["title"],
        definition=content["definition"],
    )
    raw_story = call_claude_text(client, story_prompt, max_tokens=512).strip()
    content["story"] = convert_story_markup(raw_story)
    log("WRITE", f"스토리 생성 완료 ({len(content['story'])}자)")

    # ── 2단계: SVG 다이어그램 ──
    log("WRITE", "SVG 다이어그램 생성 중...")
    svg_prompt = format_prompt(
        load_prompt("draw_diagram.txt"),
        topic=content["title"],
        definition=content["definition"],
    )
    svg_text = call_claude_text(client, svg_prompt, max_tokens=4096).strip()
    log("WRITE", f"SVG 원문 길이: {len(svg_text)}자 | 앞부분: {svg_text[:80]!r} | 끝부분: {svg_text[-60:]!r}")

    # SVG 태그만 추출 (greedy — 가장 큰 SVG 블록)
    svg_match = re.search(r"(<svg[\s\S]*</svg>)", svg_text, re.IGNORECASE)
    if not svg_match:
        # </svg> 없으면 직접 닫기 시도
        if svg_text.startswith("<svg") and not svg_text.rstrip().endswith("</svg>"):
            svg_text = svg_text + "\n</svg>"
            svg_match = re.search(r"(<svg[\s\S]*</svg>)", svg_text, re.IGNORECASE)
    content["diagram_svg"] = svg_match.group(1) if svg_match else "<svg viewBox='0 0 936 380' xmlns='http://www.w3.org/2000/svg' width='936' height='380'></svg>"
    log("WRITE", f"SVG 추출 완료 ({len(content['diagram_svg'])}자)")

    return content


# ── 단계 3: 캐러셀 5장 렌더링 ────────────────────────────────────────

def render_three_slides(content: dict, topic: str, timestamp: str) -> list[Path]:
    """3장 슬라이드 렌더링: 커버 → 다이어그램 → 핵심정리. 브라우저 1개 재사용."""
    from playwright.sync_api import sync_playwright

    log("RENDER", "3장 슬라이드 렌더링 시작...")
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    safe_topic = re.sub(r"[^\w가-힣]", "_", topic)

    ctx = dict(
        title=content["title"],
        hook=content.get("hook", content["title"]),
        category=content.get("category", ""),
        summary=content.get("summary", ""),
        definition=content.get("definition", ""),
        story=content.get("story", ""),
        diagram_svg=content.get("diagram_svg", ""),
        tags=content.get("tags", []),
    )

    slide_templates = [
        ("template_slide1.html", f"slide1_{safe_topic}_{timestamp}.png", "커버"),
        ("template_slide2.html", f"slide2_{safe_topic}_{timestamp}.png", "다이어그램"),
        ("template_slide3.html", f"slide3_{safe_topic}_{timestamp}.png", "핵심정리"),
    ]

    paths = []
    tmp_files = []

    try:
        # HTML 파일을 먼저 모두 생성
        slides = []
        for tmpl_name, fname, label in slide_templates:
            html = env.get_template(tmpl_name).render(**ctx)
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".html", delete=False, encoding="utf-8"
            ) as tmp:
                tmp.write(html)
                tmp_files.append(tmp.name)
            slides.append((tmp.name, OUTPUT_DIR / fname, label))

        # 브라우저 1개로 3장 순차 캡처
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": CARD_WIDTH, "height": CARD_HEIGHT})
            for tmp_path, out, label in slides:
                page.goto(f"file://{tmp_path}", wait_until="load")
                page.wait_for_timeout(3000)
                page.screenshot(path=str(out), full_page=False, clip={
                    "x": 0, "y": 0, "width": CARD_WIDTH, "height": CARD_HEIGHT
                })
                paths.append(out)
                log("RENDER", f"슬라이드 {label}: {out.name}")
            browser.close()
    finally:
        for f in tmp_files:
            try:
                os.unlink(f)
            except OSError:
                pass

    log("RENDER", "3장 완료")
    return paths


# ── 단계 4: 자가검증 ──────────────────────────────────────────────────

def review_card(client: anthropic.Anthropic, image_path: Path) -> dict:
    """카드 이미지 자가검증. 결과 dict 반환 (pass, issues, fix)."""
    log("REVIEW", "카드 검증 중...")

    prompt = load_prompt("review_card.txt")
    response_text = call_claude_vision(client, prompt, image_path)
    result = parse_json_response(response_text)

    passed = result.get("pass", False)
    issues = result.get("issues", [])
    fix = result.get("fix", "")

    if passed:
        log("REVIEW", "검증 통과")
    else:
        log("REVIEW", f"검증 실패: {issues}")
        log("REVIEW", f"수정 지시: {fix}")

    return result


def apply_fix(content: dict, fix_instruction: str) -> dict:
    """검증 실패 시 fix 지시에 따라 content 자동 수정 (길이 축약)."""
    log("FIX", f"수정 적용 중: {fix_instruction}")

    # 'points'를 줄여야 한다는 지시가 있으면 모든 point 축약
    if "points" in fix_instruction.lower() or "핵심" in fix_instruction:
        # 지시에서 목표 글자 수 추출 시도
        char_match = re.search(r"(\d+)\s*자", fix_instruction)
        if char_match:
            max_chars = int(char_match.group(1))
            content["points"] = [
                p[:max_chars] if len(p) > max_chars else p
                for p in content["points"]
            ]
        else:
            # 목표 글자 수 명시 없으면 0.8 비율로 축약
            content["points"] = [shorten_text(p, 0.8) for p in content["points"]]

    # 'summary'를 줄여야 한다는 지시
    if "summary" in fix_instruction.lower() or "요약" in fix_instruction:
        char_match = re.search(r"(\d+)\s*자", fix_instruction)
        if char_match:
            max_chars = int(char_match.group(1))
            content["summary"] = content["summary"][:max_chars]
        else:
            content["summary"] = shorten_text(content["summary"], 0.8)

    # 'title'을 줄여야 한다는 지시
    if "title" in fix_instruction.lower() or "제목" in fix_instruction:
        char_match = re.search(r"(\d+)\s*자", fix_instruction)
        if char_match:
            max_chars = int(char_match.group(1))
            content["title"] = content["title"][:max_chars]
        else:
            content["title"] = shorten_text(content["title"], 0.8)

    return content


def render_and_review_loop(
    client: anthropic.Anthropic,
    content: dict,
    topic: str,
    timestamp: str,
) -> tuple[list[Path], dict]:
    """3장 슬라이드 렌더링 + 커버 자가검증 루프. MAX_RETRIES 이내에 통과해야 함."""
    for attempt in range(1, MAX_RETRIES + 1):
        log("LOOP", f"렌더링+검증 시도 {attempt}/{MAX_RETRIES}")
        paths = render_three_slides(content, topic, timestamp)
        # 커버(슬라이드 1)를 기준으로 검증
        review_result = review_card(client, paths[0])

        if review_result.get("pass", False):
            log("LOOP", f"검증 통과 (시도 {attempt}회)")
            return paths, content

        if attempt < MAX_RETRIES:
            fix_instruction = review_result.get("fix", "")
            if fix_instruction:
                content = apply_fix(content, fix_instruction)
            else:
                for key in ("definition", "why", "usage", "confusion"):
                    if key in content:
                        content[key] = shorten_text(content[key], 0.85)

    log("LOOP", f"경고: {MAX_RETRIES}회 시도 후에도 검증 미통과. 마지막 결과물 사용.")
    return paths, content


# ── 단계 5: Azure Blob 업로드 ─────────────────────────────────────────

def upload_to_azure(image_path: Path) -> str:
    """Azure Blob Storage에 PNG 업로드 후 공개 URL 반환."""
    log("UPLOAD", "Azure Blob Storage 업로드 중...")

    try:
        from azure.storage.blob import BlobServiceClient, ContentSettings
    except ImportError:
        raise RuntimeError(
            "azure-storage-blob 패키지가 설치되지 않았습니다. "
            "pip install azure-storage-blob 실행 후 재시도하세요."
        )

    conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    account_name = os.environ.get("AZURE_STORAGE_ACCOUNT")
    container_name = os.environ.get("AZURE_CONTAINER_NAME", "concept-cards")

    if not conn_str:
        raise ValueError("환경변수 AZURE_STORAGE_CONNECTION_STRING 미설정")
    if not account_name:
        raise ValueError("환경변수 AZURE_STORAGE_ACCOUNT 미설정")

    # Instagram은 한글 URL을 지원하지 않으므로 파일명을 ASCII로 변환
    import re as _re
    stem = _re.sub(r'[^\w]', '_', image_path.stem, flags=_re.ASCII)
    blob_name = f"cards/{stem}.png"

    blob_service_client = BlobServiceClient.from_connection_string(conn_str)
    blob_client = blob_service_client.get_blob_client(
        container=container_name, blob=blob_name
    )

    with open(image_path, "rb") as data:
        blob_client.upload_blob(
            data,
            overwrite=True,
            content_settings=ContentSettings(content_type="image/png"),
        )

    public_url = (
        f"https://{account_name}.blob.core.windows.net/{container_name}/{blob_name}"
    )
    log("UPLOAD", f"업로드 완료: {public_url}")
    return public_url


# ── 단계 6: Instagram 캡션 생성 ──────────────────────────────────────

def generate_instagram_caption(client: anthropic.Anthropic, content: dict) -> str:
    """Claude로 Instagram 캡션 자동 생성."""
    tags = content.get("tags", [])
    hashtags_str = " ".join(f"#{t.lstrip('#')}" for t in tags)

    prompt = f"""당신은 인프라/개발 지식을 쉽게 전달하는 인스타그램 계정 운영자입니다.
아래 개념 카드 내용을 바탕으로 Instagram 게시물 캡션을 작성해주세요.

[개념 카드 정보]
- 제목: {content.get('title', '')}
- 훅(hook): {content.get('hook', '')}
- 요약: {content.get('summary', '')}
- 정의: {content.get('definition', '')}

[캡션 형식 - 반드시 이 구조를 따르세요]
1. 첫 줄: 독자의 공감/호기심을 자극하는 질문이나 문장 (1~2줄)
2. 빈 줄
3. 핵심 개념을 쉽게 풀어쓴 설명 (3~5줄, 비유 활용)
4. 빈 줄
5. CTA: "더 자세한 설명은 유튜브 감테크에서 확인하세요 🎬"
6. 빈 줄
7. "💙 팔로우하면 매주 인프라 상식 카드를 받아볼 수 있어요"
8. 빈 줄
9. 해시태그: {hashtags_str} #인프라카드 #개발자 #IT상식 #감테크

주의사항:
- **, *, #, __ 같은 마크다운 문법 절대 사용 금지
- 강조하고 싶으면 따옴표나 말투로만 표현하세요
- 캡션 텍스트만 출력하세요. 설명이나 부가 텍스트 없이."""

    msg = client.messages.create(
        model=MODEL,
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    caption = msg.content[0].text.strip()
    log("CAPTION", f"캡션 생성 완료 ({len(caption)}자)")
    return caption


# ── 단계 7: Instagram 발행 ────────────────────────────────────────────

def publish_carousel_to_instagram(image_urls: list[str], content: dict) -> str:
    """Instagram Graph API로 캐러셀(5장) 발행. 게시물 ID 반환."""
    log("INSTAGRAM", f"Instagram 캐러셀 발행 중 ({len(image_urls)}장)...")

    ig_user_id = os.environ.get("IG_USER_ID")
    ig_access_token = os.environ.get("IG_ACCESS_TOKEN")

    if not ig_user_id:
        raise ValueError("환경변수 IG_USER_ID 미설정")
    if not ig_access_token:
        raise ValueError("환경변수 IG_ACCESS_TOKEN 미설정")

    base_url = f"https://graph.instagram.com/v21.0/{ig_user_id}"
    caption = content.get("caption", f"{content.get('title', '')}\n\n{content.get('summary', '')}")

    # 1단계: 각 이미지 개별 미디어 컨테이너 생성
    child_ids = []
    for i, url in enumerate(image_urls, 1):
        resp = requests.post(f"{base_url}/media", data={
            "image_url": url,
            "is_carousel_item": "true",
            "access_token": ig_access_token,
        }, timeout=30)
        if not resp.ok:
            raise RuntimeError(f"슬라이드 {i} 미디어 컨테이너 오류 {resp.status_code}: {resp.text}")
        child_id = resp.json().get("id")
        if not child_id:
            raise RuntimeError(f"슬라이드 {i} 컨테이너 생성 실패: {resp.json()}")
        child_ids.append(child_id)
        log("INSTAGRAM", f"슬라이드 {i}/{len(image_urls)} 컨테이너: {child_id}")

    # 2단계: 캐러셀 컨테이너 생성
    carousel_resp = requests.post(f"{base_url}/media", data={
        "media_type": "CAROUSEL",
        "children": ",".join(child_ids),
        "caption": caption,
        "access_token": ig_access_token,
    }, timeout=30)
    if not carousel_resp.ok:
        raise RuntimeError(f"캐러셀 컨테이너 오류 {carousel_resp.status_code}: {carousel_resp.text}")
    carousel_id = carousel_resp.json().get("id")
    if not carousel_id:
        raise RuntimeError(f"캐러셀 컨테이너 생성 실패: {carousel_resp.json()}")
    log("INSTAGRAM", f"캐러셀 컨테이너 ID: {carousel_id}")

    # 3단계: 발행 (Instagram 처리 대기 후 시도)
    import time as _time
    _time.sleep(3)
    publish_resp = requests.post(f"{base_url}/media_publish", data={
        "creation_id": carousel_id,
        "access_token": ig_access_token,
    }, timeout=30)
    if not publish_resp.ok:
        raise RuntimeError(f"발행 오류 {publish_resp.status_code}: {publish_resp.text}")
    post_id = publish_resp.json().get("id")
    if not post_id:
        raise RuntimeError(f"게시물 발행 실패: {publish_resp.json()}")

    log("INSTAGRAM", f"발행 완료. 게시물 ID: {post_id}")
    return post_id


# ── 단계 7: 이력 기록 ─────────────────────────────────────────────────

def record_published(
    topic: str,
    content: dict,
    image_path: Path,
    post_id: str | None = None,
    image_url: str | None = None,
) -> None:
    """published.json에 발행 이력 추가."""
    log("RECORD", "발행 이력 기록 중...")

    published = load_json(PUBLISHED_FILE)
    if not isinstance(published, list):
        published = []

    # topics.json에서 next_question 찾기
    next_question = ""
    try:
        all_topics = load_json(TOPICS_FILE)
        if isinstance(all_topics, list) and all_topics and isinstance(all_topics[0], dict):
            matched = next((t for t in all_topics if t.get("topic") == topic), None)
            if matched:
                next_question = matched.get("next_question", "")
    except Exception:
        pass

    entry = {
        "topic": topic,
        "title": content.get("title", ""),
        "next_question": next_question,
        "thread": "",
        "published_at": datetime.now(KST).isoformat(),
        "image_file": str(image_path.name),
        "post_id": post_id,
        "image_url": image_url,
    }
    published.append(entry)
    save_json(PUBLISHED_FILE, published)
    log("RECORD", f"이력 저장 완료 (총 {len(published)}건)")


# ── 메인 ──────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="AI 개념카드 자동발행 에이전트",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  python agent.py --render-only --topic "프롬프트 엔지니어링"
  python agent.py --dry-run
  python agent.py --dry-run --topic "벡터 DB"
  python agent.py
""",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="실제 발행(Azure 업로드, Instagram) 없이 전체 흐름 실행",
    )
    parser.add_argument(
        "--topic",
        type=str,
        default=None,
        metavar="TOPIC",
        help="발행할 주제 강제 지정 (미지정 시 AI가 자동 선택)",
    )
    parser.add_argument(
        "--render-only",
        action="store_true",
        help="렌더링까지만 실행 (검증, 업로드, 발행 생략). API 키만 있으면 됨.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    print("=" * 60)
    print("  AI 개념카드 자동발행 에이전트")
    print(f"  실행 시각: {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S KST')}")
    if args.dry_run:
        print("  모드: DRY-RUN (실제 발행 없음)")
    elif args.render_only:
        print("  모드: RENDER-ONLY (렌더링까지만)")
    else:
        print("  모드: 실제 발행")
    print("=" * 60)

    # 환경변수 확인
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[ERROR] ANTHROPIC_API_KEY 환경변수가 설정되지 않았습니다.")
        sys.exit(1)

    # Anthropic 클라이언트 초기화
    client = anthropic.Anthropic(api_key=api_key)

    # 출력 디렉토리 생성
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        # ── 단계 1: 주제 선택 ──
        topic = select_topic(client, forced_topic=args.topic)

        # ── 단계 2: 웹 리서치 (최신 Copilot 정보 수집) ──
        references = fetch_topic_references(topic)

        # ── 단계 3: 문안 생성 ──
        content = write_card_content(client, topic, references)

        # 타임스탬프
        safe_topic = re.sub(r"[^\w가-힣]", "_", topic)
        timestamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")

        if args.render_only:
            # ── 렌더링만 (검증 없음) ──
            paths = render_three_slides(content, topic, timestamp)
            print()
            print(f"[완료] 3장 슬라이드 저장:")
            for p in paths:
                print(f"  {p}")
            return

        # ── 단계 4+5: 렌더링 + 자가검증 루프 ──
        final_paths, final_content = render_and_review_loop(
            client, content, topic, timestamp
        )

        if args.dry_run:
            print()
            print(f"[DRY-RUN] 렌더링 및 검증 완료. 업로드/발행 생략.")
            for p in final_paths:
                print(f"[DRY-RUN] PNG: {p}")
            return

        # ── 단계 6: Azure 업로드 (3장) ──
        image_urls = [upload_to_azure(p) for p in final_paths]

        # ── 단계 6.5: Instagram 캡션 생성 ──
        caption = generate_instagram_caption(client, final_content)

        # ── 단계 7: Instagram 캐러셀 발행 (3장) ──
        post_id = publish_carousel_to_instagram(image_urls, {**final_content, "caption": caption})

        # ── 단계 8: 이력 기록 ──
        record_published(topic, final_content, final_paths[0], post_id=post_id, image_url=image_urls[0])

        print()
        print("=" * 60)
        print("  발행 완료!")
        print(f"  주제: {topic}")
        print(f"  게시물 ID: {post_id}")
        print(f"  슬라이드: {len(final_paths)}장")
        print("=" * 60)

        # Mac 알림
        import subprocess
        subprocess.run([
            "osascript", "-e",
            f'display notification "📸 {topic} 카드가 Instagram에 발행됐어요! (게시물 ID: {post_id})" with title "개념카드 에이전트 ✅" sound name "Glass"'
        ], check=False)

    except KeyboardInterrupt:
        print("\n[중단] 사용자에 의해 중단되었습니다.")
        sys.exit(130)
    except Exception as e:
        print(f"\n[ERROR] 실행 중 오류 발생: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
