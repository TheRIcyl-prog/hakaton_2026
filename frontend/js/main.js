// АРМ координатора: работа с инцидентами. Видеостена — wall.js.
import * as R from "./render.js";
import * as Sound from "./sound.js";
import { computeAlerts, processEvents, ring } from "./alerts.js";
import { api, getMe, setMe, startPolling } from "./api.js";
import { openTransfer, openEscalate, closeModal } from "./desk.js";

const HIGHLIGHT_MS = 15000;
const CHANGE_TYPES = new Set(["priority_changed", "deadline_changed", "team_changed", "assignee_changed", "status_changed",
  "became_critical", "to_coordinators", "from_coordinators", "resolved", "reopened", "critical_resolved", "new", "comment", "escalated"]);

const state = { snap: null, online: true, lastOkMs: 0, offsetMs: 0, openId: null, started: false, alerts: [], escalated: false, meta: null };
const ui = { query: "", changed: new Map(), open: (id) => openDrawer(id) };
const nowMs = () => Date.now() + state.offsetMs;

// ---------- данные ----------
startPolling(
  (snap) => {
    state.snap = snap;
    state.offsetMs = Date.parse(snap.now) - Date.now();
    const fresh = processEvents(snap, { toast: R.toast, notify });
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

function renderAll() {
  const snap = state.snap;
  if (!snap) return;
  const now = nowMs();
  state.alerts = computeAlerts(snap);
  R.renderCounters(snap, jumpTo);
  R.renderClock(snap, now);
  R.renderAlerts(state.alerts, now, { ack: doAck, ackAll: doAckAll, open: openDrawer });
  R.renderActions(snap, { copy, open: openDrawer, transfer: doTransfer, escalate: doEscalate });
  R.renderBoard(snap, now, ui);
  R.renderFeed(snap, openDrawer);
  if (state.openId) R.renderDrawer(snap.incidents[state.openId], snap, now, drawerHandlers, state.meta);
  if (state.started) state.escalated = ring(state.alerts, now, { notify });
  R.tickCountdowns(now);
  updateChrome();
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
  if (expired) R.renderBoard(state.snap, now, ui);
  if (state.started) state.escalated = ring(state.alerts, now, { notify });
  blink = !blink;
  updateChrome();
}, 1000);

function updateChrome() {
  const crit = state.alerts.filter((a) => a.group === "critical").length;
  const coord = state.alerts.filter((a) => a.group === "coordinators").length;
  const base = "АРМ координатора МИ";
  if (crit) document.title = blink ? `🔴 КРИТИЧЕСКИЙ (${crit}) — ${base}` : `⚠ ${crit} не принято — ${base}`;
  else if (coord) document.title = blink ? `🟣 На координаторах (${coord}) — ${base}` : base;
  else document.title = base;
  setFavicon(crit ? (blink ? "#ff0032" : "#ffffff") : coord ? "#a78bfa" : "#ff0032");
  document.getElementById("topbar").classList.toggle("escalated", state.escalated);
  const me = document.getElementById("me");
  if (me) me.textContent = getMe() ? `👤 ${getMe()}` : "👤 представьтесь";
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

const doAck = (key) => act(() => api.ack(key));
const doAckAll = (keys) => act(() => Promise.all(keys.map((k) => api.ack(k))));
function doTransfer(id) {
  if (needName()) return;
  const inc = state.snap.incidents[id];
  if (inc) openTransfer(inc, state.snap, { copy, onDone: (t) => R.toast(`✔ ${t}`) });
}
function doEscalate(id, draft) {
  if (needName()) return;
  const inc = state.snap.incidents[id];
  if (inc) openEscalate(inc, draft, { copy, onDone: (t) => R.toast(`✔ ${t}`) });
}

const drawerHandlers = {
  close: () => openDrawer(null),
  copy,
  open: (id) => openDrawer(id),
  claim: (id) => act(() => api.claim(id), "Взято в работу"),
  release: (id) => act(() => api.release(id)),
  transfer: doTransfer,
  escalate: doEscalate,
  setState: (id, s) => act(() => api.state(id, s), `Статус: ${state.meta ? state.meta.states[s] : s}`),
  comment: (id, text) => act(() => api.comment(id, text), "Комментарий добавлен"),
};

function openDrawer(id) {
  state.openId = id && state.snap && state.snap.incidents[id] ? id : null;
  R.renderDrawer(state.openId ? state.snap.incidents[state.openId] : null, state.snap, nowMs(), drawerHandlers, state.meta);
  R.tickCountdowns(nowMs());
}

function jumpTo(key) {
  const col = document.getElementById(`col-${key}`);
  if (!col) return;
  col.scrollIntoView({ behavior: "smooth", inline: "start" });
  col.style.outline = "2px solid var(--muted)";
  setTimeout(() => (col.style.outline = ""), 800);
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

// ---------- начало смены ----------
const meInput = document.getElementById("me-input");
meInput.value = getMe();
function start(withSound) {
  const name = meInput.value.trim();
  if (!name) { meInput.focus(); meInput.classList.add("invalid"); return; }
  setMe(name);
  document.getElementById("start-overlay").classList.add("hidden");
  if (withSound) {
    Sound.initAudio();
    if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
  } else {
    Sound.setEnabled(false);
  }
  state.started = true;
  R.renderSoundBtn(Sound.isEnabled(), Sound.hasVoice());
  setTimeout(() => R.renderSoundBtn(Sound.isEnabled(), Sound.hasVoice()), 1000);
  renderAll();
}
document.getElementById("start-btn").onclick = () => start(true);
document.getElementById("start-silent").onclick = () => start(false);
meInput.onkeydown = (e) => { if (e.key === "Enter") start(true); };
document.getElementById("me").onclick = () => {
  document.getElementById("start-overlay").classList.remove("hidden");
  meInput.focus();
};
document.getElementById("sound-btn").onclick = () => {
  if (!Sound.isEnabled()) Sound.initAudio(); else Sound.setEnabled(false);
  R.renderSoundBtn(Sound.isEnabled(), Sound.hasVoice());
  if (Sound.isEnabled()) Sound.playTick();
};

// Импорт / экспорт файла инцидентов
document.getElementById("export-btn").onclick = () => { window.location.href = "/api/export"; };
const importInput = document.getElementById("import-input");
document.getElementById("import-btn").onclick = () => importInput.click();
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
search.oninput = () => { ui.query = search.value.trim(); if (state.snap) R.renderBoard(state.snap, nowMs(), ui); R.tickCountdowns(nowMs()); };

// Горячие клавиши: Пробел — принять верхнюю тревогу, Esc — закрыть, / — поиск
document.addEventListener("keydown", (e) => {
  const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName);
  if (e.key === "Escape") {
    if (!document.getElementById("modal").classList.contains("hidden")) closeModal();
    else if (typing) e.target.blur();
    else openDrawer(null);
  } else if (e.key === "/" && !typing) { e.preventDefault(); search.focus(); }
  else if (e.key === " " && !typing && state.started && state.alerts.length) { e.preventDefault(); doAck(state.alerts[0].key); }
});
