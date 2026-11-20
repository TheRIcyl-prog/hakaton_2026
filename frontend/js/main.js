// АРМ координатора: работа с инцидентами. Видеостена — wall.js.
import * as R from "./render.js";
import * as Sound from "./sound.js";
import * as F from "./filters.js";
import { computeAlerts, processEvents, ring, watchOverdue } from "./alerts.js";
import { api, getMe, setMe, startPolling } from "./api.js";
import { openTransfer, openEscalate, closeModal } from "./desk.js";

const HIGHLIGHT_MS = 15000;
const VIEW_KEY = "mi-dashboard:view";
const CHANGE_TYPES = new Set(["priority_changed", "deadline_changed", "team_changed", "assignee_changed", "status_changed",
  "became_critical", "to_coordinators", "from_coordinators", "resolved", "reopened", "critical_resolved", "new", "comment", "escalated"]);

const state = {
  snap: null, online: true, lastOkMs: 0, offsetMs: 0, openId: null, started: false, alerts: [], escalated: false, meta: null,
  view: loadView(), feedOpen: false, feedSeenSeq: null,
};
const ui = {
  query: "", changed: new Map(), open: (id) => openDrawer(id), predicate: null, limits: {},
  selected: null, alarmIds: new Set(), actions: new Map(),
  more: (key) => { ui.limits[key] = (ui.limits[key] || 60) + 60; renderMain(); },
  act: {
    transfer: (id) => doTransfer(id),
    escalate: (id, draft) => doEscalate(id, draft),
    claim: (id) => doClaim(id),
  },
};
const nowMs = () => Date.now() + state.offsetMs;
const filtersEl = document.getElementById("filters");

function loadView() {
  try { return localStorage.getItem(VIEW_KEY) === "queue" ? "queue" : "board"; } catch { return "board"; }
}

// ---------- данные ----------
startPolling(
  (snap) => {
    state.snap = snap;
    state.offsetMs = Date.parse(snap.now) - Date.now();
    if (state.feedSeenSeq === null || snap.last_event_seq < state.feedSeenSeq) state.feedSeenSeq = snap.last_event_seq;
    const fresh = processEvents(snap, { toast: R.toast, notify, me: getMe(), quiet: !state.started });
    const until = Date.now() + HIGHLIGHT_MS;
    for (const e of fresh) {
      if (!CHANGE_TYPES.has(e.type)) continue;
      const prev = ui.changed.get(e.incident_id);
      const texts = prev && prev.until > Date.now() ? prev.texts : [];
      const t = e.text + (e.by ? ` (${e.by.split(" (")[0]})` : "");
      if (!texts.includes(t)) texts.push(t);
      ui.changed.set(e.incident_id, { until, texts: texts.slice(-3) });
    }
    if (fresh.some((e) => !e.alert && CHANGE_TYPES.has(e.type))) Sound.playTick();
    renderAll();
  },
  (online) => {
    state.online = online;
    if (online) state.lastOkMs = Date.now();
    R.renderStatus(state);
  },
);
api.meta().then((m) => { state.meta = m; renderAll(); }).catch(() => {});

F.mountFilters(filtersEl, { change: () => { ui.limits = {}; renderMain(); } });

function renderAll() {
  const snap = state.snap;
  if (!snap) return;
  const now = nowMs();
  state.alerts = computeAlerts(snap);
  R.renderCounters(snap, jumpTo, { compact: true });
  R.renderClock(snap, now);
  R.renderAlertLine(state.alerts, alertHandlers);
  renderMain();
  if (state.feedOpen) R.renderFeedList(snap, document.getElementById("feed-pop"), openFromFeed);
  if (state.openId) R.renderDrawer(snap.incidents[state.openId], snap, now, drawerHandlers, state.meta);
  if (state.started) state.escalated = ring(state.alerts, now, { notify, toast: R.toast });
  R.tickCountdowns(now);
  updateChrome();
}

// Доска или очередь + фильтры. Вызывается и при смене фильтров/поиска.
function renderMain() {
  const snap = state.snap;
  if (!snap) return;
  const now = nowMs();
  ui.predicate = F.makePredicate(snap, now, getMe());
  ui.selected = state.openId;
  ui.alarmIds = new Set(state.alerts.map((a) => a.incident_id));
  ui.actions = new Map();
  for (const a of snap.actions || []) if (!ui.actions.has(a.incident_id)) ui.actions.set(a.incident_id, a);

  const shown = R.renderBoard(snap, now, ui);
  const total = new Set(snap.groups.flatMap((g) => g.ids)).size;
  const shownUniq = ui.predicate || ui.query
    ? new Set(snap.groups.flatMap((g) => g.ids).filter(matchesAll)).size
    : total;
  F.renderFilters(filtersEl, snap, shownUniq, total);

  const queueEl = document.getElementById("queue");
  if (state.view === "queue") R.renderQueue(snap, queueEl, { copy, open: openDrawer, transfer: doTransfer, escalate: doEscalate }, ui.predicate);
  const qn = (snap.actions || []).length;
  document.getElementById("queue-count").textContent = qn ? String(qn) : "";
  document.getElementById("board").classList.toggle("hidden", state.view !== "board");
  queueEl.classList.toggle("hidden", state.view !== "queue");
  document.querySelectorAll(".tab[data-view]").forEach((t) => t.classList.toggle("active", t.dataset.view === state.view));

  const unseen = Math.max(0, snap.last_event_seq - (state.feedSeenSeq ?? snap.last_event_seq));
  document.getElementById("feed-count").textContent = unseen && !state.feedOpen ? String(Math.min(unseen, 99)) : "";
  R.tickCountdowns(now);
  return shown;
}

// Подходит ли инцидент под поиск и фильтры (для счётчика «показано X из Y», не зависит от порций)
function matchesAll(id) {
  const inc = state.snap.incidents[id];
  if (!inc) return false;
  if (ui.predicate && !ui.predicate(inc)) return false;
  if (!ui.query) return true;
  const hay = [inc.id, inc.title, inc.product, inc.service, inc.team, inc.assignee, inc.state, inc.priority].join(" ").toLowerCase();
  return ui.query.toLowerCase().split(/\s+/).every((w) => hay.includes(w));
}

// ---------- раз в секунду ----------
let blink = false;
setInterval(() => {
  if (!state.snap) return;
  const now = nowMs();
  R.tickCountdowns(now);
  R.renderClock(state.snap, now);
  let expired = false;
  for (const [id, c] of ui.changed) if (c.until <= Date.now()) { ui.changed.delete(id); expired = true; }
  if (expired) renderMain();
  if (state.started) {
    state.escalated = ring(state.alerts, now, { notify, toast: R.toast });
    watchOverdue(state.snap, now, { toast: R.toast });
  }
  blink = !blink;
  updateChrome();
}, 1000);
// «ждут N мин» в строке тревог
setInterval(() => { if (state.snap) R.renderAlertLine(state.alerts, alertHandlers); }, 15000);

function updateChrome() {
  const crit = state.alerts.filter((a) => a.group === "critical").length;
  const coord = state.alerts.filter((a) => a.group === "coordinators").length;
  const base = "АРМ координатора МИ";
  if (crit) document.title = blink ? `🔴 КРИТИЧЕСКИЙ (${crit}) — ${base}` : `⚠ ${crit} не взято — ${base}`;
  else if (coord) document.title = blink ? `🟣 На координаторах (${coord}) — ${base}` : base;
  else document.title = base;
  setFavicon(crit ? (blink ? "#ff0032" : "#ffffff") : coord ? "#a78bfa" : "#ff0032");
  document.getElementById("topbar").classList.toggle("escalated", state.escalated);
  const me = document.getElementById("me");
  const name = getMe();
  if (me) { me.textContent = name ? `👤 ${name.split(" ")[0]}` : "👤 представьтесь"; me.title = name ? `${name} · сменить имя` : "Представьтесь"; }
}

let lastFav = "";
function setFavicon(color) {
  if (color === lastFav) return;
  lastFav = color;
  const c = document.createElement("canvas");
  c.width = c.height = 32;
  const g = c.getContext("2d");
  g.fillStyle = color;
  g.fillRect(0, 0, 32, 32);
  g.fillStyle = color === "#ffffff" ? "#ff0032" : "#fff";
  g.font = "bold 11px Arial";
  g.fillText("МИ", 4, 27);
  document.getElementById("favicon").href = c.toDataURL();
}

// ---------- действия координатора ----------
function needName() {
  if (getMe()) return false;
  document.getElementById("start-overlay").classList.remove("hidden");
  document.getElementById("me-input").focus();
  return true;
}

async function act(fn, okText) {
  if (needName()) return;
  try {
    await fn();
    if (okText) R.toast(okText);
  } catch (e) {
    R.toast(`⚠ ${e.message}`, "bad");
  }
}

// «Взять в работу»: тревога снимается у всех. Уже у другого → предложить забрать себе.
async function doClaim(id, force) {
  if (needName()) return;
  try {
    await api.claim(id, force);
    R.toast(force ? "Забрали себе в работу" : "Взято в работу");
  } catch (e) {
    const who = e.status === 409 && e.data && e.data.coordinator;
    const name = who && (who.name || who.by || who);
    if (name && !force && confirm(`Инцидент уже в работе у ${name}. Забрать себе?`)) return doClaim(id, true);
    if (!name) R.toast(`⚠ ${e.message}`, "bad");
  }
}
const alertHandlers = { claim: (id) => doClaim(id), open: (id) => openDrawer(id) };
function doTransfer(id) {
  if (needName()) return;
  const inc = state.snap.incidents[id];
  if (inc) openTransfer(inc, state.snap, { copy, onDone: (t) => R.toast(`✔ ${t}`) });
}
function doEscalate(id, draft) {
  if (needName()) return;
  const inc = state.snap.incidents[id];
  if (!draft) {
    const a = (state.snap.actions || []).find((x) => x.incident_id === id && x.kind === "escalate");
    draft = a ? a.draft : null;
  }
  if (inc) openEscalate(inc, draft, { copy, onDone: (t) => R.toast(`✔ ${t}`) });
}

const drawerHandlers = {
  close: () => openDrawer(null),
  copy,
  open: (id) => openDrawer(id),
  get me() { return getMe(); },
  claim: (id, force) => doClaim(id, force),
  release: (id) => act(() => api.release(id), "Отпустили — инцидент снова свободен"),
  transfer: doTransfer,
  escalate: doEscalate,
  setState: (id, s) => act(() => api.state(id, s), `Статус: ${state.meta ? state.meta.states[s] : s}`),
  comment: (id, text) => act(() => api.comment(id, text), "Комментарий добавлен"),
};

function openDrawer(id) {
  state.openId = id && state.snap && state.snap.incidents[id] ? id : null;
  document.getElementById("workspace").classList.toggle("with-panel", !!state.openId);
  R.renderDrawer(state.openId ? state.snap.incidents[state.openId] : null, state.snap, nowMs(), drawerHandlers, state.meta);
  ui.selected = state.openId;
  document.querySelectorAll("#board .card").forEach((c) => c.classList.toggle("selected", c.dataset.id === state.openId));
  R.tickCountdowns(nowMs());
  if (state.view === "board" && state.snap) R.renderBoard(state.snap, nowMs(), ui);
}

function openFromFeed(id) {
  setFeed(false);
  openDrawer(id);
}

function setView(v) {
  state.view = v;
  try { localStorage.setItem(VIEW_KEY, v); } catch { /* приватный режим */ }
  renderMain();
}

function jumpTo(key) {
  if (state.view !== "board") setView("board");
  const col = document.getElementById(`col-${key}`);
  if (!col) return;
  col.scrollIntoView({ behavior: "smooth", inline: "nearest", block: "nearest" });
  col.classList.add("pulse-col");
  setTimeout(() => col.classList.remove("pulse-col"), 900);
}

async function copy(text, btn) {
  // ссылка в ITSM в черновике — полная, с адресом сервера, с которого открыт АРМ
  text = text.replace(/(^|\s)\/itsm\//g, `$1${location.origin}/itsm/`);
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta); ta.select();
    document.execCommand("copy"); ta.remove();
  }
  if (btn) { const old = btn.textContent; btn.textContent = "Скопировано ✓"; setTimeout(() => (btn.textContent = old), 1500); }
}

function notify(title, body, id, critical) {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  try {
    const n = new Notification(title, { body, tag: `${title}:${id}`, requireInteraction: !!critical });
    n.onclick = () => { window.focus(); openDrawer(id); n.close(); };
  } catch { /* браузер запретил */ }
}

// ---------- шапка: звук, голос, меню, лента ----------
Sound.loadPrefs("arm", { sound: true, voice: true });
const paintSound = Sound.bindToggles(document.getElementById("sound-btn"), document.getElementById("voice-btn"));

const menuPop = document.getElementById("menu-pop");
document.getElementById("menu-btn").onclick = (e) => { e.stopPropagation(); setFeed(false); menuPop.classList.toggle("hidden"); };
const feedPop = document.getElementById("feed-pop");
function setFeed(open) {
  state.feedOpen = open;
  feedPop.classList.toggle("hidden", !open);
  document.getElementById("feed-btn").classList.toggle("active", open);
  if (open && state.snap) {
    state.feedSeenSeq = state.snap.last_event_seq;
    R.renderFeedList(state.snap, feedPop, openFromFeed);
    const b = document.getElementById("feed-btn").getBoundingClientRect();
    feedPop.style.left = `${Math.max(8, Math.min(b.left, window.innerWidth - feedPop.offsetWidth - 8))}px`;
    feedPop.style.top = `${b.bottom + 6}px`;
  }
  if (state.snap) renderMain();
}
document.getElementById("feed-btn").onclick = (e) => { e.stopPropagation(); menuPop.classList.add("hidden"); setFeed(!state.feedOpen); };
feedPop.onclick = (e) => e.stopPropagation();
menuPop.onclick = (e) => e.stopPropagation();
document.addEventListener("click", () => { menuPop.classList.add("hidden"); if (state.feedOpen) setFeed(false); });
document.querySelectorAll(".tab[data-view]").forEach((t) => (t.onclick = () => setView(t.dataset.view)));

// ---------- начало смены ----------
const meInput = document.getElementById("me-input");
meInput.value = getMe();
function start(withSound) {
  const name = meInput.value.trim();
  if (!name) { meInput.focus(); meInput.classList.add("invalid"); return; }
  setMe(name);
  document.getElementById("start-overlay").classList.add("hidden");
  Sound.unlock();
  if (withSound) {
    if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
  } else {
    Sound.setSound(false);
    Sound.setVoice(false);
  }
  state.started = true;
  paintSound();
  setTimeout(paintSound, 1000);
  renderAll();
}
document.getElementById("start-btn").onclick = () => start(true);
document.getElementById("start-silent").onclick = () => start(false);
meInput.onkeydown = (e) => { if (e.key === "Enter") start(true); };
document.getElementById("me").onclick = () => {
  document.getElementById("start-overlay").classList.remove("hidden");
  meInput.focus();
};

// Импорт / экспорт файла инцидентов
document.getElementById("export-btn").onclick = () => { menuPop.classList.add("hidden"); window.location.href = "/api/export"; };
const importInput = document.getElementById("import-input");
document.getElementById("import-btn").onclick = () => { menuPop.classList.add("hidden"); importInput.click(); };
importInput.onchange = async () => {
  const f = importInput.files[0];
  importInput.value = "";
  if (!f) return;
  if (!confirm(`Заменить текущий файл инцидентов на «${f.name}»? Текущий будет сохранён в cache/backups.`)) return;
  try {
    const r = await fetch("/api/import", { method: "POST", body: await f.text() });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || r.status);
    R.toast(`✔ Загружено инцидентов: ${d.count}`);
  } catch (e) {
    R.toast(`⚠ Импорт не удался: ${e.message}`, "bad");
  }
};

const search = document.getElementById("search");
search.oninput = () => { ui.query = search.value.trim(); ui.limits = {}; renderMain(); };

// Горячие клавиши: В — взять в работу открытый инцидент (или верхнюю тревогу), Esc — закрыть, / — поиск
document.addEventListener("keydown", (e) => {
  const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName);
  if (e.key === "Escape") {
    if (!document.getElementById("modal").classList.contains("hidden")) closeModal();
    else if (typing) e.target.blur();
    else if (state.feedOpen) setFeed(false);
    else openDrawer(null);
  } else if (e.key === "/" && !typing) { e.preventDefault(); search.focus(); }
  else if (!typing && !e.ctrlKey && !e.metaKey && !e.altKey && ["в", "В", "d", "D"].includes(e.key)) {
    const id = state.openId || (state.alerts[0] && state.alerts[0].incident_id);
    const inc = id && state.snap && state.snap.incidents[id];
    if (inc && !R.ownerOf(inc, state.snap)) { e.preventDefault(); doClaim(id); }
  }
});
