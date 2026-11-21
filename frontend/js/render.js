// Отрисовка — общая для АРМ координатора и видеостены (ui.wall).
// Карточки кэшируются по подписи (hash + группы + ИИ + «в работе» + журнал), чтобы не мигать
// и не сбрасывать подсветку; таймеры обновляются отдельно раз в секунду.
import { esc, fmtDateTime, fmtTime, fmtLeft, fmtTimer, fmtWhen, plural, shortId } from "./format.js";
import { isDup } from "./filters.js";

const GROUP_SHORT = { critical: "критические", coordinators: "на координаторах", high: "высокий", research: "исследование" };
const COUNTER_LABEL = {
  critical: ["Критические", ""],
  coordinators: ["На коорди-", "наторах"],
  high: ["Высокий", "приоритет"],
  research: ["Исследо-", "вание"],
};
const COUNTER_SHORT = { critical: "Критические", coordinators: "На координаторах", high: "Высокий", research: "Исследование" };
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
  el.innerHTML = snap.groups.map((g) => {
    const n = snap.counts[g.key] ?? g.count;
    if (opts.wall) {
      const f = failures(snap, g);
      const [a, b] = COUNTER_LABEL[g.key] || [g.title, ""];
      return `<div class="counter ${g.key} ${n ? "has" : ""}"><b>${n}</b><span>${a}<br>${b}${f < n ? ` <i>(${f} ${plural(f, "сбой", "сбоя", "сбоев")})</i>` : ""}</span></div>`;
    }
    if (opts.compact) {
      return `<button class="counter compact ${g.key} ${n ? "has" : ""}" data-key="${g.key}" title="${esc(g.title)}"><span>${COUNTER_SHORT[g.key] || esc(g.title)}</span><b>${n}</b></button>`;
    }
    const [a, b] = COUNTER_LABEL[g.key] || [g.title, ""];
    return `<button class="counter ${g.key} ${n ? "has" : ""}" data-key="${g.key}"><b>${n}</b><span>${a}<br>${b}</span></button>`;
  }).join("") + (opts.wall && snap.outside_active ? `<div class="outside-note"><b>${snap.outside_active}</b><span>ещё активных<br>вне групп</span></div>` : "");
  if (onJump) el.querySelectorAll(".counter").forEach((b) => (b.onclick = () => onJump(b.dataset.key)));
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
  const st = $("#status");
  st.innerHTML = `<span class="dot ${cls}"></span><span class="status-text">${text}</span>`;
  st.title = text;
  st.classList.toggle("problem", !!cls);

  const bar = $("#filebar");
  if (snap && snap.file.status !== "ok") {
    const when = snap.file.last_ok_at ? new Date(snap.file.last_ok_at).toLocaleTimeString("ru-RU") : "—";
    const what = snap.file.status === "missing" ? "Файл с инцидентами не найден" : "Файл с инцидентами повреждён или записан не до конца";
    bar.className = "filebar";
    bar.textContent = `⚠ ${what}. Показаны последние целые данные (прочитаны в ${when}). ${snap.file.error || ""}`;
  } else if (!online) {
    bar.className = "filebar bad";
    bar.textContent = "⚠ Нет связи с сервером дашборда. Данные могут быть устаревшими — переподключаемся…";
  } else {
    bar.className = "filebar hidden";
  }
}

// ---------- тревога на стене: красная полоса на всю ширину ----------
// Стена без звука и показывает только критические: полоса 5 секунд, дальше мигает карточка до «Взять в работу».
export function renderWallBanner(fresh, nowMs) {
  const el = $("#alerts");
  if (!fresh.length) { el.innerHTML = ""; el.className = "alerts"; return; }
  const inc = fresh[0].inc;
  const rem = inc.deadline ? (Date.parse(inc.deadline) - nowMs) / 1000 : null;
  const n = fresh.length;
  el.className = "alerts wall-banner";
  el.innerHTML = `<div class="alert critical">
      <span class="alert-kind">${n > 1 ? `! НОВЫХ КРИТИЧЕСКИХ: ${n}` : "! КРИТИЧЕСКИЙ"}</span>
      <span class="alert-text"><b>${esc(inc.product || "")}</b> · ${esc(inc.title)} · ${esc(inc.team || "")}</span>
      <span class="alert-time" data-deadline="${esc(inc.deadline || "")}">${rem == null ? "срок не задан" : (rem < 0 ? "просрочен " : "до срока ") + fmtLeft(rem)}</span>
    </div>`;
}

// ---------- тревоги: одна строка (АРМ) ----------
// Карточка тревоги мигает в своей колонке, внимание держат звук и голос. Снимает тревогу «Взять» —
// каждую берут отдельно (общей кнопки нет: decisions.md, п. 4).
export function renderAlertLine(alerts, handlers) {
  const el = $("#alerts");
  if (!alerts.length) { el.innerHTML = ""; el.className = "alerts"; return; }
  const crit = alerts.some((a) => a.group === "critical");
  const n = alerts.length;
  const max = 6;
  const oldest = Math.max(...alerts.map((a) => (a.since ? Date.now() - Date.parse(a.since) : 0)));
  const waitMin = Math.floor(oldest / 60000);
  el.className = `alerts alert-line ${crit ? "critical" : "coordinators"}`;
  el.innerHTML = `
    <span class="al-icon">🔔</span>
    <b class="al-title">Не взяты в работу: ${n}</b>
    <span class="al-chips">${alerts.slice(0, max).map((a) => `
      <span class="al-chip ${a.group}" data-id="${esc(a.incident_id)}" data-key="${esc(a.key)}" title="${esc(a.group === "critical" ? "Критический" : "На координаторах")} · ${esc(a.inc.title)}">
        <span class="al-name">${a.group === "critical" ? "! " : "→ "}${esc(a.inc.product || shortId(a.inc.id))}</span><button class="al-take" title="Взять в работу — тревога снимется у всех">Взять</button>
      </span>`).join("")}${n > max ? `<span class="al-rest">и ещё ${n - max}</span>` : ""}</span>
    <span class="al-wait">${waitMin ? `не взяты до ${waitMin} мин` : "только что"}</span>`;
  el.querySelectorAll(".al-chip").forEach((c) => {
    c.querySelector(".al-name").onclick = () => handlers.open(c.dataset.id);
    c.querySelector(".al-take").onclick = () => handlers.claim(c.dataset.id);
  });
}

// ---------- очередь «Что делать сейчас» (вкладка АРМ) ----------
export function renderQueue(snap, el, handlers, predicate) {
  const acts = (snap.actions || []).filter((a) => !predicate || predicate(snap.incidents[a.incident_id]));
  if (!acts.length) {
    el.innerHTML = `<div class="actions-empty">${predicate ? "По фильтру срочных действий нет" : "Срочных действий нет — всё под контролем"}</div>`;
    return;
  }
  el.innerHTML = acts.map((a) => {
    const inc = snap.incidents[a.incident_id] || {};
    const crit = inc.priority_level === "critical" ? "crit" : "";
    const conf = a.confidence != null ? ` · уверенность ${Math.round(a.confidence * 100)}%` : "";
    const claim = ownerOf(inc, snap);
    const main = a.kind === "route"
      ? `<button class="btn small btn-primary" data-act="transfer">${a.target_team ? "Передать" : "Выбрать команду"}</button>`
      : `<button class="btn small btn-primary" data-act="escalate">Эскалировать</button>`;
    return `<div class="action ${a.kind} ${crit}" data-id="${esc(a.id)}" data-inc="${esc(a.incident_id)}">
      <div class="action-head"><span>${a.kind === "route" ? "ПЕРЕДАТЬ" : "ЭСКАЛИРОВАТЬ"} · ${esc(inc.product || "")}</span>
        <span class="countdown-mini" data-deadline="${esc(inc.deadline || "")}"></span></div>
      <div class="action-title">${esc(a.title)}</div>
      <div class="action-reason">${a.kind === "route" ? aiTag(inc.ai) : ""}${esc(a.reason)}${conf}${claim ? ` · <b class="owner">👤 ${esc(firstName(claim.by))}</b>` : ""}</div>
      <div class="action-btns">
        ${main}
        <button class="btn small" data-act="copy" title="Скопировать текст ${a.kind === "route" ? "передачи" : "эскалации"}">Текст</button>
        <a class="btn small" href="${esc(inc.itsm_url || "#")}" target="_blank" rel="noopener">ITSM</a>
        <button class="btn small" data-act="open">Подробнее</button>
      </div>
    </div>`;
  }).join("");
  el.querySelectorAll(".action").forEach((card) => {
    const act = acts.find((x) => x.id === card.dataset.id);
    card.querySelector("[data-act=copy]").onclick = (e) => handlers.copy(act.draft, e.target);
    card.querySelector("[data-act=open]").onclick = () => handlers.open(card.dataset.inc);
    const t = card.querySelector("[data-act=transfer]");
    if (t) t.onclick = () => handlers.transfer(card.dataset.inc);
    const e = card.querySelector("[data-act=escalate]");
    if (e) e.onclick = () => handlers.escalate(card.dataset.inc, act.draft);
  });
}

// ---------- колонки ----------
const cardCache = new Map(); // `${group}:${id}` -> {sig, el}

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

// ui: {query, changed, wall, open, predicate, limits, more, selected, alarmIds, actions, act}
export function renderBoard(snap, nowMs, ui) {
  const board = $("#board");
  if (!board.children.length || board.dataset.groups !== snap.groups.map((g) => g.key).join(",")) {
    board.dataset.groups = snap.groups.map((g) => g.key).join(",");
    board.innerHTML = snap.groups.map((g) => `
      <section class="column" data-key="${g.key}" id="col-${g.key}">
        <div class="column-head"><h2>${esc(g.title)}</h2><span class="count"></span></div>
        <div class="column-body"></div>
        <div class="column-more hidden"></div>
      </section>`).join("");
  }
  const alive = new Set();
  const filtering = !!(ui.query || ui.predicate);
  const seenWall = new Set(); // стена: инцидент уже показан полной карточкой в левой колонке → здесь компактная строка
  const outages = new Map();
  const overlappingDuplicates = new Set();
  let shown = 0;
  if (ui.wall) {
    for (const outage of snap.outages || []) {
      for (const id of outage.incident_ids) outages.set(id, outage);
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
    const hasCrit = !!(crit && crit.ids.length);
    board.style.gridTemplateColumns = snap.groups.map((g) => g.key === "critical" ? (hasCrit ? "minmax(0, 2fr)" : "minmax(0, .75fr)") : "minmax(0, 1fr)").join(" ");
    board.classList.toggle("no-critical", !hasCrit);
  }
  for (const g of snap.groups) {
    const col = board.querySelector(`.column[data-key="${g.key}"]`);
    const body = col.querySelector(".column-body");
    const more = col.querySelector(".column-more");
    const ids = g.ids.filter((id) => matches(snap.incidents[id], ui.query) && (!ui.predicate || ui.predicate(snap.incidents[id])));
    shown += ids.length;
    const cnt = col.querySelector(".count");
    const n = snap.counts[g.key] ?? g.count;
    const f = failures(snap, g);
    cnt.innerHTML = filtering ? `${ids.length}<small> из ${g.ids.length}</small>`
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
      const sig = [inc.hash, inc.groups.join(), inc.resolved_in.join(), JSON.stringify(inc.ai), inc.forecast && inc.forecast.level,
        claim ? claim.by : "", inc.work_log.length, changed ? changed.until : 0, action ? action.id : "", compact, hideDuplicate].join("|");
      let entry = cardCache.get(key);
      if (!entry || entry.sig !== sig) {
        const el = entry ? entry.el : document.createElement("article");
        el.dataset.id = id;
        if (compact) fillCompactWall(el, inc, g.key, nowMs, claim);
        else if (ui.wall) fillCardWall(el, inc, g.key, nowMs, changed, claim, hideDuplicate);
        else fillCardArm(el, inc, g.key, nowMs, changed, claim, action, ui.act);
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
  if (ui.wall) fitColumns();
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
    const limit = body.clientHeight;
    let hidden = Number(col.dataset.rest || 0);
    for (const c of cards) {
      if (c.offsetTop - body.offsetTop + c.offsetHeight > limit) { c.style.display = "none"; hidden++; }
    }
    more.classList.toggle("hidden", hidden === 0);
    more.textContent = hidden ? `+ ещё ${hidden}` : "";
  });
}

function lastEscalation(inc) {
  for (let i = inc.work_log.length - 1; i >= 0; i--) if (inc.work_log[i].action === "escalate") return inc.work_log[i];
  return null;
}

function linkTags(inc) {
  const t = [];
  if (inc.is_master) t.push("мастер");
  if (inc.is_double) t.push("дубль");
  const d = inc.ai && inc.ai.duplicates;
  if (!inc.is_double && d && (Array.isArray(d) ? d.length : d.ids && d.ids.length)) t.push("дубли");
  return t.map((x) => `<span class="tag-dup" title="Связанные инциденты">${x}</span>`).join("");
}

// Пять полей из задания — на карточке: продукт, возник, исполнитель «команда / сотрудник», статус, срок-таймер.
export function cardTitle(inc) {
  // выжимка ИИ вместо длинной сути (оригинал — в панели); префикс «Продукт:» у выжимки срезаем — продукт уже в шапке
  const sum = inc.ai && inc.ai.summary;
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

// Решённый в этой группе (висит 30 мин) — сжатая зелёная строка «решён в 14:05»
function fillResolvedLine(el, inc, nowMs, wall) {
  el.className = "card is-resolved one-line";
  el.innerHTML = `<span class="ok">✓</span><b class="card-product">${esc(inc.product || "—")}</b>
    <span class="line-title">${esc(cardTitle(inc).text)}</span>
    <span class="line-when">решён${inc.resolved_at ? " в " + esc(fmtWhen(inc.resolved_at, nowMs)) : ""}${!wall && ownerName(inc) ? " · " + esc(ownerName(inc)) : ""}</span>`;
}

function stateNote(inc, nowMs) {
  if (inc.state_kind === "active") return "";
  const when = inc.resolved_at ? " " + esc(fmtWhen(inc.resolved_at, nowMs)) : "";
  return `<div class="resolved-note">✓ ${inc.state_kind === "research" ? `решён${when}, идёт разбор причин` : `${esc(inc.state || "решён")}${when}`}</div>`;
}

const metaLine = (inc, nowMs, wall) =>
  `<div class="card-meta1">возник ${esc(fmtWhen(inc.created_at, nowMs))} · ${executor(inc, wall)} · <span class="st st-${inc.state_kind}">${esc(inc.state || "—")}</span></div>`;

// Карточка АРМ
function fillCardArm(el, inc, group, nowMs, changed, claim, action, act) {
  if (inc.resolved_in.includes(group)) { fillResolvedLine(el, inc, nowMs, false); return; }
  el.className = "card";
  el.classList.toggle("is-critical", inc.priority_level === "critical");
  el.classList.toggle("flash", !!changed && changed.until > Date.now());
  const also = inc.groups.filter((g) => g !== group).map((g) => GROUP_SHORT[g]);
  const t = cardTitle(inc);
  const active = inc.state_kind === "active";

  const buttons = [];
  if (claim) buttons.push(`<span class="owner" title="координатор с ${esc(fmtTime(claim.at))}">в работе: ${esc(firstName(claim.by))}</span>`);
  else if (active) buttons.push(`<button class="btn small btn-take" data-act="claim" title="Клавиша В">Взять в работу</button>`);
  if (group === "coordinators" && active) buttons.push(`<button class="btn small btn-primary" data-act="transfer">Передать</button>`);
  if (action && action.kind === "escalate" && active) buttons.push(`<button class="btn small" data-act="escalate">Эскалировать</button>`);
  const esc_ = lastEscalation(inc);
  const tags = [
    esc_ ? `<span class="escalated" title="эскалирован">⇪ эскалирован ${esc(fmtTime(esc_.at))}</span>` : "",
    also.length ? `<span class="also" title="также в группах">также: ${also.join(", ")}</span>` : "",
  ].filter(Boolean).join("");

  el.innerHTML = `
    <div class="card-top">
      <span class="card-product">${esc(inc.product || "—")}</span>
      ${linkTags(inc)}
      <span class="card-prio prio-${inc.priority_level}">${esc(inc.priority || "")}</span>
      ${timer(inc, nowMs)}
    </div>
    <div class="card-title" title="${esc(inc.title || "")}">${t.ai ? aiTag(inc.ai) : ""}${esc(t.text)}</div>
    ${metaLine(inc, nowMs, false)}
    ${stateNote(inc, nowMs)}
    ${aiLineFor(inc, group)}
    ${buttons.length ? `<div class="card-btns">${buttons.join("")}</div>` : ""}
    ${tags ? `<div class="card-foot">${tags}</div>` : ""}
    ${changed && changed.until > Date.now() ? `<div class="changed">● ${esc(changed.texts.join(" · "))}</div>` : ""}`;

  const on = (sel, fn) => { const b = el.querySelector(sel); if (b) b.onclick = (e) => { e.stopPropagation(); fn(); }; };
  if (act) {
    on("[data-act=claim]", () => act.claim(inc.id));
    on("[data-act=transfer]", () => act.transfer(inc.id));
    on("[data-act=escalate]", () => act.escalate(inc.id, action && action.draft));
  }
}

// Карточка стены: крупно, без кнопок, ИИ и номера; исполнитель — только команда; «в работе у Ольги»
function fillCardWall(el, inc, group, nowMs, changed, claim, hideDuplicate) {
  if (inc.resolved_in.includes(group)) { fillResolvedLine(el, inc, nowMs, true); return; }
  el.className = "card";
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
    <div class="card-title">${esc(size > 1 && d.name ? d.name : cardTitle(inc).text)}</div>
    ${metaLine(inc, nowMs, true)}
    ${inc.state_kind === "active" ? `<div class="deadline">
        <div class="deadline-row">${timer(inc, nowMs)}${inc.deadline ? `<small>до ${esc(fmtWhen(inc.deadline, nowMs))}</small>` : ""}</div>
        ${fc ? `<div class="bar ${fc.level}"><i style="width:${Math.min(100, fc.progress * 100)}%"></i></div>` : ""}
      </div>` : stateNote(inc, nowMs)}
    ${workLine ? `<div class="work-line">${workLine}</div>` : ""}
    ${changed && changed.until > Date.now() ? `<div class="changed">● ${esc(changed.texts.join(" · "))}</div>` : ""}`;
}

// Стена, инцидент уже есть полной карточкой в колонке левее — здесь компактная строка
function fillCompactWall(el, inc, group, nowMs, claim) {
  if (inc.resolved_in.includes(group)) { fillResolvedLine(el, inc, nowMs, true); return; }
  el.className = "card one-line compact-row";
  el.classList.toggle("is-critical", inc.priority_level === "critical");
  el.innerHTML = `<b class="card-product">${esc(inc.product || "—")}</b>
    <span class="line-title">${esc(cardTitle(inc).text)}</span>
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
      el.textContent = (sec < 0 ? "просрочен " : "до срока ") + fmtLeft(sec);
    } else if (el.classList.contains("countdown-mini")) {
      el.textContent = sec < 0 ? "просрочен" : fmtLeft(sec);
    } else {
      const text = fmtTimer(sec);
      el.textContent = wall ? text.replace("осталось ", "") : text;
      const start = Date.parse(el.dataset.start || "");
      const frac = start && end > start ? (nowMs - start) / (end - start) : 0;
      const fc = el.dataset.fc;
      el.classList.toggle("overdue", sec < 0);
      el.classList.toggle("danger", sec >= 0 && sec < 900);
      el.classList.toggle("warn", sec >= 900 && (frac >= 0.75 || fc === "breach" || fc === "risk"));
    }
  });
}

// ---------- лента ----------
const EVENT_ICON = { became_critical: "🔴", critical_resolved: "✅", to_coordinators: "🟣", resolved: "✔", reopened: "↺",
  new: "＋", removed: "－", comment: "💬", escalated: "⇪" };
const feedItem = (e) => `<span class="feed-item alert-${e.alert || ""}" data-id="${esc(e.incident_id)}">${fmtTime(e.clock_at)} ${EVENT_ICON[e.type] || "•"} <b>${esc(e.product || shortId(e.incident_id))}</b> ${esc(e.text)}${e.by ? ` <i>· ${esc(firstName(e.by))}</i>` : ""}</span>`;

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
  el.innerHTML = `<div class="pop-title">Лента изменений — последние ${evs.length}</div>` + (evs.length
    ? `<div class="feed-list">${evs.map((e) => `<div class="feed-row">${feedItem(e)}</div>`).join("")}</div>`
    : `<div class="pop-empty">Изменений пока нет — правки в файле и действия координаторов появятся здесь</div>`);
  el.querySelectorAll(".feed-item").forEach((i) => (i.onclick = () => onOpen(i.dataset.id)));
}

// ---------- вкладка «Мои»: инциденты в работе у меня ----------
export function renderMine(snap, el, me, nowMs, ui) {
  const incs = Object.values(snap.incidents).filter((inc) => me && ownerOf(inc, snap) && ownerOf(inc, snap).by === me);
  incs.sort((x, y) => (x.state_kind === "active" ? 0 : 1) - (y.state_kind === "active" ? 0 : 1)
    || (Date.parse(x.deadline || "2999-01-01") - Date.parse(y.deadline || "2999-01-01")));
  if (!me) { el.innerHTML = `<div class="actions-empty">Представьтесь — тогда здесь будут инциденты, которые вы взяли в работу</div>`; return 0; }
  if (!incs.length) { el.innerHTML = `<div class="actions-empty">Вы пока ничего не взяли в работу. «Взять в работу» — на карточке, в панели или клавиша В</div>`; return 0; }
  el.innerHTML = "";
  for (const inc of incs) {
    const card = document.createElement("article");
    card.dataset.id = inc.id;
    const group = inc.groups.find((g) => !inc.resolved_in.includes(g)) || inc.groups[0] || "";
    const action = ui.actions ? ui.actions.get(inc.id) : null;
    fillCardArm(card, inc, group, nowMs, ui.changed.get(inc.id), ownerOf(inc, snap), action, ui.act);
    card.classList.toggle("selected", ui.selected === inc.id);
    card.onclick = () => ui.open(inc.id);
    el.appendChild(card);
  }
  return incs.length;
}

export function toast(text, kind = "") {
  const box = $("#toasts");
  if (!box) return;
  const t = document.createElement("div");
  t.className = `toast ${kind}`;
  t.textContent = text;
  box.appendChild(t);
  setTimeout(() => t.remove(), 8000);
}
