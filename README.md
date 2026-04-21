# ScaffoldOrganizer 2.0

> 🇰🇷 한국어 / [🇺🇸 English](README_english.md)

Telegram 외부 캡처 + 로컬 브레인덤프 + AI 구조화를 한 앱으로 통합한
**WSLg 기반 pywebview 데스크톱 앱**입니다. 백엔드는 FastAPI (WSL 네이티브 또는
Docker 컨테이너).

SQLite 가 single source of truth. OpenAI Responses API 의 서버측 Prompt ID 로
분류 / 명령 라우팅 / 브레인덤프 구조화 / 업무일지 작성을 처리합니다. API key
없으면 결정론적 로컬 fallback 으로 오프라인에서도 동작.

---

## 미리보기

| Inbox | Active (todo + doing) |
|:---:|:---:|
| [![Inbox](docs/screens/01-inbox.svg)](docs/screens/01-inbox.svg) | [![Active](docs/screens/02-active.svg)](docs/screens/02-active.svg) |

| Long-term | Sessions |
|:---:|:---:|
| [![Long-term](docs/screens/03-longterm.svg)](docs/screens/03-longterm.svg) | [![Sessions](docs/screens/04-sessions.svg)](docs/screens/04-sessions.svg) |

| Work Log | Done |
|:---:|:---:|
| [![Work Log](docs/screens/05-worklog.svg)](docs/screens/05-worklog.svg) | [![Done](docs/screens/06-done.svg)](docs/screens/06-done.svg) |

개별 SVG 와 시각 규칙(상태 border 색 / priority chip / doing 강조)은
[docs/screens/](docs/screens/) 참조.

---

## 주요 기능

- **Telegram 외부 캡처** — `telegram_allowed_chat_ids` allowlist 체크를
  AI 호출/DB write 전에 선행, 무단 chat 은 로그 남기고 drop
- **Chat-like 자연어 명령** — `/chat/command` → command_router LLM →
  결정론적 backend action 실행
- **브레인덤프 자동 분해** — classifier 가 1차 판별 (단일 vs 멀티-아이템) →
  멀티면 task_structurer 로 위임 → 여러 item 자동 생성 (inbox 우선)
- **item 상태** — `inbox` / `todo` / `doing` / `done` / `archived`
- **horizon** — `now` / `soon` / `later` / `long_term`
- **장기 backlog 보호** — AI 응답으로 재생성/삭제되지 않음
- **session save / load / export** (Markdown)
- **업무일지 드래프트** — 그 날의 이벤트 + 상태 전이에서 자동 생성, 검토 후 저장/출력
- **Graceful lifecycle** — GUI 닫으면 `/shutdown` → backend 자동 종료

---

## 아키텍처

```
┌─ WSL (host) ─────────────────────────────┐
│                                          │
│   .venv  (pywebview + requests + backend)│
│     │                                    │
│     ▼  python scripts/run_gui.py         │
│   gui/launcher.py                        │
│     │                                    │
│     ├── subprocess ──▶ backend           │
│     │     (SCAFFOLD_BACKEND_MODE):       │
│     │       wsl    → python -m uvicorn   │
│     │       docker → docker compose up   │
│     │       external → skip, assume up   │
│     │                                    │
│     └── pywebview window ─▶ file://index │
│                      │                   │
│                      └─ HTTP 127.0.0.1:8765 ─▶ backend
└──────────────────────────────────────────┘
```

주요 모듈:

| 경로 | 역할 |
| --- | --- |
| [backend/main.py](backend/main.py) | FastAPI app, lifecycle, endpoints |
| [backend/services/ai_service.py](backend/services/ai_service.py) | OpenAI Responses API 경계, 역할별 prompt_id / response_format |
| [backend/services/router_service.py](backend/services/router_service.py) | chat 명령 라우팅, LLM 호출 외부에서 DB write |
| [backend/services/brain_dump_service.py](backend/services/brain_dump_service.py) | 브레인덤프 → task_structurer → 여러 item 자동 생성 |
| [backend/services/telegram_service.py](backend/services/telegram_service.py) | 폴링, allowlist 강제, exponential backoff |
| [backend/services/item_service.py](backend/services/item_service.py) | item CRUD + 상태 전이 + hard delete |
| [backend/services/session_service.py](backend/services/session_service.py) | 브레인덤프 세션 save / load / export |
| [backend/services/worklog_service.py](backend/services/worklog_service.py) | 일일 컨텍스트 수집 + 업무일지 드래프트 |
| [backend/db/schema.sql](backend/db/schema.sql) | SQLite 스키마 (WAL 모드) |
| [gui/launcher.py](gui/launcher.py) | backend 기동, pywebview 창 open, 종료 시 backend stop |
| [gui/index.html](gui/index.html) + [assets/app.js](gui/assets/app.js) + [assets/style.css](gui/assets/style.css) | 600×400 UI, Noto Sans 폰트 기본, sticky command bar |
| [scripts/release.sh](scripts/release.sh) | `developing/` → `building/` rsync + venv + docker + 시작 메뉴 등록 |

---

## 사전 요건

### Windows (호스트)

- Windows 10 22H2+ 또는 Windows 11 (WSLg / `$DISPLAY=:0` 필요)
- Docker Desktop (선택 — `SCAFFOLD_BACKEND_MODE=docker` 일 때만)

### WSL (Ubuntu 22.04 / 24.04)

pywebview GTK 백엔드 + 한국어 렌더링 용 시스템 패키지:

```bash
sudo apt update
sudo apt install -y \
  python3-venv python3-gi python3-gi-cairo \
  gir1.2-webkit2-4.1 \
  fonts-noto-cjk fonts-noto-cjk-extra \
  ibus ibus-hangul      # WSL 에서 한글 직접 입력할 때만 필요
sudo fc-cache -fv
```

한글 IME (ibus-hangul) 세팅 (한 번만):

```bash
cat >> ~/.bashrc <<'EOF'
export GTK_IM_MODULE=ibus
export XMODIFIERS=@im=ibus
export QT_IM_MODULE=ibus
EOF
source ~/.bashrc
ibus-daemon -drx
ibus-setup    # Input Method → Add → Korean → Hangul
```

---

## 설정

예시 템플릿 복사해서 실제 값 채우기:

```bash
cp config/config_example.json config/config.json
```

`config/config.json` 은 **gitignored**. 커밋되는 건 `config_example.json` 와
`config.schema.json` 뿐.

필수 필드 구조:

```jsonc
{
  "app_name": "ScaffoldOrganizer 2.0",
  "backend_host": "127.0.0.1",
  "backend_port": 8765,

  "db_path": "data/scaffold_workbench.sqlite3",

  "telegram_enabled": false,
  "telegram_bot_token": "",               // 또는 $TELEGRAM_BOT_TOKEN env
  "telegram_allowed_chat_ids": [],        // 비어있으면 모두 허용 + WARNING 로그
  "telegram_poll_interval_seconds": 1.0,

  "openai_api_key": "",                   // 또는 $OPENAI_API_KEY env

  "ai_roles": {
    "command_router":  { "prompt_id": "pmpt_...", "prompt_file": "backend/prompts/command_router.md",  "response_format": "json"     },
    "classifier":      { "prompt_id": "pmpt_...", "prompt_file": "backend/prompts/classifier.md",      "response_format": "json"     },
    "task_structurer": { "prompt_id": "pmpt_...", "prompt_file": "backend/prompts/task_structurer.md", "response_format": "json"     },
    "worklog_writer":  { "prompt_id": "pmpt_...", "prompt_file": "backend/prompts/worklog_writer.md",  "response_format": "json"     }
  },

  "wsl_backend_entrypoint": "/home/<user>/.../developing",
  "wsl_distribution_name": "Ubuntu",

  "ui_refresh_interval_ms": 2000,
  "pywebview": { "width": 600, "height": 400 }
}
```

### AI 역할

각 역할의 developer message 는 OpenAI 대시보드에 등록하고 `prompt_id` 로
참조합니다. 대시보드 default 버전 자동 사용 (pinning 없음) — 프롬프트 수정이
즉시 반영됩니다.

`prompt_id` 가 비어있으면 로컬 `backend/prompts/*.md` 를 developer message
로 보내는 fallback 경로로 동작. 개발/오프라인용.

**model / temperature / max_output_tokens 은 보내지 않습니다** — 전부 대시보드
prompt 설정에 위임. 일부 모델(o-series 등)은 `temperature` 를 아예 거부하므로
일관성을 위해 제거.

`response_format` 은 클라이언트 파싱 힌트:
- `"json"` — `json.loads` 로 dict 파싱 (classifier, command_router, task_structurer, worklog_writer 전부)
- 내부 필드에 Markdown 이 담김 — `task_structurer.items[]` + `structured_markdown`,
  `worklog_writer.content_md` 등

### Secret

`openai_api_key`, `telegram_bot_token` 은 환경변수 `OPENAI_API_KEY`,
`TELEGRAM_BOT_TOKEN` 로 주입하면 `config.json` 에 안 써도 됩니다.

### Telegram allowlist

`telegram_allowed_chat_ids` 에 없는 chat_id 의 메시지는 `_handle_update`
진입 직후 drop — AI 호출 / DB write 모두 전에. 드롭될 때 `chat_id` + title 이
로그에 남아 어떤 chat 을 허용해야 하는지 확인할 수 있습니다.

빈 리스트는 모두 허용 + 시작 시 WARNING 로그.

---

## 실행 — 개발 모드

```bash
python3 -m venv --system-site-packages .venv     # GTK gi 시스템 패키지 공유
source .venv/bin/activate
pip install -r requirements-gui.txt
pip install -r requirements-backend.txt

# 기본: wsl 모드 — launcher 가 로컬에서 python -m uvicorn 실행
python scripts/run_gui.py

# docker 모드 — launcher 가 docker compose up --build backend 실행
SCAFFOLD_BACKEND_MODE=docker python scripts/run_gui.py

# external 모드 — 이미 :8765 에 떠 있는 백엔드 사용
SCAFFOLD_BACKEND_MODE=external python scripts/run_gui.py
```

환경변수 오버라이드:

| 변수 | 의미 |
| --- | --- |
| `SCAFFOLD_BACKEND_MODE` | `wsl` (기본) \| `docker` \| `external` |
| `SCAFFOLD_BACKEND_URL` | launcher 가 `/health` 를 polling 할 URL (기본 자동 유도) |
| `SCAFFOLD_BACKEND_READY_TIMEOUT` | `/health` 응답 대기 초 (기본 `45`) |
| `SCAFFOLD_WSL_ENTRYPOINT` | config 의 `wsl_backend_entrypoint` 오버라이드 |
| `SCAFFOLD_WSL_DIST` | config 의 `wsl_distribution_name` 오버라이드 |
| `OPENAI_API_KEY`, `TELEGRAM_BOT_TOKEN` | 백엔드 env 로 주입되는 secret |

백엔드만 (GUI 없이):

```bash
bash scripts/run_backend.sh                 # 로컬 uvicorn
bash scripts/run_backend_docker.sh          # docker compose up backend
```

---

## 실행 — 릴리즈 (WSL + 시작 메뉴 연동)

`scripts/release.sh` 가 형제 디렉토리 `building/` 에 런치 준비 상태로
배포하고, WSLg `.desktop` 엔트리와 Windows 시작 메뉴 `.lnk` 단축파일까지
자동 생성합니다.

```bash
cd developing
./scripts/release.sh                 # rsync + venv + docker image + .desktop + .lnk
./scripts/release.sh --rsync-only    # rsync 만, 나머지 생략
./scripts/release.sh --mode docker   # run.sh 에 SCAFFOLD_BACKEND_MODE=docker 박음
./scripts/release.sh --no-docker     # docker 이미지 빌드 생략
./scripts/release.sh --no-lnk        # Windows 시작 메뉴 .lnk 생성 생략
```

산출물:

```
../building/
├── .venv/                   # pywebview + backend 의존성
├── run.sh                   # 실행 스크립트 (IME env + ibus 자동 기동 포함)
├── config/config.json       # 가능하면 source 에서 복사
└── backend/ gui/ scripts/ ...

%LOCALAPPDATA%\ScaffoldOrganizer2\
├── launch.vbs               # 숨긴 상태로 wsl.exe 실행 (콘솔 플래시 제거)
└── icon.ico                 # (gui/assets/icon.ico 가 있으면)

%APPDATA%\...\Start Menu\Programs\
└── ScaffoldOrganizer 2.0.lnk  # wscript.exe + launch.vbs 를 가리킴
```

실행:

```bash
~/personal/260420_scaffold_assistant_2/building/run.sh
# 또는: Windows 시작 메뉴 → "Scaffold" 검색
```

`run.sh` 는 `.venv` 활성화 + IME 환경변수 export + ibus-daemon 자동 기동 +
release 시점의 `SCAFFOLD_BACKEND_MODE` export 후 `python scripts/run_gui.py`
실행. 창 닫으면 backend 자동 종료.

release.sh 재실행은 안전 (idempotent) — `.venv`, `data/`, `logs/`,
`exports/` 는 rsync exclude 로 보존됩니다.

---

## 사용법

### 탭 구성

| 탭 | 내용 |
| --- | --- |
| **Inbox** | 확정되지 않은 캡처 (Telegram raw, 저확신 분류, 일반 chat 캡처 기본값) |
| **Active** | `status in (todo, doing)` 한 페이지 통합. doing 이 상단에 위치, 짙은 머스타드 border + `● DOING` 배지 |
| **Long-term** | `horizon=long_term` — AI 재정리로부터 보호 |
| **Sessions** | 브레인덤프 에디터 + 세션 히스토리 (Load 로 복원) |
| **Work Log** | 업무일지 드래프트 편집 + Save / Save & Export |
| **Done** | 완료된 item (line-through + 72% 투명) |

### Command bar (하단 고정)

자연어 명령. 예시:

| 입력 | 동작 |
| --- | --- |
| `이건 장기 과제로 보내` + 선택 | 선택된 item → `horizon=long_term` |
| `선택한거 doing 으로 바꿔` + 선택 | 선택된 item → `status=doing` |
| `선택한 2개 중요도 매우높음` + 선택 | 선택된 item → priority=P1 |
| `선택한거 완전히 지워줘` + 선택 | 선택된 item **하드 삭제** (복원 불가) |
| `선택한거 지워` + 선택 | 선택된 item **archive** (복원 가능) |
| `오늘 한 일로 업무일지 만들어` | 업무일지 드래프트 생성 |
| `A 끝낸 다음 B 하고 C` (멀티 task) | classifier → decompose=true → task_structurer → 3개 item 자동 생성 |
| 한 줄짜리 캡처 | classifier → 단일 item 생성 (Inbox 로) |

`Ctrl/Cmd+Enter` 로 전송. 선택은 카드 체크박스로 먼저 지정.

### 툴바 버튼

| 버튼 | 동작 |
| --- | --- |
| Save | 현재 에디터 내용을 세션으로 저장 |
| Load | 저장된 세션 선택 팝업 (2개+ 이상일 때) |
| Reset | **모든 item 하드 삭제** (long-term 포함) + 에디터 초기화. 확인 팝업 있음 |
| Export | 모든 item 을 Markdown 파일로 내보내기 |
| Work Log | 업무일지 드래프트 생성 → 검토 → Save / Save & Export |

### 카드 인라인 편집

카드 우측 `✎` → 제목 / 내용 / priority 인라인 편집. Edit 모드에서 `✕ Delete`
(빨강) 는 영구 삭제 (archive 아님).

### 단축키

- `Ctrl/Cmd+Enter` — command bar 전송 / edit mode 저장
- `Esc` — edit mode 취소
- Filter 입력은 180ms debounce — title, project, content, tags 매칭

---

## API

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/health` | launcher liveness probe |
| GET | `/status` | Backend / Telegram / AI dot 상태 |
| GET | `/config/ui` | GUI 부팅 시 필요한 비-secret 값 |
| POST | `/shutdown` | graceful drain, uvicorn 종료 |
| GET | `/items` | `status`, `horizon`, `item_type`, `exclude_horizon` 필터. `status` 는 comma-separated 허용 |
| POST | `/items` | 수동 item 생성 |
| PATCH | `/items/{id}` | 부분 업데이트 (title / content / priority 등) |
| POST | `/items/{id}/status` | 상태 전이 (event 기록) |
| POST | `/items/{id}/archive` | `status=archived` 바로가기 |
| DELETE | `/items/{id}` | **하드 삭제** (row 제거) |
| POST | `/items/delete` | 다중 하드 삭제 (body: `{item_ids: [...]}`) |
| POST | `/items/reset` | 전체 하드 삭제 (long-term 포함) |
| POST | `/chat/command` | 자연어 명령 → router → 결정론적 action |
| POST | `/chat/capture` | classifier 만 거쳐 단일 item 생성 |
| POST | `/brain-dump/structure` | 세션 저장 + task_structurer → 여러 item 자동 생성 |
| GET | `/sessions` | 목록 |
| GET | `/sessions/{id}` | 연결된 items 포함 |
| POST | `/sessions/save` | 새 세션 생성 |
| POST | `/sessions/{id}/export` | 세션 Markdown 파일 출력 |
| POST | `/export/markdown` | 모든 item 을 상태별로 Markdown export |
| POST | `/worklog/generate` | 드래프트만 생성 (DB 안 건드림) |
| POST | `/worklog/save` | 사용자가 검토한 드래프트 영구 저장 |
| GET | `/worklogs` | 목록 |
| POST | `/worklogs/{id}/export` | 업무일지 Markdown 파일 출력 |

---

## 배포 시 주의

git 에 **절대 올리지 말 것** (전부 `.gitignore` 처리됨):

- `config/config.json`, `config/config.*.json` (`config_example.json` / `config_docker_example.json` / `config.schema.json` 제외)
- `.env`, `.env.*` (`.env.example` 제외)
- `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.crt`, `secrets.*`, `credentials.*`
- `data/`, `logs/`, `exports/`, `*.db`, `*.sqlite*`
- `dist/`, `build/`, `*.spec`, `*.exe`, `*.dll`, `*.egg-info/`
- `__pycache__/`, `.pytest_cache/`, `.venv/`, `venv/`
- `.claude*` (Claude Code 관련)
- `scripts/release.sh`, `/run.sh` (개인 경로 하드코딩됨)
- OS 잔재: `.DS_Store`, `._*`, `Thumbs.db`, 등

전부 [`.gitignore`](.gitignore) 에 반영되어 있습니다.

---

## 문제 해결

**`RuntimeError: Backend did not become healthy within 45 seconds`**
launcher 가 실제로 보내는 명령을 직접 실행해서 뭐가 막히는지 확인:

```bash
# wsl 모드
python3 -m uvicorn backend.main:app --host 127.0.0.1 --port 8765

# docker 모드
docker compose up --build backend
```

흔한 원인: 백엔드 의존성 미설치 (`pip install -r requirements-backend.txt`),
포트 8765 점유, docker 미구동.

**`python scripts/run_gui.py` 실행했는데 창이 안 뜸** — `echo $DISPLAY` 가
`:0` 를 반환해야 함. 비어있으면 WSLg 비활성 → 위 "사전 요건" 참조.

**한글이 딩벳 박스로 나옴** — CJK 폰트 설치:
`sudo apt install -y fonts-noto-cjk && fc-cache -fv` 후 앱 재시작.

**textarea 에 한글 IME 입력 안 됨** — ibus-hangul 미구동 또는 GTK 연동 안됨.
위 "사전 요건" 재확인. 붙여넣기 (Ctrl+V) 는 IME 와 무관하게 항상 동작.

**AI dot 이 `Local fallback` 또는 `Error` 로 표시됨** — dot 에 마우스오버하면
실제 에러 메시지 툴팁으로 뜸. 흔한 원인:
- `openai_api_key` 비어있음 → env 또는 config 에 추가
- openai SDK 버전 < 1.51 → `.venv/bin/pip install --upgrade "openai>=1.63"`
- `.venv` 가 `--system-site-packages` 로 만들어져 시스템의 낡은 openai 가
  shadow 하는 경우 → `--force-reinstall` 로 venv 내부에 재설치
  (release.sh 가 이미 이 처리를 함)

**"AI call failed — fallback used. 'openai' object has no attribute 'responses'"**
SDK 버전 너무 낮음 (Responses API 는 1.51+). 위 항목 참조.

**"Unsupported parameter: 'temperature' is not supported with this model"**
이 앱 최신 버전은 `temperature` / `max_output_tokens` 를 전송하지 않도록 제거
되어 있습니다. 메시지가 계속 뜨면 `building/` 쪽 코드가 오래된 것 — `./scripts/release.sh`
재실행하여 동기화.

**Telegram 메시지가 조용히 drop 됨** — `telegram_allowed_chat_ids` 에 sender
chat_id 가 없는 것 (의도된 동작). 백엔드 로그 확인:
`telegram: dropped message from unauthorized chat_id=… — add to
telegram_allowed_chat_ids to accept`.

---

## 테스트

```bash
docker run --rm -v "$PWD":/app -w /app developing-backend:latest \
  sh -c "pip install -q pytest && python -m pytest tests/ -v"
```

커버: config validation / DB bootstrap + 상태 전이 / router fallback (한국어
명령 포함) / worklog 컨텍스트 수집 / AIService path 선택 (prompt_id vs 로컬
developer message) / 브레인덤프 파서 / reset 및 delete 서비스 / classifier
decompose 플래그 정규화 / `digest_items` 토큰 트림.

---

## 라이선스

[LICENSE](LICENSE) 참조.
