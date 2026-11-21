// Фильтры АРМ: продукт, команда, исполнитель, «мои», просроченные, горит срок и др.
// Выбор запоминается на каждом АРМ. На стене фильтров нет — она всегда показывает всё.
// Колонки фильтруются в браузере (в снимке все инциденты групп), а «всего по фильтру» и список
// по всем тысячам — сервер (GET /api/search, параметры — toParams).
import { esc } from "./format.js";
import { aiEnabled } from "./ai-mode.js";

const KEY = "mi-dashboard:filters";
const HOUR = 3600_000;

const EMPTY = {
  product: "", team: "", person: "", stype: "",
  mine: false, overdue: false, burning: false,
  unacked: false, noassignee: false, risk: false, dups: false, fresh: false,
};

// Переключатели: подпись чипа и проверка. ctx = {snap, now, me, unacked:Set}
const TOGGLES = {
  mine: { label: "Мои", hint: "в работе у меня", test: (inc, c) => !!c.me && claimBy(c, inc) === c.me },
  overdue: { label: "Просроченные", hint: "срок уже нарушен", test: (inc, c) => isActive(inc) && inc.deadline && Date.parse(inc.deadline) < c.now },
  burning: { label: "Горит срок", hint: "прогноз: не успеют", test: (inc) => isActive(inc) && inc.forecast && ["breach", "overdue"].includes(inc.forecast.level) },
  unacked: { label: "Не взятые в работу", hint: "тревога, и никто не взял в работу", test: (inc, c) => c.unacked.has(inc.id), more: true },
  noassignee: { label: "Без исполнителя", hint: "не назначен инженер", test: (inc) => !inc.assignee, more: true },
  risk: { label: "Высокий риск по ИИ", hint: "может стать критическим", test: (inc) => !!(inc.ai && inc.ai.risk && inc.ai.risk.level === "high"), more: true },
  dups: { label: "Дубли и массовые", hint: "мастер, дубль или найден ИИ", test: (inc) => isDup(inc), more: true },
  fresh: { label: "Новые за час", hint: "возникли за последний час", test: (inc, c) => inc.created_at && c.now - Date.parse(inc.created_at) <= HOUR, more: true },
};

const isActive = (inc) => inc.state_kind === "active";
const claimBy = (c, inc) => (inc.coordinator && inc.coordinator.name) || ((c.snap.claims || {})[inc.id] || {}).by || "";
export function isDup(inc) {
  const d = inc.ai && inc.ai.duplicates;
  const n = Array.isArray(d) ? d.length : d && d.ids ? d.ids.length : 0;
  return !!(inc.is_master || inc.is_double || n);
}

let f = load();
let onChange = () => {};
let lastOpts = "";
let allowAI = true;

export function syncFilterMode(snap) {
  const changed = allowAI !== aiEnabled(snap);
  allowAI = aiEnabled(snap);
  return changed && f.risk;
}

function load() {
  try { return { ...EMPTY, ...JSON.parse(localStorage.getItem(KEY) || "{}") }; } catch { return { ...EMPTY }; }
}
function save() {
  try { localStorage.setItem(KEY, JSON.stringify(f)); } catch { /* приватный режим */ }
}

export function isActiveFilter() {
  return Object.keys(EMPTY).some((k) => (k !== "risk" || allowAI) && f[k] !== EMPTY[k]);
}

// Предикат для инцидента
export function makePredicate(snap, now, me) {
  if (!isActiveFilter()) return null;
  const c = { snap, now, me, unacked: new Set((snap.alerts || []).filter((a) => !a.taken_by).map((a) => a.incident_id)) };
  const on = Object.keys(TOGGLES).filter((k) => f[k] && (k !== "risk" || allowAI));
  return (inc) => {
    if (!inc) return false;
    if (f.product && inc.product !== f.product) return false;
    if (f.team && inc.team !== f.team) return false;
    if (f.stype && (f.stype === "client" ? inc.service_type !== "Клиентский" : inc.service_type === "Клиентский")) return false;
    if (f.person) {
      const [kind, name] = [f.person.slice(0, 2), f.person.slice(2)];
      if (kind === "c:" && claimBy(c, inc) !== name) return false;
      if (kind === "a:" && inc.assignee !== name) return false;
    }
    return on.every((k) => TOGGLES[k].test(inc, c));
  };
}

// Те же фильтры параметрами /api/search (по всем инцидентам, только активные)
export function toParams(me) {
  const p = { status: "active" };
  if (f.product) p.product = f.product;
  if (f.team) p.team = f.team;
  if (f.stype) p.service_type = f.stype;
  if (f.person) {
    const [kind, name] = [f.person.slice(0, 2), f.person.slice(2)];
    if (kind === "c:") p.coordinator = name;
    else p.q = name.split(" (")[0];
  }
  if (f.mine) p.coordinator = me || "—";
  if (f.overdue) p.overdue = 1;
  if (f.burning) p.burning = 1;
  if (f.unacked) p.untaken = 1;
  if (f.noassignee) p.unassigned = 1;
  if (allowAI && f.risk) p.risk_high = 1;
  if (f.dups) p.duplicates = 1;
  if (f.fresh) p.new_hour = 1;
  return p;
}

// Подпись фильтров для заголовка списка: «Зеркальце · просроченные»
export function describe() {
  const parts = [f.product, f.team, f.person && f.person.slice(2).split(" (")[0]].filter(Boolean);
  for (const [k, t] of Object.entries(TOGGLES)) if (f[k] && (k !== "risk" || allowAI)) parts.push(t.label.toLowerCase());
  if (f.stype) parts.push(f.stype === "client" ? "клиентские" : "внутренние");
  return parts.join(" · ");
}

// Панель фильтров. handlers.change() — перерисовать, handlers.all() — список всех по фильтру,
// handlers.outside() — список активных вне групп.
let listHandlers = {};
export function mountFilters(el, handlers) {
  onChange = handlers.change;
  listHandlers = handlers;
  el.innerHTML = `
    <span class="fl-title">Фильтры</span>
    <select class="fl-select" data-f="product" title="Продукт"></select>
    <select class="fl-select" data-f="team" title="Команда"></select>
    <select class="fl-select" data-f="person" title="В работе у координатора или исполнитель"></select>
    ${Object.entries(TOGGLES).filter(([, t]) => !t.more).map(([k, t]) => `<button class="fl-chip" data-t="${k}" title="${esc(t.hint)}">${esc(t.label)}</button>`).join("")}
    <div class="fl-more">
      <button class="fl-chip" data-act="more">Ещё ▾</button>
      <div class="fl-menu hidden">
        ${Object.entries(TOGGLES).filter(([, t]) => t.more).map(([k, t]) => `<label data-filter="${k}"><input type="checkbox" data-t="${k}"> ${esc(t.label)} <small>${esc(t.hint)}</small></label>`).join("")}
        <div class="fl-menu-sep">Тип сервиса</div>
        <label><input type="radio" name="stype" value=""> все</label>
        <label><input type="radio" name="stype" value="client"> клиентские</label>
        <label><input type="radio" name="stype" value="internal"> внутренние и технологические</label>
      </div>
    </div>
    <span class="fl-extra"></span>
    <span class="fl-count"></span>
    <button class="fl-reset linklike hidden" data-act="reset">сбросить</button>`;

  el.querySelectorAll("select[data-f]").forEach((s) => (s.onchange = () => set(s.dataset.f, s.value)));
  el.querySelectorAll("button[data-t]").forEach((b) => (b.onclick = () => set(b.dataset.t, !f[b.dataset.t])));
  el.querySelectorAll(".fl-menu input[data-t]").forEach((i) => (i.onchange = () => set(i.dataset.t, i.checked)));
  el.querySelectorAll(".fl-menu input[name=stype]").forEach((i) => (i.onchange = () => set("stype", i.value)));
  const menu = el.querySelector(".fl-menu");
  el.querySelector("[data-act=more]").onclick = (e) => { e.stopPropagation(); menu.classList.toggle("hidden"); };
  menu.onclick = (e) => e.stopPropagation();
  document.addEventListener("click", () => menu.classList.add("hidden"));
  el.querySelector("[data-act=reset]").onclick = () => { f = { ...EMPTY }; save(); onChange(); };
}

function set(k, v) {
  f[k] = v;
  save();
  onChange();
}

// Обновить списки, состояние чипов и счётчики «12 из 83 в группах · всего 340», «ещё N вне групп»
export function renderFilters(el, snap, shown, total, serverTotal) {
  el.querySelector('[data-filter="risk"]').classList.toggle("hidden", !allowAI);
  el.querySelector('[data-filter="dups"] small').textContent = allowAI ? TOGGLES.dups.hint : "мастер, дубль или совпадение по правилам";
  const groupIds = new Set(snap.groups.flatMap((g) => g.ids));
  const incs = [...groupIds].map((id) => snap.incidents[id]).filter(Boolean);
  const uniq = (xs) => [...new Set(xs.filter(Boolean))].sort((a, b) => a.localeCompare(b, "ru"));
  const products = uniq(snap.products && snap.products.length ? [...snap.products] : incs.map((i) => i.product));
  const teams = uniq(snap.teams && snap.teams.length ? [...snap.teams] : incs.map((i) => i.team));
  const coords = uniq([...(snap.coordinators || []), ...Object.values(snap.claims || {}).map((c) => c.by)]);
  const assignees = uniq(incs.map((i) => i.assignee));
  // если выбранное значение пропало из данных — оставляем его в списке, чтобы было видно, что фильтр стоит
  if (f.product && !products.includes(f.product)) products.unshift(f.product);
  if (f.team && !teams.includes(f.team)) teams.unshift(f.team);

  const sig = JSON.stringify([products, teams, coords, assignees, f.product, f.team, f.person]);
  if (sig !== lastOpts) {
    lastOpts = sig;
    const opt = (v, label, cur) => `<option value="${esc(v)}" ${v === cur ? "selected" : ""}>${esc(label)}</option>`;
    el.querySelector("[data-f=product]").innerHTML = opt("", "Продукт: все", f.product) + products.map((p) => opt(p, p, f.product)).join("");
    el.querySelector("[data-f=team]").innerHTML = opt("", "Команда: все", f.team) + teams.map((t) => opt(t, t, f.team)).join("");
    el.querySelector("[data-f=person]").innerHTML = opt("", "Исполнитель: все", f.person)
      + (coords.length ? `<optgroup label="В работе у координатора">${coords.map((c) => opt("c:" + c, c, f.person)).join("")}</optgroup>` : "")
      + (assignees.length ? `<optgroup label="Исполнитель в команде">${assignees.map((a) => opt("a:" + a, a, f.person)).join("")}</optgroup>` : "");
  }
  el.querySelectorAll("select[data-f]").forEach((s) => { s.value = f[s.dataset.f]; s.classList.toggle("active", !!f[s.dataset.f]); });
  el.querySelectorAll("button[data-t]").forEach((b) => b.classList.toggle("active", !!f[b.dataset.t]));
  el.querySelectorAll(".fl-menu input[data-t]").forEach((i) => (i.checked = !!f[i.dataset.t]));
  el.querySelectorAll(".fl-menu input[name=stype]").forEach((i) => (i.checked = i.value === f.stype));

  // включённые фильтры из «Ещё» — видны чипами с крестиком
  const extra = Object.entries(TOGGLES).filter(([k, t]) => t.more && f[k] && (k !== "risk" || allowAI)).map(([k, t]) => [k, t.label]);
  if (f.stype) extra.push(["stype", f.stype === "client" ? "Клиентские" : "Внутренние"]);
  const box = el.querySelector(".fl-extra");
  box.innerHTML = extra.map(([k, l]) => `<button class="fl-chip active" data-x="${k}" title="убрать">${esc(l)} ✕</button>`).join("");
  box.querySelectorAll("[data-x]").forEach((b) => (b.onclick = () => set(b.dataset.x, b.dataset.x === "stype" ? "" : false)));

  const active = isActiveFilter();
  const cnt = el.querySelector(".fl-count");
  const outside = snap.outside_active || 0;
  cnt.innerHTML = active
    ? `в группах <b>${shown}</b> из ${total} · <span class="linklike" data-list="all">всего по фильтру ${serverTotal == null ? "…" : serverTotal} ›</span>`
    : `${total} в группах${outside ? ` · <span class="linklike" data-list="outside" title="средние и низкие — через поиск и фильтры">ещё ${outside} активных вне групп ›</span>` : ""}`;
  cnt.querySelectorAll("[data-list]").forEach((s) => (s.onclick = () => listHandlers[s.dataset.list] && listHandlers[s.dataset.list]()));
  el.querySelector(".fl-reset").classList.toggle("hidden", !active);
  el.classList.toggle("filtering", active);
}
