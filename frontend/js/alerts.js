// Тревоги приходят с сервера (общие для стены и всех АРМ): эпизод начинается, когда инцидент
// входит в «критические» или «на координаторах», и висит, пока кто-то не возьмёт инцидент в работу
// (кнопки «Принял» нет — decisions.md, п. 4; «в работе у …» — поле coordinator в файле).
// Здесь — только звук, голос и системные уведомления (лестница эскалации).
import { playCritical, playCoordinators, playResolved, speak } from "./sound.js";
import { speakLeft } from "./format.js";

const REPEAT_CRITICAL_MS = 60_000;
const REPEAT_COORD_MS = 120_000;
const ESCALATE_AFTER_MS = 120_000;

const firstSeen = new Map(); // key -> ms (локально), когда тревогу впервые увидели в этой вкладке
const lastRing = new Map();
let processedSeq = null;
let bootId = null;

// Тревоги, которые никто не взял в работу: критические первыми
export function computeAlerts(snap) {
  return (snap.alerts || [])
    .filter((a) => !a.taken_by && snap.incidents[a.incident_id] && !(snap.incidents[a.incident_id].coordinator))
    .map((a) => ({ ...a, inc: snap.incidents[a.incident_id] }));
}

// Новые события: звук и тост на «критический решён». Возвращает свежие события.
export function processEvents(snap, opts) {
  if (processedSeq === null || snap.boot_id !== bootId) {
    bootId = snap.boot_id; // при открытии и после перезапуска сервера старые события не озвучиваем
    processedSeq = snap.last_event_seq;
    return [];
  }
  const fresh = (snap.events || []).filter((e) => e.seq > processedSeq);
  processedSeq = Math.max(processedSeq, snap.last_event_seq);
  for (const e of fresh) {
    if (e.type === "critical_resolved") {
      playResolved();
      speak(`Критический инцидент решён. ${e.product || ""}`, { delay: 700 });
      opts.toast(`✅ Критический решён: ${e.incident_id} · ${e.product || ""}${e.by ? " · " + e.by : ""}`, "green");
      opts.notify("Критический инцидент решён", `${e.incident_id} · ${e.title || ""}`, e.incident_id, false);
    }
  }
  return fresh;
}

function remaining(inc, nowMs) {
  return inc.deadline ? (Date.parse(inc.deadline) - nowMs) / 1000 : null;
}

function voiceFor(a, nowMs) {
  const inc = a.inc;
  if (a.group === "critical") {
    return `Внимание! Критический инцидент. ${inc.product || ""}. ${inc.title || ""}. ${speakLeft(remaining(inc, nowMs))}.`;
  }
  const route = inc.ai && inc.ai.route;
  return `На координаторов назначен инцидент. ${inc.product || ""}. ${route ? "Рекомендация: передать в " + route.team : ""}`;
}

// Лестница: звук+голос сразу, повтор (крит. — раз в минуту, коорд. — раз в 2 мин),
// через 2 минуты, если никто не взял, — громче и мигает шапка. Возвращает true, если пора мигать.
export function ring(alerts, nowMs, opts) {
  const keys = new Set(alerts.map((a) => a.key));
  for (const k of [...firstSeen.keys()]) if (!keys.has(k)) { firstSeen.delete(k); lastRing.delete(k); }
  let escalated = false, rangCritical = false, rangCoord = false;
  for (const a of alerts) {
    const isNew = !firstSeen.has(a.key);
    if (isNew) firstSeen.set(a.key, Date.now());
    // возраст считаем от начала эпизода на сервере — одинаково на стене и АРМ
    const age = Math.max(Date.now() - firstSeen.get(a.key), a.since ? Date.now() - Date.parse(a.since) : 0);
    const repeat = a.group === "critical" ? REPEAT_CRITICAL_MS : REPEAT_COORD_MS;
    const due = isNew || Date.now() - (lastRing.get(a.key) || 0) >= repeat;
    if (a.group === "critical" && age >= ESCALATE_AFTER_MS) escalated = true;
    if (!due) continue;
    lastRing.set(a.key, Date.now());
    if (a.group === "critical" && !rangCritical) { playCritical(age >= ESCALATE_AFTER_MS); rangCritical = true; }
    if (a.group === "coordinators" && !rangCoord && !rangCritical) { playCoordinators(); rangCoord = true; }
    if (opts.voice !== false) speak(voiceFor(a, nowMs), { delay: a.group === "critical" ? 1400 : 1000, urgent: a.group === "critical" });
    if (isNew && opts.notify) {
      const title = a.group === "critical" ? "КРИТИЧЕСКИЙ инцидент" : "Назначен на координаторов";
      opts.notify(title, `${a.inc.id} · ${a.inc.product || ""} · ${a.inc.title || ""}`, a.inc.id, a.group === "critical");
    }
  }
  return escalated;
}
