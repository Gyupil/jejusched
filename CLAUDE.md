# CLAUDE.md — jejusched

제주도 「주요일정」 hwpx 파일을 감시해 Google Calendar에 자동 반영하는 윈도우 트레이 앱.
macOS에서 개발·테스트하고 GitHub Actions(`windows-latest`)에서 PyInstaller로 빌드한다.

> **중요 — 저장소에 없는 것들**
> `docs/*.md`(설계서·파서 설계·완성 절차), `docs/example_docs/`(샘플 PDF),
> `docs/hwpx_raws/`(hwpx 원본), `docs/hwpx_fixtures/`(파서 정답지),
> `tests/fixtures/*.json`은 실제 일정이 담겨 있어 `.gitignore` 대상이다.
> **새로 클론한 저장소에는 없다.** 이 문서가 설계 결정의 단일 출처다.
>
> hwpx 원본에는 작성자 이름과 문서보안(Fasoo) 추적 ID가 들어 있다 —
> **공개 저장소에 절대 올리지 않는다.**
>
> 자료가 없으면 의존 테스트 45개가 **건너뛰기(skip)**로 처리된다 — 실패가 아니다.
> 나머지 127개는 어디서나 돈다(hwpx 파서는 합성 문서로도 검증한다).
> 원본 PDF가 있으면 `python tools/pdf_to_fixture.py`로 픽스처를 되살린다.

## 이 프로젝트에서 일하는 법

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest -q                      # 자료 있으면 172 통과 / 없으면 127 통과 + 45 skip
.venv/bin/python -m jejusched status               # 설정·계정 확인
.venv/bin/python -m jejusched apply docs/hwpx_raws/0910_주요일정.hwpx --dry-run
.venv/bin/python -m jejusched add-account          # 브라우저 OAuth (실제 계정 필요)
.venv/bin/python tools/pdf_to_fixture.py           # 샘플 PDF → 픽스처 재생성
.venv/bin/python tools/make_icon.py                # build/jejusched.ico 재생성

# hwpx가 안 읽힐 때는 **먼저 격자를 눈으로 본다** — 구분이 밀렸는지 한눈에 보인다
.venv/bin/python -m jejusched.parsers.hwpx.dump 파일.hwpx --grid
.venv/bin/python -m jejusched.parsers.hwpx.dump 파일.hwpx          # 구분별 건수 + 항목 목록
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
- **`applied` 파일 기록을 내리지 마라.** 시작 시 스캔이 이미 적용한 파일을 `skipped_dup`으로
  덮으면 `max_applied_file_date()`가 비어 오래된 파일 규칙이 무력해지고, 재시작할 때마다
  같은 파일이 다시 적용된다 → `State.has_applied()`와 `record_file`의 강등 방지.
- **MARK도 `extendedProperties.private`를 통째로 보낸다.** 일부만 보내 `app`이 날아가면 그
  이벤트는 `privateExtendedProperty=app=jjsched` 조회에서 빠져 영영 고아가 된다.
- **SQLite 연결은 `check_same_thread=False`로 열어야 한다.** 트레이는 메인 스레드에서
  `State`를 만들고 워커·설정 창·강제 새로고침은 **각자 다른 스레드**에서 쓴다. 기본값이면
  그 스레드들이 전부 `ProgrammingError`로 죽는다. 마법사만 메인 스레드라 **"계정 추가·로그인·
  캘린더 생성은 되는데 그 뒤로 아무것도 안 되는"** 모습이 된다 — v0.1.0이 실제로 그랬다.
- **윈도우 GUI 빌드에는 stderr가 없다(`None`).** 스레드에서 터진 예외는 기본 훅이 stderr에
  쓰므로 **아무 데도 남지 않는다.** 위 버그로 스레드 넷이 죽는 동안 로그는 깨끗했다.
  `logging_setup.install_thread_excepthook()`이 이제 로그 파일로 끌어낸다. 이걸 지우지 마라.
- **테스트가 스레드 경계를 넘지 않으면 이 부류를 못 잡는다.** `Worker.process`를 테스트
  스레드에서 그냥 부르면 172개가 통과해도 실기에서는 죽는다. `test_watcher.py`의
  `test_state_is_usable_from_another_thread`·`test_worker_runs_in_a_background_thread`가
  그 경계를 지킨다. **파일 DB여야 재현된다** — `:memory:`는 스레드 의미가 다르다.
- **PyInstaller 엔트리(`__main__.py`)는 절대 임포트여야 한다.** 번들은 이 파일을 패키지가
  아닌 최상위 `__main__`으로 실행하므로 `from .main import ...`은 exe에서만 죽는다.
- **hwpx의 병합 셀은 아예 내보내지지 않는다.** `<hp:tr>`을 순서대로 읽으면 구분이 통째로
  밀려 도지사 일정이 실 일정으로 들어간다. `cellAddr`로 좌표를 잡고 `cellSpan`만큼 펼치면
  채워 내리기가 저절로 된다 → `parsers/hwpx/table.py:build_grid`.
- **한 문단 안의 여러 `hp:t`는 공백 없이 이어 붙인다.** 서식이 바뀌는 지점마다 런이 갈린다.
  공백을 끼우면 `수립(안) 에 따른`이 된다.
- **부기 줄은 `*`가 아니라 글자 크기로 가른다.** `(여자 개인전 DB, 육성종목)`처럼 기호 없이
  작기만 한 줄이 있다. `*`·`※` 규칙은 charPr을 못 읽었을 때의 폴백이다.
- **force의 `user_deleted` 삭제는 파일 날짜 범위로 한정한다.** 범위 없이 지우면 9월 파일을
  force했을 뿐인데 8월에 사용자가 직접 지운 일정이 되살아난다.

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
  autostart.py(윈도우 시작프로그램) updates.py(새 버전 확인)
  parsers/ base.py(Protocol) fixture_json.py
    hwpx/  __init__.py(HwpxParser) container.py(ZIP·섹션) xmlutil.py(로컬이름 탐색)
           charpr.py(색·크기) table.py(격자 복원) extract.py(열 결합·해석) dump.py(진단 CLI)
  gcal/    auth.py client.py(Protocol+Google) fake.py
  llm/     resolver.py(Protocol+Gemini+Fake) prompts.py cache.py budget.py
  watcher/ folder_watch.py intake.py worker.py
  ui/      tray.py settings_window.py wizard.py notify.py
tools/pdf_to_fixture.py   # 샘플 PDF → tests/fixtures/*.json 재생성
tools/make_icon.py        # 트레이 그림 → build/jejusched.ico (6가지 크기)
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
| M3 | OAuth auth · GoogleCalendarClient · 대상 추가/삭제 | **완료 — 실계정 검증** (2026-09-16, mamonde1015@gmail.com) |
| M4 | GeminiResolver · 캐시 · 예산 · 폴백 체인 | **완료 — 실호출 검증** (폴백까지 실제로 동작) |
| M5 | watcher · intake · force · 배치 축약 | **완료** — intake는 테스트됨, watchdog 실동작은 미검증 |
| M6 | 트레이 · 설정 창 · 마법사 · 토스트 | **코드 완료, 실행 미검증** (헤드리스라 띄울 수 없음) |
| M7 | GitHub Actions 빌드 | **워크플로 작성 완료, 실빌드 미검증** |
| M8 | HwpxParser | **완료 — 원본 4종 검증** (정답지와 필드 단위 완전 일치, 경고 0건) |

### M8 검증 기록 (2026-09-16)

`docs/hwpx_raws/*.hwpx` 4종을 `docs/hwpx_fixtures/fixture_*.json` 정답지와 대조했다.

| 파일 | 항목 | 경고 | 도지사 | 행정부지사 | 기후경제부지사 | 실 일정 |
|---|---|---|---|---|---|---|
| 0907 | 63 | 0 | 1 | 2 | 4 | 56 |
| 0908 | 69 | 0 | 5 | 4 | 4 | 56 |
| 0909 | 70 | 0 | 4 | 4 | 4 | 58 |
| 0910 | 60 | 0 | 2 | 1 | 3 | 54 |

- 262개 항목의 **모든 필드**가 정답지와 일치한다(`test_hwpx_matches_the_answer_key`).
- **§8 기준선이 hwpx 원본으로 그대로 재현된다** — 최종 118건 · `*` 4건
  (`test_design_section_8_baseline_holds_from_real_hwpx`). PDF 픽스처 경로와 결과가 같다.
- PDF 픽스처와 다른 항목은 **딱 하나**: 9/14 `전국장애인체육대회 경기장 현장 참관 및 격려`의
  `(여자 개인전 DB, 육성종목)`. PDF에는 글자 크기가 없어 행사명에 붙어 있었고 hwpx는 부기로
  가른다 — **hwpx 쪽이 옳다.** 이 차이는 테스트에 명시적으로 적혀 있다.
- `zipfile` + 표준 `xml.etree`만 쓴다. lxml·한/글 설치를 요구하지 않는다.

### 다음에 할 일 (우선순위 순)

1. **M6 윈도우 실행 확인** — 아래 "알려진 위험" 참고. 자동 실행 체크박스는
   빌드된 exe에서만 켜진다(`autostart.available()`).
2. **M7 태그 빌드** — `v0.1.0` 태그를 밀어 windows-latest 빌드를 돌리고 새 PC에서 zip만으로 실행.
   `GOOGLE_OAUTH_CLIENT_JSON` Secret이 이미 등록되어 있다.
3. **폴더 감시 실동작** — 한/글로 저장할 때 잠금이 풀린 뒤 처리되는지는 윈도우에서만 볼 수 있다.

## 실계정 검증 기록 (2026-09-16, mamonde1015@gmail.com)

**§8 표 전체가 실제 Google Calendar + 실제 Gemini에서 그대로 재현됐다.**

| 파일 | 추가 | 덮어쓰기 | 표시 | LLM | 비고 |
|---|---|---|---|---|---|
| 0907 | 63 | 0 | 0 | 0 | |
| 0907 재적용 | **0** | 0 | 0 | 0 | **변경없음 63** — `extendedProperties` 왕복 확인 |
| 0908 | 23 | 1 | 1 | 1 | `gemini-3.8-flash`가 JIBS 쌍을 "같음" 판정 |
| 0909 | 19 | 1 | 0 | 0 | |
| 0910 | 13 | 2 | 3 | 1 | 주 모델 503 → **폴백**이 판정 |

최종 118건 · `*` 표시 4건(성화 출발식 9/10, 임명장 9/10, 현안업무 9/11, 생명지킴 9/18).
9/11의 "다름" 판정으로 임명장 08:50과 `* 현안업무` 09:00이 나란히 남은 것까지 §8과 일치.

확인된 사실 (설계서가 불확실하다고 적었던 것들):
- **`calendar.app.created`로 `calendarList.list`가 된다.** 전체 `calendar` 스코프로 넘어갈 필요가 없었다.
- `gemini-3.8-flash` / `gemini-3.5-flash-lite` 둘 다 실재하고 `response_schema`·`thinking_level`을 받는다.
- **폴백 체인이 실전에서 발동했다** — 주 모델 503(일시 과부하) → 폴백이 정확히 판정. §9의 대응이 맞았다.
- 결정적 id(base32hex 26자)를 Calendar가 그대로 받는다.
- Google은 기본값을 응답에서 생략한다 — `transparency: opaque`는 `None`으로 돌아온다(정상).

### 원격·헤드리스 환경에서 계정 추가하기

브라우저가 다른 기기에 있으면 `http://localhost:<port>`로 리디렉션이 돌아오지 못한다.

```bash
.venv/bin/python -m jejusched add-account --port 8765 --no-browser   # 인증 URL이 출력된다
# 다른 기기에서 그 URL을 열고 승인 → "연결할 수 없음" 오류 페이지가 뜬다(정상)
# 주소창의 전체 URL을 복사해 이 기기에서 연다:
curl 'http://localhost:8765/?state=...&code=...'
```

코드 교환은 PKCE 검증자를 쥔 **대기 중인 프로세스**가 해야 하므로 반드시 그 기기에서 열어야 한다.

### 남아 있을 수 있는 함정 (대비 코드는 넣어 뒀다)

| 증상 | 원인·대응 |
|---|---|
| `add-account`가 `Warning: Scope has changed`로 실패 | 구글이 돌려준 스코프가 요청과 다르다. `add_target()`이 `OAUTHLIB_RELAX_TOKEN_SCOPE=1`을 미리 켠다 |
| `ensure_calendar`가 목록 조회에서 403 | 잡아서 바로 생성으로 넘어간다. 계속 문제면 `use_full_calendar_scope=true` |
| Gemini가 `thinking_config`에 400 | 그것만 빼고 같은 모델로 재시도한 뒤 폴백으로 넘어간다 |
| 모든 묶음이 HOLD | 예산 소진(`llm.daily_budget`) 또는 두 모델 모두 실패. 로그에 어느 쪽인지 남는다 |

### 알려진 위험 (M6, 미검증)

`open_settings_window`는 pystray가 메인 스레드를 쥔 상태에서 데몬 스레드에 `ctk.CTk()`
루트를 만든다. Tcl은 스레드에 예민해서 두 번째로 열 때 오작동할 수 있다.

**1차 대응은 적용했다** — 설정 창을 단일 인스턴스로 묶었다(`ui/tray.py`의 `settings_thread`).
이미 열려 있으면 새로 만들지 않고 토스트로 알린다. 그래도 윈도우에서 증상이 나오면
pystray를 별도 스레드로 돌리고 Tk를 메인 스레드에 두는 쪽으로 바꾼다
(종료 처리 `icon.stop()` ↔ `window.destroy()`를 다시 짜야 한다).

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
| `test_hwpx_parser.py` | **M8** — 합성 hwpx로 격자·병합·부기·시간 해석 / 원본 4종 정답지 대조 / §8 기준선을 hwpx로 재현 |
| `test_watcher.py` | `wait_until_settled`(가짜 시계) · `is_interesting` · force의 `user_deleted` 날짜 범위 · 캘린더 삭제 |
| `test_autostart.py` | 윈도우 자동 실행 — 소스 실행 중에는 켜지지 않을 것, 끄기는 어디서나 안전할 것 |
| `test_updates.py` | 새 버전 확인 — **네트워크 실패가 예외로 새 나오지 않을 것** |

`test_scenarios.py`에는 회귀 방지용으로 남긴 것이 둘 있다 — `test_sync_runs_are_journalled`
([10] Journal이 실제로 기록되는지)와 `test_mark_keeps_every_extended_property`
(MARK 후에도 이벤트가 다시 읽히는지). 둘 다 없으면 조용히 망가지는 종류다.

§8 기준선은 **두 경로로** 지킨다 — `test_sequence.py`(PDF 픽스처)와
`test_hwpx_parser.py::test_design_section_8_baseline_holds_from_real_hwpx`(hwpx 원본).
둘 다 통과해야 "파서를 갈아 끼워도 캘린더 결과가 같다"가 성립한다.
