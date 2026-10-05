"""ITMO.ID (Keycloak, realm `itmo`): вход через браузер владельца по коду с PKCE и продление токенов.

Протокол — по исследованию и реализации alllexey-dev/itmo-mcp (MIT, research/auth.md, src/auth/itmo-id.ts):
публичный клиент `student-personal-cabinet` принимает только адреса возврата на my.itmo.ru, поэтому владелец входит
в своём браузере, а адрес страницы после входа (с `code` и `state`) вставляет в плагин. Возврат — на
`https://my.itmo.ru/robots.txt`, а не на `/login/callback`: там сайт my.itmo.ru сам меняет код на токены, и код
(одноразовый) до плагина уже не доходит; robots.txt — простой текст без скриптов, код остаётся в адресе нетронутым.
Код живёт около минуты; access token — 30 минут, refresh token — 30 дней и меняется при каждом продлении.
"""

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

ISSUER = "https://id.itmo.ru/auth/realms/itmo"
CLIENT_ID = "student-personal-cabinet"
REDIRECT_URI = "https://my.itmo.ru/robots.txt"
RETURN_HOST = "my.itmo.ru"
SCOPE = "openid profile"
USER_AGENT = "pulse-itmo (+https://github.com/pulse-assist/pulse-itmo)"


class AuthError(Exception):
    """Вход не удался: текст — для владельца."""


class SessionExpired(AuthError):
    """ITMO.ID отверг код или refresh token (invalid_grant): нужно войти заново."""

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = detail


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str
    expires_at: float               # epoch, секунды
    refresh_expires_at: float | None
    id_token: str | None = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, data: dict) -> "TokenSet":
        return cls(**{k: data.get(k) for k in ("access_token", "refresh_token", "expires_at", "refresh_expires_at", "id_token")})


def pkce_pair() -> tuple[str, str]:
    """(verifier, challenge) для S256."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(state: str, challenge: str) -> str:
    params = {"response_type": "code", "client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI, "scope": SCOPE,
              "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}
    return f"{ISSUER}/protocol/openid-connect/auth?{urlencode(params)}"


def parse_callback(url: str) -> tuple[str, str]:
    """(code, state) из адреса страницы после входа: https://my.itmo.ru/robots.txt?state=…&code=…"""
    parts = urlsplit(url.strip())
    if parts.scheme != "https" or parts.netloc != RETURN_HOST:
        raise AuthError("Это не тот адрес: нужен адрес страницы my.itmo.ru после входа, он начинается с "
                        f"{REDIRECT_URI}?")
    query = {k: v[0] for k, v in parse_qs(parts.query + ("&" + parts.fragment if parts.fragment else "")).items()}
    if query.get("error"):
        raise AuthError(f"ITMO.ID вернул ошибку: {query.get('error_description') or query['error']}")
    if not query.get("code") or not query.get("state"):
        raise AuthError("В адресе нет кода входа — скопируйте адрес целиком, вместе с ?state=…&code=…")
    return query["code"], query["state"]


def claims(jwt: str | None) -> dict:
    """Поля токена без проверки подписи: токен получен прямо от ITMO.ID."""
    try:
        payload = (jwt or "").split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


class ItmoIdClient:
    def __init__(self, http: httpx.Client | None = None, now=time.time):
        self.http = http or httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT})
        self.now = now

    def exchange(self, code: str, verifier: str) -> TokenSet:
        return self._token({"grant_type": "authorization_code", "client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI,
                            "code": code, "code_verifier": verifier}, "код входа")

    def refresh(self, refresh_token: str) -> TokenSet:
        return self._token({"grant_type": "refresh_token", "client_id": CLIENT_ID, "refresh_token": refresh_token},
                           "сохранённый вход")

    def _token(self, form: dict, what: str) -> TokenSet:
        try:
            response = self.http.post(f"{ISSUER}/protocol/openid-connect/token", data=form)
        except httpx.HTTPError as exc:
            raise AuthError(f"ITMO.ID недоступен: {exc.__class__.__name__}") from exc
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 400 or not isinstance(body.get("access_token"), str):
            error = body.get("error") or f"HTTP {response.status_code}"
            detail = body.get("error_description") or error
            if error == "invalid_grant":
                raise SessionExpired(f"ITMO.ID отверг {what} ({detail}) — войдите заново", detail)
            raise AuthError(f"ITMO.ID не выдал токен ({detail})")
        now = self.now()
        refresh_in = body.get("refresh_expires_in")
        return TokenSet(access_token=body["access_token"], refresh_token=str(body.get("refresh_token") or ""),
                        expires_at=now + float(body.get("expires_in") or 300),
                        refresh_expires_at=now + float(refresh_in) if refresh_in else None,
                        id_token=body.get("id_token"))
