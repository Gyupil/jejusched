"""설정 창 — 설계서 §6.

탭: 상태 · 계정 · 설정 · 로그
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import AppConfig, DEFAULT_COLOR_MAP, load_gemini_api_key
from ..core.state import State
from ..gcal.auth import Authenticator
from .. import autostart
from ..gcal.client import GoogleCalendarClient
from ..logging_setup import log_dir
from ..watcher.worker import Worker

log = logging.getLogger(__name__)

COLOR_NAMES = {
    "11": "토마토", "6": "귤", "2": "세이지", "9": "블루베리",
    "8": "그래파이트", "1": "라벤더", "3": "포도", "4": "플라밍고",
    "5": "바나나", "7": "공작", "10": "바질",
}


def open_settings_window(config: AppConfig, state: State, auth: Authenticator, worker: Worker) -> None:
    try:
        import customtkinter as ctk
        from tkinter import filedialog, messagebox
    except ImportError:
        log.error("customtkinter가 없다 — `pip install .[ui]`")
        return

    window = ctk.CTk()
    window.title("주요일정 동기화 — 설정")
    window.geometry("680x520")
    tabs = ctk.CTkTabview(window)
    tabs.pack(fill="both", expand=True, padx=12, pady=12)
    for name in ("상태", "계정", "설정", "로그"):
        tabs.add(name)

    # ---------------------------------------------------------- 상태
    status = tabs.tab("상태")
    watch_label = ctk.CTkLabel(status, text=f"감시 폴더: {config.watch_dir or '(미설정)'}", anchor="w")
    watch_label.pack(fill="x", padx=12, pady=(12, 4))

    recent = ctk.CTkTextbox(status, height=260)
    recent.pack(fill="both", expand=True, padx=12, pady=8)

    def refresh_recent():
        recent.configure(state="normal")
        recent.delete("1.0", "end")
        rows = state.recent_runs(20)
        if not rows:
            recent.insert("end", "아직 처리한 파일이 없습니다.\n")
        for row in rows:
            recent.insert(
                "end",
                f"{row['finished_at']}  추가 {row['created']} · 덮어쓰기 "
                f"{row['updated'] + row['restored']} · 표시 {row['marked']} · 보류 {row['held']}"
                + (f"  오류: {row['error']}" if row["error"] else "") + "\n",
            )
        recent.configure(state="disabled")

    refresh_recent()

    restore_var = ctk.BooleanVar(value=False)
    buttons = ctk.CTkFrame(status)
    buttons.pack(fill="x", padx=12, pady=8)

    def do_force():
        import threading

        def _run():
            worker.force_refresh(restore_deleted=restore_var.get())
            window.after(0, refresh_recent)

        threading.Thread(target=_run, daemon=True).start()

    ctk.CTkButton(buttons, text="강제 새로고침", command=do_force).pack(side="left", padx=6)
    ctk.CTkCheckBox(buttons, text="직접 삭제한 일정도 복구", variable=restore_var).pack(
        side="left", padx=6
    )

    # ---------------------------------------------------------- 계정
    accounts = tabs.tab("계정")
    account_box = ctk.CTkScrollableFrame(accounts, height=320)
    account_box.pack(fill="both", expand=True, padx=12, pady=12)

    def refresh_accounts():
        for child in account_box.winfo_children():
            child.destroy()
        rows = state.conn.execute("SELECT * FROM targets ORDER BY id").fetchall()
        if not rows:
            ctk.CTkLabel(account_box, text="등록된 계정이 없습니다.").pack(pady=12)
        for row in rows:
            frame = ctk.CTkFrame(account_box)
            frame.pack(fill="x", pady=4)
            note = " · 재로그인 필요" if row["needs_reauth"] else ""
            ctk.CTkLabel(frame, text=f"{row['email']}{note}", anchor="w").pack(
                side="left", padx=10, fill="x", expand=True
            )
            ctk.CTkButton(
                frame, text="재로그인", width=80,
                command=lambda e=row["email"]: relogin(e),
            ).pack(side="left", padx=4)
            ctk.CTkButton(
                frame, text="삭제", width=60, fg_color="#a33",
                command=lambda e=row["email"]: remove(e),
            ).pack(side="left", padx=4)

    def add_account():
        try:
            result = auth.add_target()
            client = GoogleCalendarClient(result.credentials)
            calendar_id = client.ensure_calendar(config.calendar_name)
            state.upsert_target(result.email, calendar_id, config.calendar_name)
            refresh_accounts()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("계정 추가 실패", str(exc))

    def relogin(email: str):
        try:
            result = auth.add_target()
            if result.email != email:
                messagebox.showwarning("다른 계정", f"{email}이 아니라 {result.email}로 로그인했습니다.")
            target = state.get_target(result.email)
            if target:
                state.set_needs_reauth(target.id, False)
            refresh_accounts()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("재로그인 실패", str(exc))

    def remove(email: str):
        target = state.get_target(email)
        if target is None:
            return
        also_calendar = delete_calendar_var.get()
        first = (
            f"{email}을 삭제할까요?\n"
            + ("보조 캘린더와 그 안의 일정도 모두 삭제됩니다."
               if also_calendar else "보조 캘린더는 남습니다.")
        )
        if not messagebox.askyesno("계정 삭제", first):
            return
        if also_calendar:
            #  되돌릴 수 없는 일이라 한 번 더 묻는다(완성 절차 1-2의 위험 항목)
            if not messagebox.askokcancel(
                "캘린더까지 삭제",
                f"'{target.calendar_name or '주요일정(자동)'}' 캘린더와 그 안의 일정이\n"
                f"모두 삭제됩니다. 되돌릴 수 없습니다.\n\n계속할까요?",
                icon="warning",
            ):
                return
            try:
                credentials = auth.load(email)
                if credentials is None:
                    raise RuntimeError("저장된 토큰을 쓸 수 없습니다")
                GoogleCalendarClient(credentials).delete_calendar(target.calendar_id)
            except Exception as exc:  # noqa: BLE001
                messagebox.showwarning(
                    "캘린더 삭제 실패",
                    f"계정만 삭제합니다. 캘린더는 구글 캘린더에서 직접 지울 수 있습니다.\n\n{exc}",
                )
        auth.revoke(email)
        state.delete_target(target.id)
        delete_calendar_var.set(False)  # 다음 삭제에 딸려가지 않게 되돌린다
        refresh_accounts()

    delete_calendar_var = ctk.BooleanVar(value=False)  # 기본은 반드시 꺼짐
    ctk.CTkCheckBox(
        accounts, text="삭제할 때 보조 캘린더와 그 안의 일정도 삭제 (되돌릴 수 없음)",
        variable=delete_calendar_var,
    ).pack(pady=(0, 6))
    ctk.CTkButton(accounts, text="계정 추가", command=add_account).pack(pady=(0, 12))
    refresh_accounts()

    # ---------------------------------------------------------- 설정
    settings = tabs.tab("설정")
    form = ctk.CTkScrollableFrame(settings)
    form.pack(fill="both", expand=True, padx=12, pady=12)

    folder_var = ctk.StringVar(value=config.watch_dir)
    glob_var = ctk.StringVar(value=config.file_glob)
    key_var = ctk.StringVar(value="●●●●●●" if load_gemini_api_key() else "")
    mark_prefix_var = ctk.StringVar(value=config.mark_prefix)
    mark_color_var = ctk.BooleanVar(value=config.mark_change_color)
    dry_run_var = ctk.BooleanVar(value=config.dry_run)
    llm_var = ctk.BooleanVar(value=config.llm.enabled)
    send_attendees_var = ctk.BooleanVar(value=config.llm.send_attendees)
    send_location_var = ctk.BooleanVar(value=config.llm.send_location)
    color_vars = {
        section: ctk.StringVar(value=config.color_map.get(section, default))
        for section, default in DEFAULT_COLOR_MAP.items()
    }

    def labelled(parent, text, widget):
        row = ctk.CTkFrame(parent)
        row.pack(fill="x", pady=4)
        ctk.CTkLabel(row, text=text, width=140, anchor="w").pack(side="left", padx=8)
        widget(row)
        return row

    def folder_row(row):
        ctk.CTkEntry(row, textvariable=folder_var).pack(side="left", fill="x", expand=True, padx=6)
        ctk.CTkButton(
            row, text="찾기", width=60,
            command=lambda: folder_var.set(filedialog.askdirectory() or folder_var.get()),
        ).pack(side="left", padx=6)

    labelled(form, "감시 폴더", folder_row)
    labelled(form, "파일 패턴", lambda r: ctk.CTkEntry(r, textvariable=glob_var).pack(
        side="left", fill="x", expand=True, padx=6))
    labelled(form, "Gemini 키", lambda r: ctk.CTkEntry(r, textvariable=key_var, show="•").pack(
        side="left", fill="x", expand=True, padx=6))
    labelled(form, "표시 접두", lambda r: ctk.CTkEntry(r, textvariable=mark_prefix_var, width=80).pack(
        side="left", padx=6))

    for section, var in color_vars.items():
        labelled(
            form, f"색상 · {section}",
            lambda r, v=var: ctk.CTkOptionMenu(
                r, values=list(COLOR_NAMES), variable=v, width=120,
                command=lambda _v: None,
            ).pack(side="left", padx=6),
        )

    for text, var in (
        ("표시할 때 색도 바꾸기", mark_color_var),
        ("LLM 판정 사용", llm_var),
        ("LLM에 참석자 전송", send_attendees_var),
        ("LLM에 장소 전송", send_location_var),
        ("계획만 확인 (dry-run)", dry_run_var),
    ):
        ctk.CTkCheckBox(form, text=text, variable=var).pack(anchor="w", padx=12, pady=3)

    #  자동 실행 — 바로가기가 실제로 있는지를 진실로 삼는다(설정값보다 정확하다)
    autostart_ok, autostart_reason = autostart.available()
    autostart_var = ctk.BooleanVar(value=autostart.is_enabled() if autostart_ok else False)
    autostart_box = ctk.CTkCheckBox(
        form, text="윈도우 시작할 때 자동 실행", variable=autostart_var
    )
    autostart_box.pack(anchor="w", padx=12, pady=3)
    if not autostart_ok:
        autostart_box.configure(state="disabled")
        ctk.CTkLabel(
            form, text=f"   {autostart_reason}", anchor="w", text_color="#888",
        ).pack(anchor="w", padx=12)

    def save():
        from .wizard import save_gemini_key

        config.watch_dir = folder_var.get()
        config.file_glob = glob_var.get()
        config.mark_prefix = mark_prefix_var.get()
        config.mark_change_color = mark_color_var.get()
        config.dry_run = dry_run_var.get()
        config.llm.enabled = llm_var.get()
        config.llm.send_attendees = send_attendees_var.get()
        config.llm.send_location = send_location_var.get()
        config.color_map = {s: v.get() for s, v in color_vars.items()}

        #  자동 실행은 파일 시스템을 건드리므로 실패해도 나머지 저장은 끝내야 한다
        if autostart_ok and autostart_var.get() != autostart.is_enabled():
            try:
                autostart.apply(autostart_var.get())
            except autostart.AutostartError as exc:
                messagebox.showwarning("자동 실행 설정 실패", str(exc))
                autostart_var.set(autostart.is_enabled())
        config.autostart = autostart_var.get()
        config.save()
        if key_var.get() and "●" not in key_var.get():
            save_gemini_key(key_var.get())
        watch_label.configure(text=f"감시 폴더: {config.watch_dir or '(미설정)'}")
        messagebox.showinfo("저장했습니다", "설정을 저장했습니다.\n감시 폴더 변경은 다시 시작하면 반영됩니다.")

    ctk.CTkButton(settings, text="저장", command=save, height=36).pack(pady=(0, 12))

    # ---------------------------------------------------------- 로그
    logs = tabs.tab("로그")
    log_box = ctk.CTkTextbox(logs)
    log_box.pack(fill="both", expand=True, padx=12, pady=12)

    def refresh_logs():
        log_box.configure(state="normal")
        log_box.delete("1.0", "end")
        path = log_dir() / "jejusched.log"
        if path.is_file():
            tail = path.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
            log_box.insert("end", "\n".join(tail))
        else:
            log_box.insert("end", "로그 파일이 아직 없습니다.")
        log_box.see("end")
        log_box.configure(state="disabled")

    refresh_logs()
    ctk.CTkButton(logs, text="새로 고침", command=refresh_logs).pack(pady=(0, 12))

    window.mainloop()
