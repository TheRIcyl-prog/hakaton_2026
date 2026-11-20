// Серверный поиск и фильтры по всем инцидентам (GET /api/search): в снимке только инциденты групп,
// остальные тысячи — здесь. Результаты — списком поверх колонок; ↑↓ выбрать, Enter открыть, Esc закрыть.
import { esc, fmtWhen, plural } from "./format.js";
import { cardTitle, ownerOf } from "./render.js";

const PAGE = 100;

// state: {open, title, params, items, total, offset, loading, error, sel}
const st = { open: false, title: "", params: null, items: [], total: 0, loading: false, error: "", sel: -1, reqId: 0 };
let handlers = { open: () => {}, claim: () => {}, close: () => {} };
const el = () => document.getElementById("results");

export async function searchTotal(params) {
  const p = new URLSearchParams(params);
  p.set("limit", "1");
  const r = await fetch(`/api/search?${p}`, { cache: "no-store" });
  if (!r.ok) throw new Error(`поиск: ${r.status}`);
  return (await r.json()).total;
}

async function load(append) {
  const id = ++st.reqId;
  st.loading = true;
  paint();
  const p = new URLSearchParams(st.params);
  p.set("limit", String(PAGE));
  p.set("offset", String(append ? st.items.length : 0));
  try {
    const r = await fetch(`/api/search?${p}`, { cache: "no-store" });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || r.status);
    if (id !== st.reqId) return; // пришёл ответ на старый запрос — пропускаем
    st.items = append ? st.items.concat(d.items) : d.items;
    st.total = d.total;
    st.error = "";
    if (!append) st.sel = -1;
  } catch (e) {
    if (id !== st.reqId) return;
    st.error = `Поиск не удался: ${e.message}`;
  }
  st.loading = false;
  paint();
}

export function mountResults(h) { handlers = h; }
export const isOpen = () => st.open;

// Открыть (или обновить) список: title — что ищем, params — параметры /api/search
export function showResults(title, params) {
  const same = st.open && JSON.stringify(params) === JSON.stringify(st.params);
  st.open = true;
  st.title = title;
  st.params = params;
  if (!same) { st.items = []; st.total = 0; }
  el().classList.remove("hidden");
  load(false);
}

// Тихо обновить открытый список (новый снимок): та же страница, без мигания
export function refreshResults() {
  if (st.open && !st.loading) load(false);
}

export function hideResults() {
  if (!st.open) return;
  st.open = false;
  st.reqId++;
  el().classList.add("hidden");
  el().innerHTML = "";
  handlers.close();
}

// ↑↓ Enter — вернёт true, если клавиша обработана
export function resultsKey(e) {
  if (!st.open || !st.items.length) return false;
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    st.sel = Math.max(0, Math.min(st.items.length - 1, st.sel + (e.key === "ArrowDown" ? 1 : -1)));
    paint();
    el().querySelector(".res-row.sel")?.scrollIntoView({ block: "nearest" });
    return true;
  }
  if (e.key === "Enter" && st.sel >= 0) { handlers.open(st.items[st.sel].id, st.items[st.sel]); return true; }
  return false;
}

export function selectedResult() {
  return st.open && st.sel >= 0 ? st.items[st.sel] : null;
}

function row(inc, i, nowMs, selectedId) {
  const t = cardTitle(inc);
  const owner = ownerOf(inc, null);
  const active = inc.state_kind === "active";
  const timer = active && inc.deadline
    ? `<span class="countdown" data-deadline="${esc(inc.deadline)}" data-start="${esc(inc.created_at || "")}" data-fc="${esc(inc.forecast ? inc.forecast.level : "")}"></span>`
    : active ? `<span class="no-deadline">срок не задан</span>` : `<span class="st st-${inc.state_kind}">${esc(inc.state || "")}</span>`;
  return `<div class="res-row ${i === st.sel ? "sel" : ""} ${inc.id === selectedId ? "open" : ""} ${inc.priority_level === "critical" && active ? "crit" : ""}" data-i="${i}">
    <span class="card-prio prio-${inc.priority_level}">${esc(inc.priority || "")}</span>
    <span class="res-main"><b>${esc(inc.product || "—")}</b> · ${esc(t.text)}
      <small>возник ${esc(fmtWhen(inc.created_at, nowMs))} · ${esc(inc.team || "—")} / ${inc.assignee ? esc(inc.assignee.split(" (")[0]) : "сотрудник не назначен"} · ${esc(inc.state || "")}</small></span>
    <span class="res-time">${timer}</span>
    <span class="res-owner">${owner ? `<span class="owner">в работе у ${esc(owner.by.split(" (")[0])}</span>`
      : active ? `<button class="btn small btn-take" data-claim="${esc(inc.id)}">Взять</button>` : ""}</span>
  </div>`;
}

export function paint(nowMs = Date.now(), selectedId = null) {
  if (!st.open) return;
  const box = el();
  const n = st.items.length;
  box.innerHTML = `
    <div class="res-head">
      <b>${esc(st.title)}</b>
      <span class="res-count">${st.loading && !n ? "ищем…" : `найдено ${st.total} ${plural(st.total, "инцидент", "инцидента", "инцидентов")}${n < st.total ? ` · показано ${n}` : ""}`}</span>
      <span class="res-hint">↑↓ выбрать · Enter открыть · Esc закрыть</span>
      <button class="btn small" data-act="close" title="Esc">✕</button>
    </div>
    ${st.error ? `<div class="res-error">${esc(st.error)}</div>` : ""}
    <div class="res-list">${n ? st.items.map((inc, i) => row(inc, i, nowMs, selectedId)).join("")
      : st.loading ? "" : `<div class="pop-empty">Ничего не найдено — попробуйте другой запрос или сбросьте фильтры</div>`}
      ${n < st.total ? `<button class="btn small res-more" data-act="more" ${st.loading ? "disabled" : ""}>Показать ещё ${Math.min(PAGE, st.total - n)} · осталось ${st.total - n}</button>` : ""}
    </div>`;
  box.querySelector("[data-act=close]").onclick = hideResults;
  const more = box.querySelector("[data-act=more]");
  if (more) more.onclick = () => load(true);
  box.querySelectorAll(".res-row").forEach((r) => {
    r.onclick = () => { st.sel = Number(r.dataset.i); handlers.open(st.items[st.sel].id, st.items[st.sel]); paint(nowMs, st.items[st.sel].id); };
  });
  box.querySelectorAll("[data-claim]").forEach((b) => (b.onclick = (e) => { e.stopPropagation(); handlers.claim(b.dataset.claim); }));
}
