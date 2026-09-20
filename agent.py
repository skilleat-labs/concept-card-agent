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

    # 주제 소진 감지: Claude가 유효한 topic을 반환하지 못한 경우
    all_topic_names = [t["topic"] for t in topics if isinstance(t, dict)]
    if topic not in all_topic_names:
        log("SELECT", f"⚠️  전체 주제 소진 — 발행 가능한 새 주제가 없습니다. 에이전트를 종료합니다.")
        sys.exit(0)

    log("SELECT", f"선택된 주제: {topic}")
    log("SELECT", f"선택 이유: {reason}")
    return topic


# ── 단계 3: 문안 생성 ─────────────────────────────────────────────────

def write_card_content(client: anthropic.Anthropic, topic: str) -> dict:
    """카드 문안 생성 — 공감형 훅 + 리스트 아이템."""
    log("WRITE", f"문안 생성 중: {topic}")

    # topics.json에서 카테고리와 아이콘 조회
    topics = load_json(TOPICS_FILE)
    topic_obj = next((t for t in topics if isinstance(t, dict) and t.get("topic") == topic), {})
    category = topic_obj.get("category", "인프라")
    icon = topic_obj.get("icon", "💡")

    prompt_template = load_prompt("write_card.txt")
    prompt = format_prompt(prompt_template, topic=topic, category=category)

    response_text = call_claude_text(client, prompt, max_tokens=2048)
    log("WRITE", f"응답 원문 (앞300자): {response_text[:300]!r}")
    content = parse_json_response(response_text)

    required_keys = ["hook", "items", "key_concept", "tags"]
    for key in required_keys:
        if key not in content:
            raise ValueError(f"문안 응답에 '{key}' 키 누락: {content}")

    content["category"] = category
    content["icon"] = icon
    log("WRITE", f"카테고리: {content['category']} | 아이템 수: {len(content.get('items', []))}")

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
        hook=content.get("hook", ""),
        category=content.get("category", ""),
        icon=content.get("icon", "💡"),
        items=content.get("items", []),
        key_concept=content.get("key_concept", {"subject": "", "why": "", "detail": ""}),
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
    try:
        result = parse_json_response(response_text)
    except Exception:
        result = {}
    if not isinstance(result, dict):
        log("REVIEW", f"검증 응답 파싱 오류 ({type(result).__name__}), 통과 처리")
        return {"pass": True, "issues": [], "fix": ""}

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

    if "items" in fix_instruction.lower() or "아이템" in fix_instruction or "핵심" in fix_instruction:
        char_match = re.search(r"(\d+)\s*자", fix_instruction)
        for item in content.get("items", []):
            if isinstance(item, dict) and "desc" in item:
                if char_match:
                    max_chars = int(char_match.group(1))
                    item["desc"] = item["desc"][:max_chars]
                else:
                    item["desc"] = shorten_text(item["desc"], 0.8)

    if "hook" in fix_instruction.lower() or "훅" in fix_instruction:
        char_match = re.search(r"(\d+)\s*자", fix_instruction)
        if char_match:
            max_chars = int(char_match.group(1))
            content["hook"] = content["hook"][:max_chars]
        else:
            content["hook"] = shorten_text(content["hook"], 0.8)

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
    items_text = "\n".join(f"- {item.get('title', '')}" for item in content.get("items", []))

    prompt = f"""당신은 주니어 개발자 취업을 돕는 인스타그램 계정 운영자입니다.
아래 카드 내용으로 저장하고 싶어지는 짧고 강렬한 캡션을 작성하세요.

[카드 정보]
- 훅: {content.get('hook', '').replace('<br>', ' ')}
- 카테고리: {content.get('category', '')}
- 핵심 포인트:
{items_text}

[캡션 형식 - 반드시 이 구조를 따르세요]
1. 첫 줄: 강렬한 훅 문장 (20자 이내, 이모지 1개)
2. 빈 줄
3. 핵심 포인트 3가지 (각 줄 이모지로 시작)
4. 빈 줄
5. "취준 중이라면 저장해두세요 🔖"
6. 빈 줄
7. "💜 팔로우하면 매일 인프라 지식 1개"
8. 빈 줄
9. 해시태그: {hashtags_str} #백엔드취업 #개발자취준 #인프라공부

주의사항:
- **, *, __ 같은 마크다운 문법 절대 사용 금지
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

    # 3단계: 발행 (최대 3회 재시도, Instagram 처리 대기)
    import time as _time
    post_id = None
    for attempt in range(1, 4):
        _time.sleep(5 * attempt)
        publish_resp = requests.post(f"{base_url}/media_publish", data={
            "creation_id": carousel_id,
            "access_token": ig_access_token,
        }, timeout=30)
        if publish_resp.ok:
            post_id = publish_resp.json().get("id")
            if post_id:
                break
        log("INSTAGRAM", f"발행 시도 {attempt}/3 실패: {publish_resp.text}")
    if not post_id:
        raise RuntimeError(f"발행 최종 실패 (carousel_id={carousel_id}): {publish_resp.text}")

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

    entry = {
        "topic": topic,
        "hook": content.get("hook", "").replace("<br>", " "),
        "category": content.get("category", ""),
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

        # ── 단계 2: 문안 생성 ──
        content = write_card_content(client, topic)

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

        # ── 단계 8: 이력 기록 (발행 직후 즉시 — 이후 에러 나도 중복 방지) ──
        record_published(topic, final_content, final_paths[0], post_id=post_id, image_url=image_urls[0])

        print()
        print("=" * 60)
        print("  발행 완료!")
        print(f"  주제: {topic}")
        print(f"  게시물 ID: {post_id}")
        print(f"  슬라이드: {len(final_paths)}장")
        print("=" * 60)

        # Mac 알림 (macOS에서만 실행)
        import sys, subprocess
        if sys.platform == "darwin":
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
