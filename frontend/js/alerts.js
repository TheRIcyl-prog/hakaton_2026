// Тревоги приходят с сервера (общие для стены и всех АРМ): эпизод начинается, когда инцидент
// входит в «критические» или «на координаторах», и висит, пока кто-то не возьмёт инцидент в работу
// (кнопки «Принял» нет — decisions.md, п. 4; «в работе у …» — поле coordinator в файле).
// Здесь — только звук, голос и системные уведомления (лестница эскалации).
import { playCritical, playCoordinators, playResolved, playTick, speak } from "./sound.js";
import { speakLeft, plural, sameCoordinator } from "./format.js";
import { aiEnabled } from "./ai-mode.js";

const MIN = 60_000;
const REPEAT_CRITICAL_MS = MIN;      // повтор звука: критический — раз в минуту
const REPEAT_COORD_MS = 2 * MIN;     // на координаторах — раз в 2 минуты
const ESCALATE_AFTER_MS = 2 * MIN;   // через 2 минуты — громче и мигает шапка
const VOICE_STEP_MS = 5 * MIN;       // каждые 5 минут — голос «не взят уже N минут»
const BATCH = 3;                     // больше трёх новых разом — одна сводная фраза

const firstSeen = new Map(); // key -> ms (локально), когда тревогу впервые увидели в этой вкладке
const lastRing = new Map();
const voicedStep = new Map(); // key -> сколько 5-минутных ступеней уже озвучено
let processedSeq = null;
let bootId = null;

const ownerName = (inc) => (inc && inc.coordinator && inc.coordinator.name) || "";
const dupKey = (inc) => (inc && inc.ai && inc.ai.duplicates && inc.ai.duplicates.group_id) || (inc && inc.id);

// Тревоги, которые никто не взял в работу: критические первыми
export function computeAlerts(snap) {
  return (snap.alerts || [])
    .filter((a) => !a.taken_by && snap.incidents[a.incident_id] && !ownerName(snap.incidents[a.incident_id]))
    .map((a) => ({ ...a, inc: snap.incidents[a.incident_id], useAI: aiEnabled(snap) }));
}

// Новые события: короткий звук на «критический решён»; «уже в работе и стал критическим» —
// громко только у владельца, остальным тихий тост. opts = {toast, notify, me, quiet}. Возвращает свежие события.
export function processEvents(snap, opts) {
  if (processedSeq === null || snap.boot_id !== bootId) {
    bootId = snap.boot_id; // при открытии и после перезапуска сервера старые события не озвучиваем
    processedSeq = snap.last_event_seq;
    return [];
  }
  const fresh = (snap.events || []).filter((e) => e.seq > processedSeq);
  processedSeq = Math.max(processedSeq, snap.last_event_seq);
  if (opts.quiet) return fresh;
  for (const e of fresh) {
    if (e.type === "critical_resolved") {
      playResolved();
      speak(`Критический инцидент решён. ${e.product || ""}`, { delay: 700 });
      opts.toast(`Критический решён: ${e.product || ""} · ${e.title || ""}${e.by ? " · " + e.by.split(" (")[0] : ""}`, "green");
      opts.notify("Критический инцидент решён", `${e.product || ""} · ${e.title || ""}`, e.incident_id, false);
    } else if (e.type === "became_critical") {
      const owner = ownerName(snap.incidents[e.incident_id]);
      if (!owner) continue; // не взят — громкая тревога придёт через alerts
      if (sameCoordinator(owner, opts.me)) {
        playCritical(true);
        speak(`Внимание! Ваш инцидент стал критическим. ${e.product || ""}. ${e.title || ""}.`, { delay: 1400, urgent: true });
        opts.toast(`Ваш инцидент стал критическим: ${e.product || ""} · ${e.title || ""}`, "bad");
        opts.notify("Ваш инцидент стал КРИТИЧЕСКИМ", `${e.product || ""} · ${e.title || ""}`, e.incident_id, true);
      } else {
        opts.toast(`Стал критическим (в работе: ${owner}): ${e.product || ""} · ${e.title || ""}`);
      }
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
  const route = a.useAI !== false && inc.ai?.route;
  return `На координаторов назначен инцидент. ${inc.product || ""}. ${route ? "Рекомендация: передать в " + route.team : ""}`;
}

// Сводка, когда новых тревог больше трёх: «Новых критических: 30, сбоев: 25. Первый: …»
function summaryFor(fresh) {
  const crit = fresh.filter((a) => a.group === "critical");
  const coord = fresh.filter((a) => a.group === "coordinators");
  const parts = [];
  if (crit.length) {
    const failures = new Set(crit.map((a) => dupKey(a.inc))).size;
    const f = crit[0].inc;
    parts.push(`Новых критических: ${crit.length}${failures < crit.length ? `, из них сбоев: ${failures}` : ""}. Первый: ${f.product || ""}, ${f.title || ""}.`);
  }
  if (coord.length) parts.push(`На координаторов назначено: ${coord.length}.`);
  return parts.join(" ");
}

// Лестница: звук+голос сразу; повтор звука (крит. — раз в минуту, коорд. — раз в 2 мин);
// через 2 минуты — громче и мигает шапка; каждые 5 минут — голос «не взят уже N минут».
// opts = {voice, notify, toast, onlyCritical}. Возвращает true, если пора мигать.
export function ring(allAlerts, nowMs, opts = {}) {
  const alerts = opts.onlyCritical ? allAlerts.filter((a) => a.group === "critical") : allAlerts;
  const keys = new Set(alerts.map((a) => a.key));
  for (const k of [...firstSeen.keys()]) if (!keys.has(k)) { firstSeen.delete(k); lastRing.delete(k); voicedStep.delete(k); }
  const now = Date.now();
  const fresh = [], steps = [];
  let escalated = false, dueCrit = false, dueCoord = false;
  for (const a of alerts) {
    const isNew = !firstSeen.has(a.key);
    if (isNew) firstSeen.set(a.key, now);
    // возраст считаем от начала эпизода на сервере — одинаково на стене и всех АРМ
    const age = Math.max(now - firstSeen.get(a.key), a.since ? now - Date.parse(a.since) : 0);
    const crit = a.group === "critical";
    if (crit && age >= ESCALATE_AFTER_MS) escalated = true;
    const step = Math.floor(age / VOICE_STEP_MS);
    if (isNew) {
      fresh.push(a);
      voicedStep.set(a.key, step); // открыли АРМ, а тревога висит 12 мин — следующая ступень на 15-й минуте
      lastRing.set(a.key, now);
      if (crit) dueCrit = true; else dueCoord = true;
      continue;
    }
    if (crit && step > (voicedStep.get(a.key) || 0)) { voicedStep.set(a.key, step); steps.push({ a, min: step * 5 }); }
    if (now - (lastRing.get(a.key) || 0) >= (crit ? REPEAT_CRITICAL_MS : REPEAT_COORD_MS)) {
      lastRing.set(a.key, now);
      if (crit) dueCrit = true; else dueCoord = true;
    }
  }

  // один звук на тик, критический важнее
  if (dueCrit) playCritical(escalated);
  else if (dueCoord) playCoordinators();

  if (opts.voice !== false) {
    if (fresh.length > BATCH) speak(summaryFor(fresh), { delay: 1400, urgent: true });
    else for (const a of fresh) speak(voiceFor(a, nowMs), { delay: a.group === "critical" ? 1400 : 1000, urgent: a.group === "critical" });
    if (steps.length === 1) {
      const { a, min } = steps[0];
      speak(`Критический инцидент не взят уже ${min} ${plural(min, "минуту", "минуты", "минут")}. ${a.inc.product || ""}.`, { delay: 1400, urgent: true });
    } else if (steps.length > 1) {
      speak(`${steps.length} ${plural(steps.length, "критический инцидент не взят", "критических инцидента не взяты", "критических инцидентов не взяты")} уже 5 минут и больше.`, { delay: 1400, urgent: true });
    }
  }

  if (fresh.length > BATCH) {
    const n = fresh.length;
    if (opts.notify) opts.notify(`${n} ${plural(n, "новая тревога", "новые тревоги", "новых тревог")}`, summaryFor(fresh), fresh[0].inc.id, fresh.some((a) => a.group === "critical"));
    if (opts.toast) opts.toast(`${n} ${plural(n, "новая тревога", "новые тревоги", "новых тревог")}`, "bad");
  } else if (opts.notify) {
    for (const a of fresh) {
      const title = a.group === "critical" ? "КРИТИЧЕСКИЙ инцидент" : "Назначен на координаторов";
      opts.notify(title, `${a.inc.product || ""} · ${a.inc.title || ""}`, a.inc.id, a.group === "critical");
    }
  }
  return escalated;
}

// Тихая тревога: срок активного критического только что нарушен — короткий звук и тост (один раз).
const overdueSeen = new Set();
let overdueReady = false;
export function watchOverdue(snap, nowMs, opts) {
  const crit = (snap.groups.find((g) => g.key === "critical") || { ids: [] }).ids
    .map((id) => snap.incidents[id])
    // только что нарушен (за последние 2 минуты): стал критическим уже просроченным — это не «сорвался срок»
    .filter((inc) => inc && inc.state_kind === "active" && inc.deadline && Date.parse(inc.deadline) < nowMs);
  for (const inc of crit) {
    if (overdueSeen.has(inc.id)) continue;
    overdueSeen.add(inc.id);
    if (!overdueReady || nowMs - Date.parse(inc.deadline) > 2 * MIN) continue; // старые просрочки не озвучиваем
    playTick();
    opts.toast(`Срок критического нарушен: ${inc.product || ""} · ${inc.title || ""}`, "bad");
  }
  overdueReady = true;
}
