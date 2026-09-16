"""엔트리 포인트 — 트레이 기동과 개발용 CLI.

인자 없이 실행하면 트레이 앱(설계서 §6). 맥에서 파이프라인을 손으로 돌려보려면
`python -m jejusched apply <파일> --dry-run`처럼 쓴다.
"""

from __future__ import annotations

import argparse
import logging
import queue
import sys
from pathlib import Path

from . import logging_setup
from . import __version__
from .config import (
    APP_NAME, AppConfig, ConfigError, app_home, is_frozen, load_gemini_api_key,
)
from .core.pipeline import Pipeline
from .core.state import State
from .gcal.auth import Authenticator
from .gcal.client import AuthExpired, CalendarClient, CalendarError, GoogleCalendarClient
from .llm.resolver import build_resolver
from .parsers import DispatchingParser
from .watcher.intake import Intake
from .watcher.worker import Worker, WorkerDeps

log = logging.getLogger(__name__)
LOCK_NAME = "jejusched.lock"


# ------------------------------------------------------------------ 조립


def build_pipeline(config: AppConfig, state: State, auth: Authenticator) -> Pipeline:
    def client_for(target) -> CalendarClient:  # noqa: ANN001
        credentials = auth.load(target.email)
        if credentials is None:
            raise AuthExpired(f"{target.email}: 저장된 토큰을 쓸 수 없다")
        return GoogleCalendarClient(credentials)

    resolver = build_resolver(config, load_gemini_api_key())
    return Pipeline(config, state, DispatchingParser(), client_for, resolver)


def build_worker(config: AppConfig, state: State, auth: Authenticator) -> tuple[Worker, "queue.Queue[Path]"]:
    from .ui.notify import notify_result

    pipeline = build_pipeline(config, state, auth)
    deps = WorkerDeps(
        config=config, state=state, pipeline=pipeline,
        intake=Intake(state, DispatchingParser(), config),
    )
    work_queue: "queue.Queue[Path]" = queue.Queue()
    return Worker(deps, work_queue, on_result=notify_result), work_queue


class SingleInstance:
    """두 번째 실행을 막는다 — 직렬 처리 전제가 깨지면 안 된다."""

    def __init__(self) -> None:
        self.path = app_home() / LOCK_NAME
        self._handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._handle = open(self.path, "w")
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if self._handle:
                self._handle.close()
                self._handle = None
            return False
        self._handle.write(str(Path(sys.argv[0]).name))
        self._handle.flush()
        return True

    def release(self) -> None:
        if self._handle:
            self._handle.close()
            self._handle = None


# ------------------------------------------------------------------ 명령


def cmd_apply(args: argparse.Namespace) -> int:
    config = AppConfig.load()
    if args.dry_run:
        config.dry_run = True
    with State(app_home() / "state.db") as state:
        targets = state.active_targets()
        if not targets:
            print("등록된 계정이 없다. 먼저 `add-account`를 실행하라.", file=sys.stderr)
            return 2
        auth = Authenticator(config)
        pipeline = build_pipeline(config, state, auth)
        result = pipeline.run(Path(args.path), targets)
        totals = result.totals()
        print(f"파일 날짜 {result.file_date} · 일정 {result.events}건 · LLM {result.llm_calls}회")
        print(f"추가 {totals['created']} · 덮어쓰기 {totals['updated'] + totals['restored']} · "
              f"표시 {totals['marked']} · 변경없음 {totals['skipped']} · 보류 {totals['held']}")
        for warning in result.warnings:
            print(f"  경고: {warning}")
        return 0


def cmd_add_account(args: argparse.Namespace) -> int:
    config = AppConfig.load()
    auth = Authenticator(config)
    try:
        result = auth.add_target(port=args.port, open_browser=not args.no_browser)
    except ConfigError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2
    client = GoogleCalendarClient(result.credentials)
    calendar_id = client.ensure_calendar(config.calendar_name)
    with State(app_home() / "state.db") as state:
        state.upsert_target(result.email, calendar_id, config.calendar_name)
    print(f"계정 추가: {result.email}\n캘린더: {config.calendar_name} ({calendar_id})")
    return 0


def cmd_list_accounts(args: argparse.Namespace) -> int:
    with State(app_home() / "state.db") as state:
        rows = state.conn.execute("SELECT * FROM targets ORDER BY id").fetchall()
        if not rows:
            print("등록된 계정이 없다.")
        for row in rows:
            flags = []
            if not row["enabled"]:
                flags.append("사용안함")
            if row["needs_reauth"]:
                flags.append("재로그인 필요")
            print(f"{row['email']}  {row['calendar_name']}  {' '.join(flags)}")
    return 0


def cmd_remove_account(args: argparse.Namespace) -> int:
    config = AppConfig.load()
    auth = Authenticator(config)
    with State(app_home() / "state.db") as state:
        target = state.get_target(args.email)
        if target is None:
            print(f"그런 계정이 없다: {args.email}", file=sys.stderr)
            return 2

        removed_calendar = False
        if args.delete_calendar:
            #  토큰을 폐기하기 **전에** 지워야 한다 — 폐기한 뒤에는 API를 부를 수 없다.
            if not args.yes and not _confirm(
                f"{target.calendar_name or '보조 캘린더'}({target.calendar_id})와 "
                f"그 안의 일정이 모두 삭제됩니다. 되돌릴 수 없습니다."
            ):
                print("취소했다.")
                return 1
            try:
                credentials = auth.load(args.email)
                if credentials is None:
                    raise CalendarError("저장된 토큰을 쓸 수 없다 — 캘린더를 지우지 못했다")
                GoogleCalendarClient(credentials).delete_calendar(target.calendar_id)
                removed_calendar = True
            except CalendarError as exc:
                print(f"캘린더 삭제 실패: {exc}", file=sys.stderr)
                print("계정만 삭제한다. 캘린더는 구글 캘린더에서 직접 지울 수 있다.", file=sys.stderr)

        auth.revoke(args.email)
        state.delete_target(target.id)
    tail = "캘린더도 삭제했다" if removed_calendar else "보조 캘린더는 남겨 두었다"
    print(f"계정 삭제: {args.email} ({tail})")
    return 0


def _confirm(message: str) -> bool:
    """되돌릴 수 없는 일을 하기 전에 한 번 더 묻는다."""
    print(message)
    try:
        return input("정말 진행할까? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def cmd_force(args: argparse.Namespace) -> int:
    config = AppConfig.load()
    with State(app_home() / "state.db") as state:
        worker, _ = build_worker(config, state, Authenticator(config))
        result = worker.force_refresh(
            Path(args.path) if args.path else None, restore_deleted=args.restore_deleted
        )
        if result is None:
            print("적용할 파일이 없다.")
            return 1
        print(result.totals())
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = AppConfig.load()
    print(f"설정 위치   : {app_home()}")
    print(f"감시 폴더   : {config.watch_dir or '(미설정)'} ({config.file_glob})")
    print(f"Gemini 키   : {'있음' if load_gemini_api_key() else '없음 → 규칙 3은 다른 일정으로 처리'}")
    with State(app_home() / "state.db") as state:
        print(f"계정        : {len(state.active_targets())}개 활성")
        for row in state.recent_runs(5):
            print(f"  {row['finished_at']} 추가 {row['created']} 표시 {row['marked']}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """어디서 막히는지 단계별로 찍는다 — 읽기만 하고 아무것도 바꾸지 않는다.

    윈도우에서 "감지가 안 된다"를 로그만 보고 쫓는 것이 너무 느려서 만들었다.
    한 번 돌리면 폴더·패턴·계정·토큰·게이트 중 어디서 걸리는지 바로 보인다.
    """
    ok, warn = "  [정상]", "  [문제]"
    config = AppConfig.load()

    print(f"버전        : {__version__}")
    print(f"설정 위치   : {app_home()}")
    print(f"로그        : {logging_setup.log_dir() / 'jejusched.log'}")
    print(f"번들 실행   : {'예 (exe)' if is_frozen() else '아니오 (소스)'}")
    print()

    # ── 1. 감시 폴더와 패턴
    print("① 감시 폴더와 파일 패턴")
    #  repr로 찍는다 — 설정 창에서 붙여 넣다 들어간 공백·개행이 여기서 드러난다
    print(f"   watch_dir  = {config.watch_dir!r}")
    print(f"   file_glob  = {config.file_glob!r}")
    if config.file_glob != config.file_glob.strip():
        print(f"{warn} 파일 패턴 앞뒤에 공백이 있다 — 어떤 파일도 맞지 않는다")
    folder = Path(config.watch_dir) if config.watch_dir else None
    if folder is None:
        print(f"{warn} 감시 폴더가 설정되지 않았다")
        return 1
    if not folder.is_dir():
        print(f"{warn} 폴더가 없거나 폴더가 아니다: {folder}")
        return 1
    print(f"{ok} 폴더가 있다")

    # ── 2. 폴더 안의 파일이 패턴에 맞는지 (하나씩 이유를 밝힌다)
    print()
    print("② 폴더 안의 파일 판정")
    try:
        entries = sorted(folder.iterdir())
    except OSError as exc:
        print(f"{warn} 폴더를 읽을 수 없다: {exc}")
        return 1
    if not entries:
        print(f"{warn} 폴더가 비어 있다")
    picked: list[Path] = []
    for entry in entries:
        why = _why_not_interesting(entry, config.file_glob)
        if why is None:
            picked.append(entry)
            print(f"   O  {entry.name}")
        else:
            print(f"   X  {entry.name}  ← {why}")
    if not picked:
        print(f"{warn} 처리 대상이 하나도 없다 — 감지도 새로고침도 여기서 끝난다")
        return 1
    print(f"{ok} 대상 {len(picked)}개")

    # ── 3. 계정
    print()
    print("③ 등록된 계정")
    with State(app_home() / "state.db") as state:
        rows = state.conn.execute("SELECT * FROM targets ORDER BY id").fetchall()
        if not rows:
            print(f"{warn} 계정이 없다 — 적용 단계에서 멈춘다")
            return 1
        for row in rows:
            flags = []
            if not row["enabled"]:
                flags.append("사용 안 함")
            if row["needs_reauth"]:
                flags.append("재로그인 필요")
            mark = "X " if flags else "O "
            print(f"   {mark} {row['email']}  캘린더={row['calendar_id']}"
                  + (f"  ← {', '.join(flags)}" if flags else ""))
        active = state.active_targets()
        if not active:
            print(f"{warn} 쓸 수 있는 계정이 없다 — `적용 시작`까지 가지 못한다")
            return 1
        print(f"{ok} 활성 계정 {len(active)}개")

        # ── 4. 토큰
        print()
        print("④ 저장된 토큰")
        auth = Authenticator(config)
        usable = 0
        for target in active:
            credentials = auth.load(target.email)
            usable += 1 if credentials else 0
            print(f"   {'O ' if credentials else 'X '} {target.email}"
                  + ("" if credentials else "  ← 읽지 못했다. 재로그인이 필요하다"))
        if not usable:
            #  토큰이 없으면 Reconcile에서 AuthExpired로 그 대상을 통째로 건너뛴다.
            #  게이트는 파일을 고르는데 캘린더에는 아무것도 안 들어가는 모습이 된다.
            print(f"{warn} 쓸 수 있는 토큰이 없다 — 파일을 골라도 캘린더에 쓰지 못한다")

        # ── 5. 게이트가 무엇을 고르는지 (읽기만 — record하지 않는다)
        print()
        print("⑤ 처리 게이트(Intake) 판정")
        intake = Intake(state, DispatchingParser(), config)
        for force in (False, True):
            label = "지금 새로고침(force)" if force else "평소 파일 감지"
            try:
                verdict = intake.evaluate(picked, force=force)
            except Exception as exc:  # noqa: BLE001
                print(f"{warn} {label}: 게이트가 터졌다 — {type(exc).__name__}: {exc}")
                continue
            if verdict.chosen:
                print(f"   O  {label} → {verdict.chosen.path.name} "
                      f"({verdict.chosen.file_date})")
            else:
                print(f"   X  {label} → 적용할 파일이 없다")
            for cand in verdict.rejected:
                print(f"        건너뜀: {cand.path.name} — {cand.status}: {cand.reason}")

        # ── 6. 이미 적용한 기록
        print()
        print("⑥ 지금까지 적용한 기록")
        print(f"   마지막 적용 파일 날짜: {state.max_applied_file_date() or '(없음)'}")
        for row in state.conn.execute(
            "SELECT path, status, file_date FROM files ORDER BY id DESC LIMIT 5"
        ).fetchall():
            print(f"   {row['status']:<18} {row['file_date']}  {Path(row['path']).name}")

    # ── 7. 폴더 감시 백엔드
    print()
    print("⑦ 폴더 감시(watchdog)")
    try:
        from watchdog.observers import Observer

        observer = Observer()
        print(f"{ok} 사용 가능 — {type(observer).__name__}")
    except Exception as exc:  # noqa: BLE001
        print(f"{warn} watchdog을 쓸 수 없다 ({exc}) — 시작 시 스캔만 동작한다")

    # ── 8. 알림
    print()
    print("⑧ 토스트 알림")
    if sys.platform != "win32":
        print("   (윈도우가 아니다 — 로그로 대체된다)")
    else:
        try:
            from windows_toasts import Toast, WindowsToaster

            toaster = WindowsToaster(APP_NAME)
            toast = Toast()
            toast.text_fields = ["주요일정 동기화", "진단 알림입니다"]
            toaster.show_toast(toast)
            print(f"{ok} 토스트를 보냈다 — 화면에 떴는지 확인하라")
        except Exception as exc:  # noqa: BLE001
            print(f"{warn} 토스트를 띄울 수 없다 ({type(exc).__name__}: {exc})")
            print("        → 기능은 돌아도 알림만 안 뜬다. 로그로 확인하라")

    print()
    print("⑨ LLM")
    print(f"   규칙 3 판정: {'켜짐' if load_gemini_api_key() else '꺼짐 (키 없음 — 정상 동작한다)'}")
    print()
    print("여기까지 [문제] 표시가 없으면 `apply`로 실제 적용을 시험하라:")
    print('   jejusched-cli.exe -v apply "파일경로.hwpx" --dry-run')
    return 0


def _why_not_interesting(path: Path, glob: str) -> str | None:
    """`is_interesting`이 왜 걸렀는지 사람 말로. 판정 규칙과 **같은 순서**로 본다."""
    from .watcher.folder_watch import IGNORED_PREFIXES, IGNORED_SUFFIXES

    if path.name.startswith(IGNORED_PREFIXES):
        return f"무시 대상 이름으로 시작한다 {IGNORED_PREFIXES}"
    if path.suffix.lower() in IGNORED_SUFFIXES:
        return f"무시 대상 확장자다 {IGNORED_SUFFIXES}"
    if not path.match(glob):
        return f"파일 패턴 {glob!r}과 맞지 않는다"
    try:
        if not path.is_file():
            return "파일이 아니다(폴더 등)"
        if path.stat().st_size <= 0:
            return "크기가 0이다"
    except OSError as exc:
        return f"읽을 수 없다: {exc}"
    return None


def cmd_tray(args: argparse.Namespace) -> int:
    from .ui.tray import run_tray

    lock = SingleInstance()
    if not lock.acquire():
        print("이미 실행 중이다.", file=sys.stderr)
        return 1
    try:
        return run_tray()
    finally:
        lock.release()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jejusched", description="주요일정 → Google Calendar 동기화")
    parser.add_argument("--verbose", "-v", action="store_true", help="디버그 로그")
    sub = parser.add_subparsers(dest="command")

    p_apply = sub.add_parser("apply", help="파일 하나를 적용한다")
    p_apply.add_argument("path")
    p_apply.add_argument("--dry-run", action="store_true", help="계획만 로그에 쓰고 끝낸다")
    p_apply.set_defaults(func=cmd_apply)

    p_add = sub.add_parser("add-account", help="구글 계정을 추가한다")
    p_add.add_argument("--port", type=int, default=0,
                       help="리디렉션을 받을 로컬 포트(기본 임의). 원격 환경에서 주소를 미리 알고 싶을 때")
    p_add.add_argument("--no-browser", action="store_true",
                       help="브라우저를 열지 않고 인증 주소만 출력한다")
    p_add.set_defaults(func=cmd_add_account)
    sub.add_parser("list-accounts", help="등록된 계정을 보여준다").set_defaults(func=cmd_list_accounts)
    sub.add_parser("status", help="현재 설정과 최근 처리를 보여준다").set_defaults(func=cmd_status)

    p_remove = sub.add_parser("remove-account", help="계정을 삭제하고 토큰을 폐기한다")
    p_remove.add_argument("email")
    p_remove.add_argument("--delete-calendar", action="store_true",
                          help="보조 캘린더와 그 안의 일정까지 삭제한다 (되돌릴 수 없다)")
    p_remove.add_argument("--yes", "-y", action="store_true", help="확인 질문을 건너뛴다")
    p_remove.set_defaults(func=cmd_remove_account)

    sub.add_parser(
        "doctor", help="어디서 막히는지 단계별로 점검한다 (아무것도 바꾸지 않는다)"
    ).set_defaults(func=cmd_doctor)

    p_force = sub.add_parser("force", help="강제 새로고침")
    p_force.add_argument("path", nargs="?")
    p_force.add_argument("--restore-deleted", action="store_true",
                         help="직접 삭제한 일정도 복구한다")
    p_force.set_defaults(func=cmd_force)

    args = parser.parse_args(argv)
    logging_setup.setup(logging.DEBUG if args.verbose else logging.INFO)

    handler = getattr(args, "func", cmd_tray)
    try:
        return handler(args)
    except KeyboardInterrupt:
        return 130
    except ConfigError as exc:
        log.error("설정 오류: %s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
