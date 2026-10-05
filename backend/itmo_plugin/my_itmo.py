"""my.itmo.ru: личное расписание и токен, который продлевается сам.

Ручка и модель — по itmo-mcp (MIT, research/services/schedule.md) и iburakov/my-itmo-ru-to-ical (MIT):
`GET https://my.itmo.ru/api/schedule/schedule/personal?date_start=YYYY-MM-DD&date_end=YYYY-MM-DD` с
`Authorization: Bearer <access token>`; ответ `{code, message, data: [день]}`, code 0 — успех.
"""

import secrets
import threading
import time
from datetime import date
from typing import Callable

import httpx

from .itmo_id import USER_AGENT, AuthError, ItmoIdClient, SessionExpired, TokenSet, authorize_url, claims, parse_callback, pkce_pair
from .store import Store

MY_ITMO = "https://my.itmo.ru"
PENDING_TTL = 15 * 60          # незавершённый вход действителен 15 минут (сам код ITMO.ID — около минуты)
MAX_DAYS = 62
CACHE_SEC = 300
# брони аудиторий (flow_type 5) и физкультура (flow_type 3, work_type 11) по умолчанию скрыты
EXTRA_FLOWS = {3, 5}
SPORT_WORK_TYPE = 11


class NotLoggedIn(AuthError):
    pass


class ApiError(Exception):
    pass


class MyItmo:
    def __init__(self, store: Store, ids: ItmoIdClient | None = None, http: httpx.Client | None = None,
                 on_expired: Callable[[], None] | None = None, now=time.time):
        self.store = store
        self.ids = ids or ItmoIdClient()
        self.http = http or httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT, "Accept-Language": "ru"})
        self.on_expired = on_expired or (lambda: None)
        self.now = now
        self._lock = threading.Lock()           # одно продление за раз: refresh token меняется при каждом
        self._cache: dict[tuple, tuple[float, list]] = {}

    # --- вход ---------------------------------------------------------------------------------------

    def start_login(self) -> str:
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(16)
        self.store.update(pending={"state": state, "verifier": verifier, "created": self.now()})
        return authorize_url(state, challenge)

    def finish_login(self, callback_url: str) -> dict:
        code, state = parse_callback(callback_url)
        pending = self.store.load().get("pending") or {}
        if pending.get("state") != state or self.now() - pending.get("created", 0) > PENDING_TTL:
            raise AuthError("Этот адрес от другого или устаревшего входа — нажмите «Открыть id.itmo.ru» и войдите ещё раз")
        try:
            tokens = self.ids.exchange(code, pending["verifier"])
        except SessionExpired as exc:
            raise AuthError(f"ITMO.ID не принял код ({exc.detail}). Код одноразовый и действует около минуты — "
                            "откройте id.itmo.ru и войдите ещё раз") from exc
        self.store.update(tokens=tokens.to_dict(), pending=None, expired_signal=None, user=self._user(tokens))
        self._cache.clear()
        return self.status()

    def logout(self) -> None:
        self.store.update(tokens=None, pending=None, user=None)
        self._cache.clear()

    def status(self) -> dict:
        state = self.store.load()
        tokens = state.get("tokens")
        return {"logged_in": bool(tokens), "user": state.get("user"),
                "refresh_expires_at": (tokens or {}).get("refresh_expires_at")}

    @staticmethod
    def _user(tokens: TokenSet) -> dict:
        info = {**claims(tokens.access_token), **claims(tokens.id_token)}
        name = info.get("name") or " ".join(filter(None, [info.get("family_name"), info.get("given_name")])) \
            or info.get("preferred_username")
        return {"name": name, "isu": info.get("isu"), "username": info.get("preferred_username")}

    def access_token(self, force: bool = False) -> str:
        with self._lock:
            data = self.store.load().get("tokens")
            if not data:
                raise NotLoggedIn("Вход в ITMO.ID не выполнен — откройте страницу «Расписание ИТМО» и войдите")
            tokens = TokenSet.from_dict(data)
            if not force and tokens.expires_at - 30 > self.now():
                return tokens.access_token
            try:
                tokens = self.ids.refresh(tokens.refresh_token)
            except SessionExpired:
                self.store.update(tokens=None)
                self.on_expired()
                raise
            self.store.update(tokens=tokens.to_dict(), user=self._user(tokens))
            return tokens.access_token

    # --- расписание ------------------------------------------------------------------------------------

    def schedule(self, start: date, end: date, include_extra: bool = False) -> list[dict]:
        if end < start:
            raise ApiError("Конец периода раньше начала")
        if (end - start).days + 1 > MAX_DAYS:
            raise ApiError(f"Не больше {MAX_DAYS} дней за раз")
        key = (start, end)
        cached = self._cache.get(key)
        if cached and self.now() - cached[0] < CACHE_SEC:
            days = cached[1]
        else:
            days = self._fetch(start, end)
            self._cache[key] = (self.now(), days)
        return [normalize_day(day, include_extra) for day in days]

    def _fetch(self, start: date, end: date) -> list:
        params = {"date_start": start.isoformat(), "date_end": end.isoformat()}
        for attempt in (0, 1):
            token = self.access_token(force=attempt == 1)
            try:
                response = self.http.get(f"{MY_ITMO}/api/schedule/schedule/personal", params=params,
                                         headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as exc:
                raise ApiError(f"my.itmo.ru недоступен: {exc.__class__.__name__}") from exc
            if response.status_code == 401 and attempt == 0:
                continue                        # токен отозван раньше срока — продлеваем и пробуем ещё раз
            if response.status_code >= 400:
                raise ApiError(f"my.itmo.ru ответил {response.status_code}")
            body = response.json()
            if body.get("code") != 0:
                raise ApiError(f"my.itmo.ru: {body.get('message') or body.get('code')}")
            return body.get("data") or []
        raise ApiError("my.itmo.ru не принял токен")


def is_extra(lesson: dict) -> bool:
    return lesson.get("flow_type_id") in EXTRA_FLOWS or lesson.get("work_type_id") == SPORT_WORK_TYPE


def normalize_day(day: dict, include_extra: bool) -> dict:
    lessons = [normalize_lesson(x) for x in day.get("lessons") or [] if include_extra or not is_extra(x)]
    return {"date": day.get("date"), "weekday": day.get("day_number"), "week": day.get("week_number"),
            "note": day.get("note"), "lessons": sorted(lessons, key=lambda x: x["start"] or "")}


def normalize_lesson(x: dict) -> dict:
    return {
        "start": x.get("time_start"), "end": x.get("time_end"),
        "subject": x.get("subject") or x.get("note") or "Занятие",
        "kind": (x.get("work_type") or x.get("type") or "").lower() or None, "kind_id": x.get("work_type_id"),
        "room": x.get("room"), "building": x.get("building"),
        "teacher": x.get("teacher_name"), "format": x.get("format"), "format_id": x.get("format_id"),
        "group": x.get("group"), "note": x.get("note") if x.get("subject") else None,
        "zoom_url": x.get("zoom_url"), "zoom_password": x.get("zoom_password"),
        "extra": is_extra(x),
    }
