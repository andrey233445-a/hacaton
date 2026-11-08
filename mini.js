/* ===== Школьный штаб — мини веб-приложение ===== */
"use strict";

const $view = document.getElementById("view");
const $app = document.getElementById("app");
const $authError = document.getElementById("auth-error");
const $loader = document.getElementById("loader");
const $toast = document.getElementById("toast");
const $modalRoot = document.getElementById("modal-root");
const $fab = document.getElementById("fab");
const $headerSub = document.getElementById("header-sub");
const $pointsValue = document.getElementById("points-value");
const $pointsChip = document.getElementById("points-chip");
const $tabbar = document.getElementById("tabbar");

const DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
const MOODS = ["😖", "😕", "😐", "🙂", "😄"];
const THEMES = [["dark", "💜 Фиолетовая"], ["light", "☀️ Светлая"], ["sunset", "🌇 Закат"]];

function applyTheme(name) {
  document.documentElement.setAttribute("data-theme", name);
  localStorage.setItem("mini_theme", name);
}
applyTheme(localStorage.getItem("mini_theme") || "dark");

// День недели нумеруется как в боте: 1 = понедельник … 7 = воскресенье.
const state = {
  me: null,
  features: [],
  tabs: [],
  view: "home",
  chat: [],
  day: new Date().getDay() || 7,
  scheduleClass: null,
};

// Состав вкладок под роль: список фильтруется по features из /mini/api/me,
// поэтому у родителя нет «Домашки» и «Антистресса», а у психолога — только
// сигналы и сводка. Порядок здесь = порядок кнопок в нижней панели.
const TABS = [
  { view: "home", icon: "🏠", label: "Главная", feature: "home" },
  { view: "homework", icon: "📜", label: "Домашка", feature: "homework" },
  { view: "children", icon: "🧒", label: "Дети", feature: "children" },
  { view: "signals", icon: "🚨", label: "Сигналы", feature: "signals" },
  { view: "psych_stats", icon: "📊", label: "Сводка", feature: "psych_stats" },
  { view: "deadlines", icon: "⏰", label: "Дедлайны", feature: "deadlines" },
  { view: "stress", icon: "🧘", label: "Антистресс", feature: "stress" },
  { view: "schedule", icon: "📚", label: "Расписание", feature: "schedule" },
  { view: "stats", icon: "📈", label: "Статистика", feature: "stats" },
  { view: "more", icon: "☰", label: "Ещё", feature: "home" },
];
const MAX_TABS = 5; // четыре раздела + «Ещё»

function can(feature) { return state.features.includes(feature); }

function renderTabs() {
  const allowed = TABS.filter((tab) => can(tab.feature));
  const main = allowed.filter((tab) => tab.view !== "more").slice(0, MAX_TABS - 1);
  const more = allowed.find((tab) => tab.view === "more");
  const tabs = more ? main.concat([more]) : main;
  state.tabs = tabs.map((tab) => tab.view);
  $tabbar.innerHTML = tabs.map((tab) =>
    '<button class="tab" data-view="' + tab.view + '"><span class="tab-icon">' + tab.icon +
    '</span><span class="tab-label">' + esc(tab.label) + "</span></button>"
  ).join("");
  $tabbar.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => show(tab.dataset.view));
  });
}

// --- Токен из ссылки бота ---
(function initToken() {
  const params = new URLSearchParams(location.search);
  const token = params.get("token");
  if (token) {
    localStorage.setItem("mini_token", token);
    history.replaceState(null, "", location.pathname);
  }
})();

function token() { return localStorage.getItem("mini_token") || ""; }

async function api(path, options = {}) {
  const headers = Object.assign({ "X-Mini-Token": token() }, options.headers || {});
  if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  const resp = await fetch("/mini/api" + path, Object.assign({}, options, { headers }));
  if (resp.status === 401) { showAuthError(); throw new Error("unauthorized"); }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    let msg = data.detail || "Ошибка запроса";
    if (Array.isArray(msg)) {
      msg = msg.map((e) => e.msg || e.detail || JSON.stringify(e)).join("; ");
    } else if (typeof msg === "object" && msg !== null) {
      msg = JSON.stringify(msg);
    }
    throw new Error(msg);
  }
  return data;
}

function showAuthError() {
  $app.classList.add("hidden");
  $loader.classList.add("hidden");
  $authError.classList.remove("hidden");
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

let toastTimer = null;
function toast(message) {
  $toast.textContent = message;
  $toast.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $toast.classList.add("hidden"), 2600);
}

function fmtDT(value) {
  if (!value) return "";
  const d = new Date(value);
  if (isNaN(d)) return value;
  return d.toLocaleDateString("ru-RU", { day: "numeric", month: "short" }) + ", " +
    d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}

function deadlineBadge(value) {
  const d = new Date(value);
  const hours = (d - Date.now()) / 3600000;
  if (hours < 0) return '<span class="badge danger">просрочено</span>';
  if (hours < 24) return '<span class="badge danger">менее суток</span>';
  if (hours < 72) return '<span class="badge warn">скоро</span>';
  return '<span class="badge accent">' + esc(fmtDT(value)) + "</span>";
}

function empty(emoji, text) {
  return '<div class="empty"><div class="empty-emoji">' + emoji + '</div><div class="empty-text">' + esc(text) + "</div></div>";
}

// --- Модалки ---
function openModal(html, onOpen) {
  $modalRoot.innerHTML = '<div class="modal">' + html + "</div>";
  $modalRoot.classList.remove("hidden");
  $modalRoot.onclick = (e) => { if (e.target === $modalRoot) closeModal(); };
  const cancel = $modalRoot.querySelector("[data-close]");
  if (cancel) cancel.onclick = closeModal;
  if (onOpen) onOpen($modalRoot.querySelector(".modal"));
}

function closeModal() {
  $modalRoot.classList.add("hidden");
  $modalRoot.innerHTML = "";
}

// --- Роутинг вкладок ---
const VIEWS = {};

async function show(view) {
  if (!VIEWS[view]) view = "home";
  state.view = view;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === view));
  $fab.classList.toggle("hidden", !fabAction());
  $view.scrollTop = 0;
  $view.innerHTML = '<div class="empty"><div class="spinner" style="margin:0 auto"></div></div>';
  try {
    await VIEWS[view]();
  } catch (err) {
    if (err.message !== "unauthorized") {
      $view.innerHTML = empty("⚠️", err.message || "Не удалось загрузить данные");
    }
  }
  $fab.classList.toggle("hidden", !fabAction());
}

// Кнопка «＋» доступна только там, где роль может создавать записи.
function fabAction() {
  if (!state.me) return null;
  switch (state.view) {
    case "homework": return can("homework_create") ? openHomeworkForm : null;
    case "deadlines": return can("deadline_create") ? openDeadlineForm : null;
    case "schedule": return can("schedule_edit") ? openLessonForm : null;
    case "announcements": return can("announcement_create") ? openAnnouncementForm : null;
    default: return null;
  }
}

$fab.addEventListener("click", () => {
  const action = fabAction();
  if (action) action();
});

// --- Старт ---
(async function boot() {
  if (!token()) { showAuthError(); return; }
  try {
    await applyMe(await api("/me"));
  } catch (err) {
    if (err.message !== "unauthorized") showAuthError();
    return;
  }
  $loader.classList.add("hidden");
  $app.classList.remove("hidden");
  await show(state.tabs.includes("home") ? "home" : state.tabs[0] || "home");
})();

// Профиль определяет состав интерфейса: features приходят с /mini/api/me.
function applyMe(me) {
  state.me = me;
  state.features = Array.isArray(me.features) ? me.features : [];
  renderHeader();
  renderTabs();
}

function renderHeader() {
  const me = state.me;
  const roleLabel = { student: "Ученик", teacher: "Учитель", parent: "Родитель", psychologist: "Психолог" }[me.role] || me.role;
  const parts = [roleLabel];
  if (me.class_name) parts.push(me.class_name + " класс");
  if (me.school) parts.push(me.school);
  $headerSub.textContent = me.name + " · " + parts.join(" · ");
  $pointsValue.textContent = me.points;
  // Очки — ученическая геймификация: родителю и психологу чип не показываем.
  $pointsChip.classList.toggle("hidden", !can("profile"));
}

async function refreshMe() {
  applyMe(await api("/me"));
}

/* ===== Главная ===== */
VIEWS.home = async function () {
  const data = await api("/home");
  // У родителя и психолога своя главная — ученические блоки им не показываем.
  if (data.role === "parent") return renderParentHome(data);
  if (data.role === "psychologist") return renderPsychologistHome(data);
  const hour = new Date().getHours();
  const greet = hour < 6 ? "Доброй ночи" : hour < 12 ? "Доброе утро" : hour < 18 ? "Добрый день" : "Добрый вечер";
  const today = new Date().toLocaleDateString("ru-RU", { weekday: "long", day: "numeric", month: "long" });
  const progress = data.homework_total ? Math.round((data.homework_done / data.homework_total) * 100) : 0;

  let html = '<div class="section-title">' + esc(greet + ", " + state.me.name.split(" ")[0]) + " 👋</div>";
  html += '<div class="card"><div class="card-text" style="text-transform:capitalize">' + esc(today) + "</div>";
  html += '<div class="card-title" style="margin-top:8px">Домашка: ' + data.homework_done + " из " + data.homework_total + " выполнено</div>";
  html += '<div class="progress"><div class="progress-fill" style="width:' + progress + '%"></div></div></div>';

  html += '<div class="stat-grid">';
  html += '<div class="stat"><div class="stat-emoji">🏮</div><div class="stat-value">' + data.points + '</div><div class="stat-label">очков</div></div>';
  html += '<div class="stat"><div class="stat-emoji">📜</div><div class="stat-value">' + (data.homework_total - data.homework_done) + '</div><div class="stat-label">заданий осталось</div></div>';
  html += '<div class="stat"><div class="stat-emoji">⏰</div><div class="stat-value">' + data.deadlines_total + '</div><div class="stat-label">дедлайнов</div></div>';
  html += "</div>";

  if (data.mood_today == null && can("stress")) {
    html += '<div class="section-title">Как ты сегодня? 🧘</div><div class="card"><div class="mood-row" id="home-mood">';
    MOODS.forEach((m, i) => { html += '<button class="mood-btn" data-score="' + (i + 1) + '">' + m + "</button>"; });
    html += "</div></div>";
  }

  html += '<div class="section-title">Уроки сегодня 📚</div>';
  if (data.lessons.length) {
    data.lessons.forEach((l) => {
      html += '<div class="card lesson-card"><div class="lesson-time">' + esc(l.time) + "</div>" +
        '<div class="card-body"><div class="card-title">' + esc(l.subject) + "</div>" +
        (l.room ? '<div class="card-text">каб. ' + esc(l.room) + "</div>" : "") + "</div></div>";
    });
  } else {
    html += empty("🎉", "Сегодня уроков нет — отдыхай!");
  }

  if (data.next_deadline_title) {
    html += '<div class="section-title">Ближайший дедлайн ⏰</div>';
    html += '<div class="card"><div class="card-title">' + esc(data.next_deadline_title) + "</div>" +
      '<div class="card-meta">' + deadlineBadge(data.next_deadline) + "</div></div>";
  }

  $view.innerHTML = html;

  const moodRow = document.getElementById("home-mood");
  if (moodRow) {
    moodRow.querySelectorAll(".mood-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const res = await api("/mood", { method: "POST", body: JSON.stringify({ score: +btn.dataset.score }) });
        toast(res.ok ? "Настроение сохранено, +3 очка 🏮" : res.reason);
        if (res.ok) { await refreshMe(); show("home"); }
      });
    });
  }
};

/* --- Главная родителя: дети и их успехи --- */
function renderParentHome(data) {
  const hour = new Date().getHours();
  const greet = hour < 6 ? "Доброй ночи" : hour < 12 ? "Доброе утро" : hour < 18 ? "Добрый день" : "Добрый вечер";
  const today = new Date().toLocaleDateString("ru-RU", { weekday: "long", day: "numeric", month: "long" });

  let html = '<div class="section-title">' + esc(greet + ", " + state.me.name.split(" ")[0]) + " 👋</div>";
  html += '<div class="card"><div class="card-text" style="text-transform:capitalize">' + esc(today) + "</div></div>";
  html += '<div class="stat-grid">' +
    '<div class="stat"><div class="stat-emoji">🧒</div><div class="stat-value">' + data.children_count + '</div><div class="stat-label">детей</div></div>' +
    '<div class="stat"><div class="stat-emoji">✅</div><div class="stat-value">' + data.completed_week + '</div><div class="stat-label">сдано за неделю</div></div>' +
    '<div class="stat"><div class="stat-emoji">⚠️</div><div class="stat-value">' + data.overdue_total + '</div><div class="stat-label">просрочено</div></div>' +
    "</div>";

  if (!data.children_count) {
    html += empty("👨‍👩‍👧", "Ребёнок ещё не привязан. Напишите боту «Привязать ребёнка» и укажите его MAX ID.");
  }
  data.children.forEach((child) => {
    html += '<div class="card"><div class="card-row"><div class="card-emoji">🧒</div><div class="card-body">' +
      '<div class="card-title">' + esc(child.name) +
      (child.class_name ? ' <span class="badge">' + esc(child.class_name) + " класс</span>" : "") + "</div>" +
      '<div class="card-text">Сдано за неделю: ' + child.completed_week +
      (child.overdue ? " · просрочено: " + child.overdue : "") + "</div>" +
      (child.deadlines.length
        ? '<div class="card-meta">' + deadlineBadge(child.deadlines[0].date) +
          "<span>" + esc(child.deadlines[0].title) + "</span></div>"
        : '<div class="card-meta"><span class="badge ok">дедлайнов нет</span></div>') +
      "</div></div></div>";
  });

  if (data.next_deadline_title) {
    html += '<div class="section-title">Ближайший дедлайн ⏰</div>' +
      '<div class="card"><div class="card-title">' + esc(data.next_deadline_title) + "</div>" +
      '<div class="card-text">' + esc(data.next_deadline_child || "") + "</div>" +
      '<div class="card-meta">' + deadlineBadge(data.next_deadline) + "</div></div>";
  }

  html += '<div class="card" style="margin-top:16px"><div class="card-text">🧒 Во вкладке «Дети» — сводка по каждому ребёнку, ' +
    "в «Дедлайнах» — ближайшие сроки, а советы по поддержке подростка ждут в меню «Ещё».</div></div>";
  $view.innerHTML = html;
}

/* --- Главная психолога: сигналы учеников --- */
function renderPsychologistHome(data) {
  const s = data.summary || {};
  let html = '<div class="section-title">Кабинет психолога 🧠</div>';
  html += '<div class="stat-grid">' +
    '<div class="stat"><div class="stat-emoji">🚨</div><div class="stat-value">' + (s.total || 0) + '</div><div class="stat-label">всего сигналов</div></div>' +
    '<div class="stat"><div class="stat-emoji">⏳</div><div class="stat-value">' + (s.pending || 0) + '</div><div class="stat-label">ожидают</div></div>' +
    '<div class="stat"><div class="stat-emoji">🧩</div><div class="stat-value">' + (s.in_progress || 0) + '</div><div class="stat-label">в работе</div></div>' +
    "</div>";

  html += '<div class="section-title">Ждут реакции ⏳</div>';
  if (!data.urgent.length) {
    html += empty("🌿", "Новых сигналов нет — все обращения в работе или закрыты.");
  }
  data.urgent.forEach((item) => { html += signalCard(item); });

  html += '<div class="card" style="margin-top:16px"><div class="card-text">🚨 Во вкладке «Сигналы» — все обращения учеников' +
    (data.ai_configured ? " и фильтр по статусам." : ". Подключите AI-психолога в настройках сервера, чтобы получать рекомендации.") +
    "</div></div>";
  $view.innerHTML = html;
  bindSignalButtons();
}

/* ===== Домашка ===== */
VIEWS.homework = async function () {
  const data = await api("/homework");
  const isTeacher = state.me.role === "teacher";
  let html = '<div class="section-title">Активные задания 📜</div>';
  if (!data.items.length) {
    html += empty("🌴", "Домашки нет — можно выдохнуть!");
  }
  const pending = data.items.filter((i) => !i.done);
  const done = data.items.filter((i) => i.done);
  const card = (item) =>
    '<div class="card' + (item.done ? " done" : "") + '"><div class="card-row">' +
    '<div class="card-body"><div class="card-title">' + esc(item.subject) +
    (item.class_name ? ' <span class="badge">' + esc(item.class_name) + "</span>" : "") + "</div>" +
    '<div class="card-text">' + esc(item.description) + "</div>" +
    '<div class="card-meta">' + deadlineBadge(item.deadline) +
    '<span class="badge ' + (item.done ? "ok" : "wait") + '">' +
    (item.done ? "✅ сдано" : "⏳ не сдано") + "</span></div></div>" +
    (isTeacher && item.mine
      ? '<button class="roster-btn" data-id="' + item.id + '" title="Отметить сдачу">👥</button>'
      : "") +
    "</div></div>";
  pending.forEach((i) => { html += card(i); });
  if (done.length) {
    html += '<div class="section-title">Выполнено ✅</div>';
    done.forEach((i) => { html += card(i); });
  }
  if (!isTeacher && data.items.length) {
    html += '<div class="card-text muted-note">Готовность отмечает учительница 👩‍🏫 — статус обновится автоматически.</div>';
  }
  $view.innerHTML = html;
  $view.querySelectorAll(".roster-btn").forEach((btn) => {
    btn.addEventListener("click", () => openRoster(+btn.dataset.id));
  });
};

/* --- Отметка сдачи учителем --- */
async function openRoster(homeworkId) {
  const data = await api("/homework/" + homeworkId + "/roster");
  const row = (s) =>
    '<div class="toggle-row"><span class="toggle-label">' + esc(s.name) + "</span>" +
    '<div class="toggle' + (s.done ? " on" : "") + '" data-student="' + s.id + '"></div></div>';
  openModal(
    '<div class="modal-title">👥 Кто сдал</div>' +
    (data.students.length
      ? data.students.map(row).join("")
      : '<div class="card-text">В классе пока нет учеников.</div>') +
    '<button class="btn secondary" data-close style="margin-top:14px">Закрыть</button>',
    (modal) => {
      modal.querySelectorAll(".toggle").forEach((tg) => {
        tg.addEventListener("click", async () => {
          const done = !tg.classList.contains("on");
          tg.classList.toggle("on", done);
          try {
            await api("/homework/" + homeworkId + "/mark", {
              method: "POST",
              body: JSON.stringify({ student_id: +tg.dataset.student, done }),
            });
            toast(done ? "Отмечено сдано ✅ (+10 очков ученику)" : "Отметка снята");
          } catch (err) {
            tg.classList.toggle("on", !done);
            toast(err.message);
          }
        });
      });
    }
  );
}

/* ===== Дедлайны ===== */
VIEWS.deadlines = async function () {
  const data = await api("/deadlines");
  let html = '<div class="section-title">Ближайшие дедлайны ⏰</div>';
  if (!data.items.length) {
    html += empty("🧘", "Дедлайнов нет. Тишина и спокойствие.");
  }
  const kindEmoji = { "контрольная": "📝", "самостоятельная": "🧪", "проект": "🚀", "зачёт": "🎯" };
  data.items.forEach((item) => {
    html += '<div class="card"><div class="card-row"><div class="card-emoji">' + (kindEmoji[item.kind] || "⏰") + "</div>" +
      '<div class="card-body"><div class="card-title">' + esc(item.title) + "</div>" +
      '<div class="card-meta"><span class="badge">' + esc(item.kind) + "</span>" +
      (item.class_name ? "<span>" + esc(item.class_name) + "</span>" : "") +
      deadlineBadge(item.date) + "</div></div></div></div>";
  });
  $view.innerHTML = html;
};

/* ===== Антистресс ===== */
VIEWS.stress = async function () {
  const data = await api("/mood");
  const todayStr = new Date().toISOString().slice(0, 10);
  const todayEntry = data.history.find((h) => h.date === todayStr);

  let html = '<div class="section-title">Трекер настроения 🌈</div><div class="card">';
  html += '<div class="mood-row" id="mood-row">';
  MOODS.forEach((m, i) => {
    const sel = todayEntry && todayEntry.score === i + 1 ? " selected" : "";
    html += '<button class="mood-btn' + sel + '" data-score="' + (i + 1) + '"' + (todayEntry ? " disabled" : "") + ">" + m + "</button>";
  });
  html += "</div>";
  html += '<div class="mood-chart-wrap">';
  const byDate = {};
  data.history.forEach((h) => { byDate[h.date] = h.score; });
  const days = [];
  for (let i = 13; i >= 0; i--) days.push(new Date(Date.now() - i * 86400000));
  const hasData = days.some((d) => byDate[d.toISOString().slice(0, 10)]);
  if (hasData) {
    html += '<div class="mood-chart">';
    days.forEach((d) => {
      const score = byDate[d.toISOString().slice(0, 10)];
      html +=
        '<div class="mood-col">' +
        (score
          ? '<div class="mood-bar s' + score + '" style="height:' + (12 + score * 12) + 'px" title="' + MOODS[score - 1] + ' ' + score + '/5"></div>'
          : '<div class="mood-bar zero"></div>') +
        '<div class="mood-day">' + d.getDate() + "</div></div>";
    });
    html += "</div>";
    const scores = data.history.map((h) => h.score);
    const avg = scores.reduce((a, b) => a + b, 0) / scores.length;
    html +=
      '<div class="mood-avg">Среднее за 2 недели: <b>' + avg.toFixed(1) +
      " / 5</b> " + MOODS[Math.max(0, Math.min(4, Math.round(avg) - 1))] + "</div>";
  } else {
    html += '<div class="mood-empty">Пока нет отметок — график появится после первой 🌱</div>';
  }
  html += "</div>";
  html += '<div class="card-text" style="margin-top:8px">' + (todayEntry ? "Сегодня настроение уже отмечено — спасибо! 🤍" : "Отметь, как ты себя чувствуешь (+3 очка)") + "</div>";
  html += "</div>";

  html += '<div class="section-title">Чат с ИИ-психологом 🤖</div>';
  html += '<div class="chat-wrap" id="chat-wrap"></div>';
  html += '<div class="chat-input-row"><textarea id="chat-input" class="chat-input" rows="1" placeholder="Расскажи, что у тебя на душе…"></textarea>' +
    '<button id="chat-send" class="send-btn">➤</button></div>';

  $view.innerHTML = html;

  document.querySelectorAll("#mood-row .mood-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const res = await api("/mood", { method: "POST", body: JSON.stringify({ score: +btn.dataset.score }) });
      toast(res.ok ? "Сохранено, +3 очка 🏮" : res.reason);
      if (res.ok) { await refreshMe(); show("stress"); }
    });
  });

  // Чат
  const wrap = document.getElementById("chat-wrap");
  const input = document.getElementById("chat-input");
  const sendBtn = document.getElementById("chat-send");

  function addBubble(text, who, crisis) {
    const div = document.createElement("div");
    div.className = "bubble " + who + (crisis ? " crisis" : "");
    div.textContent = text;
    wrap.appendChild(div);
    div.scrollIntoView({ block: "end" });
    return div;
  }

  if (!state.chat.length) {
    state.chat.push({ who: "ai", text: "Привет! Я твой школьный ИИ-психолог 🤍 Здесь можно поделиться чем угодно — я не осужу и всё останется между нами." });
  }
  state.chat.forEach((m) => addBubble(m.text, m.who, m.crisis));

  async function sendMessage() {
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    input.style.height = "auto";
    state.chat.push({ who: "me", text });
    addBubble(text, "me");
    const typing = document.createElement("div");
    typing.className = "bubble ai typing-dots";
    typing.innerHTML = "<span></span><span></span><span></span>";
    wrap.appendChild(typing);
    typing.scrollIntoView({ block: "end" });
    sendBtn.disabled = true;
    try {
      const res = await api("/chat", { method: "POST", body: JSON.stringify({ message: text }) });
      typing.remove();
      state.chat.push({ who: "ai", text: res.reply, crisis: res.crisis });
      addBubble(res.reply, "ai", res.crisis);
      if (res.needs_human) {
        const note = document.createElement("div");
        note.className = "card-text";
        note.style.textAlign = "center";
        note.textContent = "💬 Хочешь поговорить с живым психологом? Напиши боту «Антистресс» — там есть кнопка передачи сигнала.";
        wrap.appendChild(note);
      }
    } catch (err) {
      typing.remove();
      addBubble("⚠️ " + (err.message || "Не удалось получить ответ"), "ai");
    } finally {
      sendBtn.disabled = false;
      input.focus();
    }
  }

  sendBtn.addEventListener("click", sendMessage);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 96) + "px";
  });
};

/* ===== Родитель: дети ===== */
VIEWS.children = async function () {
  const data = await api("/children");
  let html = '<div class="section-title">Мои дети 🧒</div>';
  if (!data.items.length) {
    html += empty("👨‍👩‍👧", "Ребёнок ещё не привязан. Напишите боту «Привязать ребёнка» и укажите его MAX ID.");
  }
  data.items.forEach((child) => {
    html += '<div class="card"><div class="card-row"><div class="card-emoji">🧒</div><div class="card-body">' +
      '<div class="card-title">' + esc(child.name) +
      (child.class_name ? ' <span class="badge">' + esc(child.class_name) + " класс</span>" : "") + "</div>" +
      (child.school ? '<div class="card-text">' + esc(child.school) + "</div>" : "") +
      '<div class="card-meta"><span class="badge ok">✅ ' + child.completed_week + " за неделю</span>" +
      (child.overdue ? '<span class="badge danger">⚠️ просрочено ' + child.overdue + "</span>" : "") +
      "</div></div>" +
      (can("summary")
        ? '<button class="roster-btn" data-child="' + child.id + '" title="Сводка">📖</button>'
        : "") +
      "</div></div>";
    child.deadlines.forEach((d) => {
      html += '<div class="card"><div class="card-row"><div class="card-emoji">⏰</div><div class="card-body">' +
        '<div class="card-title">' + esc(d.title) + '</div><div class="card-meta">' +
        '<span class="badge">' + esc(d.kind) + "</span>" + deadlineBadge(d.date) +
        "</div></div></div></div>";
    });
  });
  html += '<div class="card" style="margin-top:12px"><div class="card-text">🔔 Уведомления о дедлайнах: ' +
    (data.notifications_enabled ? "включены" : "выключены") + ". Изменить — в «Настройках».</div></div>";
  if (can("tips")) {
    html += '<button class="menu-item" id="go-tips"><span class="mi-emoji">💡</span>' +
      '<span>Советы по поддержке подростка</span><span class="mi-arrow">›</span></button>';
  }
  $view.innerHTML = html;
  $view.querySelectorAll("[data-child]").forEach((btn) => {
    btn.addEventListener("click", () => openSummary(+btn.dataset.child));
  });
  const tipsBtn = document.getElementById("go-tips");
  if (tipsBtn) tipsBtn.addEventListener("click", () => openTips("children"));
};

/* --- Сводка по ребёнку --- */
async function openSummary(childId) {
  const data = await api("/parent/summary" + (childId ? "?child_id=" + childId : ""));
  if (!data.child) { toast("Сводка недоступна: ребёнок не привязан."); return; }
  let html = backRow("Сводка · " + data.child.name, "children");
  html += '<div class="stat-grid">' +
    '<div class="stat"><div class="stat-emoji">✅</div><div class="stat-value">' + data.completed_week + '</div><div class="stat-label">сдано за неделю</div></div>' +
    '<div class="stat"><div class="stat-emoji">⚠️</div><div class="stat-value">' + data.overdue + '</div><div class="stat-label">просрочено</div></div>' +
    '<div class="stat"><div class="stat-emoji">⏰</div><div class="stat-value">' + data.deadlines.length + '</div><div class="stat-label">дедлайнов</div></div>' +
    "</div>";
  if (data.child.class_name || data.child.school) {
    html += '<div class="card"><div class="card-text">' +
      esc([data.child.class_name ? data.child.class_name + " класс" : null, data.child.school].filter(Boolean).join(" · ")) +
      "</div></div>";
  }
  html += '<div class="section-title">Ближайшие сроки ⏰</div>';
  if (!data.deadlines.length) html += empty("🧘", "Дедлайнов нет. Тишина и спокойствие.");
  data.deadlines.forEach((d) => {
    html += '<div class="card"><div class="card-row"><div class="card-emoji">⏰</div><div class="card-body">' +
      '<div class="card-title">' + esc(d.title) + "</div>" +
      '<div class="card-meta"><span class="badge">' + esc(d.kind) + "</span>" +
      (d.class_name ? "<span>" + esc(d.class_name) + "</span>" : "") +
      deadlineBadge(d.date) + "</div></div></div></div>";
  });
  $view.innerHTML = html;
  bindBack();
}

/* --- Советы родителю --- */
async function openTips(from) {
  const data = await api("/parent/tips");
  let html = backRow("Советы родителю 💡", from || "more");
  data.items.forEach((tip) => {
    html += '<div class="card"><div class="card-row"><div class="card-emoji">💡</div>' +
      '<div class="card-body"><div class="card-text">' + esc(tip) + "</div></div></div></div>";
  });
  html += '<div class="card-text muted-note">🧠 Если ребёнку тяжело — напишите школьному психологу через бота.</div>';
  $view.innerHTML = html;
  bindBack();
}

/* ===== Психолог: сигналы учеников ===== */
const RISK_LABELS = { 1: "низкий", 2: "средний", 3: "высокий" };
state.signalStatus = "all";

function nextStatus(status) { return status === "pending" ? "in_progress" : "closed"; }
function statusActionLabel(status) { return status === "pending" ? "В работу" : "Закрыть"; }

function signalCard(item) {
  const risk = item.risk_level >= 3 ? "danger" : item.risk_level === 2 ? "warn" : "accent";
  return '<div class="card"><div class="card-row"><div class="card-emoji">🚨</div><div class="card-body">' +
    '<div class="card-title">' + esc(item.student_name) +
    (item.class_name ? ' <span class="badge">' + esc(item.class_name) + "</span>" : "") + "</div>" +
    '<div class="card-text">' + esc(item.themes) + "</div>" +
    '<div class="card-meta">' +
    '<span class="badge ' + risk + '">риск: ' + esc(RISK_LABELS[item.risk_level] || item.risk_level) + "</span>" +
    '<span class="badge">' + esc(item.status_label) + "</span>" +
    (item.preferred_format ? "<span>" + esc(item.preferred_format) + "</span>" : "") +
    (item.created_at ? "<span>" + esc(fmtDT(item.created_at)) + "</span>" : "") +
    "</div></div>" +
    (can("signal_status") && item.status !== "closed"
      ? '<button class="btn small" data-signal="' + item.id + '" data-next="' + nextStatus(item.status) + '">' +
        esc(statusActionLabel(item.status)) + "</button>"
      : "") +
    "</div></div>";
}

function bindSignalButtons() {
  $view.querySelectorAll("[data-signal]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        const res = await api("/signals/" + btn.dataset.signal, {
          method: "POST",
          body: JSON.stringify({ status: btn.dataset.next }),
        });
        toast("Статус: " + res.status_label);
        show(state.view);
      } catch (err) { toast(err.message); }
    });
  });
}

VIEWS.signals = async function () {
  const data = await api("/signals");
  const filter = state.signalStatus || "all";
  const items = filter === "all" ? data.items : data.items.filter((i) => i.status === filter);
  let html = '<div class="section-title">Сигналы учеников 🚨</div>';
  html += '<div class="day-tabs" style="margin-bottom:8px">' +
    [["all", "Все"]].concat(data.statuses.map((s) => [s.key, s.label])).map((pair) =>
      '<button class="day-tab' + (pair[0] === filter ? " active" : "") + '" data-filter="' + pair[0] + '">' +
      esc(pair[1]) + "</button>").join("") + "</div>";
  if (!items.length) html += empty("🌿", "Сигналов в этом статусе нет.");
  items.forEach((item) => { html += signalCard(item); });
  html += '<div class="card-text muted-note">🔒 Переписка ученика с AI-психологом не передаётся: ' +
    "видны только уровень риска, темы и удобный формат помощи.</div>";
  $view.innerHTML = html;
  $view.querySelectorAll("[data-filter]").forEach((btn) => {
    btn.addEventListener("click", () => { state.signalStatus = btn.dataset.filter; show("signals"); });
  });
  bindSignalButtons();
};

VIEWS.psych_stats = async function () {
  const data = await api("/signals");
  const s = data.summary;
  let html = backRow("Сводка психолога 📊");
  html += '<div class="stat-grid">' +
    '<div class="stat"><div class="stat-emoji">🚨</div><div class="stat-value">' + s.total + '</div><div class="stat-label">всего сигналов</div></div>' +
    '<div class="stat"><div class="stat-emoji">⏳</div><div class="stat-value">' + s.pending + '</div><div class="stat-label">ожидают</div></div>' +
    '<div class="stat"><div class="stat-emoji">🧩</div><div class="stat-value">' + s.in_progress + '</div><div class="stat-label">в работе</div></div>' +
    "</div>";
  const closedPct = s.total ? Math.round((s.closed / s.total) * 100) : 0;
  html += '<div class="card"><div class="card-title">Закрыто: ' + s.closed + " из " + s.total + "</div>" +
    '<div class="progress"><div class="progress-fill" style="width:' + closedPct + '%"></div></div></div>';

  const byRisk = { 1: 0, 2: 0, 3: 0 };
  data.items.forEach((item) => {
    const level = Math.min(3, Math.max(1, item.risk_level || 1));
    byRisk[level] += 1;
  });
  html += '<div class="section-title">По уровню риска</div>';
  [3, 2, 1].forEach((level) => {
    html += '<div class="card"><div class="card-row"><div class="card-emoji">' +
      (level === 3 ? "🔥" : level === 2 ? "⚠️" : "🌿") + '</div><div class="card-body">' +
      '<div class="card-title">' + esc(RISK_LABELS[level]) + '</div>' +
      '<div class="card-text">' + byRisk[level] + " сигналов</div></div></div></div>";
  });

  const open = data.items.filter((item) => item.status !== "closed");
  html += '<div class="section-title">Требуют внимания</div>';
  if (!open.length) html += empty("🌿", "Открытых сигналов нет.");
  open.slice(0, 5).forEach((item) => { html += signalCard(item); });
  $view.innerHTML = html;
  bindBack();
  bindSignalButtons();
};

/* ===== Ещё ===== */
function backRow(title, target) {
  // Кнопка «Назад» не нужна, если раздел сам является вкладкой нижней панели.
  if (!target && state.tabs.includes(state.view)) {
    return '<div class="section-title">' + esc(title) + "</div>";
  }
  return '<div class="section-title" style="display:flex;align-items:center;gap:10px">' +
    '<button class="btn secondary small" data-back="' + esc(target || "more") +
    '" style="width:auto">← Назад</button>' + esc(title) + "</div>";
}

function bindBack() {
  const btn = $view.querySelector("[data-back]");
  if (btn) btn.addEventListener("click", () => show(btn.dataset.back || "more"));
}

VIEWS.more = async function () {
  const me = state.me;
  const items = [];
  // Пункты меню появляются только если роль имеет соответствующий feature.
  const add = (emoji, label, feature, action) => {
    if (can(feature)) items.push([emoji, label, action]);
  };

  add("📚", "Расписание", "schedule", () => show("schedule"));
  add("📢", "Объявления", "announcements", openAnnouncements);
  add("🧒", "Мои дети", "children", () => show("children"));
  add("📖", "Сводка по ребёнку", "summary", () => openSummary());
  add("💡", "Советы родителю", "tips", () => openTips("more"));
  add("🚨", "Сигналы учеников", "signals", () => show("signals"));
  add("📊", "Сводка по сигналам", "psych_stats", () => show("psych_stats"));
  add("🏮", "Профиль и награды", "profile", openProfile);
  if (me.role === "student") add("🏫", me.class_name ? "Сменить класс" : "Войти в класс", "join_class", openJoinClass);
  add("🏫", "Мои классы", "classes", openClasses);
  add("📈", "Статистика", "stats", () => show("stats"));
  add("📢", "Создать объявление", "announcement_create", openAnnouncementForm);
  add("⚙️", "Настройки", "settings", openSettings);

  let html = '<div class="section-title">Меню ☰</div>';
  if (!items.length) html += empty("🧭", "Для вашей роли разделов пока нет.");
  items.forEach((item, idx) => {
    html += '<button class="menu-item" data-idx="' + idx + '"><span class="mi-emoji">' + item[0] + "</span>" +
      "<span>" + esc(item[1]) + '</span><span class="mi-arrow">›</span></button>';
  });
  html += '<div class="card" style="margin-top:16px"><div class="card-text">💬 Чат-режим всегда доступен: просто напиши боту — кнопки меню продублируют всё то же самое.</div></div>';
  $view.innerHTML = html;
  $view.querySelectorAll(".menu-item").forEach((btn) => {
    btn.addEventListener("click", () => items[+btn.dataset.idx][2]());
  });
};

/* --- Расписание: ученик смотрит, учитель редактирует --- */
VIEWS.schedule = async function () {
  const query = state.scheduleClass ? "?class_name=" + encodeURIComponent(state.scheduleClass) : "";
  const data = await api("/schedule" + query);
  state.scheduleClass = data.class_name;
  const editable = !!data.editable;

  function render() {
    let html = backRow("Расписание" + (data.class_name ? " · " + data.class_name + " класс" : ""));
    if (data.classes.length > 1) {
      html += '<div class="day-tabs" style="margin-bottom:8px">' + data.classes.map((name) =>
        '<button class="day-tab' + (name === data.class_name ? " active" : "") +
        '" data-class="' + esc(name) + '">' + esc(name) + "</button>").join("") + "</div>";
    }
    html += "<div class=\"day-tabs\">" + DAYS.map((d, i) =>
      '<button class="day-tab' + (i + 1 === state.day ? " active" : "") +
      '" data-day="' + (i + 1) + '">' + d + "</button>").join("") + "</div>";
    const lessons = data.days[String(state.day)] || [];
    if (!lessons.length) {
      html += empty("🎈", editable ? "В этот день уроков нет — добавьте первый." : "В этот день уроков нет.");
    }
    lessons.forEach((l) => {
      const mine = editable && l.can_edit;
      html += '<div class="card lesson-card"><div class="lesson-time">' + esc(l.time) + "</div>" +
        '<div class="card-body"><div class="card-title">' + esc(l.subject) + "</div>" +
        (l.room ? '<div class="card-text">каб. ' + esc(l.room) + "</div>" : "") +
        (data.role === "teacher" && l.class_name
          ? '<div class="card-meta"><span class="badge">' + esc(l.class_name) + " класс</span></div>"
          : "") +
        "</div>" +
        (mine ? '<button class="roster-btn" data-edit="' + l.id + '" title="Изменить урок">✏️</button>' : "") +
        (mine ? '<button class="roster-btn" data-del="' + l.id + '" title="Удалить урок">🗑</button>' : "") +
        "</div>";
    });
    if (editable) {
      html += '<div class="card-text muted-note">✏️ Менять и удалять можно только те уроки, ' +
        "которые добавили вы. Кнопка «＋» — новый урок.</div>";
    }
    $view.innerHTML = html;
    bindBack();
    $view.querySelectorAll(".day-tab[data-day]").forEach((btn) => {
      btn.addEventListener("click", () => { state.day = +btn.dataset.day; render(); });
    });
    $view.querySelectorAll(".day-tab[data-class]").forEach((btn) => {
      btn.addEventListener("click", () => { state.scheduleClass = btn.dataset.class; show("schedule"); });
    });
    $view.querySelectorAll("[data-edit]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const lesson = lessons.find((l) => l.id === +btn.dataset.edit);
        if (lesson) openLessonForm(lesson);
      });
    });
    $view.querySelectorAll("[data-del]").forEach((btn) => {
      btn.addEventListener("click", () => deleteLesson(+btn.dataset.del));
    });
  }
  render();
};

function openSchedule() { show("schedule"); }

/* --- Форма урока: добавление и правка (учитель) --- */
async function openLessonForm(lesson) {
  await ensureClassList();
  const currentDay = lesson ? lesson.day : state.day;
  const dayOptions = DAYS.map((d, i) =>
    '<option value="' + (i + 1) + '"' + (currentDay === i + 1 ? " selected" : "") + ">" + d + "</option>"
  ).join("");
  openModal(
    '<div class="modal-title">' + (lesson ? "✏️ Изменить урок" : "📚 Новый урок") + "</div>" +
    (lesson
      ? '<div class="card-text" style="margin-bottom:12px">' + esc(lesson.class_name || "") + " класс</div>"
      : '<div class="field"><label>Класс</label><select id="ls-class">' +
        classOptions(state.scheduleClass) + "</select></div>") +
    '<div class="field"><label>День недели</label><select id="ls-day">' + dayOptions + "</select></div>" +
    '<div class="field"><label>Время</label><input id="ls-time" placeholder="08:30" autocomplete="off" value="' +
    esc(lesson ? lesson.time : "") + '"></div>' +
    '<div class="field"><label>Предмет</label><input id="ls-subject" placeholder="Математика" autocomplete="off" value="' +
    esc(lesson ? lesson.subject : "") + '"></div>' +
    '<div class="field"><label>Кабинет</label><input id="ls-room" placeholder="204" autocomplete="off" value="' +
    esc(lesson && lesson.room ? lesson.room : "") + '"></div>' +
    '<button class="btn" id="ls-submit">' + (lesson ? "Сохранить изменения" : "Добавить урок") + "</button>" +
    '<button class="btn secondary" data-close style="margin-top:8px">Отмена</button>'
  );
  document.getElementById("ls-submit").addEventListener("click", async () => {
    const body = {
      day: +document.getElementById("ls-day").value,
      time: document.getElementById("ls-time").value.trim(),
      subject: document.getElementById("ls-subject").value.trim(),
      room: document.getElementById("ls-room").value.trim(),
    };
    if (!body.subject) { toast("Введите предмет."); return; }
    if (!body.time) { toast("Введите время урока, например 08:30."); return; }
    try {
      if (lesson) {
        await api("/schedule/" + lesson.id, { method: "PATCH", body: JSON.stringify(body) });
        state.day = body.day;
        toast("Урок обновлён ✅");
      } else {
        const classField = document.getElementById("ls-class");
        body.class_name = classField ? classField.value : state.scheduleClass;
        if (!body.class_name) { toast("Сначала создайте класс в боте."); return; }
        await api("/schedule", { method: "POST", body: JSON.stringify(body) });
        state.scheduleClass = body.class_name;
        state.day = body.day;
        toast("Урок добавлен в расписание 📚");
      }
      closeModal();
      show("schedule");
    } catch (err) { toast(err.message); }
  });
}

async function deleteLesson(lessonId) {
  openModal(
    '<div class="modal-title">🗑 Удалить урок?</div>' +
    '<div class="card-text" style="margin-bottom:14px">Урок исчезнет из расписания класса. Отменить действие нельзя.</div>' +
    '<button class="btn danger" id="ls-del">Удалить</button>' +
    '<button class="btn secondary" data-close style="margin-top:8px">Отмена</button>'
  );
  document.getElementById("ls-del").addEventListener("click", async () => {
    try {
      await api("/schedule/" + lessonId, { method: "DELETE" });
      closeModal();
      toast("Урок удалён 🗑");
      show("schedule");
    } catch (err) { toast(err.message); }
  });
}

/* --- Объявления --- */
async function openAnnouncements() {
  const data = await api("/announcements");
  let html = backRow("Объявления 📢", "more");
  if (!data.items.length) html += empty("🔕", "Объявлений пока нет.");
  data.items.forEach((a) => {
    html += '<div class="card"><div class="card-title">' + esc(a.class_name) + " класс</div>" +
      '<div class="card-text">' + esc(a.text) + "</div>" +
      '<div class="card-meta"><span>' + esc(fmtDT(a.created_at)) + "</span></div></div>";
  });
  $view.innerHTML = html;
  bindBack();
}

/* --- Профиль и награды --- */
async function openProfile() {
  const data = await api("/profile");
  let html = backRow("Профиль 🏮", "more");
  html += '<div class="card"><div class="card-title">' + esc(data.name) + "</div>" +
    '<div class="card-text">' + [data.school, data.class_name ? data.class_name + " класс" : null].filter(Boolean).map(esc).join(" · ") + "</div>" +
    '<div class="stat-grid" style="margin-top:12px"><div class="stat"><div class="stat-emoji">🏮</div><div class="stat-value">' + data.points + '</div><div class="stat-label">очков</div></div>' +
    '<div class="stat"><div class="stat-emoji">🎁</div><div class="stat-value">' + data.rewards.filter((r) => r.owned).length + '</div><div class="stat-label">наград</div></div>' +
    '<div class="stat"><div class="stat-emoji">🛒</div><div class="stat-value">' + data.rewards.filter((r) => !r.owned && data.points >= r.cost).length + '</div><div class="stat-label">доступно</div></div></div></div>';
  html += '<div class="section-title">Магазин наград 🎁</div>';
  data.rewards.forEach((r) => {
    const can = !r.owned && data.points >= r.cost;
    html += '<div class="card"><div class="card-row"><div class="card-body">' +
      '<div class="card-title">' + esc(r.title) + "</div>" +
      '<div class="card-text">' + esc(r.description) + "</div>" +
      '<div class="card-meta"><span class="badge ' + (r.owned ? "ok" : can ? "accent" : "") + '">' + (r.owned ? "получено" : r.cost + " очков") + "</span></div></div>" +
      (can ? '<button class="btn small" data-reward="' + esc(r.key) + '">Забрать</button>' : "") +
      "</div></div>";
  });
  $view.innerHTML = html;
  bindBack();
  $view.querySelectorAll("[data-reward]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await api("/rewards", { method: "POST", body: JSON.stringify({ key: btn.dataset.reward }) });
        toast("Награда получена! 🎉");
        await refreshMe();
        openProfile();
      } catch (err) { toast(err.message); }
    });
  });
}

/* --- Мои классы (учитель) --- */
async function openClasses() {
  const data = await api("/classes");
  let html = backRow("Мои классы 🏫", "more");
  if (!data.items.length) html += empty("🏫", "Классов пока нет. Создайте класс в боте: меню → «➕ Создать класс».");
  data.items.forEach((c) => {
    html += '<div class="card"><div class="card-title">' + esc(c.name) + " класс</div>" +
      '<div class="card-text">Логин: <b>' + esc(c.login) + "</b> · Пароль: <b>" + esc(c.password) + "</b></div>" +
      '<div class="card-meta"><span class="badge accent">👥 ' + c.members + " учеников</span></div></div>";
  });
  $view.innerHTML = html;
  bindBack();
}

/* --- Статистика (учитель) --- */
VIEWS.stats = async function () {
  const data = await api("/stats");
  let html = backRow("Статистика 📈");
  html += '<div class="stat-grid"><div class="stat"><div class="stat-emoji">✅</div><div class="stat-value">' + data.today_done + '</div><div class="stat-label">сдано сегодня</div></div>' +
    '<div class="stat"><div class="stat-emoji">📜</div><div class="stat-value">' + data.today_total + '</div><div class="stat-label">всего сегодня</div></div>' +
    '<div class="stat"><div class="stat-emoji">🏫</div><div class="stat-value">' + data.classes.length + '</div><div class="stat-label">классов</div></div></div>';
  if (data.classes.length) html += '<div class="section-title">Сдача домашки по классам</div>';
  data.classes.forEach((c) => {
    const pct = c.total ? Math.round((c.done / c.total) * 100) : 0;
    html += '<div class="card"><div class="card-title">' + esc(c.class_name) + " класс</div>" +
      '<div class="card-text">' + c.done + " из " + c.total + " заданий сдано</div>" +
      '<div class="progress"><div class="progress-fill" style="width:' + pct + '%"></div></div></div>';
  });
  $view.innerHTML = html;
  bindBack();
};

/* --- Войти в класс (ученик) --- */
function openJoinClass() {
  openModal(
    '<div class="modal-title">🏫 Войти в класс</div>' +
    '<div class="field"><label>Логин класса</label><input id="jc-login" autocomplete="off" placeholder="Например: class10b"></div>' +
    '<div class="field"><label>Пароль</label><input id="jc-pass" type="password" placeholder="Пароль от класса"></div>' +
    '<button class="btn" id="jc-submit">Войти</button>' +
    '<button class="btn secondary" data-close style="margin-top:8px">Отмена</button>'
  );
  document.getElementById("jc-submit").addEventListener("click", async () => {
    const login = document.getElementById("jc-login").value;
    const password = document.getElementById("jc-pass").value;
    try {
      const res = await api("/join_class", { method: "POST", body: JSON.stringify({ login, password }) });
      closeModal();
      toast(res.message);
      await refreshMe();
      show("more");
    } catch (err) { toast(err.message); }
  });
}

/* --- Настройки --- */
function openSettings() {
  const me = state.me;
  const toggle = (id, label, on) =>
    '<div class="toggle-row"><span class="toggle-label">' + label + '</span>' +
    '<div class="toggle' + (on ? " on" : "") + '" id="' + id + '"></div></div>';
  openModal(
    '<div class="modal-title">⚙️ Настройки</div>' +
    '<div class="section-title" style="margin-top:0">Тема оформления</div>' +
    '<div class="theme-row">' +
    THEMES.map((t) =>
      '<button class="theme-opt' +
      ((localStorage.getItem("mini_theme") || "dark") === t[0] ? " on" : "") +
      '" data-theme-key="' + t[0] + '">' + t[1] + "</button>"
    ).join("") +
    "</div>" +
    '<div class="section-title">Напоминания</div>' +
    toggle("t-rem", "🔔 Напоминания", me.reminders_enabled) +
    toggle("t-7", "За неделю до дедлайна", me.notify_7d) +
    toggle("t-3", "За 3 дня", me.notify_3d) +
    toggle("t-1", "За 1 день", me.notify_1d) +
    '<button class="btn secondary" data-close style="margin-top:14px">Закрыть</button>',
    (modal) => {
      async function bindToggle(id, buildBody, successText) {
        modal.querySelector("#" + id).addEventListener("click", async (e) => {
          const el = e.currentTarget;
          const next = !el.classList.contains("on");
          el.classList.toggle("on", next);
          try {
            await api("/settings", { method: "POST", body: JSON.stringify(buildBody(next)) });
            await refreshMe();
            toast(successText ? successText(next) : "Настройка сохранена");
          } catch (err) {
            el.classList.toggle("on", !next);
            toast(err.message);
          }
        });
      }
      modal.querySelectorAll(".theme-opt").forEach((btn) => {
        btn.addEventListener("click", () => {
          applyTheme(btn.dataset.themeKey);
          modal.querySelectorAll(".theme-opt").forEach((b) =>
            b.classList.toggle("on", b === btn)
          );
          toast("Тема применена: " + btn.textContent.trim());
        });
      });
      bindToggle("t-rem", (on) => ({ reminders_enabled: on }));
      bindToggle("t-7", (on) => ({ notify_7d: on }));
      bindToggle("t-3", (on) => ({ notify_3d: on }));
      bindToggle("t-1", (on) => ({ notify_1d: on }));
    }
  );
}

/* ===== Формы учителя ===== */
function classOptions(selected) {
  let html = "";
  const names = state.classList || [];
  names.forEach((n) => {
    html += '<option value="' + esc(n) + '"' + (n === selected ? " selected" : "") + ">" + esc(n) + " класс</option>";
  });
  return html;
}

async function ensureClassList() {
  if (state.classList) return;
  try {
    const data = await api("/classes");
    state.classList = data.items.map((c) => c.name);
  } catch (err) {
    state.classList = [];
  }
  if (!state.classList.length && state.me.class_name) state.classList = [state.me.class_name];
}

function defaultDeadline() {
  const d = new Date(Date.now() + 86400000);
  d.setHours(23, 59, 0, 0);
  const pad = (n) => String(n).padStart(2, "0");
  return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + "T" + pad(d.getHours()) + ":" + pad(d.getMinutes());
}

async function openHomeworkForm() {
  await ensureClassList();
  openModal(
    '<div class="modal-title">📜 Новая домашка</div>' +
    '<div class="field"><label>Класс</label><select id="hw-class">' + classOptions() + "</select></div>" +
    '<div class="field"><label>Предмет</label><input id="hw-subject" placeholder="Математика" autocomplete="off"></div>' +
    '<div class="field"><label>Задание</label><textarea id="hw-desc" placeholder="Что нужно сделать?"></textarea></div>' +
    '<div class="field"><label>Сдать до</label><input id="hw-deadline" type="datetime-local" value="' + defaultDeadline() + '"></div>' +
    '<button class="btn" id="hw-submit">Задать домашку</button>' +
    '<button class="btn secondary" data-close style="margin-top:8px">Отмена</button>'
  );
  document.getElementById("hw-submit").addEventListener("click", async () => {
    const body = {
      class_name: document.getElementById("hw-class").value,
      subject: document.getElementById("hw-subject").value,
      description: document.getElementById("hw-desc").value,
      deadline: document.getElementById("hw-deadline").value,
    };
    if (!body.class_name) { toast("Сначала создайте класс в боте."); return; }
    try {
      await api("/homework", { method: "POST", body: JSON.stringify(body) });
      closeModal();
      toast("Домашка добавлена ✅");
      show("homework");
    } catch (err) { toast(err.message); }
  });
}

async function openDeadlineForm() {
  await ensureClassList();
  openModal(
    '<div class="modal-title">⏰ Новый дедлайн</div>' +
    '<div class="field"><label>Класс</label><select id="dl-class">' + classOptions() + "</select></div>" +
    '<div class="field"><label>Название</label><input id="dl-title" placeholder="Контрольная по алгебре" autocomplete="off"></div>' +
    '<div class="field"><label>Тип</label><select id="dl-kind">' +
    ["контрольная", "самостоятельная", "проект", "зачёт"].map((k) => '<option value="' + k + '">' + k + "</option>").join("") +
    "</select></div>" +
    '<div class="field"><label>Дата и время</label><input id="dl-date" type="datetime-local" value="' + defaultDeadline() + '"></div>' +
    '<button class="btn" id="dl-submit">Добавить дедлайн</button>' +
    '<button class="btn secondary" data-close style="margin-top:8px">Отмена</button>'
  );
  document.getElementById("dl-submit").addEventListener("click", async () => {
    const body = {
      class_name: document.getElementById("dl-class").value,
      title: document.getElementById("dl-title").value,
      kind: document.getElementById("dl-kind").value,
      date: document.getElementById("dl-date").value,
    };
    try {
      await api("/deadlines", { method: "POST", body: JSON.stringify(body) });
      closeModal();
      toast("Дедлайн добавлен ⏰");
      show("deadlines");
    } catch (err) { toast(err.message); }
  });
}

async function openAnnouncementForm() {
  await ensureClassList();
  openModal(
    '<div class="modal-title">📢 Объявление для класса</div>' +
    '<div class="field"><label>Класс</label><select id="an-class">' + classOptions() + "</select></div>" +
    '<div class="field"><label>Текст</label><textarea id="an-text" placeholder="Что передать ученикам?"></textarea></div>' +
    '<div class="card-text" style="margin-bottom:12px">Ученики класса получат объявление push-сообщением в MAX.</div>' +
    '<button class="btn" id="an-submit">Отправить объявление</button>' +
    '<button class="btn secondary" data-close style="margin-top:8px">Отмена</button>'
  );
  document.getElementById("an-submit").addEventListener("click", async () => {
    const body = {
      class_name: document.getElementById("an-class").value,
      text: document.getElementById("an-text").value,
    };
    if (!body.class_name) { toast("Сначала создайте класс в боте."); return; }
    try {
      const res = await api("/announcements", { method: "POST", body: JSON.stringify(body) });
      closeModal();
      toast("Объявление отправлено " + res.sent + " из " + res.recipients + " ученикам 📢");
    } catch (err) { toast(err.message); }
  });
}