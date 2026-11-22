// Отрисовка — общая для АРМ координатора и видеостены (ui.wall).
// Карточки кэшируются по подписи (hash + группы + ИИ + «в работе» + журнал), чтобы не мигать
// и не сбрасывать подсветку; таймеры обновляются отдельно раз в секунду.
import { esc, fmtDateTime, fmtTime, fmtLeft, fmtTimer, fmtWhen, plural, shortId, sameCoordinator } from "./format.js";
import { isDup } from "./filters.js";
import { icon } from "./icons.js";
import { incidentLabels } from "./incident-labels.js";
import { orderGroups } from "./column-settings.js";
import { aiEnabled, outageTitle } from "./ai-mode.js";

const GROUP_SHORT = { critical: "критические", coordinators: "на координаторах", high: "высокий", research: "исследование" };
const COUNTER_LABEL = {
  critical: ["Критические", ""],
  coordinators: ["На коорди-", "наторах"],
  high: ["Высокий", "приоритет"],
  research: ["Исследо-", "вание"],
};
const FORECAST_TEXT = { ok: "успевают", risk: "срок на исходе", breach: "прогноз: не успеют", overdue: "срок нарушен" };
const AI_SOURCE = { rules: "правила по истории", gigachat: "GigaChat", cache: "GigaChat (кэш)" };

const WALL_MAX = 30;   // на стене в колонке не больше стольких карточек в DOM, остальное — «+ ещё N»
const ARM_PAGE = 60;   // на АРМ список подгружается порциями

const $ = (sel) => document.querySelector(sel);
const firstName = (s) => (s || "").split(" (")[0];
// «в работе у Ольги»: поле coordinator инцидента (пишется в файл), claims — запасной путь для старого снимка
export function ownerOf(inc, snap) {
  if (inc && inc.coordinator && inc.coordinator.name) return { by: inc.coordinator.name, at: inc.coordinator.at };
  return (snap && snap.claims && inc && snap.claims[inc.id]) || null;
}
const aiTag = (ai) => `<span class="ai-tag" title="Подсказка ИИ · ${esc(AI_SOURCE[ai && ai.source] || "правила")}">ИИ</span>`;

// ---------- шапка ----------
export function renderCounters(snap, onJump, opts = {}) {
  const el = $("#counters");
  const focusedKey = el.contains(document.activeElement) ? document.activeElement.dataset.key : null;
  const markup = snap.groups.map((g) => {
    const n = snap.counts[g.key] ?? g.count;
    if (opts.wall) {
      const f = failures(snap, g);
      return `<div class="counter ${g.key} ${n ? "has" : ""}"><b>${n}</b><span>${esc(g.title)}${f < n ? ` <i>(${f} ${plural(f, "сбой", "сбоя", "сбоев")})</i>` : ""}</span></div>`;
    }
    if (opts.arm) {
      const unclaimed = g.ids.filter((id) => {
        const inc = snap.incidents[id];
        return inc?.is_active && !inc.resolved_in.includes(g.key) && !ownerOf(inc, snap);
      }).length;
      return `<button class="counter ${g.key}" data-key="${g.key}"><span>${esc(g.title)}</span><div><b>${n}</b>${unclaimed ? `<small>Без координатора: ${unclaimed}</small>` : ""}</div></button>`;
    }
    const [a, b] = COUNTER_LABEL[g.key] || [g.title, ""];
    return `<button class="counter ${g.key} ${n ? "has" : ""}" data-key="${g.key}"><b>${n}</b><span>${a}<br>${b}</span></button>`;
  }).join("") + (opts.wall && snap.outside_active ? `<div class="outside-note"><b>${snap.outside_active}</b><span>Активных вне групп</span></div>` : "");
  if (el.dataset.markup === markup) return;
  el.dataset.markup = markup;
  el.innerHTML = markup;
  if (onJump) el.querySelectorAll(".counter").forEach((b) => {
    b.onclick = () => onJump(b.dataset.key);
    if (b.dataset.key === focusedKey) b.focus({ preventScroll: true });
  });
}

export function renderClock(snap, nowMs) {
  const mode = snap.time_mode === "export" ? "время выгрузки" : "реальное время";
  $("#clock").innerHTML = `<b>${fmtTime(new Date(nowMs).toISOString())}</b><small>МСК · ${mode}</small>`;
}

export function renderStatus(state) {
  const { snap, online, lastOkMs } = state;
  const ago = lastOkMs ? Math.round((Date.now() - lastOkMs) / 1000) : null;
  let cls = "", text = `онлайн · ${snap ? snap.file.count : 0} в файле`;
  if (!online) { cls = "bad"; text = `нет связи · ${ago != null ? ago + " с назад" : ""}`; }
  else if (snap && snap.file.status !== "ok") { cls = "warn"; text = "файл не читается"; }
  else if (snap && document.body.classList.contains("arm")) text = "Текущий снимок";
  const st = $("#status");
  st.innerHTML = `<span class="dot ${cls}"></span><span class="status-text">${text}</span>`;
  st.title = text;
  st.classList.toggle("problem", !!cls);

  const bar = $("#filebar");
  if (snap && snap.file.status !== "ok") {
    const when = fmtDateTime(snap.file.last_ok_at);
    const what = snap.file.status === "missing" ? "Файл с инцидентами не найден" : "Файл с инцидентами повреждён или записан не до конца";
    bar.className = "filebar";
    const lastRead = snap.file.last_ok_at ? `Показаны последние целые данные (${when} МСК).` : "Корректная выгрузка ещё не загружена.";
    bar.textContent = `${what}. ${lastRead} ${snap.file.error || ""}`;
  } else if (!online) {
    bar.className = "filebar bad";
    bar.textContent = "Нет связи с сервером дашборда. Данные могут быть устаревшими — переподключаемся…";
  } else if (snap?.file.warnings?.length) {
    bar.className = "filebar";
    bar.textContent = `Проверьте выгрузку: ${snap.file.warnings[0]}.`;
  } else {
    bar.className = "filebar hidden";
  }
}

// ---------- тревога на стене: красная полоса на всю ширину ----------
// На стене — число невзятых критических и ближайший срок.
export function renderWallBanner(fresh, nowMs) {
  const el = $("#alerts");
  if (!fresh.length) { el.innerHTML = ""; el.className = "alerts"; return; }
  const inc = [...fresh].sort(byAlertDeadline)[0].inc;
  const rem = inc.deadline ? (Date.parse(inc.deadline) - nowMs) / 1000 : null;
  const n = fresh.length;
  el.className = "alerts wall-banner";
  el.innerHTML = `<div class="alert critical corners">
      <span class="alert-kind">${icon("alert")} ${n > 1 ? `${n} критических без координатора` : "Критический без координатора"}</span>
      <span class="alert-text"><b>${esc(inc.product || "")}</b> · ${esc(inc.title)} · ${esc(inc.team || "")}</span>
      <span class="alert-time" data-deadline="${esc(inc.deadline || "")}">${rem == null ? "срок не задан" : (rem < 0 ? "просрочен " : "до срока ") + fmtLeft(rem)}</span>
    </div>`;
}

function byAlertDeadline(a, b) {
  return (Date.parse(a.inc.deadline) || Infinity) - (Date.parse(b.inc.deadline) || Infinity);
}

export function renderAlertLine(alerts, handlers) {
  const el = $("#alerts");
  const openGroups = new Set([...el.querySelectorAll("details[open]")].map((node) => node.dataset.group));
  const focused = el.contains(document.activeElement) ? document.activeElement : null;
  const markup = ["critical", "coordinators"].map((group) => {
    const entries = alerts.filter((alert) => alert.group === group).sort(byAlertDeadline);
    if (!entries.length) return "";
    const title = group === "critical" ? "Критических без координатора" : "Новых назначений на координаторов";
    return `<details class="shift-signal corners ${group}" data-group="${group}">
      <summary><b>${title}: ${entries.length}</b><small class="signal-sla"></small></summary>
      <ul>${entries.map(({ inc }) => `<li data-id="${esc(inc.id)}">
        <button type="button" class="signal-open linklike" data-act="open">${esc(inc.title || inc.id)}</button>
        ${inc.deadline ? `<small class="alert-time" data-deadline="${esc(inc.deadline)}"></small>` : "<small>Срок не задан</small>"}
        <button type="button" class="btn small" data-act="claim" aria-label="Взять в работу ${esc(inc.id)}">Взять</button>
      </li>`).join("")}</ul>
    </details>`;
  }).join("");
  if (el.dataset.markup === markup) return;
  el.dataset.markup = markup;
  el.innerHTML = markup;
  for (const details of el.querySelectorAll("details")) {
    details.open = openGroups.has(details.dataset.group);
    if (focused?.closest("details")?.dataset.group === details.dataset.group) {
      const row = [...details.querySelectorAll("li")].find((li) => li.dataset.id === focused.closest("li")?.dataset.id);
      (row?.querySelector(`[data-act="${focused.dataset.act}"]`) || details.querySelector("summary")).focus({ preventScroll: true });
    }
  }
  if (focused && document.activeElement === document.body) $(".tab.active")?.focus({ preventScroll: true });
  el.onclick = (event) => {
    const button = event.target.closest("button[data-act]");
    if (button) handlers[button.dataset.act](button.closest("li").dataset.id);
  };
}

// ---------- колонки ----------
const boardCaches = new WeakMap();

function matches(inc, q) {
  if (!q) return true;
  const hay = [inc.id, inc.title, inc.product, inc.service, inc.team, inc.assignee, inc.state, inc.priority].join(" ").toLowerCase();
  return q.toLowerCase().split(/\s+/).every((w) => hay.includes(w));
}

function wallGroupRows(ids, incidents, group, alarmIds, outages, overlappingDuplicates) {
  const rows = [];
  const collapsed = new Map();
  const dupSeen = new Set();
  for (const id of ids) {
    const inc = incidents[id];
    const outage = outages.get(id);
    if (outage) {
      let row = collapsed.get(outage.id);
      if (!row) {
        row = { outage, count: 0, alarmCount: 0, resolved: true, critical: false };
        collapsed.set(outage.id, row);
        rows.push(row);
      }
      row.count++;
      if (!inc.resolved_in.includes(group)) {
        row.resolved = false;
        row.critical ||= inc.priority_level === "critical";
        if (alarmIds.has(id)) row.alarmCount++;
      }
      continue;
    }
    const dupId = inc.ai?.duplicates?.group_id;
    const hideDuplicate = overlappingDuplicates.has(dupId);
    if (dupId && !hideDuplicate) {
      if (dupSeen.has(dupId)) continue;
      dupSeen.add(dupId);
    }
    rows.push({ id, hideDuplicate });
  }
  return rows;
}

function fillOutageRow(el, row) {
  const { outage, count, alarmCount, resolved, critical } = row;
  el.dataset.outage = outage.id;
  el.className = "card one-line outage-group-row";
  el.classList.toggle("outage-row-resolved", resolved);
  el.classList.toggle("is-critical", critical || alarmCount > 0);
  el.classList.toggle("alarm", alarmCount > 0);
  el.innerHTML = `<b>+${count} из сбоя</b><span class="outage-group-name" title="${esc(outage.name)}">${esc(outage.name)}</span>
    ${alarmCount ? `<strong class="outage-alarm-label">! ${alarmCount} не взято</strong>` : ""}`;
}

const closeTimers = new WeakMap();

// ui: {query, changed, wall, open, predicate, limits, more, selected, alarmIds, actions, act}
export function renderBoard(snap, nowMs, ui, board = $("#board")) {
  const started = performance.now();
  clearTimeout(closeTimers.get(board));
  let nextClose = Infinity;
  const stillVisible = (id) => {
    const until = Date.parse(snap.incidents[id]?.closed_until);
    if (!Number.isFinite(until)) return true;
    if (until <= nowMs) return false;
    nextClose = Math.min(nextClose, until);
    return true;
  };
  const useAI = aiEnabled(snap);
  if (!boardCaches.has(board)) boardCaches.set(board, new Map());
  const cardCache = boardCaches.get(board);
  const ordered = ui.wall ? snap.groups : orderGroups(snap.groups, ui.columns.order);
  if (!board.children.length || board.dataset.groups !== snap.groups.map((g) => g.key).join(",")) {
    board.dataset.groups = snap.groups.map((g) => g.key).join(",");
    board.innerHTML = snap.groups.map((g) => `
      <section class="column" data-key="${esc(g.key)}" id="${board.id === "mine" ? "mine-col-" : "col-"}${esc(g.key)}">
        <div class="column-head"><h2>${esc(g.title)}</h2><span class="count"></span></div>
        <div class="column-body"></div>
        <div class="column-more hidden"></div>
      </section>`).join("") + (ui.wall ? "" : '<p class="board-empty hidden" role="status"></p>');
  }
  const empty = ui.wall ? null : board.querySelector(".board-empty");
  if (empty) {
    const columns = new Map([...board.querySelectorAll(".column")].map((col) => [col.dataset.key, col]));
    const focused = document.activeElement;
    ordered.forEach((g, i) => {
      const col = columns.get(g.key);
      if (board.children[i] !== col) board.insertBefore(col, board.children[i]);
    });
    if (board.contains(focused) && document.activeElement !== focused) focused.focus({ preventScroll: true });
  }
  const alive = new Set();
  const filtering = !!(ui.query || ui.predicate);
  const seenWall = new Set(); // стена: инцидент уже показан полной карточкой в левой колонке → здесь компактная строка
  const outages = new Map();
  const overlappingDuplicates = new Set();
  let shown = 0;
  let visibleColumns = 0;
  if (ui.wall) {
    for (const outage of snap.outages || []) {
      const summary = useAI ? outage : { ...outage, name: outageTitle(outage, useAI) };
      for (const id of outage.incident_ids) outages.set(id, summary);
    }
    if (outages.size) {
      for (const inc of Object.values(snap.incidents)) {
        const dup = inc.ai?.duplicates;
        if (dup?.group_id && (outages.has(inc.id) || dup.ids?.some((id) => outages.has(id)))) {
          overlappingDuplicates.add(dup.group_id);
        }
      }
    }
    // «Критические» вдвое шире; нет критических — колонка сужается и крупно «Критических нет»
    const crit = snap.groups.find((g) => g.key === "critical");
    const hasCrit = !!crit?.ids.some(stillVisible);
    board.style.gridTemplateColumns = snap.groups.map((g) => g.key === "critical" ? (hasCrit ? "minmax(0, 2fr)" : "minmax(0, .75fr)") : "minmax(0, 1fr)").join(" ");
    board.classList.toggle("no-critical", !hasCrit);
  }
  for (const g of ordered) {
    const col = board.querySelector(`.column[data-key="${CSS.escape(g.key)}"]`);
    const body = col.querySelector(".column-body");
    const more = col.querySelector(".column-more");
    const currentIds = g.ids.filter(stillVisible);
    const ids = currentIds.filter((id) => matches(snap.incidents[id], ui.query) && (!ui.predicate || ui.predicate(snap.incidents[id])));
    shown += ids.length;
    if (!ui.wall) {
      const hidden = !ids.length && (ui.columns.collapse || g.key === "outside");
      col.classList.toggle("hidden", hidden);
      if (!hidden) visibleColumns++;
    }
    const cnt = col.querySelector(".count");
    const n = snap.counts[g.key] ?? g.count;
    const f = failures(snap, g);
    cnt.innerHTML = filtering ? `${ids.length}<small> из ${currentIds.length}</small>`
      : `${n}${f < n ? `<small> (${f} ${plural(f, "сбой", "сбоя", "сбоев")})</small>` : ""}`;
    const rows = ui.wall ? wallGroupRows(ids, snap.incidents, g.key, ui.alarmIds, outages, overlappingDuplicates) : ids.map((id) => ({ id }));
    const limit = ui.wall ? WALL_MAX : (ui.limits && ui.limits[g.key]) || ARM_PAGE;
    const page = rows.slice(0, limit);
    const els = [];
    for (const row of page) {
      if (row.outage) {
        const key = JSON.stringify([g.key, row.outage.id]);
        const sig = JSON.stringify([row.outage.name, row.count, row.alarmCount, row.resolved, row.critical]);
        let entry = cardCache.get(key);
        if (!entry || entry.sig !== sig) {
          const el = entry ? entry.el : document.createElement("article");
          fillOutageRow(el, row);
          entry = { sig, el };
          cardCache.set(key, entry);
        }
        alive.add(key);
        els.push(entry.el);
        continue;
      }
      const { id, hideDuplicate } = row;
      const inc = snap.incidents[id];
      const key = `${g.key}:${id}`;
      alive.add(key);
      const changed = ui.changed.get(id);
      const claim = ownerOf(inc, snap);
      const action = ui.actions ? ui.actions.get(id) : null;
      const compact = ui.wall && seenWall.has(id);
      if (ui.wall) seenWall.add(id);
      const sig = ui.wall ? [inc.hash, inc.groups.join(), inc.resolved_in.join(), JSON.stringify(inc.ai), inc.forecast && inc.forecast.level,
        claim ? claim.by : "", inc.work_log.length, changed ? changed.until : 0, action ? action.id : "", compact, hideDuplicate, useAI,
      ].join("|") : armCardSignature(inc, ui, changed, claim, action);
      let entry = cardCache.get(key);
      if (!entry || entry.sig !== sig) {
        const el = entry ? entry.el : document.createElement("article");
        el.dataset.id = id;
        if (compact) fillCompactWall(el, inc, g.key, nowMs, claim, useAI);
        else if (ui.wall) fillCardWall(el, inc, g.key, nowMs, changed, claim, hideDuplicate, useAI);
        else fillCardArm(el, inc, g.key, nowMs, changed, claim, action, ui);
        if (ui.open) el.onclick = () => ui.open(id);
        entry = { sig, el };
        cardCache.set(key, entry);
      }
      if (!ui.wall) entry.el.classList.toggle("selected", ui.selected === id);
      entry.el.classList.toggle("alarm", !!(ui.alarmIds && ui.alarmIds.has(id)) && !inc.resolved_in.includes(g.key));
      els.push(entry.el);
    }
    body.querySelector(".column-empty")?.remove();
    els.forEach((el, i) => { if (body.children[i] !== el) body.insertBefore(el, body.children[i] || null); });
    while (body.children.length > els.length) body.lastChild.remove();
    if (!els.length) {
      body.innerHTML = ui.wall && g.key === "critical" ? `<div class="column-empty big">Критических нет</div>`
        : `<div class="column-empty">${filtering ? "нет по фильтру" : "пусто"}</div>`;
    }

    const rest = rows.length - page.length;
    col.dataset.rest = String(rest);
    if (!ui.wall) {
      more.classList.toggle("hidden", rest <= 0);
      if (rest > 0) {
        more.innerHTML = `<button class="btn small">Показать ещё ${Math.min(ARM_PAGE, rest)} · осталось ${rest}</button>`;
        more.firstChild.onclick = () => ui.more(g.key);
      }
    }
  }
  for (const k of [...cardCache.keys()]) if (!alive.has(k)) cardCache.delete(k);
  if (Number.isFinite(nextClose)) {
    const timer = setTimeout(() => {
      renderBoard(snap, nowMs + performance.now() - started, ui, board);
    }, Math.ceil(nextClose - nowMs - (performance.now() - started)));
    closeTimers.set(board, timer);
  }
  if (ui.wall) fitColumns();
  else {
    const message = ui.emptyText || (filtering ? "Нет инцидентов по фильтрам. Измените или сбросьте фильтры." : "В группах пока нет инцидентов.");
    if (empty.textContent !== message) empty.textContent = message;
    empty.classList.toggle("hidden", shown > 0);
    board.classList.toggle("board-no-results", shown === 0);
    board.style.setProperty("--visible-columns", Math.max(1, visibleColumns));
  }
  return shown;
}

// Сколько разных сбоев среди активных инцидентов группы: дубли одного сбоя (ai.duplicates.group_id) — один
export function failures(snap, g) {
  const keys = new Set();
  for (const id of g.ids) {
    const inc = snap.incidents[id];
    if (!inc || inc.resolved_in.includes(g.key)) continue;
    keys.add((inc.ai && inc.ai.duplicates && inc.ai.duplicates.group_id) || id);
  }
  return keys.size;
}

// Видеостена не скроллится: показываем столько карточек, сколько влезает, и «+ ещё N»; счётчики в шапке точные
export function fitColumns() {
  document.querySelectorAll(".column").forEach((col) => {
    const body = col.querySelector(".column-body");
    const more = col.querySelector(".column-more");
    const cards = [...body.querySelectorAll(".card")];
    cards.forEach((c) => (c.style.display = ""));
    // строку «ещё N» показываем до замера: она съедает высоту колонки, иначе нижняя карточка обрезается
    more.classList.remove("hidden");
    more.textContent = "+ ещё";
    const limit = body.getBoundingClientRect().bottom;
    const overflow = cards.filter((card) => card.getBoundingClientRect().bottom > limit);
    overflow.forEach((card) => (card.style.display = "none"));
    const hidden = Number(col.dataset.rest || 0) + overflow.length;
    more.classList.toggle("hidden", hidden === 0);
    more.textContent = hidden ? `+ ещё ${hidden}` : "";
  });
}

function lastEscalation(inc) {
  for (let i = inc.work_log.length - 1; i >= 0; i--) if (inc.work_log[i].action === "escalate") return inc.work_log[i];
  return null;
}

// Пять полей из задания — на карточке: продукт, возник, исполнитель «команда / сотрудник», статус, срок-таймер.
export function cardTitle(inc, useAI = true) {
  // выжимка ИИ вместо длинной сути (оригинал — в панели); префикс «Продукт:» у выжимки срезаем — продукт уже в шапке
  const sum = useAI && inc.ai?.summary;
  if (sum && (!inc.title || inc.title.length > 60)) {
    const p = (inc.product || "") + ":";
    return { text: sum.startsWith(p) ? sum.slice(p.length).trim() : sum, ai: true };
  }
  return { text: inc.title || inc.short_description || "—", ai: false };
}

function executor(inc, wall) {
  const team = inc.team || "команда не указана";
  if (wall) return esc(team);
  return `${esc(team)} / ${inc.assignee ? esc(firstName(inc.assignee)) : `<span class="dim">сотрудник не назначен</span>`}`;
}

// Таймер срока. Текст и цвет обновляет tickCountdowns раз в секунду.
function timer(inc, nowMs) {
  if (inc.state_kind === "research") {
    return `<span class="no-deadline">${inc.deadline ? `срок: ${esc(fmtDateTime(inc.deadline, nowMs))}` : "срок не задан"}</span>`;
  }
  if (inc.state_kind !== "active") return "";
  if (!inc.deadline) {
    return inc.priority_level === "critical"
      ? `<span class="no-deadline warn" title="у активного критического не задан срок">нет срока</span>`
      : `<span class="no-deadline">срок не задан</span>`;
  }
  const fc = inc.forecast;
  return `<span class="countdown" data-deadline="${esc(inc.deadline)}" data-start="${esc(inc.created_at || "")}" data-fc="${esc(fc ? fc.level : "")}" title="срок ${esc(fmtDateTime(inc.deadline, nowMs))}${fc ? " · " + esc(FORECAST_TEXT[fc.level] + " · " + fc.basis) : ""}"></span>`;
}

// Строка ИИ: на координаторах — кому передать; иначе «нарастает» (динамика) или высокий риск
function aiLineFor(inc, group) {
  const ai = inc.ai || {};
  if (group === "coordinators" && ai.route) {
    const conf = ai.route.confidence_text || `${Math.round((ai.route.confidence || 0) * 100)}%`;
    return `<div class="ai-line route">${aiTag(ai)}передать в «<b>${esc(ai.route.team)}</b>», ${esc(conf)}</div>`;
  }
  if (ai.dynamics && ai.dynamics.flag) {
    return `<div class="ai-line dyn">${aiTag(ai)}<b>${esc(ai.dynamics.label || "нарастает")}</b>${ai.dynamics.text ? ": " + esc(ai.dynamics.text) : ""}</div>`;
  }
  if (ai.risk && ai.risk.level === "high") {
    return `<div class="ai-line">${aiTag(ai)}риск стать критическим: <b class="risk-high">высокий · ${ai.risk.score}</b></div>`;
  }
  return "";
}

const ownerName = (inc) => (inc.coordinator && inc.coordinator.name ? firstName(inc.coordinator.name) : "");

// Завершённый или закрытый инцидент показываем компактно, пока не истекло окно показа.
function fillResolvedLine(el, inc, nowMs, wall, useAI) {
  const labels = wall ? "" : incidentLabels(inc, 2);
  const status = inc.state_kind === "closed" ? "закрыт" : `решён${inc.resolved_at ? " в " + esc(fmtWhen(inc.resolved_at, nowMs)) : ""}`;
  el.className = `card corners is-resolved one-line${labels ? " has-labels" : ""}`;
  el.innerHTML = `<span class="ok">${icon("check")}</span><b class="card-product">${esc(inc.product || "—")}</b>
    <span class="line-title">${esc(cardTitle(inc, useAI).text)}</span>
    <span class="line-when">${status}${!wall && ownerName(inc) ? " · " + esc(ownerName(inc)) : ""}</span>
    ${labels ? `<div class="card-foot">${labels}</div>` : ""}`;
}

function stateNote(inc, nowMs) {
  if (inc.state_kind === "active") return "";
  const when = inc.resolved_at ? " " + esc(fmtWhen(inc.resolved_at, nowMs)) : "";
  return `<div class="resolved-note">${icon("check")} ${inc.state_kind === "research" ? `решён${when}, идёт разбор причин` : `${esc(inc.state || "решён")}${when}`}</div>`;
}

const metaLine = (inc, nowMs, wall) =>
  `<div class="card-meta1">возник ${esc(fmtWhen(inc.created_at, nowMs))} · ${executor(inc, wall)} · <span class="st st-${inc.state_kind}">${esc(inc.state || "—")}</span></div>`;

function armCardSignature(inc, ui, changed, claim, action) {
  return JSON.stringify([inc.hash, inc.groups, inc.resolved_in, inc.recurrence?.count, inc.recurrence?.level, inc.labels,
    claim?.by, claim?.at, inc.work_log.length, changed?.until, changed?.texts, action?.id, action?.draft,
    inc.forecast?.level, inc.forecast?.basis, ui.cardFields, ui.aiEnabled,
    ui.aiEnabled && ui.cardFields.ai ? inc.ai : null, ui.aiEnabled && ui.cardFields.summary ? inc.ai?.summary : null]);
}

// Карточка АРМ
function fillCardArm(el, inc, group, nowMs, changed, claim, action, ui) {
  const fields = ui.cardFields;
  const closed = inc.state_kind === "closed";
  const resolved = inc.resolved_in.includes(group) || (group === "outside" && (closed || inc.state_kind === "resolved"));
  const active = !resolved && inc.state_kind === "active";
  const focused = el.contains(document.activeElement) ? document.activeElement.dataset.act : null;
  el.className = resolved ? "card arm-card corners is-resolved" : "card arm-card corners";
  el.classList.toggle("is-critical", !resolved && inc.priority_level === "critical");
  el.classList.toggle("flash", !!changed && changed.until > Date.now());
  const title = esc(inc.title || inc.short_description || "—");
  const meta = [
    fields.created && `Возник: ${esc(fmtWhen(inc.created_at, nowMs))}`,
    fields.executor && executor(inc, false),
  ].filter(Boolean).join(" · ");
  const buttons = [];
  if (claim) buttons.push(`<span class="owner" title="координатор с ${esc(fmtTime(claim.at))}">в работе: ${esc(firstName(claim.by))}</span>`);
  else if (active) buttons.push(`<button class="btn small btn-take" data-act="claim" title="Клавиша В">Взять в работу</button>`);
  if (group === "coordinators" && !resolved && (active || inc.state_kind === "research")) {
    buttons.push(`<button class="btn small btn-primary" data-act="transfer">Передать</button>`);
  }
  if (action?.kind === "escalate" && active) buttons.push(`<button class="btn small" data-act="escalate">Эскалировать</button>`);
  const labels = fields.labels ? incidentLabels(inc, 2) : "";

  el.innerHTML = `
    ${fields.product || fields.state ? `<div class="card-top arm-card-head">
      ${fields.product ? `<b class="card-product" title="${esc(inc.product || "—")}">${esc(inc.product || "—")}</b>` : ""}
      ${fields.state ? `<span class="chip st-${resolved ? "resolved" : esc(inc.state_kind)}">${resolved ? (closed ? "Закрыт" : "Решён") : esc(inc.state || "—")}</span>` : ""}
    </div>` : ""}
    <button type="button" class="arm-card-title" data-act="open" title="${title}">${title}</button>
    ${ui.aiEnabled && fields.summary && inc.ai?.summary ? `<div class="card-meta1 arm-card-summary">Краткая суть · ${esc(inc.ai.summary)}</div>` : ""}
    ${meta ? `<div class="card-meta1">${meta}</div>` : ""}
    <div class="arm-card-footer">
      <span class="inc-id">${esc(inc.id)}</span>
      <span class="card-prio prio-${esc(inc.priority_level)}">${esc(inc.priority || "—")}</span>
      ${resolved ? `<span class="arm-card-resolved">${closed ? "Закрыт" : `Решён${fields.state && inc.resolved_at ? " в " + esc(fmtWhen(inc.resolved_at, nowMs)) : ""}`}</span>` : fields.deadline ? timer(inc, nowMs) : ""}
    </div>
    ${ui.aiEnabled && fields.ai && !resolved ? aiLineFor(inc, group) : ""}
    ${buttons.length || labels ? `<div class="card-btns">${buttons.join("")}${labels}</div>` : ""}
    ${fields.updates && changed?.until > Date.now() ? `<div class="changed">● ${esc(changed.texts.join(" · "))}</div>` : ""}`;

  const on = (name, fn) => {
    const button = el.querySelector(`[data-act=${name}]`);
    if (button) button.onclick = (event) => { event.stopPropagation(); fn(); };
  };
  if (ui.act) {
    on("claim", () => ui.act.claim(inc.id));
    on("transfer", () => ui.act.transfer(inc.id));
    on("escalate", () => ui.act.escalate(inc.id, action?.draft));
  }
  if (focused) {
    const button = [...el.querySelectorAll("button")].find((node) => node.dataset.act === focused);
    (button || el.querySelector(".arm-card-title")).focus({ preventScroll: true });
  }
}

export function renderCardPreview(el, inc, group, nowMs, cardFields, aiEnabled, claim) {
  fillCardArm(el, inc, group, nowMs, null, claim, null, { cardFields, aiEnabled });
}

// Карточка стены: крупно, без кнопок, ИИ и номера; исполнитель — только команда; «в работе у Ольги»
function fillCardWall(el, inc, group, nowMs, changed, claim, hideDuplicate, useAI) {
  if (inc.resolved_in.includes(group)) { fillResolvedLine(el, inc, nowMs, true, useAI); return; }
  el.className = "card corners";
  el.classList.toggle("is-critical", inc.priority_level === "critical");
  el.classList.toggle("flash", !!changed && changed.until > Date.now());
  const fc = inc.forecast;
  const esc_ = lastEscalation(inc);
  const workLine = [
    claim ? `<span class="owner">в работе: ${esc(firstName(claim.by))}</span>` : "",
    esc_ ? `<span class="escalated">⇪ эскалирован ${esc(fmtTime(esc_.at))}</span>` : "",
  ].filter(Boolean).join("");
  const d = !hideDuplicate && inc.ai?.duplicates;
  const size = d ? d.size || (d.ids || []).length : 0;
  el.innerHTML = `
    <div class="card-top">
      <span class="card-product">${esc(inc.product || "—")}</span>
      ${size > 1 ? `<span class="dup-badge">один сбой · ${size} ${plural(size, "заявка", "заявки", "заявок")}</span>` : ""}
      <span class="card-prio prio-${inc.priority_level}">${esc(inc.priority || "")}</span>
    </div>
    <div class="card-title">${esc(useAI && size > 1 && d.name ? d.name : cardTitle(inc, useAI).text)}</div>
    ${metaLine(inc, nowMs, true)}
    ${inc.state_kind === "active" ? `<div class="deadline">
        <div class="deadline-row">${timer(inc, nowMs)}${inc.deadline ? `<small>до ${esc(fmtWhen(inc.deadline, nowMs))}</small>` : ""}</div>
        ${fc ? `<div class="bar ${fc.level}"><i style="width:${Math.min(100, fc.progress * 100)}%"></i></div>` : ""}
      </div>` : stateNote(inc, nowMs) + timer(inc, nowMs)}
    ${workLine ? `<div class="work-line">${workLine}</div>` : ""}
    ${changed && changed.until > Date.now() ? `<div class="changed">● ${esc(changed.texts.join(" · "))}</div>` : ""}`;
}

// Стена, инцидент уже есть полной карточкой в колонке левее — здесь компактная строка
function fillCompactWall(el, inc, group, nowMs, claim, useAI) {
  if (inc.resolved_in.includes(group)) { fillResolvedLine(el, inc, nowMs, true, useAI); return; }
  el.className = "card corners one-line compact-row";
  el.classList.toggle("is-critical", inc.priority_level === "critical");
  el.innerHTML = `<b class="card-product">${esc(inc.product || "—")}</b>
    <span class="line-title">${esc(cardTitle(inc, useAI).text)}</span>
    ${claim ? `<span class="owner">${esc(firstName(claim.by))}</span>` : ""}
    ${timer(inc, nowMs)}`;
}

// Таймеры раз в секунду: жёлтый при ≥75% срока или прогнозе «не успеют», красный < 15 мин, просроченный мигает
export function tickCountdowns(nowMs) {
  const wall = document.body.classList.contains("wall");
  document.querySelectorAll("[data-deadline]").forEach((el) => {
    const dl = el.dataset.deadline;
    if (!dl) return;
    const end = Date.parse(dl);
    const sec = (end - nowMs) / 1000;
    if (el.classList.contains("alert-time")) {
      el.textContent = sec < 0 ? `просрочен · ${fmtLeft(sec)}` : `до срока ${fmtLeft(sec)}`;
      el.classList.toggle("overdue", sec < 0);
    } else if (el.classList.contains("countdown-mini")) {
      el.textContent = fmtLeft(sec);
      el.classList.toggle("overdue", sec < 0);
      el.classList.toggle("danger", sec >= 0 && sec < 900);
      const label = el.nextElementSibling;
      if (label?.classList.contains("queue-sla-label")) label.textContent = sec < 0 ? "SLA просрочен" : "До нарушения";
    } else {
      const text = fmtTimer(sec);
      el.textContent = wall ? text.replace("осталось ", "") : text;
      const start = Date.parse(el.dataset.start || "");
      const frac = start && end > start ? (nowMs - start) / (end - start) : 0;
      const fc = el.dataset.fc;
      const progress = el.parentElement.querySelector(".sla-progress");
      if (progress) progress.value = Math.max(0, Math.min(100, frac * 100));
      el.classList.toggle("overdue", sec < 0);
      el.classList.toggle("danger", sec >= 0 && sec < 900);
      el.classList.toggle("warn", sec >= 900 && (frac >= 0.75 || fc === "breach" || fc === "risk"));
    }
  });
  document.querySelectorAll(".shift-signal").forEach((signal) => {
    const seconds = [...signal.querySelectorAll("[data-deadline]")].map(({ dataset }) =>
      (Date.parse(dataset.deadline) - nowMs) / 1000);
    const overdue = seconds.filter((sec) => sec < 0).length;
    const next = seconds.find((sec) => sec >= 0);
    const text = [
      overdue ? `Просрочено: ${overdue}` : "",
      next === undefined ? "" : `Ближайший SLA через ${fmtLeft(next)}`,
    ].filter(Boolean).join(" · ");
    const summary = signal.querySelector(".signal-sla");
    if (summary.textContent !== text) summary.textContent = text;
  });
}

// ---------- лента ----------
const EVENT_ICON = { became_critical: "alert", critical_resolved: "check", to_coordinators: "right",
  resolved: "check", reopened: "reopen", new: "plus", removed: "minus", comment: "message", escalated: "up" };
const feedText = (e) => `<time datetime="${esc(e.clock_at || "")}">${fmtTime(e.clock_at)}</time> ${icon(Object.hasOwn(EVENT_ICON, e.type) ? EVENT_ICON[e.type] : "dot")} <b>${esc(e.product || shortId(e.incident_id))}</b> ${esc(e.text)}${e.by ? ` <i>· ${esc(firstName(e.by))}</i>` : ""}`;
const feedItem = (e) => `<span class="feed-item alert-${esc(e.alert || "")}" data-id="${esc(e.incident_id)}">${feedText(e)}</span>`;

// Бегущая строка внизу (стена)
export function renderFeed(snap, onOpen) {
  const el = $("#feed");
  const evs = (snap.events || []).slice(-12).reverse();
  el.innerHTML = `<span class="feed-title">ЛЕНТА</span>` + (evs.length
    ? evs.map(feedItem).join("")
    : `<span>изменений пока нет — правки в файле и действия координаторов появятся здесь</span>`);
  if (onOpen) el.querySelectorAll(".feed-item").forEach((i) => (i.onclick = () => onOpen(i.dataset.id)));
}

// Список событий во всплывающей панели (АРМ, кнопка «Лента»)
export function renderFeedList(snap, el, onOpen) {
  const evs = (snap.events || []).slice(-50).reverse();
  const markup = evs.length
    ? `<ul class="feed-list corners">${evs.map((event) => `<li class="feed-row"><button type="button" class="feed-item alert-${esc(event.alert || "")}" data-id="${esc(event.incident_id)}" data-seq="${esc(event.seq)}">${feedText(event)}</button></li>`).join("")}</ul>`
    : `<p class="pop-empty">Изменений пока нет — правки в файле и действия координаторов появятся здесь</p>`;
  if (el.dataset.markup === markup) return;
  const focusedSeq = el.contains(document.activeElement) ? document.activeElement.dataset.seq : null;
  el.dataset.markup = markup;
  el.innerHTML = markup;
  el.querySelectorAll("button.feed-item").forEach((button) => {
    button.onclick = () => onOpen(button.dataset.id);
    if (button.dataset.seq === focusedSeq) button.focus({ preventScroll: true });
  });
  if (focusedSeq && document.activeElement === document.body) $(".tab.active")?.focus({ preventScroll: true });
}

// «Мои»: одна карточка на инцидент в первой подходящей группе из снимка.
export function renderMine(snap, el, me, nowMs, ui) {
  const incs = Object.values(snap.incidents).filter((inc) => sameCoordinator(ownerOf(inc, snap)?.by, me));
  incs.sort((a, b) => (a.state_kind === "active" ? 0 : 1) - (b.state_kind === "active" ? 0 : 1)
    || (Date.parse(a.deadline || "2999-01-01") - Date.parse(b.deadline || "2999-01-01")));
  const groups = snap.groups.map((group) => ({ ...group, ids: [] }));
  const byKey = new Map(groups.map((group) => [group.key, group]));
  const outside = { key: "outside", title: "Вне групп", ids: [] };
  for (const inc of incs) {
    const keys = inc.groups.filter((key) => byKey.has(key));
    const primary = keys.find((key) => !inc.resolved_in.includes(key)) || keys[0];
    (byKey.get(primary) || outside).ids.push(inc.id);
  }
  groups.push(outside);
  const counts = Object.fromEntries(groups.map((group) => [group.key, group.ids.length]));
  const emptyText = !incs.length ? "У вас пока нет инцидентов в работе." : undefined;
  const shown = renderBoard({ ...snap, groups, counts }, nowMs, { ...ui, emptyText }, el);
  return { total: incs.length, shown };
}

export function toast(text, kind = "") {
  const box = $("#toasts");
  if (!box) return;
  const t = document.createElement("div");
  t.className = `toast ${kind}`;
  if (kind === "bad" || kind === "green") {
    t.innerHTML = icon(kind === "bad" ? "alert" : "check");
    t.append(" ");
  }
  t.append(text);
  box.appendChild(t);
  setTimeout(() => t.remove(), 8000);
}
