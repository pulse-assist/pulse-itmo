"""Плагин «my.itmo»: вход через ITMO.ID в браузере владельца и личное расписание.

Для страницы (pulse.plugin): GET /status, POST /login/start, POST /login/finish {url}, POST /logout,
GET /schedule?date_start&date_end&all. Для агентов: pulse itmo schedule|today|status.
"""

from datetime import date, datetime, timedelta, timezone

from fastapi import Body, HTTPException, Query
from pulse_plugin import CommandError, Plugin

from .itmo_id import AuthError, SessionExpired
from .my_itmo import ApiError, MyItmo, NotLoggedIn
from .store import Store

MOSCOW = timezone(timedelta(hours=3), "MSK")     # без перехода на летнее время с 2014 г.; tzdata не нужна
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

plugin = Plugin()
app = plugin.app


def expired_signal() -> None:
    """Продлить вход не удалось — один сигнал владельцу, пока он не войдёт снова."""
    if itmo.store.load().get("expired_signal"):
        return
    itmo.store.update(expired_signal=True)
    try:
        plugin.signal("Войдите в ITMO.ID снова",
                      body="Вход в my.itmo истёк — расписание не обновляется. Откройте [Расписание ИТМО](/p/itmo/schedule) "
                           "и войдите через ITMO.ID.", priority=75, tags=["итмо"])
    except Exception:  # noqa: BLE001 — сигнал не обязателен: страница всё равно покажет вход
        pass


itmo = MyItmo(Store(plugin.data_dir), on_expired=expired_signal)


def today() -> date:
    return datetime.now(MOSCOW).date()


def parse_date(value: str | None, default: date) -> date:
    if not value:
        return default
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CommandError(f"дата — в формате ГГГГ-ММ-ДД, а не «{value}»") from exc


def include_extra(flag) -> bool:
    if flag in (True, "1", "true", "yes", "да"):
        return True
    return bool(plugin.setting("show_extra", False))


def http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (NotLoggedIn, SessionExpired)):        # страница покажет вход
        return HTTPException(401, str(exc))
    return HTTPException(400 if isinstance(exc, AuthError) else 502, str(exc))


# --- страница -------------------------------------------------------------------------------------------

@app.get("/status")
def status() -> dict:
    return {**itmo.status(), "show_extra": bool(plugin.setting("show_extra", False)), "today": today().isoformat()}


@app.post("/login/start")
def login_start() -> dict:
    return {"url": itmo.start_login()}


@app.post("/login/finish")
def login_finish(url: str = Body(..., embed=True)) -> dict:
    try:
        return itmo.finish_login(url)
    except AuthError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/logout")
def logout() -> dict:
    itmo.logout()
    return itmo.status()


@app.get("/schedule")
def schedule(date_start: str | None = Query(None), date_end: str | None = Query(None), all: str | None = Query(None)) -> dict:
    try:
        start = parse_date(date_start, today())
        end = parse_date(date_end, start + timedelta(days=6))
        days = itmo.schedule(start, end, include_extra(all))
    except CommandError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (AuthError, ApiError) as exc:
        raise http_error(exc) from exc
    return {"days": days, "user": itmo.status()["user"], "today": today().isoformat()}


# --- команды агентов --------------------------------------------------------------------------------------

def _lessons_for(start: date, end: date, extra) -> list[dict]:
    try:
        days = itmo.schedule(start, end, include_extra(extra))
    except (AuthError, ApiError) as exc:
        raise CommandError(str(exc)) from exc
    out = []
    for day in days:
        if not day["lessons"]:
            continue
        d = date.fromisoformat(day["date"])
        out.append({"date": day["date"], "weekday": WEEKDAYS[d.weekday()], "week": day["week"],
                    "lessons": [{k: v for k, v in lesson.items() if v not in (None, "") and k not in ("zoom_password", "extra")}
                                for lesson in day["lessons"]]})
    return out


@plugin.command("schedule", "Расписание за период")
def schedule_command(args: dict) -> dict:
    start = parse_date(args.get("from"), today())
    end = parse_date(args.get("to"), start + timedelta(days=6))
    return {"from": start.isoformat(), "to": end.isoformat(), "days": _lessons_for(start, end, args.get("all"))}


@plugin.command("today", "Пары на сегодня")
def today_command(args: dict) -> dict:
    day = today()
    days = _lessons_for(day, day, args.get("all"))
    return {"date": day.isoformat(), "weekday": WEEKDAYS[day.weekday()], "lessons": days[0]["lessons"] if days else []}


@plugin.command("status", "Выполнен ли вход в ITMO.ID")
def status_command(args: dict) -> dict:
    return itmo.status()
