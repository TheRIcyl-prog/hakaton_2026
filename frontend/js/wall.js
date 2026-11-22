// Видеостена: общий экран над рабочими местами. Только просмотр — действия делают на АРМ.
// Вёрстка рассчитана на 1600 px ширины и масштабируется (zoom) под любой экран, хоть 4K.
import * as R from "./render.js";
import * as Sound from "./sound.js";
import { computeAlerts, processEvents, ring } from "./alerts.js";
import { startPolling } from "./api.js";
import { renderWallOutages, tickOutageGrowth } from "./outages.js";
import { paintWallOverview } from "./wall-overview.js";

const HIGHLIGHT_MS = 20000;
const DESIGN_WIDTH = 1600;
const outagesEl = document.getElementById("wall-outages");
const state = { snap: null, online: true, lastOkMs: 0, offsetMs: 0, alerts: [] };
const ui = { query: "", changed: new Map(), wall: true, alarmIds: new Set() };
const nowMs = () => Date.now() + state.offsetMs;

function scale() {
  const z = Math.max(0.6, window.innerWidth / DESIGN_WIDTH);
  document.body.style.zoom = String(z);
  document.body.style.height = `${window.innerHeight / z}px`; // 100vh под zoom растягивается — задаём высоту явно
  if (state.snap) R.fitColumns();
}
window.addEventListener("resize", scale);
scale();

startPolling(
  (snap) => {
    state.snap = snap;
    state.offsetMs = Date.parse(snap.now) - Date.now();
    const fresh = processEvents(snap, { toast: R.toast, notify: () => {} });
    const until = Date.now() + HIGHLIGHT_MS;
    for (const e of fresh) {
      if (["removed"].includes(e.type)) continue;
      const prev = ui.changed.get(e.incident_id);
      const texts = prev && prev.until > Date.now() ? prev.texts : [];
      const t = e.text + (e.by ? ` (${e.by.split(" (")[0]})` : "");
      if (!texts.includes(t)) texts.push(t);
      ui.changed.set(e.incident_id, { until, texts: texts.slice(-2) });
    }
    render();
  },
  (online) => {
    state.online = online;
    if (online) state.lastOkMs = Date.now();
    R.renderStatus(state);
  },
);

function render() {
  const snap = state.snap;
  if (!snap) return;
  const now = nowMs();
  // стена визуально показывает только критические тревоги
  state.alerts = computeAlerts(snap).filter((a) => a.group === "critical");
  ui.alarmIds = new Set(state.alerts.map((a) => a.incident_id));
  R.renderCounters(snap, null, { wall: true });
  R.renderClock(snap, now);
  renderBanner(now);
  renderWallOutages(outagesEl, snap, ui.alarmIds);
  tickOutageGrowth(now);
  paintWallOverview(snap, now);
  R.renderBoard(snap, now, ui);
  R.tickCountdowns(now);
  ring(state.alerts, now, { voice: true, onlyCritical: true }); // звук и голос молчат, если выключены (по умолчанию)
}

let bannerShown = "";
function renderBanner(now) {
  const sig = JSON.stringify(state.alerts.map((a) => [a.key, a.inc.hash]));
  if (sig === bannerShown) return;
  bannerShown = sig;
  R.renderWallBanner(state.alerts, now);
  R.fitColumns(); // полоса съедает высоту — пересчитать «ещё N»
}

let blink = false;
setInterval(() => {
  if (!state.snap) return;
  const now = nowMs();
  tickOutageGrowth(now);
  R.tickCountdowns(now);
  R.renderClock(state.snap, now);
  paintWallOverview(state.snap, now);
  let expired = false;
  for (const [id, c] of ui.changed) if (c.until <= Date.now()) { ui.changed.delete(id); expired = true; }
  if (expired) R.renderBoard(state.snap, now, ui);
  renderBanner(now);
  ring(state.alerts, now, { voice: true, onlyCritical: true });
  blink = !blink;
  const crit = state.alerts.filter((a) => a.group === "critical").length;
  document.title = crit && blink ? `КРИТИЧЕСКИЙ (${crit}) — видеостена` : "Видеостена · массовые инциденты";
}, 1000);

// Звук и голос на стене — отдельные переключатели, по умолчанию выключены (обычно звук — в гарнитурах на АРМ).
// Выбор запоминается; после перезагрузки браузер даст звук только после первого клика по странице.
Sound.loadPrefs("wall", { sound: false, voice: false });
const paintSound = Sound.bindToggles(document.getElementById("sound-btn"), document.getElementById("voice-btn"));
function unlockOnce() {
  if (!Sound.isUnlocked()) { Sound.unlock(); paintSound(); setTimeout(paintSound, 800); }
  document.removeEventListener("pointerdown", unlockOnce);
  document.removeEventListener("keydown", unlockOnce);
}
document.addEventListener("pointerdown", unlockOnce);
document.addEventListener("keydown", unlockOnce);

// Курсор не мешает на большом экране
let hideTimer;
document.addEventListener("mousemove", () => {
  document.body.classList.remove("no-cursor");
  clearTimeout(hideTimer);
  hideTimer = setTimeout(() => document.body.classList.add("no-cursor"), 4000);
});
