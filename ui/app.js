// Страница «Расписание ИТМО». Всё — через мост Пульса: pulse.plugin(...) к серверу плагина.
// Состояние в адресе (pulse.setState): week=ГГГГ-ММ-ДД (понедельник), all=1, day=ГГГГ-ММ-ДД (телефон).

const app = document.getElementById("app");
const MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"];
const WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
const KINDS = [["лекц", "k-lecture"], ["практ", "k-practice"], ["лаб", "k-lab"], ["экзам", "k-exam"], ["зач", "k-credit"],
               ["консул", "k-consult"], ["физ", "k-sport"], ["спорт", "k-sport"]];

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const parse = (s) => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); };
const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
const monday = (d) => addDays(d, -((d.getDay() + 6) % 7));
const kindClass = (kind) => (KINDS.find(([k]) => (kind || "").includes(k)) || [null, "k-consult"])[1];

const state = Object.fromEntries(new URLSearchParams(pulse.state));
let status = null;

function saveState() {
  pulse.setState(new URLSearchParams(Object.entries(state).filter(([, v]) => v)).toString());
}

async function load() {
  try {
    status = await pulse.plugin("/status");
  } catch (e) {
    app.innerHTML = `<div class="pl-error">Плагин не отвечает: ${esc(e.message)}</div>`;
    return;
  }
  if (!status.logged_in) renderLogin();
  else await renderWeek();
}

// --- вход ---------------------------------------------------------------------------------------------

function renderLogin(error) {
  app.innerHTML = `
    <div class="login">
      <h2>Войдите через ITMO.ID</h2>
      <p>Пароль вы вводите на id.itmo.ru — плагин его не видит. Плагин получает доступ к расписанию и дальше продлевает
         вход сам: заново входить понадобится, только если не пользоваться им больше 30 дней или выйти.</p>
      <div class="step active"><span class="step-n">1</span><div class="step-body">
        <div class="step-title">Откройте ITMO.ID и войдите</div>
        <div><button class="btn btn-primary" id="open">Открыть id.itmo.ru ↗</button></div>
        <div class="hint" id="open-hint"></div>
      </div></div>
      <div class="step"><span class="step-n">2</span><div class="step-body">
        <div class="step-title">Скопируйте адрес страницы после входа</div>
        <div>Браузер откроет my.itmo.ru/robots.txt — страницу с парой строк служебного текста, так и задумано.
             Скопируйте адрес из адресной строки: он начинается с <span class="mono">https://my.itmo.ru/robots.txt?…</span></div>
      </div></div>
      <div class="step"><span class="step-n">3</span><div class="step-body">
        <div class="step-title">Вставьте его сюда — в течение минуты</div>
        <form class="paste" id="paste"><input id="url" placeholder="https://my.itmo.ru/robots.txt?state=…&code=…" autocomplete="off">
          <button class="btn" type="submit">Войти</button></form>
        <div class="hint">Код из адреса действует около минуты; не успели — нажмите «Открыть id.itmo.ru» ещё раз.</div>
        ${error ? `<div class="error">${esc(error)}</div>` : ""}
      </div></div>
    </div>`;
  document.getElementById("open").addEventListener("click", openItmoId);
  document.getElementById("paste").addEventListener("submit", async (e) => {
    e.preventDefault();
    const url = document.getElementById("url").value.trim();
    if (!url) return;
    try {
      await pulse.plugin("/login/finish", { method: "POST", body: { url } });
      await load();
    } catch (err) {
      renderLogin(err.message);
    }
  });
}

async function openItmoId() {
  // окно открываем сразу (по клику), адрес подставляем, когда плагин подготовит вход
  const win = window.open("", "_blank");
  const hint = document.getElementById("open-hint");
  try {
    const { url } = await pulse.plugin("/login/start", { method: "POST" });
    if (win) win.location.href = url;
    else hint.innerHTML = `Браузер не открыл окно — <a href="${esc(url)}" target="_blank" rel="noopener">откройте ссылку</a>.`;
  } catch (e) {
    if (win) win.close();
    hint.textContent = `Не удалось начать вход: ${e.message}`;
  }
}

// --- неделя ---------------------------------------------------------------------------------------------

async function renderWeek() {
  const today = parse(status.today);
  const start = state.week ? monday(parse(state.week)) : monday(today);
  const end = addDays(start, 6);
  const all = state.all === "1" || (state.all === undefined && status.show_extra);
  app.innerHTML = `${statusLine()}<div class="pl-empty">Загружаю расписание…</div>`;
  let data;
  try {
    data = await pulse.plugin("/schedule", { params: { date_start: iso(start), date_end: iso(end), all: all ? "1" : "0" } });
  } catch (e) {
    if (e.status === 401) { status = await pulse.plugin("/status"); return renderLogin(e.message); }
    app.innerHTML = `${statusLine()}<div class="pl-error">Не удалось загрузить расписание: ${esc(e.message)}</div>`;
    bindStatus();
    return;
  }
  const days = data.days.filter((d) => parse(d.date).getDay() !== 0 || d.lessons.length);   // воскресенье — только если есть пары
  const weekNo = data.days.find((d) => d.week)?.week;
  const picked = state.day || (days.some((d) => d.date === status.today) ? status.today : days[0]?.date);
  const rangeText = start.getMonth() === end.getMonth()
    ? `${start.getDate()}–${end.getDate()} ${MONTHS[end.getMonth()]}`
    : `${start.getDate()} ${MONTHS[start.getMonth()]} – ${end.getDate()} ${MONTHS[end.getMonth()]}`;
  app.innerHTML = `
    ${statusLine()}
    <div class="toolbar">
      <button class="btn btn-icon" id="prev" aria-label="Предыдущая неделя">‹</button>
      <span class="range">${rangeText}${weekNo ? ` · неделя ${weekNo}` : ""}</span>
      <button class="btn btn-icon" id="next" aria-label="Следующая неделя">›</button>
      <button class="btn" id="today">Сегодня</button>
      <span class="grow"></span>
      <label><input type="checkbox" id="all" ${all ? "checked" : ""}>брони и физкультура</label>
    </div>
    <div class="days-strip">${days.map((d) => {
      const dt = parse(d.date);
      return `<button data-day="${d.date}" class="${d.date === picked ? "on" : ""}">${WD[(dt.getDay() + 6) % 7]} ${dt.getDate()}</button>`;
    }).join("")}</div>
    <div class="week" style="--days: ${days.length}">${days.map((d) => dayColumn(d, d.date === status.today, d.date === picked)).join("")}</div>`;
  bindStatus();
  const go = (week) => { state.week = iso(week); delete state.day; saveState(); renderWeek(); };
  document.getElementById("prev").onclick = () => go(addDays(start, -7));
  document.getElementById("next").onclick = () => go(addDays(start, 7));
  document.getElementById("today").onclick = () => { delete state.week; state.day = status.today; saveState(); renderWeek(); };
  document.getElementById("all").onchange = (e) => { state.all = e.target.checked ? "1" : "0"; saveState(); renderWeek(); };
  document.querySelectorAll(".days-strip button").forEach((b) => b.addEventListener("click", () => {
    state.day = b.dataset.day; saveState();
    document.querySelectorAll(".days-strip button").forEach((x) => x.classList.toggle("on", x === b));
    document.querySelectorAll(".week .day").forEach((x) => x.classList.toggle("picked", x.dataset.day === b.dataset.day));
  }));
}

function dayColumn(day, isToday, picked) {
  const dt = parse(day.date);
  const title = `${WD[(dt.getDay() + 6) % 7]} ${dt.getDate()} ${MONTHS[dt.getMonth()].slice(0, 3)}${isToday ? " · сегодня" : ""}`;
  const lessons = day.lessons.map(lessonCard).join("") || `<div class="day-empty">пар нет</div>`;
  return `<div class="day ${picked ? "picked" : ""}" data-day="${day.date}"><div class="day-head ${isToday ? "today" : ""}">${esc(title)}</div>${lessons}</div>`;
}

function lessonCard(x) {
  const place = [x.building, x.room].filter(Boolean).join(" · ") || (x.format_id === 3 ? "онлайн" : "");
  const meta = [place, x.teacher, x.group].filter(Boolean).map(esc).join(" · ");
  const zoom = x.zoom_url ? ` · <a href="${esc(x.zoom_url)}" target="_blank" rel="noopener">Zoom ↗</a>${x.zoom_password ? ` (пароль ${esc(x.zoom_password)})` : ""}` : "";
  return `<div class="lesson ${kindClass(x.kind)}">
    <div class="lesson-time"><b>${esc(x.start)}–${esc(x.end)}</b>${x.kind ? `<span class="kind">${esc(x.kind)}</span>` : ""}${x.format_id === 3 ? `<span class="kind">дистанционно</span>` : ""}</div>
    <div class="lesson-subject">${esc(x.subject)}</div>
    <div class="lesson-meta">${meta}${zoom}${x.note ? `<br>${esc(x.note)}` : ""}</div>
  </div>`;
}

function statusLine() {
  const user = status.user || {};
  const who = [user.name, user.isu ? `ИСУ ${user.isu}` : ""].filter(Boolean).join(" · ");
  return `<div class="status">Вход: ${esc(who || "выполнен")}
    <button class="btn-link" id="refresh">Обновить</button><button class="btn-link" id="logout">Выйти</button></div>`;
}

function bindStatus() {
  const refresh = document.getElementById("refresh");
  if (refresh) refresh.onclick = () => renderWeek();
  const logout = document.getElementById("logout");
  // confirm() во фрейме плагина недоступен (песочница) — подтверждение вторым нажатием
  if (logout) logout.onclick = async () => {
    if (!logout.dataset.armed) {
      logout.dataset.armed = "1";
      logout.textContent = "Точно выйти? Расписание перестанет обновляться";
      setTimeout(() => { delete logout.dataset.armed; logout.textContent = "Выйти"; }, 5000);
      return;
    }
    status = await pulse.plugin("/logout", { method: "POST" });
    renderLogin();
  };
}

load();
