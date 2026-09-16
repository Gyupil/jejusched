"""OAuth 흐름과 토큰 보관 — 설계서 §5.

데스크톱 클라이언트 + 루프백(`http://127.0.0.1:<임의포트>`)을 쓴다. 동의 화면은
External + 프로덕션 게시(미검증)이므로 "확인되지 않은 앱" 경고에서 고급→이동을
한 번 거치지만 7일 만료는 없다.

토큰은 평문으로 두지 않는다. 윈도우는 DPAPI(현재 사용자만 복호화 가능), 맥은
keychain에 넣는다. 둘 다 안 되면 거부하지 않고 평문으로 저장하되 경고를 남긴다
(그래야 CI·테스트 환경에서도 동작한다).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..config import APP_NAME, AppConfig, app_home, load_oauth_client

log = logging.getLogger(__name__)

TOKEN_DIR_NAME = "tokens"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"


class TokenStore(Protocol):
    def save(self, email: str, payload: str) -> None: ...
    def load(self, email: str) -> str | None: ...
    def delete(self, email: str) -> None: ...


def _token_path(email: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "._-@" else "_" for ch in email)
    return app_home() / TOKEN_DIR_NAME / f"{safe}.json"


class DpapiTokenStore:
    """윈도우 전용. 현재 사용자 계정으로만 복호화된다."""

    def save(self, email: str, payload: str) -> None:
        import win32crypt  # type: ignore[import-not-found]

        blob = win32crypt.CryptProtectData(payload.encode("utf-8"), APP_NAME, None, None, None, 0)
        path = _token_path(email)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)

    def load(self, email: str) -> str | None:
        import win32crypt  # type: ignore[import-not-found]

        path = _token_path(email)
        if not path.is_file():
            return None
        _, data = win32crypt.CryptUnprotectData(path.read_bytes(), None, None, None, 0)
        return data.decode("utf-8")

    def delete(self, email: str) -> None:
        _token_path(email).unlink(missing_ok=True)


class KeyringTokenStore:
    """맥·리눅스. OS 자격 증명 저장소에 넣는다."""

    def save(self, email: str, payload: str) -> None:
        import keyring

        keyring.set_password(APP_NAME, email, payload)

    def load(self, email: str) -> str | None:
        import keyring

        return keyring.get_password(APP_NAME, email)

    def delete(self, email: str) -> None:
        import keyring

        try:
            keyring.delete_password(APP_NAME, email)
        except Exception:  # noqa: BLE001 — 없으면 그만이다
            pass


class PlaintextTokenStore:
    """마지막 수단. 암호화 저장소가 없을 때만 쓰이고 경고를 남긴다."""

    def save(self, email: str, payload: str) -> None:
        path = _token_path(email)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        log.warning("토큰을 평문으로 저장했다: %s — 암호화 저장소를 쓸 수 없다", path)

    def load(self, email: str) -> str | None:
        path = _token_path(email)
        return path.read_text(encoding="utf-8") if path.is_file() else None

    def delete(self, email: str) -> None:
        _token_path(email).unlink(missing_ok=True)


def default_token_store() -> TokenStore:
    if sys.platform == "win32":
        try:
            import win32crypt  # noqa: F401

            return DpapiTokenStore()
        except ImportError:
            log.warning("pywin32가 없다 — DPAPI를 쓸 수 없다")
    else:
        try:
            import keyring

            keyring.get_keyring()
            return KeyringTokenStore()
        except Exception:  # noqa: BLE001
            log.warning("keyring을 쓸 수 없다")
    return PlaintextTokenStore()


@dataclass
class AuthResult:
    email: str
    credentials: Any


class Authenticator:
    def __init__(self, config: AppConfig, store: TokenStore | None = None):
        self.config = config
        self.store = store or default_token_store()

    # ------------------------------------------------------------ 로그인

    def add_target(self) -> AuthResult:
        """브라우저를 열어 계정을 추가한다. 이메일과 자격증명을 돌려준다."""
        from google_auth_oauthlib.flow import InstalledAppFlow

        #  구글이 돌려주는 스코프 집합은 요청과 정확히 일치하지 않을 때가 있다
        #  (openid가 끼거나, 그 계정이 예전에 더 넓게 승인했을 때). oauthlib의
        #  엄격 검사에 걸려 로그인이 실패하는 것을 막는다.
        os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

        flow = InstalledAppFlow.from_client_config(load_oauth_client(), scopes=self.config.scopes)
        #  port=0 → 임의 포트. 클라이언트의 redirect_uri가 http://localhost여도 동작한다.
        credentials = flow.run_local_server(
            port=0,
            prompt="consent",  # refresh_token을 확실히 받는다
            access_type="offline",
            authorization_prompt_message="브라우저에서 구글 계정으로 로그인하세요...",
            success_message="인증이 끝났습니다. 이 창을 닫아도 됩니다.",
        )
        email = self.fetch_email(credentials)
        self.save(email, credentials)
        return AuthResult(email=email, credentials=credentials)

    def fetch_email(self, credentials: Any) -> str:
        import google.auth.transport.requests as gtr

        session = gtr.AuthorizedSession(credentials)
        response = session.get(USERINFO_URL, timeout=30)
        response.raise_for_status()
        email = response.json().get("email")
        if not email:
            raise RuntimeError("userinfo에서 이메일을 얻지 못했다")
        return str(email)

    # ------------------------------------------------------- 토큰 보관

    def save(self, email: str, credentials: Any) -> None:
        self.store.save(email, credentials.to_json())

    def load(self, email: str) -> Any | None:
        """저장된 자격증명을 되살린다. 만료면 조용히 갱신한다."""
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        payload = self.store.load(email)
        if not payload:
            return None
        credentials = Credentials.from_authorized_user_info(
            json.loads(payload), scopes=self.config.scopes
        )
        if credentials.valid:
            return credentials
        if credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
            except RefreshError as exc:
                log.error("%s: 토큰 갱신 실패 — 재로그인이 필요하다 (%s)", email, exc)
                return None
            self.save(email, credentials)
            return credentials
        return None

    def revoke(self, email: str) -> None:
        """토큰을 폐기하고 로컬에서 지운다."""
        import requests

        payload = self.store.load(email)
        if payload:
            token = json.loads(payload).get("refresh_token") or json.loads(payload).get("token")
            if token:
                try:
                    requests.post(
                        REVOKE_URL,
                        params={"token": token},
                        headers={"content-type": "application/x-www-form-urlencoded"},
                        timeout=30,
                    )
                except Exception as exc:  # noqa: BLE001 — 폐기 실패해도 로컬은 지운다
                    log.warning("%s: 토큰 폐기 요청 실패 (%s)", email, exc)
        self.store.delete(email)
