import { esc, fmtTime } from "./format.js";

const urgency = document.getElementById("wall-urgency");
const owners = document.getElementById("wall-owners");
const feed = document.getElementById("feed");
const rendered = new WeakMap();

function paint(el, html) {
  if (rendered.get(el) === html) return;
  rendered.set(el, html);
  el.innerHTML = html;
}

export function paintWallOverview(snap, nowMs) {
  const ids = new Set((snap.groups || []).flatMap((group) => group.ids));
  let overdue = 0;
  let burning = 0;
  for (const id of ids) {
    const incident = snap.incidents[id];
    if (incident?.state_kind !== "active") continue;
    const remaining = Date.parse(incident.deadline) - nowMs;
    if (remaining <= 0) overdue++;
    else if (Number.isFinite(remaining) && (remaining <= 900000 || incident.forecast?.level === "breach")) burning++;
  }
  const activeOutages = (snap.outages || []).filter((outage) => outage.status === "active").length;
  paint(urgency, `<span>В группах</span><span>Просрочено <b>${overdue}</b></span>
    <span>Горит срок <b>${burning}</b></span><span>Активных сбоев <b>${activeOutages}</b></span>`);

  const counts = new Map((snap.coordinators || []).map((name) => [name, 0]));
  for (const claim of Object.values(snap.claims || {})) {
    if (claim.by) counts.set(claim.by, (counts.get(claim.by) || 0) + 1);
  }
  const ranked = [...counts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], "ru"));
  paint(owners, `<h2>Кто чем занят</h2><p>Активные в работе</p>${ranked.length
    ? `<ul>${ranked.slice(0, 12).map(([name, count]) => `<li><span title="${esc(name)}">${esc(name)}</span><b>${count}</b></li>`).join("")}</ul>`
    : `<p>Взятых инцидентов пока нет</p>`}${ranked.length > 12 ? `<p>Ещё ${ranked.length - 12}</p>` : ""}`);

  const events = (snap.events || []).filter((event) => {
    const age = nowMs - Date.parse(event.clock_at || event.at);
    return age >= 0 && age <= 600000;
  }).slice(-5).reverse();
  paint(feed, `<span class="feed-title">Последние 10 минут</span>${events.length
    ? events.map((event) => `<span class="wall-recent">${esc(fmtTime(event.clock_at || event.at))} <b>${esc(event.product || "")}</b> ${esc(event.text || "")}</span>`).join("")
    : `<span>Новых событий нет</span>`}`);
}
