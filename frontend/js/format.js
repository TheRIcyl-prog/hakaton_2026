// Время, сроки и склонения. Всё показываем в МСК.
const MSK_OFFSET_MIN = 180;

export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

export function sameCoordinator(owner, name) {
  return !!(owner && name && owner.trim().toLowerCase() === name.trim().toLowerCase());
}

function msk(ms) {
  return new Date(ms + MSK_OFFSET_MIN * 60000);
}

const pad = (n) => String(n).padStart(2, "0");

export function fmtTime(iso) {
  if (!iso) return "—";
  const d = msk(Date.parse(iso));
  return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}

export function fmtDateTime(iso, nowMs) {
  if (!iso) return "—";
  const ms = Date.parse(iso);
  const d = msk(ms);
  const time = `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
  if (nowMs) {
    const n = msk(nowMs);
    if (n.getUTCFullYear() === d.getUTCFullYear() && n.getUTCMonth() === d.getUTCMonth()) {
      const diff = n.getUTCDate() - d.getUTCDate();
      if (diff === 0) return `сегодня ${time}`;
      if (diff === 1) return `вчера ${time}`;
    }
  }
  return `${pad(d.getUTCDate())}.${pad(d.getUTCMonth() + 1)} ${time}`;
}

// «14:05» сегодня, «вчера 21:42», иначе «18.11 21:42» — для «возник» на карточке
export function fmtWhen(iso, nowMs) {
  const t = fmtDateTime(iso, nowMs);
  return t.startsWith("сегодня ") ? t.slice(8) : t;
}

// Таймер карточки: «осталось 42:10» (последний час посекундно), «осталось 5 ч 02 мин», «осталось 2 дня»,
// «просрочен на 12 мин», «просрочен на 2 ч 05 мин», «просрочен на 3 дня»
export function fmtTimer(sec) {
  const neg = sec < 0;
  const s = Math.abs(Math.floor(sec));
  const d = Math.floor(s / 86400), h = Math.floor(s / 3600), m = Math.floor(s / 60);
  let out;
  if (d >= 1) out = `${d} ${plural(d, "день", "дня", "дней")}${!neg && d < 3 && h % 24 ? ` ${h % 24} ч` : ""}`;
  else if (h >= 1) out = `${h} ч ${pad(m % 60)} мин`;
  else if (neg) out = `${Math.max(1, m)} мин`;
  else out = `${m}:${pad(s % 60)}`;
  return neg ? `просрочен на ${out}` : `осталось ${out}`;
}

export function plural(n, one, few, many) {
  const a = Math.abs(n) % 100, b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b > 1 && b < 5) return few;
  if (b === 1) return one;
  return many;
}

// «2 ч 05 мин» / «42:10» / «−1 ч 20 мин»
export function fmtLeft(sec) {
  const neg = sec < 0;
  let s = Math.abs(Math.floor(sec));
  const d = Math.floor(s / 86400); s -= d * 86400;
  const h = Math.floor(s / 3600); s -= h * 3600;
  const m = Math.floor(s / 60); s -= m * 60;
  let out;
  if (d > 0) out = `${d} д ${h} ч`;
  else if (h > 0) out = `${h} ч ${pad(m)} мин`;
  else out = `${m}:${pad(s)}`;
  return (neg ? "−" : "") + out;
}

// Для голоса: «45 минут», «1 час 20 минут»
export function speakLeft(sec) {
  if (sec == null) return "срок не задан";
  const neg = sec < 0;
  const total = Math.round(Math.abs(sec) / 60);
  const h = Math.floor(total / 60), m = total % 60;
  const parts = [];
  if (h) parts.push(`${h} ${plural(h, "час", "часа", "часов")}`);
  if (m || !h) parts.push(`${m} ${plural(m, "минута", "минуты", "минут")}`);
  return neg ? `срок нарушен на ${parts.join(" ")}` : `до срока ${parts.join(" ")}`;
}

export function shortId(id) {
  return id ? id.replace(/^INC0+/, "INC…") : "";
}
