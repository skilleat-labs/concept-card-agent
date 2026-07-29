# 인프라 개념카드 자동발행 에이전트

개발자·엔지니어를 위한 인프라 상식을 **3장짜리 인스타그램 캐러셀 카드**로 자동 생성하는 AI 에이전트.

Claude가 주제 선택 → 문안 생성 → SVG 다이어그램 → 렌더링 → 자가검증을 스스로 수행한다.

---

## 카드 구성 (3장)

| 슬라이드 | 역할 | 핵심 요소 |
|---------|------|----------|
| Slide 1 | **훅 커버** | 캐릭터 일러스트 + 물음표 + 궁금증 유발 질문 + "꼭 알아야 할 인프라 상식" 배너 |
| Slide 2 | **개념 설명** | 주제 제목(대형) + 요약 2문장 + SVG 다이어그램 + 한 줄 정의 |
| Slide 3 | **스토리텔링** | 키워드 색상 강조(파랑/초록/주황/빨강)된 3~4문장 내러티브 + 해시태그 |

- 사이즈: **1080×1080px (1:1 정사각형)**
- 시리즈 구조: 매 카드가 다음 주제의 궁금증으로 자연스럽게 연결됨

---

## 전체 흐름

```
[1] 주제 선택
    topics.json + published.json 참고
    Claude가 오늘의 주제 1개 선택
          |
          v
[2] 문안 생성 (3단계 분리 호출)
    2-1. 텍스트 콘텐츠 JSON 생성
         (title / category / hook / summary / definition / tags)
    2-2. 스토리 텍스트 생성
         (3~4문장, [blue]키워드[/blue] 마커로 색상 강조)
    2-3. SVG 다이어그램 생성
         (가로 일렬 흐름, 겹침 없는 박스+화살표)
          |
          v
[3] 렌더링 (3장)
    Jinja2 → HTML × 3 → Playwright → PNG × 3 (각 1080×1080)
          |
          v
[4] 자가검증 (최대 3회 반복)
    Claude Vision으로 Slide 1 커버 이미지 검사
    불합격 → 텍스트 축약 → [3]으로 재시도
    합격  → 다음 단계로
          |
          v
[5] Azure Blob 업로드 (3장)
    PNG × 3 → 공개 URL × 3 생성
          |
          v
[6] Instagram 캐러셀 발행 + 이력 기록
    Graph API로 3장 캐러셀 게시 → published.json 업데이트
```

---

## 파일 구조

```
concept-card-agent/
├── agent.py                    # 메인 에이전트 스크립트
├── topics.json                 # 인프라 주제 후보 30개
├── published.json              # 발행 이력 (자동 업데이트)
├── requirements.txt            # Python 패키지 목록
├── .env                        # 환경변수 (Git 추적 제외)
│
├── template_slide1.html        # Slide 1: 훅 커버 템플릿
├── template_slide2.html        # Slide 2: 개념 설명 + 다이어그램 템플릿
├── template_slide3.html        # Slide 3: 스토리텔링 템플릿
│
├── prompts/
│   ├── select_topic.txt        # 주제 선택 프롬프트
│   ├── write_card.txt          # 텍스트 콘텐츠 생성 프롬프트 (JSON)
│   ├── write_story.txt         # 스토리 단락 생성 프롬프트
│   ├── draw_diagram.txt        # SVG 다이어그램 생성 프롬프트
│   └── review_card.txt         # 카드 검증 프롬프트 (Vision)
│
├── output/                     # 생성된 PNG 저장 폴더
│   └── slide1_주제_날짜.png    # slide1/2/3_주제_타임스탬프.png
│
└── .github/
    └── workflows/
        └── daily.yml           # GitHub Actions 자동화 워크플로우
```

---

## 전제 조건

- Python 3.11 이상
- Playwright (chromium 브라우저)
- Anthropic API 키 (필수)
- Azure Blob Storage 계정 (실제 발행 시 필요)
- Instagram Business 계정 + Facebook App (실제 발행 시 필요)

---

## 설치

```bash
pip3 install -r requirements.txt
python3 -m playwright install chromium
```

---

## 환경변수 설정 (.env)

```dotenv
# ── 필수 ──────────────────────────────────
ANTHROPIC_API_KEY=sk-ant-api03-...

# ── Azure Blob Storage (발행 시 필요) ──────
AZURE_STORAGE_CONNECTION_STRING=DefaultEndpointsProtocol=https;AccountName=...
AZURE_STORAGE_ACCOUNT=myaicardstorage
AZURE_CONTAINER_NAME=concept-cards

# ── Instagram Graph API (발행 시 필요) ─────
IG_USER_ID=12345678901234567
IG_ACCESS_TOKEN=EAA...
```

> Instagram 액세스 토큰 발급: [Facebook Developers](https://developers.facebook.com/) → 앱 생성 → Instagram Graph API → 장기 토큰 교환

---

## 실행 방법

### 렌더링만 (API 키만 있어도 됨)

특정 주제로 3장 PNG 생성. Azure/Instagram 불필요.

```bash
python3 agent.py --render-only --topic "컨테이너"
```

결과물: `output/slide1_컨테이너_YYYYMMDD_HHMMSS.png` (3장)

### 주제 자동 선택 렌더링

```bash
python3 agent.py --render-only
```

### 전체 흐름 드라이런 (발행 없음)

```bash
python3 agent.py --dry-run
python3 agent.py --dry-run --topic "쿠버네티스(K8s)"
```

### 실제 발행 (Azure + Instagram 설정 필요)

```bash
python3 agent.py
python3 agent.py --topic "CI/CD 파이프라인"
```

---

## 콘텐츠 생성 구조

### 생성되는 필드 (write_card.txt)

| 필드 | 설명 | 제한 |
|------|------|------|
| `title` | 개념 이름 | 10자 이내 |
| `category` | 영문 카테고리 | 예: `Container · Docker` |
| `hook` | 훅 질문 (`<br>`로 2줄 분리) | 각 줄 15자 이내 |
| `summary` | 개념 등장 배경 + 해결책 2문장 | 각 50자 이내 |
| `definition` | 한 줄 정의 (비유 포함) | 40자 이내 |
| `tags` | 해시태그 4개 배열 | — |

### 스토리 마커 → HTML 변환 (write_story.txt)

스토리 텍스트는 JSON 분리 호출로 생성되며, 마커를 HTML로 자동 변환:

```
[blue]키워드[/blue]   →  <em class='blue'>키워드</em>
[green]키워드[/green]  →  <em class='green'>키워드</em>
[orange]키워드[/orange] →  <em class='orange'>키워드</em>
[red]키워드[/red]    →  <em class='red'>키워드</em>
```

### SVG 다이어그램 규칙 (draw_diagram.txt)

- 가로 일렬 흐름 (3~4개 박스)
- 화살표는 수평선만 허용 (대각선·교차 금지)
- 박스 좌표 고정으로 텍스트-화살표 겹침 방지
- 어두운 배경(#1A1A35)에 맞는 밝은 색 팔레트

---

## GitHub Actions 설정

### Secrets 등록

GitHub 저장소 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

| Secret 이름 | 설명 |
|------------|------|
| `ANTHROPIC_API_KEY` | Anthropic API 키 |
| `AZURE_STORAGE_CONNECTION_STRING` | Azure 스토리지 연결 문자열 |
| `AZURE_STORAGE_ACCOUNT` | Azure 스토리지 계정 이름 |
| `AZURE_CONTAINER_NAME` | Blob 컨테이너 이름 |
| `IG_USER_ID` | Instagram Business User ID |
| `IG_ACCESS_TOKEN` | Instagram 장기 액세스 토큰 |

### 자동 실행 스케줄

매일 **KST 09:00** (UTC 00:00)에 자동 실행.

### 수동 실행

GitHub 저장소 → **Actions** → **Daily Card Publisher** → **Run workflow**
- `topic` 입력란에 주제 입력 시 해당 주제로 발행 (비워두면 AI 자동 선택)

---

## 자가검증 루프

```
3장 렌더링 완료
      ↓
Claude Vision으로 Slide 1(커버) 검사
      ↓
합격?  ── YES ──→ 다음 단계
  │
  NO
  ↓
fix 지시 분석 → 텍스트 자동 축약
      ↓
재렌더링 (최대 3회)
```

검사 항목: 글자 넘침 / 어색한 줄바꿈 / 텍스트 가독성 / 여백 쏠림

---

## 트러블슈팅

### Playwright 실행 오류

```bash
python3 -m playwright install chromium
python3 -m playwright install-deps chromium
```

### .env 파일 없음 오류

```bash
# 프로젝트 루트에 .env 생성
cp .env.example .env  # 또는 직접 생성 후 API 키 입력
```

### SVG 다이어그램 미생성 (68자 폴백)

Claude가 응답을 `</svg>` 없이 끊은 경우 자동으로 닫힘 처리됨. 재실행 시 정상 생성됨.

### Azure 업로드 403 오류

컨테이너의 **공개 액세스 수준**이 "Blob (익명 읽기 액세스)"으로 설정되어 있는지 확인.

### Instagram API 오류

- 액세스 토큰 만료 여부 확인 (장기 토큰도 60일 후 만료)
- Instagram 계정이 **비즈니스 계정** 또는 **크리에이터 계정**인지 확인
- Facebook App의 `instagram_basic`, `instagram_content_publish` 권한 확인

### published.json 초기화

```bash
echo '[]' > published.json
```
