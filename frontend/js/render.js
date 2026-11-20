// Отрисовка — общая для АРМ координатора и видеостены (ui.wall).
// Карточки кэшируются по подписи (hash + группы + ИИ + «в работе» + журнал), чтобы не мигать
// и не сбрасывать подсветку; таймеры обновляются отдельно раз в секунду.
import { esc, fmtDateTime, fmtTime, fmtLeft, shortId } from "./format.js";

const GROUP_SHORT = { critical: "критические", coordinators: "на координаторах", high: "высокий", research: "исследование" };
const COUNTER_LABEL = {
  critical: ["Критические", ""],
  coordinators: ["На коорди-", "наторах"],
  high: ["Высокий", "приоритет"],
  research: ["Исследо-", "вание"],
};
const FORECAST_TEXT = { ok: "успевают", risk: "срок на исходе", breach: "прогноз: не успеют", overdue: "срок нарушен" };

const $ = (sel) => document.querySelector(sel);
const firstName = (s) => (s || "").split(" (")[0];

// ---------- шапка ----------
export function renderCounters(snap, onJump) {
  const el = $("#counters");
  el.innerHTML = snap.groups.map((g) => {
    const n = snap.counts[g.key] ?? g.count;
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
  if (!online) { cls = "bad"; text = `нет связи с сервером · ${ago != null ? ago + " с назад" : ""}`; }
  else if (snap && snap.file.status !== "ok") { cls = "warn"; text = "файл не читается"; }
  $("#status").innerHTML = `<span class="dot ${cls}"></span>${text}`;

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

export function renderSoundBtn(enabled, voiceOk) {
  const b = $("#sound-btn");
  if (b) b.textContent = enabled ? (voiceOk ? "🔊 звук + голос" : "🔊 звук") : "🔇 без звука";
}

// ---------- тревоги ----------
export function renderAlerts(alerts, nowMs, handlers, opts = {}) {
  const el = $("#alerts");
  const max = opts.wall ? 2 : 3;
  const shown = alerts.slice(0, max);
  el.innerHTML = shown.map((a) => {
    const inc = a.inc;
    const rem = inc.deadline ? (Date.parse(inc.deadline) - nowMs) / 1000 : null;
    const kind = a.group === "critical" ? "! КРИТИЧЕСКИЙ" : "→ НА КООРДИНАТОРАХ";
    const route = a.group === "coordinators" && inc.ai && inc.ai.route ? ` · рекомендуем: «${esc(inc.ai.route.team)}»` : "";
    const waiting = a.since ? Math.floor((Date.now() - Date.parse(a.since)) / 60000) : 0;
    const wait = `<span class="alert-wait">ждёт «Принял» ${waiting ? waiting + " мин" : "сейчас"}</span>`;
    const buttons = opts.wall ? wait : `
      <a class="btn" href="${esc(inc.itsm_url || "#")}" target="_blank" rel="noopener">ITSM</a>
      <button class="btn" data-act="open">Подробнее</button>
      <button class="btn btn-ack" data-act="ack" title="Пробел">Принял</button>`;
    return `<div class="alert ${a.group}" data-key="${esc(a.key)}" data-id="${esc(inc.id)}">
      <span class="alert-kind">${kind}</span>
      <span class="alert-text"><b>${esc(inc.product || "")}</b> · ${esc(inc.title)} · ${esc(shortId(inc.id))} · ${esc(inc.team || "")}${route}</span>
      <span class="alert-time" data-deadline="${esc(inc.deadline || "")}">${rem == null ? "срок —" : (rem < 0 ? "просрочен " : "до срока ") + fmtLeft(rem)}</span>
      ${buttons}
    </div>`;
  }).join("") + (alerts.length > max ? `<div class="alert-more">ещё ${alerts.length - max} непринятых тревог${opts.wall ? "" : ` · <span class="linklike" data-act="ackall">принять все</span>`}</div>` : "");
  if (opts.wall) return;
  el.querySelectorAll(".alert").forEach((row) => {
    row.querySelector("[data-act=ack]").onclick = () => handlers.ack(row.dataset.key);
    row.querySelector("[data-act=open]").onclick = () => handlers.open(row.dataset.id);
  });
  const all = el.querySelector("[data-act=ackall]");
  if (all) all.onclick = () => handlers.ackAll(alerts.map((a) => a.key));
}

// ---------- что делать сейчас ----------
export function renderActions(snap, handlers) {
  const el = $("#actions");
  const acts = (snap.actions || []).slice(0, 6);
  if (!acts.length) {
    el.innerHTML = `<div class="actions-empty">Срочных действий нет — всё под контролем</div>`;
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
      <div class="action-reason">${esc(a.reason)}${conf}${claim ? ` · <b class="owner">👤 ${esc(firstName(claim.by))}</b>` : ""}</div>
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
    if (e) e.onclick = (ev) => handlers.escalate(card.dataset.inc, act.draft, ev.target);
  });
}

// ---------- колонки ----------
const cardCache = new Map(); // `${group}:${id}` -> {sig, el}

function matches(inc, q) {
  if (!q) return true;
  const hay = [inc.id, inc.title, inc.product, inc.service, inc.team, inc.assignee, inc.state, inc.priority].join(" ").toLowerCase();
  return q.toLowerCase().split(/\s+/).every((w) => hay.includes(w));
}

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
  for (const g of snap.groups) {
    const col = board.querySelector(`.column[data-key="${g.key}"]`);
    const body = col.querySelector(".column-body");
    const ids = g.ids.filter((id) => matches(snap.incidents[id], ui.query));
    col.querySelector(".count").textContent = snap.counts[g.key] ?? g.count;
    const els = [];
    for (const id of ids) {
      const inc = snap.incidents[id];
      const key = `${g.key}:${id}`;
      alive.add(key);
      const changed = ui.changed.get(id);
      const claim = claims[id];
      const sig = [inc.hash, inc.groups.join(), inc.resolved_in.join(), JSON.stringify(inc.ai), inc.forecast && inc.forecast.level,
        claim ? claim.by : "", inc.work_log.length, changed ? changed.until : 0].join("|");
      let entry = cardCache.get(key);
      if (!entry || entry.sig !== sig) {
        const el = entry ? entry.el : document.createElement("article");
        el.className = "card";
        el.dataset.id = id;
        fillCard(el, inc, g.key, nowMs, changed, claim, ui.wall);
        if (ui.open) el.onclick = () => ui.open(id);
        entry = { sig, el };
        cardCache.set(key, entry);
      }
      els.push(entry.el);
    }
    els.forEach((el, i) => { if (body.children[i] !== el) body.insertBefore(el, body.children[i] || null); });
    while (body.children.length > els.length) body.lastChild.remove();
    if (!els.length) body.innerHTML = `<div class="column-empty">${ui.query ? "ничего не найдено" : "пусто"}</div>`;
    else body.querySelector(".column-empty")?.remove();
  }
  for (const k of [...cardCache.keys()]) if (!alive.has(k)) cardCache.delete(k);
  if (ui.wall) fitColumns();
}

// Видеостена не скроллится: показываем столько карточек, сколько влезает, и «+ ещё N»
export function fitColumns() {
  document.querySelectorAll(".column").forEach((col) => {
    const body = col.querySelector(".column-body");
    const more = col.querySelector(".column-more");
    const cards = [...body.querySelectorAll(".card")];
    cards.forEach((c) => (c.style.display = ""));
    const limit = body.clientHeight;
    let hidden = 0;
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

function fillCard(el, inc, group, nowMs, changed, claim, wall) {
  const resolvedHere = inc.resolved_in.includes(group);
  el.classList.toggle("is-critical", inc.priority_level === "critical" && !resolvedHere);
  el.classList.toggle("is-resolved", resolvedHere);
  el.classList.toggle("flash", !!changed && changed.until > Date.now());

  const also = inc.groups.filter((g) => g !== group).map((g) => GROUP_SHORT[g]);
  const fc = inc.forecast;
  const ai = inc.ai || {};
  let aiLine = "";
  if (ai.route && group === "coordinators") {
    aiLine = `<div class="ai-line route"><span class="ai-tag">ИИ</span>→ <b>${esc(ai.route.team)}</b> · ${Math.round(ai.route.confidence * 100)}%</div>`;
  } else if (ai.risk && group === "high" && !wall) {
    aiLine = `<div class="ai-line"><span class="ai-tag">ИИ</span><span class="risk-badge risk-${ai.risk.level}">риск ${ai.risk.score}</span> · ${esc(ai.risk.factors.slice(0, 2).join(", ") || "факторов нет")}</div>`;
  } else if (ai.summary && (group === "critical" || group === "research") && !wall) {
    aiLine = `<div class="ai-line"><span class="ai-tag">ИИ</span>${esc(ai.summary)}</div>`;
  }
  const esc_ = lastEscalation(inc);
  const workLine = [
    claim ? `<span class="owner">👤 в работе: ${esc(firstName(claim.by))}</span>` : "",
    esc_ ? `<span class="escalated">⇪ эскалирован ${esc(fmtTime(esc_.at))}${esc_.author ? " · " + esc(firstName(esc_.author)) : ""}</span>` : "",
  ].filter(Boolean).join("");
  const stCls = `st-${inc.state_kind}`;
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

  const meta = wall
    ? `<div class="card-meta">
        <span>${esc(shortId(inc.id))}</span><b>${esc(fmtDateTime(inc.created_at, nowMs))}</b>
        <span>команда</span><b title="${esc(inc.team)}">${esc(inc.team || "—")}</b>
      </div>`
    : `<div class="card-meta">
        <span>${esc(shortId(inc.id))}</span><b>возник ${esc(fmtDateTime(inc.created_at, nowMs))}</b>
        <span>команда</span><b title="${esc(inc.team)}">${esc(inc.team || "—")}</b>
        <span>исполн.</span><b title="${esc(inc.assignee)}">${esc(inc.assignee || "не назначен")}</b>
        <span>статус</span><span><span class="chip ${stCls}">${esc(inc.state || "—")}</span></span>
      </div>`;

  el.innerHTML = `
    <div class="card-top">
      <span class="card-product">${esc(inc.product || "—")}</span>
      <span class="card-prio prio-${inc.priority_level}">${esc(inc.priority || "")}</span>
    </div>
    <div class="card-title">${esc(inc.title)}</div>
    ${meta}
    ${deadlineBlock}
    ${aiLine}
    ${workLine ? `<div class="work-line">${workLine}</div>` : ""}
    ${changed && changed.until > Date.now() ? `<div class="changed">● ${esc(changed.texts.join(" · "))}</div>` : ""}
    ${also.length ? `<div class="also">также в: <span>${also.join(", ")}</span></div>` : ""}`;
}

// Таймеры раз в секунду
export function tickCountdowns(nowMs) {
  document.querySelectorAll("[data-deadline]").forEach((el) => {
    const dl = el.dataset.deadline;
    if (!dl) return;
    const sec = (Date.parse(dl) - nowMs) / 1000;
    if (el.classList.contains("alert-time")) {
      el.textContent = (sec < 0 ? "просрочен " : "до срока ") + fmtLeft(sec);
    } else if (el.classList.contains("countdown-mini")) {
      el.textContent = sec < 0 ? "просрочен" : fmtLeft(sec);
    } else {
      el.textContent = sec < 0 ? `просрочен ${fmtLeft(-sec)}` : fmtLeft(sec);
      el.classList.toggle("overdue", sec < 0);
      el.classList.toggle("soon", sec >= 0 && sec < 3600);
    }
  });
}

// ---------- лента ----------
const EVENT_ICON = { became_critical: "🔴", critical_resolved: "✅", to_coordinators: "🟣", resolved: "✔", reopened: "↺",
  new: "＋", removed: "－", comment: "💬", escalated: "⇪" };
export function renderFeed(snap, onOpen) {
  const el = $("#feed");
  const evs = (snap.events || []).slice(-12).reverse();
  el.innerHTML = `<span class="feed-title">ЛЕНТА</span>` + (evs.length
    ? evs.map((e) => `<span class="feed-item alert-${e.alert || ""}" data-id="${esc(e.incident_id)}">${fmtTime(e.clock_at)} ${EVENT_ICON[e.type] || "•"} <b>${esc(shortId(e.incident_id))}</b> ${esc(e.text)}${e.by ? ` <i>· ${esc(firstName(e.by))}</i>` : ""}</span>`).join("")
    : `<span>изменений пока нет — правки в файле и действия координаторов появятся здесь</span>`);
  if (onOpen) el.querySelectorAll(".feed-item").forEach((i) => (i.onclick = () => onOpen(i.dataset.id)));
}

// ---------- панель деталей (АРМ) ----------
export function renderDrawer(inc, snap, nowMs, handlers, meta) {
  const el = $("#drawer");
  if (!inc) { el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  const ai = inc.ai || {};
  const fc = inc.forecast;
  const action = (snap.actions || []).find((a) => a.incident_id === inc.id);
  const route = ai.route;
  const claim = (snap.claims || {})[inc.id];
  const similar = route ? route.similar_ids.map((id) => `<span class="linklike" data-open="${esc(id)}">${esc(shortId(id))}</span>`).join(", ") : "";
  const groups = inc.groups.map((g) => GROUP_SHORT[g] + (inc.resolved_in.includes(g) ? " (решён)" : "")).join(", ") || "не в группах";
  const transitions = meta ? (meta.transitions[inc.state_id] || []) : [];
  const stateBtns = transitions.map((s) => `<button class="btn small" data-state="${s}">→ ${esc(meta.states[s])}</button>`).join("");
  const canWork = inc.state_kind !== "closed";
  el.innerHTML = `
    <button class="btn drawer-close" data-act="close" title="Esc">✕</button>
    <div class="card-prio prio-${inc.priority_level}" style="display:inline-block">${esc(inc.priority || "")}</div>
    <h2>${esc(inc.title)}</h2>
    <div style="color:var(--muted)">${esc(inc.id)} · ${esc(inc.product || "")} · ${groups}</div>

    <div class="work-box">
      <div class="work-owner">${claim ? `👤 В работе: <b>${esc(claim.by)}</b> · с ${esc(fmtTime(claim.at))}` : "Никто из координаторов не взял в работу"}</div>
      <div class="drawer-btns">
        ${claim ? `<button class="btn" data-act="release">Отпустить</button>` : `<button class="btn btn-primary" data-act="claim">Взять в работу</button>`}
        ${canWork && inc.state_kind !== "resolved" ? `<button class="btn btn-primary" data-act="transfer">Передать в команду</button>` : ""}
        ${inc.state_kind === "active" ? `<button class="btn" data-act="escalate">Эскалировать</button>` : ""}
        <a class="btn" href="${esc(inc.itsm_url || "#")}" target="_blank" rel="noopener">Открыть в ITSM</a>
      </div>
      ${stateBtns ? `<div class="drawer-btns"><span class="muted-label">Статус:</span>${stateBtns}</div>` : ""}
      <form class="comment-form" data-act="comment"><input name="text" placeholder="Комментарий в журнал работ…" autocomplete="off"><button class="btn small">Добавить</button></form>
    </div>

    <h3>ИИ-помощник</h3>
    ${ai.summary ? `<div class="ai-box"><b>Выжимка:</b> ${esc(ai.summary)}</div>` : ""}
    ${route ? `<div class="ai-box"><b>Куда передать: ${esc(route.team)}</b> · уверенность ${Math.round(route.confidence * 100)}%<br>
      <span style="color:var(--muted)">${esc(route.reason)}</span>
      ${route.alternatives.length ? `<br>Альтернативы: ${route.alternatives.map((x) => `${esc(x.team)} (${Math.round(x.score * 100)}%)`).join(", ")}` : ""}
      ${similar ? `<br>Похожие случаи: ${similar}` : ""}</div>` : ""}
    ${ai.risk ? `<div class="ai-box"><b>Риск стать критическим: <span class="risk-${ai.risk.level}">${ai.risk.score}/100</span></b><br>
      <span style="color:var(--muted)">${esc(ai.risk.factors.join(" · ") || "значимых факторов нет")}</span></div>` : ""}
    ${fc ? `<div class="ai-box"><b>Прогноз срока: ${FORECAST_TEXT[fc.level]}</b><br>
      <span style="color:var(--muted)">прошло ${Math.round(fc.progress * 100)}% срока · ${esc(fc.basis)}${fc.eta ? " · ожидаемое решение ~" + fmtDateTime(fc.eta, nowMs) : ""}</span></div>` : ""}

    ${action ? `<h3>Черновик ${action.kind === "route" ? "передачи" : "эскалации"} <span class="linklike" data-act="copy">скопировать</span></h3><div class="draft">${esc(action.draft)}</div>` : ""}

    <h3>Журнал работ</h3>
    ${inc.work_log.length ? `<div class="worklog">${inc.work_log.slice().reverse().map((w) => `
      <div class="wl"><span>${esc(fmtDateTime(w.at, nowMs))}</span><b>${esc(w.author || "")}</b><div>${esc(w.text || "")}</div></div>`).join("")}</div>`
      : `<div style="color:var(--dim)">записей нет</div>`}

    <h3>Карточка</h3>
    <dl class="kv">
      <dt>Статус</dt><dd>${esc(inc.state)}${inc.state_reason && inc.state_reason !== "Нет" ? " · " + esc(inc.state_reason) : ""}</dd>
      <dt>Возник</dt><dd>${esc(fmtDateTime(inc.created_at, nowMs))}</dd>
      <dt>Срок</dt><dd>${esc(fmtDateTime(inc.deadline, nowMs))} <span class="countdown" data-deadline="${esc(inc.deadline || "")}"></span></dd>
      ${inc.resolved_at ? `<dt>Завершён</dt><dd>${esc(fmtDateTime(inc.resolved_at, nowMs))}</dd>` : ""}
      <dt>Команда</dt><dd>${esc(inc.team || "—")}</dd>
      <dt>Исполнитель</dt><dd>${esc(inc.assignee || "—")}</dd>
      <dt>Сервис</dt><dd>${esc(inc.service || "—")}</dd>
      <dt>Влияние / срочность</dt><dd>${esc(inc.impact || "—")} / ${esc(inc.urgency || "—")}</dd>
      <dt>Тип сервиса</dt><dd>${esc(inc.service_type || "—")}</dd>
      <dt>Канал</dt><dd>${esc(inc.channel || "—")}</dd>
      <dt>Связи</dt><dd>${[inc.is_master && "мастер", inc.is_double && "дубль", inc.is_reason && "дочерний", inc.is_effect && "причина других"].filter(Boolean).join(", ") || "—"}</dd>
    </dl>
    <h3>Описание</h3><div>${esc(inc.description || "—")}</div>
    ${inc.solution ? `<h3>Решение</h3><div>${esc(inc.solution)}</div>` : ""}`;

  const on = (sel, fn) => { const b = el.querySelector(sel); if (b) b.onclick = fn; };
  on("[data-act=close]", handlers.close);
  on("[data-act=claim]", () => handlers.claim(inc.id));
  on("[data-act=release]", () => handlers.release(inc.id));
  on("[data-act=transfer]", () => handlers.transfer(inc.id));
  on("[data-act=escalate]", (e) => handlers.escalate(inc.id, action && action.kind === "escalate" ? action.draft : null, e.target));
  on("[data-act=copy]", (e) => handlers.copy(action.draft, e.target));
  el.querySelectorAll("[data-state]").forEach((b) => (b.onclick = () => handlers.setState(inc.id, Number(b.dataset.state))));
  el.querySelectorAll("[data-open]").forEach((s) => (s.onclick = () => handlers.open(s.dataset.open)));
  el.querySelector("[data-act=comment]").onsubmit = (e) => {
    e.preventDefault();
    const t = e.target.text.value.trim();
    if (t) handlers.comment(inc.id, t);
  };
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
