"""Плагин без сети: ITMO.ID и my.itmo.ru подменены (httpx.MockTransport). Запуск из backend: python -m pytest"""

import json
import os
import tempfile
from datetime import date
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault("PULSE_PLUGIN_DATA", tempfile.mkdtemp(prefix="itmo-test-"))

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from itmo_plugin import app as module  # noqa: E402
from itmo_plugin.itmo_id import AuthError, ItmoIdClient, authorize_url, claims, parse_callback, pkce_pair  # noqa: E402
from itmo_plugin.my_itmo import MyItmo  # noqa: E402
from itmo_plugin.store import Store  # noqa: E402


def jwt(payload: dict) -> str:
    import base64
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"h.{body}.s"


DAY = {"date": "2026-10-07", "day_number": 3, "week_number": 6, "lessons": [
    {"pair_id": 2, "time_start": "13:30", "time_end": "15:00", "subject": "Английский язык", "work_type": "Практические занятия",
     "work_type_id": 3, "format_id": 3, "zoom_url": "https://zoom/x", "teacher_name": "Smith J.", "flow_type_id": 2},
    {"pair_id": 1, "time_start": "11:40", "time_end": "13:10", "subject": "Программирование", "work_type": "Лабораторные занятия",
     "work_type_id": 2, "room": "304", "building": "Кронверкский 49", "teacher_name": "Сидоров П. А.", "flow_type_id": 2},
    {"pair_id": 3, "time_start": "15:20", "time_end": "16:50", "subject": "Физическая культура", "work_type_id": 11, "flow_type_id": 3},
]}


class Fake:
    """ITMO.ID и my.itmo.ru: выдают токены, меняют refresh token, отдают расписание."""

    def __init__(self):
        self.refresh_ok = True
        self.issued = 0
        self.schedule_calls = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            form = parse_qs(request.content.decode())
            grant = form["grant_type"][0]
            if grant == "authorization_code" and form["code"][0] != "good-code":
                return httpx.Response(400, json={"error": "invalid_grant"})
            if grant == "refresh_token" and not self.refresh_ok:
                return httpx.Response(400, json={"error": "invalid_grant"})
            self.issued += 1
            return httpx.Response(200, json={
                "access_token": jwt({"isu": 123456, "preferred_username": "dybov"}), "expires_in": 1800,
                "refresh_token": f"r{self.issued}", "refresh_expires_in": 2592000,
                "id_token": jwt({"name": "Дыбов Артём", "isu": 123456})})
        if request.url.path == "/api/schedule/schedule/personal":
            self.schedule_calls.append(dict(request.url.params))
            return httpx.Response(200, json={"code": 0, "message": "OK", "data": [DAY]})
        return httpx.Response(404)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    fake = Fake()
    transport = httpx.MockTransport(fake.handler)
    clock = {"t": 1_000_000.0}
    signals = []
    itmo = MyItmo(Store(tmp_path), ItmoIdClient(httpx.Client(transport=transport), now=lambda: clock["t"]),
                  httpx.Client(transport=transport), on_expired=module.expired_signal, now=lambda: clock["t"])
    monkeypatch.setattr(module, "itmo", itmo)
    monkeypatch.setattr(module.plugin, "signal", lambda title, **kw: signals.append(title))
    monkeypatch.setattr(module, "today", lambda: date(2026, 10, 7))
    return TestClient(module.app), itmo, fake, clock, signals


def login(http, itmo) -> dict:
    url = http.post("/login/start").json()["url"]
    state = parse_qs(urlsplit(url).query)["state"][0]
    return http.post("/login/finish", json={"url": f"https://my.itmo.ru/login/callback?state={state}&session_state=x&code=good-code"})


def test_authorize_url_and_callback():
    verifier, challenge = pkce_pair()
    query = parse_qs(urlsplit(authorize_url("st", challenge)).query)
    assert query["client_id"] == ["student-personal-cabinet"] and query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == ["https://my.itmo.ru/login/callback"] and len(verifier) > 43
    assert parse_callback(" https://my.itmo.ru/login/callback?state=st&code=c1 ") == ("c1", "st")
    for bad, needle in [("https://example.com/?code=1&state=2", "не тот адрес"),
                        ("https://my.itmo.ru/login/callback?state=st", "нет кода"),
                        ("https://my.itmo.ru/login/callback?error=access_denied", "ошибку")]:
        with pytest.raises(AuthError, match=needle):
            parse_callback(bad)
    assert claims(jwt({"isu": 1})) == {"isu": 1} and claims("мусор") == {}


def test_login_schedule_and_refresh(setup):
    http, itmo, fake, clock, signals = setup
    assert http.get("/status").json()["logged_in"] is False
    assert http.get("/schedule").status_code == 401
    response = login(http, itmo)
    assert response.status_code == 200, response.text
    assert response.json()["user"] == {"name": "Дыбов Артём", "isu": 123456, "username": "dybov"}

    days = http.get("/schedule", params={"date_start": "2026-10-05", "date_end": "2026-10-11"}).json()["days"]
    lessons = days[0]["lessons"]
    assert [x["subject"] for x in lessons] == ["Программирование", "Английский язык"]      # по времени, без физкультуры
    assert lessons[0]["kind"] == "лабораторные занятия" and lessons[1]["zoom_url"] == "https://zoom/x"
    assert fake.schedule_calls[-1] == {"date_start": "2026-10-05", "date_end": "2026-10-11"}
    assert len(http.get("/schedule", params={"date_start": "2026-10-05", "date_end": "2026-10-11", "all": "1"})
               .json()["days"][0]["lessons"]) == 3

    # токен истёк — плагин продлевает сам, новый refresh token сохраняется
    clock["t"] += 3600
    itmo._cache.clear()
    assert http.get("/schedule").status_code == 200
    assert itmo.store.load()["tokens"]["refresh_token"] == "r2"

    # продлить не удалось — вход сброшен, один сигнал владельцу
    fake.refresh_ok = False
    clock["t"] += 3600
    itmo._cache.clear()
    assert http.get("/schedule").status_code == 401
    assert http.get("/schedule").status_code == 401
    assert signals == ["Войдите в ITMO.ID снова"] and http.get("/status").json()["logged_in"] is False


def test_login_errors(setup):
    http, itmo, *_ = setup
    http.post("/login/start")
    stale = http.post("/login/finish", json={"url": "https://my.itmo.ru/login/callback?state=другой&code=good-code"})
    assert stale.status_code == 400 and "устаревшего входа" in stale.json()["detail"]
    url = http.post("/login/start").json()["url"]
    state = parse_qs(urlsplit(url).query)["state"][0]
    expired = http.post("/login/finish", json={"url": f"https://my.itmo.ru/login/callback?state={state}&code=old"})
    assert expired.status_code == 400 and "около минуты" in expired.json()["detail"]


def test_commands(setup):
    http, itmo, *_ = setup
    assert http.post("/commands/today", json={}).status_code == 400            # без входа — понятная ошибка
    login(http, itmo)
    today = http.post("/commands/today", json={}).json()
    assert today["date"] == "2026-10-07" and today["weekday"] == "среда" and len(today["lessons"]) == 2
    assert "zoom_password" not in today["lessons"][1]
    week = http.post("/commands/schedule", json={"from": "2026-10-05", "to": "2026-10-11"}).json()
    assert week["days"][0]["date"] == "2026-10-07"
    assert http.post("/commands/schedule", json={"from": "вчера"}).status_code == 400
    assert http.post("/commands/status", json={}).json()["logged_in"] is True
    assert http.post("/logout").json()["logged_in"] is False
