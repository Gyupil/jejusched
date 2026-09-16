# CLAUDE.md — jejusched

제주도 「주요일정」 hwpx 파일을 감시해 Google Calendar에 자동 반영하는 윈도우 트레이 앱.
macOS에서 개발·테스트하고 GitHub Actions(`windows-latest`)에서 PyInstaller로 빌드한다.

> **중요 — 저장소에 없는 것들**
> `docs/design.md`(원본 설계서), `docs/example_docs/`(샘플 PDF), `tests/fixtures/*.json`은
> 실제 일정이 담겨 있어 `.gitignore` 대상이다. **새로 클론한 저장소에는 없다.**
> 이 문서가 설계 결정의 단일 출처다. 로컬에 design.md가 있으면 먼저 읽되, 없어도
> 이 문서만으로 같은 품질의 작업이 가능해야 한다.
>
> 픽스처가 없으면 의존 테스트 23개가 **건너뛰기(skip)**로 처리된다 — 실패가 아니다.
> 원본 PDF가 있으면 `python tools/pdf_to_fixture.py`로 되살린다.

## 이 프로젝트에서 일하는 법

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest -q                      # 픽스처 있으면 73 통과 / 없으면 50 통과 + 23 skip
.venv/bin/python -m jejusched status               # 설정·계정 확인
.venv/bin/python -m jejusched apply tests/fixtures/0910.json --dry-run
.venv/bin/python -m jejusched add-account          # 브라우저 OAuth (실제 계정 필요)
.venv/bin/python tools/pdf_to_fixture.py           # 샘플 PDF → 픽스처 재생성
```

- 언어: 코드 주석·커밋·문서는 **한국어**, 식별자는 영어. 설계서 용어(구분/표시/덮어쓰기)를 그대로 쓴다.
- 실제 Google/Gemini 호출이 필요한 코드는 Protocol 뒤에 두고, 테스트는 `Fake*` 구현으로 한다.
  `FakeCalendarClient`, `FakeResolver`, `NullResolver`가 이미 있다. 새 외부 의존이 생기면 같은 방식으로.
- **§8 시나리오(아래)는 회귀 테스트의 기준선이다.** matcher/normalize를 고칠 때 이 숫자가
  바뀌면 코드가 아니라 이해가 틀린 것이다. `tests/test_sequence.py`가 이를 강제한다.
- 비밀값을 커밋하지 않는다. `client_secret_*.json`, `*.env`, `build/client_secret.json`은 gitignore 대상.
- UI·플랫폼 의존(`pystray`, `customtkinter`, `win32crypt`, `google.genai`)은 **함수 안에서 임포트**한다.
  그래야 맥/CI에서 `jejusched.main`을 불러도 터지지 않는다. 이 규칙을 깨면 테스트가 임포트 단계에서 죽는다.

### 이미 한 번 물린 함정

- **`content_hash`는 공백에 둔감해야 한다.** 표 칸 너비가 달라지면 같은 행사명의 줄바꿈 위치가
  바뀐다. 이걸 내용 변경으로 보면 매일 불필요한 덮어쓰기가 생긴다 → `normalize.hash_text()`.
- **연산 직후 `events` 인덱스를 갱신해야 한다**(`pipeline._execute`). 이 기록이 없으면 다음
  Reconcile에서 "사용자가 직접 지웠다"를 판별하지 못해 지운 일정이 되살아난다.
- **PDF 표의 페이지 넘김 행**은 시간 셀이 비어 있다. `tools/pdf_to_fixture.py`가 이걸로 병합을
  판단한다. hwpx 파서를 쓸 때도 같은 종류의 이어지는 행을 조심하라.

## 핵심 개념 (설계 확정 사항)

| 항목 | 결정 |
|---|---|
| 인증 | OAuth 2.0 데스크톱 클라이언트, External + 프로덕션 게시(미검증). 스코프 `calendar.app.created` + `openid` + `userinfo.email` |
| 캘린더 | 계정마다 **전용 보조 캘린더**("주요일정(자동)")를 만들어 거기에만 기록. primary는 건드리지 않음 |
| 구분(섹션) | 도지사 / 행정부지사 / 기후경제부지사 / 실 일정 — 전부 반영, `colorId`로 구분 |
| 기본 정책 | 같은 일정은 새 파일로 **덮어쓰기**, 새 일정은 **추가**, 파일에서 빠진 일정은 **삭제하지 않고 행사명 앞에 `* ` 표시** |
| 진실의 원천 | **Google Calendar**. 로컬 SQLite는 캐시/인덱스. 상태는 `extendedProperties.private`에 보존되어 DB가 사라져도 복원된다 |
| 다중 계정 | 대상(target) 목록. 파일 1개를 모든 활성 대상에 동일 미러링 |
| LLM | Gemini(`gemini-3.8-flash` → 폴백 `gemini-3.5-flash-lite`). 규칙 3 쌍만 **파일당 1회** 일괄 질의 |

### 매칭 규칙 5개 (이 프로젝트의 심장 — `core/matcher.py`)

같은 **날짜 + 같은 구분** 안에서만 비교한다. 한 번 짝지어진 일정은 다음 규칙에서 다시 쓰지 않는다.

1. **시간·행사명 동일** (`event_key` 일치) → 같은 일정. 내용 다르면 UPDATE, `*` 붙어 있었으면 RESTORE.
2. 남은 것 중 **행사명 동일이 양쪽에 1건씩만** → 같은 일정(시간 변경). UPDATE / RESTORE.
3. 남은 쌍은 **LLM 판단**. "같음" → UPDATE / RESTORE.
4. 짝 없는 새 일정 → **CREATE**. 단 `user_deleted`에 있으면 재생성 안 함.
5. 짝 없는 기존 일정 → **MARK**(`* ` 접두). 단 (날짜, 구분)이 `judged`에 있을 때만, 과거 날짜는 제외.

- `judged = {(e.date, e.section) for e in events if e.date >= file_date}` — 새 파일이 그 날짜·구분에
  일정을 **하나라도** 적었을 때만 "없어졌다"고 판단한다. 과거 날짜는 절대 표시하지 않는다.
- 규칙 3의 판정이 없으면(LLM 실패) 그 (날짜, 구분)만 **HOLD** — 4·5를 적용하지 않는다. 다른 날짜는 정상 진행.
- LLM 비활성(키 없음)이면 규칙 3은 항상 "다름"으로 처리한다(추가 + 표시).

### 정규화 (`core/normalize.py`)

- `title_norm`: NFKC → 공백 전부 제거 → 괄호·따옴표류(`「」『』<>〈〉“”‘’"'`) 제거 → 소문자 → 선행 `* ` 제거.
- `slot`: 시작 시각(`"10:00"`) 또는 `"ALLDAY"`. "시간이 같다"는 **시작 시각이 같다**는 뜻.
- `event_key = sha1(date|section|slot|title_norm)[:16]`, `content_hash = sha1(모든 필드)[:16]`.
- 종료 시각 없으면 **60분**. `종일` + 행사명 안 `(14:00)`은 종일로 두고 시간은 `note`에 보존.
- 연도 추정: 파일 연도 = mtime 연도, 헤더 요일과 대조해 불일치면 ±1년 보정. 일정 연도는
  {Y-1, Y, Y+1} 중 파일 날짜와 가장 가까운 해.

## 코드 구조

```
jejusched/
  main.py config.py logging_setup.py
  core/    models.py normalize.py matcher.py planner.py state.py pipeline.py
  parsers/ base.py(Protocol) fixture_json.py hwpx.py(후순위)
  gcal/    auth.py client.py(Protocol+Google) fake.py
  llm/     resolver.py(Protocol+Gemini+Fake) prompts.py cache.py budget.py
  watcher/ folder_watch.py intake.py worker.py
  ui/      tray.py settings_window.py wizard.py notify.py
tools/pdf_to_fixture.py   # 샘플 PDF → tests/fixtures/*.json 재생성
```

## 설정 로딩 (`config.py`)

OAuth 클라이언트 JSON은 **세 곳**에서 같은 함수로 찾는다:
1. 환경변수 `GOOGLE_OAUTH_CLIENT_JSON` (JSON 문자열 — GitHub Actions Secret이 이 이름)
2. 프로젝트 루트의 `client_secret_*.json` (로컬 개발)
3. exe 번들 내부(`sys._MEIPASS`) — 빌드 시 워크플로가 여기에 써 넣는다

`GEMINI_API_KEY`는 환경변수 → `local.env` → 암호화 저장소 순. **없어도 예외를 던지지 않고**
`llm.enabled=false`가 된다. 런타임 설정은 `%APPDATA%\JejuSched\config.json`.

## §8 기준선 — 0907→0910 순차 적용 (회귀 테스트 기준)

픽스처 `tests/fixtures/{0907,0908,0909,0910}.json`은 `tools/pdf_to_fixture.py`가 샘플 PDF에서
생성한다(커밋하지 않는다 — 위 "저장소에 없는 것들" 참고).
0907은 63건(도지사 1 · 행정부지사 2 · 기후경제부지사 4 · 실 9 · 미래 블록 47).

| 파일 | 추가 | 덮어쓰기 | 표시 | LLM |
|---|---|---|---|---|
| 0907 (빈 캘린더) | 63 | 0 | 0 | 0 |
| 0908 | 23 | 1 | 1 | 1회 |
| 0909 | 19 | 1 | 0 | 0회 |
| 0910 | 13 | 2 | 3 | 1회 |

- 0908의 LLM: 9/10 실 — 새 `(방송대담) JIBS<시사이슈 결> 10:00` ↔ 기존 `(토론)jibs"제주는 안전한가?" 10:00`
  = **같음**(덮어쓰기), 기존 `09:40 성화 출발식`은 짝 없음 → `*` 표시.
- 0910의 LLM: 9/11 실 — 새 `08:50 임명장 수여식` ↔ 기존 `09:00 현안업무 점검회의` = **다름**
  → 임명장 추가 + 현안업무 `*` 표시.
- 첫 실행에 4개가 모두 있으면 **배치 축약**으로 0910만 적용된다.

## 구현 순서와 현재 상태

| 단계 | 내용 | 상태 |
|---|---|---|
| M1 | models · normalize · matcher · planner · 픽스처 · FakeResolver | **완료** — §8 표와 일치 |
| M2 | SQLite state · reconcile · FakeCalendarClient · HOLD/재시도 | **완료** — 크래시·직접삭제·다중대상 시나리오 테스트 |
| M3 | OAuth auth · GoogleCalendarClient · 대상 추가/삭제 | **코드 완료, 실호출 미검증** |
| M4 | GeminiResolver · 캐시 · 예산 · 폴백 체인 | **코드 완료, 실호출 미검증** (프롬프트 구성·응답 검증은 테스트됨) |
| M5 | watcher · intake · force · 배치 축약 | **완료** — intake는 테스트됨, watchdog 실동작은 미검증 |
| M6 | 트레이 · 설정 창 · 마법사 · 토스트 | **코드 완료, 실행 미검증** (헤드리스라 띄울 수 없음) |
| M7 | GitHub Actions 빌드 | **워크플로 작성 완료, 실빌드 미검증** |
| M8 | HwpxParser | **미착수** — `parsers/hwpx.py`가 자리만 잡고 예외를 던진다 |

### 다음에 할 일 (우선순위 순)

1. **M3 실검증** — `add-account`로 개발 계정을 붙이고 `apply --dry-run` → 실적용으로
   보조 캘린더에 실제로 쓰이는지 확인. 확인할 것: 색상, `transparency`, 설명 본문,
   `extendedProperties`가 되읽히는지(= 두 번째 실행에서 전부 SKIP 이어야 한다).
2. **M4 실호출 1~2회** — 0908·0910 시나리오로 Gemini를 실제로 불러 판정이 §8과 같은지 확인.
   `llm_cache`에 들어가므로 같은 쌍은 두 번 부르지 않는다.
3. **M7 태그 빌드** — `v0.1.0` 태그를 밀어 windows-latest 빌드를 돌리고 새 PC에서 zip만으로 실행.
   `GOOGLE_OAUTH_CLIENT_JSON` Secret이 이미 등록되어 있다.
4. **M6 윈도우 실행 확인** — 트레이·설정 창·마법사는 윈도우에서 한 번 띄워 봐야 한다.
5. **M8 HwpxParser** — `Parser` Protocol만 만족하면 된다. 픽스처와 파싱 결과가 같은지
   비교하는 테스트를 붙일 것.

### 알려진 한계 (설계서에 명시된 것)

- 한 (날짜, 구분)의 일정이 **전부** 사라지면 파일에 행이 남지 않아 감지할 수 없다(§4-[4]).
- 로컬 DB가 사라지면 `user_deleted` 기록만 유실된다 — 직접 지운 일정이 1회 재생성될 수 있다.
- 무료 티어 약관상 Gemini로 보낸 내용이 구글 제품 개선에 쓰일 수 있다. 참석·장소 전송은
  설정으로 끌 수 있다(`llm.send_attendees`, `llm.send_location`).

## 테스트 지도

| 파일 | 지키는 것 |
|---|---|
| `test_sequence.py` | **§8 기준선** — 0907→0910 순차 적용의 추가/덮어쓰기/표시/LLM 횟수 |
| `test_normalize.py` | title_norm · 시간 파싱 · 연도 추정 · 해시 · judged · 결정적 ID |
| `test_matcher.py` | 규칙 1~5 각각, HOLD 범위, 1:1 강제 |
| `test_planner.py` | Google 이벤트 본문 매핑(색상·transparency·description·표시 접두) |
| `test_scenarios.py` | §9 실패 모드 — DB 유실·크래시·409·직접삭제·재로그인·다중대상·예산 |
| `test_intake.py` | §4-[2] 배치 축약·중복·오래된 파일·force |
| `test_prompts.py` | LLM에 보낼 필드 제한(§11-9), 응답 검증(없는 id·1:1 위반) |
