"""최초 실행 마법사 — 설계서 §6.

① 폴더 선택 ② Gemini 키 입력 ③ 구글 계정 추가 ④ 완료
"""

from __future__ import annotations

import logging

from ..config import AppConfig, load_env_file, app_home
from ..core.state import State
from ..gcal.auth import Authenticator
from ..gcal.client import GoogleCalendarClient

log = logging.getLogger(__name__)


def save_gemini_key(key: str) -> None:
    """키를 앱 홈의 local.env에 넣는다. config.load_gemini_api_key가 여기도 본다."""
    path = app_home() / "local.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    values = load_env_file(path)
    values["GEMINI_API_KEY"] = key.strip()
    path.write_text(
        "\n".join(f"{k}={v}" for k, v in values.items() if v) + "\n", encoding="utf-8"
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass


def run_wizard(config: AppConfig, state: State, auth: Authenticator) -> AppConfig | None:
    """창을 띄워 초기 설정을 받는다. 취소하면 None."""
    try:
        import customtkinter as ctk
        from tkinter import filedialog, messagebox
    except ImportError:
        log.error("customtkinter가 없다 — `pip install .[ui]`")
        return None

    ctk.set_appearance_mode("system")
    window = ctk.CTk()
    window.title("주요일정 동기화 — 처음 설정")
    window.geometry("560x420")
    result: dict[str, AppConfig | None] = {"config": None}

    ctk.CTkLabel(
        window, text="주요일정 자동 동기화", font=("", 20, "bold")
    ).pack(pady=(24, 4))
    ctk.CTkLabel(
        window,
        text="hwpx 파일이 저장되는 폴더를 감시해 구글 캘린더에 반영합니다.",
        wraplength=480,
    ).pack(pady=(0, 16))

    # ① 폴더
    folder_var = ctk.StringVar(value=config.watch_dir)
    row = ctk.CTkFrame(window)
    row.pack(fill="x", padx=24, pady=6)
    ctk.CTkLabel(row, text="① 감시 폴더", width=110, anchor="w").pack(side="left", padx=8)
    ctk.CTkEntry(row, textvariable=folder_var).pack(side="left", fill="x", expand=True, padx=8)

    def pick_folder():
        chosen = filedialog.askdirectory(title="주요일정 파일이 저장되는 폴더")
        if chosen:
            folder_var.set(chosen)

    ctk.CTkButton(row, text="찾기", width=64, command=pick_folder).pack(side="left", padx=8)

    # ② Gemini 키
    key_var = ctk.StringVar()
    row2 = ctk.CTkFrame(window)
    row2.pack(fill="x", padx=24, pady=6)
    ctk.CTkLabel(row2, text="② Gemini 키", width=110, anchor="w").pack(side="left", padx=8)
    ctk.CTkEntry(row2, textvariable=key_var, show="•").pack(
        side="left", fill="x", expand=True, padx=8
    )
    ctk.CTkLabel(
        window,
        text="비워 두어도 됩니다. 키가 없으면 헷갈리는 일정을 '다른 일정'으로 처리합니다.",
        text_color="gray",
        wraplength=480,
    ).pack(pady=(0, 10))

    # ③ 계정
    status_var = ctk.StringVar(value=f"등록된 계정 {len(state.active_targets())}개")
    ctk.CTkLabel(window, textvariable=status_var).pack(pady=4)

    def add_account():
        try:
            auth_result = auth.add_target()
            client = GoogleCalendarClient(auth_result.credentials)
            calendar_id = client.ensure_calendar(config.calendar_name)
            state.upsert_target(auth_result.email, calendar_id, config.calendar_name)
            status_var.set(f"{auth_result.email} 추가됨 (총 {len(state.active_targets())}개)")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("계정 추가 실패", str(exc))

    ctk.CTkButton(window, text="③ 구글 계정 추가 (브라우저가 열립니다)", command=add_account).pack(pady=6)

    def finish():
        if not folder_var.get():
            messagebox.showwarning("폴더가 필요합니다", "감시할 폴더를 골라 주세요.")
            return
        if not state.active_targets():
            messagebox.showwarning("계정이 필요합니다", "구글 계정을 하나 이상 추가해 주세요.")
            return
        if key_var.get().strip():
            save_gemini_key(key_var.get())
        config.watch_dir = folder_var.get()
        config.save()
        result["config"] = config
        window.destroy()

    ctk.CTkButton(window, text="④ 완료", command=finish, height=36).pack(pady=(16, 8))
    window.mainloop()
    return result["config"]
