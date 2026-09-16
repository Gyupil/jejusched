"""설정 로딩 — 설계서 §5, §6.

비밀값(OAuth 클라이언트 JSON, Gemini 키)은 저장소에 커밋하지 않는다.
찾는 순서는 개발(맥)과 빌드(exe)에서 동일한 함수를 쓴다.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

APP_NAME = "JejuSched"
DEFAULT_CALENDAR_NAME = "주요일정(자동)"

#  구분별 기본 색상 (Google Calendar colorId) — 설계서 §2.3
DEFAULT_COLOR_MAP = {
    "도지사": "11",  # 토마토
    "행정부지사": "6",  # 귤
    "기후경제부지사": "2",  # 세이지
    "실 일정": "9",  # 블루베리
}
DEFAULT_TRANSPARENT_SECTIONS = ["도지사", "행정부지사", "기후경제부지사"]

SCOPES_APP_CREATED = [
    "https://www.googleapis.com/auth/calendar.app.created",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]
SCOPES_FULL = [
    "https://www.googleapis.com/auth/calendar",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]


class ConfigError(RuntimeError):
    """설정이 없거나 깨져서 진행할 수 없을 때."""


# ------------------------------------------------------------------ 경로


def app_home() -> Path:
    """설정·토큰·DB·로그가 사는 곳.

    윈도우 `%APPDATA%\\JejuSched`, 맥 `~/Library/Application Support/JejuSched`.
    테스트는 `JEJUSCHED_HOME`으로 격리한다.
    """
    override = os.environ.get("JEJUSCHED_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_NAME


def project_root() -> Path:
    """개발 중 저장소 루트. 빌드된 exe에서는 의미가 없다."""
    return Path(__file__).resolve().parents[1]


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_dirs() -> list[Path]:
    """번들된 자원을 찾을 후보 경로들.

    onefile은 `sys._MEIPASS`(임시 해제 폴더), onedir는 exe 옆과 `_internal`에
    자원이 놓인다. 어느 쪽으로 빌드해도 찾을 수 있게 전부 살핀다.
    """
    dirs: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(Path(meipass))
    if is_frozen():
        exe_dir = Path(sys.executable).parent
        dirs += [exe_dir, exe_dir / "_internal"]
    dirs.append(project_root())
    #  순서를 지키며 중복만 없앤다
    seen: set[Path] = set()
    return [d for d in dirs if not (d in seen or seen.add(d))]


# --------------------------------------------------------------- 비밀값


def load_oauth_client() -> dict[str, Any]:
    """OAuth 데스크톱 클라이언트 JSON을 찾는다.

    1. 환경변수 `GOOGLE_OAUTH_CLIENT_JSON` (JSON 문자열) — GitHub Actions Secret
    2. 저장소 루트의 `client_secret_*.json` — 로컬 개발
    3. 번들 안의 `client_secret.json` — 배포된 exe

    반환값은 `{"installed": {...}}` 형태 그대로다(InstalledAppFlow가 받는 모양).
    """
    raw = os.environ.get("GOOGLE_OAUTH_CLIENT_JSON")
    if raw:
        try:
            return _validate_client(json.loads(raw), "GOOGLE_OAUTH_CLIENT_JSON")
        except json.JSONDecodeError as exc:
            raise ConfigError("GOOGLE_OAUTH_CLIENT_JSON이 올바른 JSON이 아니다") from exc

    candidates = sorted(project_root().glob("client_secret_*.json"))
    candidates += [d / "client_secret.json" for d in bundle_dirs()]
    for path in candidates:
        if path.is_file():
            try:
                return _validate_client(json.loads(path.read_text(encoding="utf-8")), str(path))
            except json.JSONDecodeError as exc:
                raise ConfigError(f"OAuth 클라이언트 JSON이 깨졌다: {path}") from exc

    raise ConfigError(
        "OAuth 클라이언트 JSON을 찾지 못했다. 환경변수 GOOGLE_OAUTH_CLIENT_JSON을 넣거나 "
        "저장소 루트에 client_secret_*.json을 두어라."
    )


def _validate_client(data: dict[str, Any], origin: str) -> dict[str, Any]:
    for kind in ("installed", "web"):
        block = data.get(kind)
        if isinstance(block, dict) and block.get("client_id") and block.get("client_secret"):
            if kind == "web":
                # 데스크톱 클라이언트가 아니면 루프백 흐름이 막힌다 — 경고 대신 그대로 진행
                return {"installed": block}
            return data
    raise ConfigError(f"OAuth 클라이언트 JSON에 installed.client_id/secret이 없다: {origin}")


def load_env_file(path: Path) -> dict[str, str]:
    """`KEY=VALUE` 한 줄씩인 단순 .env 파서(따옴표·주석·export 허용)."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_gemini_api_key() -> str | None:
    """Gemini 키를 찾는다. **없어도 예외를 던지지 않는다** — llm.enabled=false가 될 뿐.

    환경변수 → 저장소 루트 `local.env` → 앱 홈 `local.env` 순.
    배포판에서는 사용자가 설정 창에 입력한 값이 암호화 저장소에 들어간다(M6).
    """
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key.strip() or None
    for candidate in (project_root() / "local.env", app_home() / "local.env"):
        value = load_env_file(candidate).get("GEMINI_API_KEY", "").strip()
        if value:
            return value
    return None


# ------------------------------------------------------------ 런타임 설정


@dataclass
class LlmSettings:
    enabled: bool = True
    models: list[str] = field(default_factory=lambda: ["gemini-3.8-flash", "gemini-3.5-flash-lite"])
    daily_budget: int = 15  # 무료 한도 20 RPD를 보호하는 안전 마진
    send_attendees: bool = True
    send_location: bool = True
    retry_minutes: int = 30
    timeout_seconds: int = 60


@dataclass
class AppConfig:
    watch_dir: str = ""
    file_glob: str = "*.hwpx"
    settle_seconds: float = 3.0
    default_duration_min: int = 60
    calendar_name: str = DEFAULT_CALENDAR_NAME
    use_full_calendar_scope: bool = False
    mark_prefix: str = "* "
    mark_change_color: bool = True
    mark_color: str = "8"  # 그래파이트
    color_map: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_COLOR_MAP))
    transparent_sections: list[str] = field(
        default_factory=lambda: list(DEFAULT_TRANSPARENT_SECTIONS)
    )
    reminders: list[int] = field(default_factory=list)  # 빈 목록 = 알림 없음
    llm: LlmSettings = field(default_factory=LlmSettings)
    dry_run: bool = False
    autostart: bool = False

    @property
    def scopes(self) -> list[str]:
        return SCOPES_FULL if self.use_full_calendar_scope else SCOPES_APP_CREATED

    # ---- 직렬화

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppConfig:
        data = dict(data or {})
        llm = LlmSettings(**{k: v for k, v in (data.pop("llm", {}) or {}).items()
                             if k in LlmSettings.__dataclass_fields__})
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__ and k != "llm"}
        return cls(**known, llm=llm)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def load(cls, path: Path | None = None) -> AppConfig:
        path = path or (app_home() / "config.json")
        if not path.is_file():
            return cls()
        try:
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError) as exc:
            raise ConfigError(f"config.json이 깨졌다: {path}") from exc

    def save(self, path: Path | None = None) -> Path:
        path = path or (app_home() / "config.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        return path

    def with_llm_disabled(self) -> AppConfig:
        return replace(self, llm=replace(self.llm, enabled=False))
