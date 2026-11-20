// Отрисовка — общая для АРМ координатора и видеостены (ui.wall).
// Карточки кэшируются по подписи (hash + группы + ИИ + «в работе» + журнал), чтобы не мигать
// и не сбрасывать подсветку; таймеры обновляются отдельно раз в секунду.
import { esc, fmtDateTime, fmtTime, fmtLeft, plural, shortId } from "./format.js";
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
const FORECAST_SHORT = { ok: "успевают", risk: "на исходе", breach: "не успеют", overdue: "" };
const RISK_TEXT = { high: "высокий", medium: "средний", low: "низкий" };
const AI_SOURCE = { rules: "правила по истории", gigachat: "GigaChat", cache: "GigaChat (кэш)" };

const WALL_MAX = 30;   // на стене в колонке не больше стольких карточек в DOM, остальное — «+ ещё N»
const ARM_PAGE = 60;   // на АРМ список подгружается порциями

const $ = (sel) => document.querySelector(sel);
const firstName = (s) => (s || "").split(" (")[0];
const aiTag = (ai) => `<span class="ai-tag" title="Подсказка ИИ · ${esc(AI_SOURCE[ai && ai.source] || "правила")}">ИИ</span>`;

// ---------- шапка ----------
export function renderCounters(snap, onJump, opts = {}) {
  const el = $("#counters");
  el.innerHTML = snap.groups.map((g) => {
    const n = snap.counts[g.key] ?? g.count;
    if (opts.compact) {
      return `<button class="counter compact ${g.key} ${n ? "has" : ""}" data-key="${g.key}" title="${esc(g.title)}"><span>${COUNTER_SHORT[g.key] || esc(g.title)}</span><b>${n}</b></button>`;
    }
    const [a, b] = COUNTER_LABEL[g.key] || [g.title, ""];
    return `<button class="counter ${g.key} ${n ? "has" : ""}" data-key="${g.key}"><b>${n}</b><span>${a}<br>${b}</span></button>`;
  }).join("");
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

// ---------- тревоги: большие плашки (стена) ----------
export function renderAlerts(alerts, nowMs, handlers, opts = {}) {
  const el = $("#alerts");
  const max = 2;
  const shown = alerts.slice(0, max);
  el.innerHTML = shown.map((a) => {
    const inc = a.inc;
    const rem = inc.deadline ? (Date.parse(inc.deadline) - nowMs) / 1000 : null;
    const kind = a.group === "critical" ? "! КРИТИЧЕСКИЙ" : "→ НА КООРДИНАТОРАХ";
    const route = a.group === "coordinators" && inc.ai && inc.ai.route ? ` · рекомендуем: «${esc(inc.ai.route.team)}»` : "";
    const waiting = a.since ? Math.floor((Date.now() - Date.parse(a.since)) / 60000) : 0;
    return `<div class="alert ${a.group}">
      <span class="alert-kind">${kind}</span>
      <span class="alert-text"><b>${esc(inc.product || "")}</b> · ${esc(inc.title)} · ${esc(shortId(inc.id))} · ${esc(inc.team || "")}${route}</span>
      <span class="alert-time" data-deadline="${esc(inc.deadline || "")}">${rem == null ? "срок —" : (rem < 0 ? "просрочен " : "до срока ") + fmtLeft(rem)}</span>
      <span class="alert-wait">ждёт «Принял» ${waiting ? waiting + " мин" : "сейчас"}</span>
    </div>`;
  }).join("") + (alerts.length > max ? `<div class="alert-more">ещё ${alerts.length - max} непринятых тревог</div>` : "");
}

// ---------- тревоги: одна строка (АРМ) ----------
// Карточка тревоги мигает в своей колонке, внимание держат звук и голос.
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
    <b class="al-title">${n} ${plural(n, "новая тревога", "новые тревоги", "новых тревог")}:</b>
    <span class="al-chips">${alerts.slice(0, max).map((a) => `
      <span class="al-chip ${a.group}" data-id="${esc(a.incident_id)}" data-key="${esc(a.key)}" title="${esc(a.group === "critical" ? "Критический" : "На координаторах")} · ${esc(a.inc.title)}">
        <span class="al-name">${a.group === "critical" ? "! " : "→ "}${esc(a.inc.product || shortId(a.inc.id))}</span><button class="al-ack" title="Принял">✓</button>
      </span>`).join("")}${n > max ? `<span class="al-rest">и ещё ${n - max}</span>` : ""}</span>
    <span class="al-wait">${waitMin ? `ждут до ${waitMin} мин` : "только что"}</span>
    <button class="btn btn-ack" data-act="ackall" title="Пробел — принять первую">Принять все</button>`;
  el.querySelectorAll(".al-chip").forEach((c) => {
    c.querySelector(".al-name").onclick = () => handlers.open(c.dataset.id);
    c.querySelector(".al-ack").onclick = () => handlers.ack(c.dataset.key);
  });
  el.querySelector("[data-act=ackall]").onclick = () => handlers.ackAll(alerts.map((a) => a.key));
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
    const claim = (snap.claims || {})[a.incident_id];
    const main = a.kind === "route"
      ? `<button class="btn small btn-primary" data-act="transfer">${a.target_team ? "Передать" : "Выбрать команду"}</button>`
      : `<button class="btn small btn-primary" data-act="escalate">Эскалировать</button>`;
    return `<div class="action ${a.kind} ${crit}" data-id="${esc(a.id)}" data-inc="${esc(a.incident_id)}">
      <div class="action-head"><span>${a.kind === "route" ? "ПЕРЕДАТЬ" : "ЭСКАЛИРОВАТЬ"} · ${esc(shortId(a.incident_id))} · ${esc(inc.product || "")}</span>
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

// ui: {query, changed, wall, open, predicate, limits, more, selected, why, alarmIds, actions, act}
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
  const claims = snap.claims || {};
  const alive = new Set();
  const filtering = !!(ui.query || ui.predicate);
  let shown = 0;
  for (const g of snap.groups) {
    const col = board.querySelector(`.column[data-key="${g.key}"]`);
    const body = col.querySelector(".column-body");
    const more = col.querySelector(".column-more");
    const ids = g.ids.filter((id) => matches(snap.incidents[id], ui.query) && (!ui.predicate || ui.predicate(snap.incidents[id])));
    shown += ids.length;
    const cnt = col.querySelector(".count");
    cnt.innerHTML = filtering ? `${ids.length}<small> из ${g.ids.length}</small>` : String(snap.counts[g.key] ?? g.count);
    const limit = ui.wall ? WALL_MAX : (ui.limits && ui.limits[g.key]) || ARM_PAGE;
    const page = ids.slice(0, limit);
    const els = [];
    for (const id of page) {
      const inc = snap.incidents[id];
      const key = `${g.key}:${id}`;
      alive.add(key);
      const changed = ui.changed.get(id);
      const claim = claims[id];
      const action = ui.actions ? ui.actions.get(id) : null;
      const why = ui.why ? ui.why.has(key) : false;
      const sig = [inc.hash, inc.groups.join(), inc.resolved_in.join(), JSON.stringify(inc.ai), inc.forecast && inc.forecast.level,
        claim ? claim.by : "", inc.work_log.length, changed ? changed.until : 0, action ? action.id : "", why].join("|");
      let entry = cardCache.get(key);
      if (!entry || entry.sig !== sig) {
        const el = entry ? entry.el : document.createElement("article");
        el.className = "card";
        el.dataset.id = id;
        if (ui.wall) fillCardWall(el, inc, g.key, nowMs, changed, claim);
        else fillCardArm(el, inc, g.key, nowMs, changed, claim, action, why, ui.act);
        if (ui.open) el.onclick = () => ui.open(id);
        entry = { sig, el };
        cardCache.set(key, entry);
      }
      if (!ui.wall) {
        entry.el.classList.toggle("selected", ui.selected === id);
        entry.el.classList.toggle("alarm", !!(ui.alarmIds && ui.alarmIds.has(id)) && !inc.resolved_in.includes(g.key));
      }
      els.push(entry.el);
    }
    body.querySelector(".column-empty")?.remove();
    els.forEach((el, i) => { if (body.children[i] !== el) body.insertBefore(el, body.children[i] || null); });
    while (body.children.length > els.length) body.lastChild.remove();
    if (!els.length) body.innerHTML = `<div class="column-empty">${filtering ? "нет по фильтру" : "пусто"}</div>`;

    const rest = ids.length - page.length;
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

// Видеостена не скроллится: показываем столько карточек, сколько влезает, и «+ ещё N»; счётчики в шапке точные
export function fitColumns() {
  document.querySelectorAll(".column").forEach((col) => {
    const body = col.querySelector(".column-body");
    const more = col.querySelector(".column-more");
    const cards = [...body.querySelectorAll(".card")];
    cards.forEach((c) => (c.style.display = ""));
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

// Карточка АРМ: продукт, суть, сколько осталось и одно действие. Остальное — в панели справа.
function fillCardArm(el, inc, group, nowMs, changed, claim, action, why, act) {
  const resolvedHere = inc.resolved_in.includes(group);
  el.classList.toggle("is-critical", inc.priority_level === "critical" && !resolvedHere);
  el.classList.toggle("is-resolved", resolvedHere);
  el.classList.toggle("flash", !!changed && changed.until > Date.now());

  const ai = inc.ai || {};
  const fc = inc.forecast;
  const also = inc.groups.filter((g) => g !== group).map((g) => GROUP_SHORT[g]);
  const key = `${group}:${inc.id}`;

  let time;
  if (resolvedHere || inc.state_kind !== "active") {
    time = `<div class="resolved-note">✓ ${inc.state_kind === "research" ? "решён, идёт разбор" : "решён" + (inc.resolved_at ? " " + esc(fmtDateTime(inc.resolved_at, nowMs)) : "")}</div>`;
  } else {
    time = `<div class="card-time">
      <span class="countdown" data-deadline="${esc(inc.deadline || "")}">${inc.deadline ? "" : "срок —"}</span>
      ${fc && FORECAST_SHORT[fc.level] ? `<span class="fc fc-${fc.level}" title="${esc(FORECAST_TEXT[fc.level] + " · " + fc.basis)}">${FORECAST_SHORT[fc.level]}</span>` : ""}
    </div>`;
  }

  let aiLine = "", whyText = "", buttons = [];
  if (group === "coordinators" && !resolvedHere) {
    if (ai.route) {
      aiLine = `<div class="ai-line route">${aiTag(ai)}в «<b>${esc(ai.route.team)}</b>», ${Math.round(ai.route.confidence * 100)}%</div>`;
      whyText = esc(ai.route.reason) + (ai.route.alternatives && ai.route.alternatives.length
        ? `<br>ещё: ${ai.route.alternatives.slice(0, 2).map((x) => `${esc(x.team)} ${Math.round(x.score * 100)}%`).join(", ")}` : "");
    }
    if (inc.state_kind !== "resolved" && inc.state_kind !== "closed") buttons.push(`<button class="btn small btn-primary" data-act="transfer">${ai.route ? "Передать" : "Выбрать команду"}</button>`);
  } else if (group === "high" && ai.risk && !resolvedHere) {
    aiLine = `<div class="ai-line">${aiTag(ai)}риск: <b class="risk-${ai.risk.level}">${RISK_TEXT[ai.risk.level] || ai.risk.level}</b> · ${ai.risk.score}</div>`;
    whyText = esc(ai.risk.factors.join(" · ") || "значимых факторов нет");
  }
  if (action && action.kind === "escalate" && !resolvedHere) buttons.push(`<button class="btn small btn-primary" data-act="escalate">Эскалировать</button>`);
  if (whyText) buttons.push(`<button class="btn small btn-ghost" data-act="why">${why ? "скрыть" : "почему?"}</button>`);

  const esc_ = lastEscalation(inc);
  const foot = [
    claim ? `<span class="owner" title="в работе у координатора">👤 ${esc(firstName(claim.by))}</span>` : "",
    esc_ ? `<span class="escalated" title="эскалирован">⇪ ${esc(fmtTime(esc_.at))}</span>` : "",
    also.length ? `<span class="also" title="также в группах">также: ${also.join(", ")}</span>` : "",
  ].filter(Boolean).join("");

  el.innerHTML = `
    <div class="card-top">
      <span class="card-product">${esc(inc.product || "—")}</span>
      ${linkTags(inc)}
      ${inc.priority_level === "critical" ? "" : `<span class="card-prio prio-${inc.priority_level}">${esc(inc.priority || "")}</span>`}
    </div>
    <div class="card-title">${esc(inc.title)}</div>
    ${time}
    ${aiLine}
    ${why && whyText ? `<div class="why">${whyText}</div>` : ""}
    ${buttons.length ? `<div class="card-btns">${buttons.join("")}</div>` : ""}
    ${foot ? `<div class="card-foot">${foot}</div>` : ""}
    ${changed && changed.until > Date.now() ? `<div class="changed">● ${esc(changed.texts.join(" · "))}</div>` : ""}`;

  const on = (sel, fn) => { const b = el.querySelector(sel); if (b) b.onclick = (e) => { e.stopPropagation(); fn(); }; };
  if (act) {
    on("[data-act=transfer]", () => act.transfer(inc.id));
    on("[data-act=escalate]", () => act.escalate(inc.id, action && action.draft));
    on("[data-act=why]", () => act.why(key));
  }
}

// Карточка стены: крупно и с метаданными — читается с другого конца зала
function fillCardWall(el, inc, group, nowMs, changed, claim) {
  const resolvedHere = inc.resolved_in.includes(group);
  el.classList.toggle("is-critical", inc.priority_level === "critical" && !resolvedHere);
  el.classList.toggle("is-resolved", resolvedHere);
  el.classList.toggle("flash", !!changed && changed.until > Date.now());

  const also = inc.groups.filter((g) => g !== group).map((g) => GROUP_SHORT[g]);
  const fc = inc.forecast;
  const ai = inc.ai || {};
  const aiLine = ai.route && group === "coordinators"
    ? `<div class="ai-line route">${aiTag(ai)}→ <b>${esc(ai.route.team)}</b> · ${Math.round(ai.route.confidence * 100)}%</div>` : "";
  const esc_ = lastEscalation(inc);
  const workLine = [
    claim ? `<span class="owner">👤 в работе: ${esc(firstName(claim.by))}</span>` : "",
    esc_ ? `<span class="escalated">⇪ эскалирован ${esc(fmtTime(esc_.at))}${esc_.author ? " · " + esc(firstName(esc_.author)) : ""}</span>` : "",
  ].filter(Boolean).join("");
  const deadlineBlock = resolvedHere || inc.state_kind !== "active"
    ? `<div class="resolved-note">✓ решён ${inc.resolved_at ? esc(fmtDateTime(inc.resolved_at, nowMs)) : ""}${inc.state_kind === "research" ? " · идёт разбор причин" : " · " + esc(inc.state)}</div>`
    : `<div class="deadline">
        <div class="deadline-row">
          <span class="countdown" data-deadline="${esc(inc.deadline || "")}">${inc.deadline ? "" : "срок —"}</span>
          <small>${inc.deadline ? "до " + esc(fmtDateTime(inc.deadline, nowMs)) : ""}</small>
        </div>
        ${fc ? `<div class="bar ${fc.level}"><i style="width:${Math.min(100, fc.progress * 100)}%"></i></div>
        <div class="forecast ${fc.level}" title="${esc(fc.basis)}">${FORECAST_TEXT[fc.level]}${fc.median_minutes ? ` · обычно ~${fc.median_minutes} мин` : ""}</div>` : ""}
      </div>`;

  el.innerHTML = `
    <div class="card-top">
      <span class="card-product">${esc(inc.product || "—")}</span>
      <span class="card-prio prio-${inc.priority_level}">${esc(inc.priority || "")}</span>
    </div>
    <div class="card-title">${esc(inc.title)}</div>
    <div class="card-meta">
      <span>${esc(shortId(inc.id))}</span><b>${esc(fmtDateTime(inc.created_at, nowMs))}</b>
      <span>команда</span><b title="${esc(inc.team)}">${esc(inc.team || "—")}</b>
    </div>
    ${deadlineBlock}
    ${aiLine}
    ${workLine ? `<div class="work-line">${workLine}</div>` : ""}
    ${changed && changed.until > Date.now() ? `<div class="changed">● ${esc(changed.texts.join(" · "))}</div>` : ""}
    ${also.length ? `<div class="also">также в: <span>${also.join(", ")}</span></div>` : ""}`;
}

// Таймеры раз в секунду
export function tickCountdowns(nowMs) {
  const left = document.body.classList.contains("wall") ? "" : "осталось ";
  document.querySelectorAll("[data-deadline]").forEach((el) => {
    const dl = el.dataset.deadline;
    if (!dl) return;
    const sec = (Date.parse(dl) - nowMs) / 1000;
    if (el.classList.contains("alert-time")) {
      el.textContent = (sec < 0 ? "просрочен " : "до срока ") + fmtLeft(sec);
    } else if (el.classList.contains("countdown-mini")) {
      el.textContent = sec < 0 ? "просрочен" : fmtLeft(sec);
    } else {
      el.textContent = sec < 0 ? `просрочен ${fmtLeft(-sec)}` : left + fmtLeft(sec);
      el.classList.toggle("overdue", sec < 0);
      el.classList.toggle("soon", sec >= 0 && sec < 3600);
    }
  });
}

// ---------- лента ----------
const EVENT_ICON = { became_critical: "🔴", critical_resolved: "✅", to_coordinators: "🟣", resolved: "✔", reopened: "↺",
  new: "＋", removed: "－", comment: "💬", escalated: "⇪" };
const feedItem = (e) => `<span class="feed-item alert-${e.alert || ""}" data-id="${esc(e.incident_id)}">${fmtTime(e.clock_at)} ${EVENT_ICON[e.type] || "•"} <b>${esc(shortId(e.incident_id))}</b> ${esc(e.text)}${e.by ? ` <i>· ${esc(firstName(e.by))}</i>` : ""}</span>`;

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
  el.innerHTML = `<div class="pop-title">Лента изменений</div>` + (evs.length
    ? `<div class="feed-list">${evs.map((e) => `<div class="feed-row">${feedItem(e)}</div>`).join("")}</div>`
    : `<div class="pop-empty">Изменений пока нет — правки в файле и действия координаторов появятся здесь</div>`);
  el.querySelectorAll(".feed-item").forEach((i) => (i.onclick = () => onOpen(i.dataset.id)));
}

// ---------- панель выбранного инцидента (АРМ) ----------
export function renderDrawer(inc, snap, nowMs, handlers, meta) {
  const el = $("#drawer");
  if (!inc) { el.classList.add("hidden"); el.innerHTML = ""; el.dataset.id = ""; return; }
  el.classList.remove("hidden");
  // при обновлении снимка не теряем набранный комментарий и прокрутку
  const same = el.dataset.id === inc.id;
  const prevInput = el.querySelector(".comment-form input");
  const keep = same && prevInput ? { v: prevInput.value, f: document.activeElement === prevInput } : null;
  const scroll = same ? el.scrollTop : 0;
  el.dataset.id = inc.id;
  const ai = inc.ai || {};
  const fc = inc.forecast;
  const action = (snap.actions || []).find((a) => a.incident_id === inc.id);
  const route = ai.route;
  const claim = (snap.claims || {})[inc.id];
  const alert = (snap.alerts || []).find((a) => a.incident_id === inc.id && !a.acked_by);
  const open = (id) => `<span class="linklike" data-open="${esc(id)}">${esc(shortId(id))}</span>`;
  const similar = route ? route.similar_ids.map(open).join(", ") : "";
  const groups = inc.groups.map((g) => GROUP_SHORT[g] + (inc.resolved_in.includes(g) ? " (решён)" : "")).join(", ") || "не в группах";
  const transitions = meta ? (meta.transitions[inc.state_id] || []) : [];
  const stateBtns = transitions.map((s) => `<button class="btn small" data-state="${s}">→ ${esc(meta.states[s])}</button>`).join("");
  const canWork = inc.state_kind !== "closed";
  const d = ai.duplicates;
  const dupIds = Array.isArray(d) ? d : d && d.ids ? d.ids : [];
  const links = [inc.is_master && "мастер", inc.is_double && "дубль", inc.is_reason && "дочерний", inc.is_effect && "причина других"].filter(Boolean);
  const active = inc.state_kind === "active";

  el.innerHTML = `
    <div class="panel-head">
      <span class="card-prio prio-${inc.priority_level}">${esc(inc.priority || "")}</span>
      <span class="chip st-${inc.state_kind}">${esc(inc.state || "—")}</span>
      <button class="btn small drawer-close" data-act="close" title="Esc">✕</button>
    </div>
    <h2>${esc(inc.product || "—")}</h2>
    <div class="panel-title">${esc(inc.title)}</div>
    <div class="panel-sub">${esc(inc.id)} · ${groups}</div>

    ${alert ? `<div class="panel-alert ${alert.group}">🔔 Тревога: ${alert.group === "critical" ? "критический" : "назначен на координаторов"} <button class="btn small btn-ack" data-act="ack">Принял</button></div>` : ""}

    <dl class="kv facts">
      <dt>Срок</dt><dd>${active && inc.deadline ? `<span class="countdown" data-deadline="${esc(inc.deadline)}"></span> <small>до ${esc(fmtDateTime(inc.deadline, nowMs))}</small>` : esc(fmtDateTime(inc.deadline, nowMs))}</dd>
      ${fc ? `<dt>Прогноз</dt><dd><span class="forecast ${fc.level}">${FORECAST_TEXT[fc.level]}</span> <small>прошло ${Math.round(fc.progress * 100)}% срока${fc.eta ? " · решат ~" + esc(fmtDateTime(fc.eta, nowMs)) : ""}</small></dd>` : ""}
      ${inc.resolved_at ? `<dt>Завершён</dt><dd>${esc(fmtDateTime(inc.resolved_at, nowMs))}</dd>` : ""}
      <dt>Команда</dt><dd>${esc(inc.team || "—")}</dd>
      <dt>Исполнитель</dt><dd>${esc(inc.assignee || "не назначен")}</dd>
      <dt>Координатор</dt><dd>${claim ? `👤 <b>${esc(claim.by)}</b> · с ${esc(fmtTime(claim.at))}` : "никто не взял в работу"}</dd>
    </dl>

    <div class="drawer-btns">
      ${claim ? `<button class="btn small" data-act="release">Отпустить</button>` : `<button class="btn small" data-act="claim">Взять в работу</button>`}
      ${canWork && inc.state_kind !== "resolved" ? `<button class="btn small btn-primary" data-act="transfer">Передать</button>` : ""}
      ${active ? `<button class="btn small" data-act="escalate">Эскалировать</button>` : ""}
      <a class="btn small" href="${esc(inc.itsm_url || "#")}" target="_blank" rel="noopener">ITSM ↗</a>
    </div>
    ${stateBtns ? `<div class="drawer-btns"><span class="muted-label">Статус:</span>${stateBtns}</div>` : ""}
    <form class="comment-form" data-act="comment"><input name="text" placeholder="Комментарий в журнал работ…" autocomplete="off"><button class="btn small">Добавить</button></form>

    <h3>${aiTag(ai)} Подсказки ИИ <small class="ai-src">${esc(AI_SOURCE[ai.source] || "")}</small></h3>
    ${ai.summary ? `<div class="ai-box"><b>Выжимка:</b> ${esc(ai.summary)}</div>` : ""}
    ${route ? `<div class="ai-box"><b>Кому передать: ${esc(route.team)}</b> · уверенность ${Math.round(route.confidence * 100)}%
      <div class="ai-why"><b>Почему:</b> ${esc(route.reason)}</div>
      ${route.alternatives.length ? `<div class="ai-why">Альтернативы: ${route.alternatives.map((x) => `${esc(x.team)} (${Math.round(x.score * 100)}%)`).join(", ")}</div>` : ""}
      ${similar ? `<div class="ai-why">Похожие случаи: ${similar}</div>` : ""}</div>` : ""}
    ${ai.risk ? `<div class="ai-box"><b>Риск стать критическим: <span class="risk-${ai.risk.level}">${RISK_TEXT[ai.risk.level] || ""} · ${ai.risk.score}/100</span></b>
      <div class="ai-why"><b>Почему:</b> ${esc(ai.risk.factors.join(" · ") || "значимых факторов нет")}</div></div>` : ""}
    ${dupIds.length || links.length ? `<div class="ai-box"><b>Дубли и связи:</b> ${links.join(", ") || ""}${dupIds.length ? `${links.length ? "<br>" : ""}похожие заявки об этом же сбое: ${dupIds.map(open).join(", ")}` : ""}
      ${d && d.reason ? `<div class="ai-why"><b>Почему:</b> ${esc(d.reason)}</div>` : ""}</div>` : ""}
    ${!ai.summary && !route && !ai.risk && !dupIds.length && !links.length ? `<div class="pop-empty">Подсказок нет</div>` : ""}

    ${action ? `<h3>Черновик ${action.kind === "route" ? "передачи" : "эскалации"} <span class="linklike" data-act="copy">скопировать</span></h3><div class="draft">${esc(action.draft)}</div>` : ""}

    <h3>Журнал работ</h3>
    ${inc.work_log.length ? `<div class="worklog">${inc.work_log.slice().reverse().map((w) => `
      <div class="wl"><span>${esc(fmtDateTime(w.at, nowMs))}</span><b>${esc(w.author || "")}</b><div>${esc(w.text || "")}</div></div>`).join("")}</div>`
      : `<div class="pop-empty">записей нет</div>`}

    <h3>Описание</h3><div class="panel-text">${esc(inc.description || "—")}</div>
    ${inc.solution && inc.state_kind !== "active" ? `<h3>Решение</h3><div class="panel-text">${esc(inc.solution)}</div>` : ""}

    <h3>Карточка</h3>
    <dl class="kv">
      <dt>Статус</dt><dd>${esc(inc.state)}${inc.state_reason && inc.state_reason !== "Нет" ? " · " + esc(inc.state_reason) : ""}</dd>
      <dt>Возник</dt><dd>${esc(fmtDateTime(inc.created_at, nowMs))}</dd>
      <dt>Сервис</dt><dd>${esc(inc.service || "—")}</dd>
      <dt>Влияние / срочность</dt><dd>${esc(inc.impact || "—")} / ${esc(inc.urgency || "—")}</dd>
      <dt>Тип сервиса</dt><dd>${esc(inc.service_type || "—")}</dd>
      <dt>Канал</dt><dd>${esc(inc.channel || "—")}</dd>
      <dt>Связи</dt><dd>${links.join(", ") || "—"}</dd>
    </dl>`;

  const on = (sel, fn) => { const b = el.querySelector(sel); if (b) b.onclick = fn; };
  on("[data-act=close]", handlers.close);
  on("[data-act=ack]", () => handlers.ack(alert.key));
  on("[data-act=claim]", () => handlers.claim(inc.id));
  on("[data-act=release]", () => handlers.release(inc.id));
  on("[data-act=transfer]", () => handlers.transfer(inc.id));
  on("[data-act=escalate]", () => handlers.escalate(inc.id, action && action.kind === "escalate" ? action.draft : null));
  on("[data-act=copy]", (e) => handlers.copy(action.draft, e.target));
  el.querySelectorAll("[data-state]").forEach((b) => (b.onclick = () => handlers.setState(inc.id, Number(b.dataset.state))));
  el.querySelectorAll("[data-open]").forEach((s) => (s.onclick = () => handlers.open(s.dataset.open)));
  const form = el.querySelector("[data-act=comment]");
  form.onsubmit = (e) => {
    e.preventDefault();
    const t = form.text.value.trim();
    if (t) { handlers.comment(inc.id, t); form.text.value = ""; }
  };
  if (keep) { form.text.value = keep.v; if (keep.f) form.text.focus(); }
  el.scrollTop = scroll;
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
