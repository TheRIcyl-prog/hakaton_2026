// Панель инцидента на АРМ: выезжает поверх колонок справа, Esc закрывает (decisions.md, п. 5–7).
// Описание, номер с «скопировать», влияние/срочность, ИИ целиком + «почему?», история изменений, журнал,
// действия координатора (только взять/отпустить, передать, эскалировать, комментарий) и ссылка в ITSM.
import { esc, fmtDateTime, fmtTime, fmtWhen, plural } from "./format.js";
import { ownerOf } from "./render.js";
import { incidentLabels } from "./incident-labels.js";
import { bindFeedback, feedbackMarkup, rememberFeedback } from "./ai-feedback.js";

const GROUP_SHORT = { critical: "критические", coordinators: "на координаторах", high: "высокий", research: "исследование" };
const FORECAST_TEXT = { ok: "успевают", risk: "срок на исходе", breach: "прогноз: не успеют", overdue: "срок нарушен" };
const RISK_TEXT = { high: "высокий", medium: "средний", low: "низкий" };
const SOURCE = { rules: "правила по истории инцидентов", gigachat: "GigaChat", cache: "GigaChat (ответ из кэша)" };
const HIST_LABEL = { priority: "приоритет", team: "команда", assignee: "исполнитель", deadline: "срок", state: "статус", reopen: "возврат в работу" };
const ACTION_LABEL = { take: "взял в работу", release: "отпустил", assign: "передал", state: "статус", priority: "приоритет",
  deadline: "срок", comment: "комментарий", escalate: "эскалация", duplicates: "дубли" };

const why = new Set(); // какие «почему?» раскрыты: `${id}:${функция}` — переживает перерисовку
const firstName = (s) => (s || "").split(" (")[0];
const aiTag = `<span class="ai-tag">ИИ</span>`;

function src(meta) {
  if (!meta) return "";
  const s = SOURCE[meta.source] || meta.source || "правила";
  return `<div class="ai-src-line">Источник: ${esc(s)}${meta.at ? ` · ${esc(fmtTime(meta.at))}` : ""}</div>`;
}

function whyBtn(inc, key) {
  return `<button class="btn small btn-ghost why-btn" data-why="${esc(key)}">${why.has(`${inc.id}:${key}`) ? "скрыть" : "почему?"}</button>`;
}
const open = (id) => `<span class="linklike" data-open="${esc(id)}">${esc(id)}</span>`;

function aiSection(inc, snap) {
  const ai = inc.ai || {};
  const boxes = [];
  const shown = (k) => why.has(`${inc.id}:${k}`);

  if (ai.summary) {
    boxes.push(`<div class="ai-box"><div class="ai-row"><span><b>Суть:</b> ${esc(ai.summary)}</span>${whyBtn(inc, "summary")}</div>
      ${shown("summary") ? `<div class="ai-why">Выжимка по заголовку, описанию, продукту, сервису и сценарию — показывается на карточке вместо длинной сути. Оригинал — ниже, в «Описании».${src(ai.summary_meta || ai)}</div>` : ""}</div>`);
  }
  const r = ai.route;
  if (r) {
    boxes.push(`<div class="ai-box route"><div class="ai-row"><span><b>Кому передать: «${esc(r.team)}»</b> — ${esc(r.confidence_text || Math.round(r.confidence * 100) + "%")}</span>${whyBtn(inc, "route")}</div>
      ${r.reason ? `<div class="ai-text">${esc(r.reason)}</div>` : ""}
      ${r.alternatives && r.alternatives.length ? `<div class="ai-why">Запасные: ${r.alternatives.map((x) => `«${esc(x.team)}» ${esc(x.confidence_text || Math.round(x.score * 100) + "%")}`).join(", ")}</div>` : ""}
      ${shown("route") ? `<div class="ai-why">
        ${r.basis ? `<div>Искали похожие ${esc(r.basis)}. Уверенность — по истории, а не со слов модели.</div>` : ""}
        ${r.candidates && r.candidates.length ? `<div class="ai-list">${r.candidates.map((c) => `<div>«${esc(c.team)}» — вела ${c.count}, решила ${c.solved}</div>`).join("")}</div>` : ""}
        ${r.similar && r.similar.length ? `<div>Похожие: ${r.similar.slice(0, 6).map((x) => `${open(x.id)} <small>${esc(x.team || "")}, ${esc(x.state || "")}</small>`).join(" · ")}</div>`
          : r.similar_ids && r.similar_ids.length ? `<div>Похожие: ${r.similar_ids.slice(0, 6).map(open).join(", ")}</div>` : ""}
        ${r.rules_team && r.rules_team !== r.team ? `<div>По правилам первой была «${esc(r.rules_team)}» — GigaChat выбрал другую.</div>` : ""}
        ${r.feedback?.count ? `<div>Учтены исправления координаторов: ${esc(r.feedback.count)}.</div>` : ""}
        ${src(r)}</div>` : ""}</div>`);
  }
  const k = ai.risk;
  if (k && k.level !== "low") {
    boxes.push(`<div class="ai-box"><div class="ai-row"><span><b>Риск стать критическим: <span class="risk-${k.level}">${RISK_TEXT[k.level] || k.level} · ${k.score}/100</span></b></span>${whyBtn(inc, "risk")}</div>
      ${k.comment ? `<div class="ai-text">${esc(k.comment)}</div>` : ""}
      ${shown("risk") ? `<div class="ai-why">
        ${k.breakdown && k.breakdown.length ? `<div class="ai-list">${k.breakdown.map((b) => `<div>${esc(b.label)} <b>+${b.points}</b></div>`).join("")}</div>`
          : `<div>${esc((k.factors || []).join(" · ") || "значимых факторов нет")}</div>`}
        <div>«Высокий» — от 70 баллов.</div>${src(k.comment_meta || k)}</div>` : ""}</div>`);
  }
  const d = ai.dynamics;
  if (d && d.flag) {
    boxes.push(`<div class="ai-box dyn"><div class="ai-row"><span><b>${esc(d.label || "нарастает")}</b>${d.text ? ": " + esc(d.text) : ""}</span>${whyBtn(inc, "dynamics")}</div>
      ${d.comment ? `<div class="ai-text">${esc(d.comment)}</div>` : ""}
      ${shown("dynamics") ? `<div class="ai-why">
        ${(d.patterns || []).map((p) => `<div>• ${esc(p.text)}</div>`).join("")}
        <div>Шаблоны: приоритет повышали ≥2 раз за 2 ч; переназначали ≥3 раз за 4 ч; срок переносили ≥2 раз; вернули в работу после «Завершен».</div>
        ${src(d)}</div>` : ""}</div>`);
  }
  const u = ai.duplicates;
  if (u && u.group_id) {
    const others = (u.ids || []).filter((x) => x !== inc.id);
    boxes.push(`<div class="ai-box"><div class="ai-row"><span><b>Похоже на один сбой${u.name ? ": " + esc(u.name) : ""}</b> — ${u.size || (u.ids || []).length} ${plural(u.size || 0, "заявка", "заявки", "заявок")}${u.confirmed ? ` <span class="ok-tag">подтверждено</span>` : ""}</span>${whyBtn(inc, "dups")}</div>
      ${u.reason ? `<div class="ai-text">${esc(u.reason)}</div>` : ""}
      ${others.length ? `<div class="ai-why">Заявки: ${others.slice(0, 10).map(open).join(", ")}${u.size > others.length + 1 ? ` и ещё ${u.size - others.length - 1}` : ""}</div>` : ""}
      ${!u.confirmed ? `<div class="drawer-btns"><button class="btn small" data-act="confirm-dups">Да, это один сбой</button></div>` : ""}
      ${shown("dups") ? `<div class="ai-why">${u.kind === "burst" ? "Всплеск по одному сервису с разными текстами — подтверждён GigaChat." : `Тот же продукт и сервис, созданы в пределах 2 ч, похожий текст${u.similarity != null ? ` (сходство ${Math.round(u.similarity * 100)}%)` : ""}.`}
        «Да, это один сбой» запишет в файл мастер и дубли.${src(u)}</div>` : ""}</div>`);
  }
  const st = snap && snap.ai_status;
  return `<h3>${aiTag} Подсказки ${st && st.label ? `<small class="ai-src">${esc(st.label)}</small>` : ""}${ai.pending ? `<small class="ai-src">· GigaChat думает…</small>` : ""}</h3>
    ${boxes.join("") || `<div class="pop-empty">Подсказок нет</div>`}${feedbackMarkup(inc, snap)}`;
}

function historyHtml(list, nowMs) {
  if (!list) return `<div class="pop-empty">загружаем…</div>`;
  if (!list.length) return `<div class="pop-empty">изменений не было</div>`;
  return `<div class="worklog">${list.slice().reverse().map((h) => `
    <div class="wl"><span>${esc(fmtDateTime(h.at, nowMs))}</span><b>${esc(HIST_LABEL[h.kind] || h.kind)}</b>
      <div>${h.kind === "reopen" ? "вернули в работу" : `${esc(h.before ?? "—")} → ${esc(h.after ?? "—")}`}${h.by ? ` · <i>${esc(firstName(h.by))}</i>` : ""}</div></div>`).join("")}</div>`;
}

function recurrenceSection(recurrence, nowMs) {
  if (!recurrence) return "";
  const { count, level, suggested_team, cases } = recurrence;
  return `<section class="recurrence-history" aria-label="Прошлые случаи">
    <h3>Прошлые случаи</h3>
    <div class="recurrence-meta">${count > cases.length
      ? `Показано ${esc(cases.length)} из ${esc(count)} за неделю`
      : `За неделю до регистрации · ${esc(count)} ${plural(count, "случай", "случая", "случаев")}`}
      ${level === "strong" ? " · Повтор после постоянного решения" : ""}</div>
    ${suggested_team ? `<p class="recurrence-advice">Рекомендуем передать в «${esc(suggested_team)}» по опыту прошлых случаев.</p>` : ""}
    <ul class="recurrence-cases">${cases.map((item) => `<li>
      <div><button type="button" class="linklike recurrence-link" data-open="${esc(item.id)}">${esc(item.id)}</button>${item.title ? ` · ${esc(item.title)}` : ""}</div>
      <div class="recurrence-meta">${item.resolved_at ? `Решён <time datetime="${esc(item.resolved_at)}">${esc(fmtDateTime(item.resolved_at, nowMs))} МСК</time> · ` : ""}${esc(item.team || "Команда не указана")}${item.state_reason && item.state_reason !== "Нет" ? ` · ${esc(item.state_reason)}` : ""}</div>
      ${item.solution ? `<div class="panel-text"><b>Как исправили</b> · ${esc(item.solution)}</div>` : ""}
    </li>`).join("")}</ul>
  </section>`;
}

// handlers: {close, copy, open, claim, release, transfer, escalate, comment, confirmDups, me}
export function renderPanel(inc, snap, nowMs, handlers, history) {
  const el = document.getElementById("drawer");
  if (!inc) { rememberFeedback(el, false); el.classList.add("hidden"); el.innerHTML = ""; el.dataset.id = ""; return; }
  el.classList.remove("hidden");
  // при обновлении снимка не теряем набранный комментарий и прокрутку
  const same = el.dataset.id === inc.id;
  rememberFeedback(el, same);
  const prevInput = el.querySelector(".comment-form input");
  const keep = same && prevInput ? { v: prevInput.value, f: document.activeElement === prevInput } : null;
  const scroll = same ? el.scrollTop : 0;
  el.dataset.id = inc.id;

  const fc = inc.forecast;
  const action = (snap.actions || []).find((a) => a.incident_id === inc.id);
  const claim = ownerOf(inc, snap);
  const mine = !!claim && claim.by === handlers.me;
  const alert = claim ? null : (snap.alerts || []).find((a) => a.incident_id === inc.id && !a.taken_by);
  const groups = inc.groups.map((g) => GROUP_SHORT[g] + (inc.resolved_in.includes(g) ? " (решён)" : "")).join(", ") || "не в группах";
  const active = inc.state_kind === "active";
  const canTransfer = inc.state_kind === "active" || inc.state_kind === "research";
  const links = [inc.is_master && "мастер", inc.is_double && "дубль", inc.is_reason && "дочерний", inc.is_effect && "причина других"].filter(Boolean);
  const esc_ = inc.work_log.slice().reverse().find((w) => w.action === "escalate");
  const labels = incidentLabels(inc);

  el.innerHTML = `
    <div class="panel-head">
      <span class="card-prio prio-${inc.priority_level}">${esc(inc.priority || "")}</span>
      <span class="chip st-${inc.state_kind}">${esc(inc.state || "—")}</span>
      ${esc_ ? `<span class="escalated">⇪ эскалирован ${esc(fmtTime(esc_.at))}</span>` : ""}
      <button class="btn small drawer-close" data-act="close" title="Esc">✕</button>
    </div>
    <h2>${esc(inc.product || "—")}</h2>
    <div class="panel-title">${esc(inc.title)}</div>
    <div class="panel-sub"><b class="inc-id">${esc(inc.id)}</b> <span class="linklike" data-act="copy-id">скопировать</span> · ${groups}</div>
    ${labels ? `<div class="panel-labels" aria-label="Метки">${labels}</div>` : ""}

    ${alert ? `<div class="panel-alert ${alert.group}">🔔 ${alert.group === "critical" ? "Критический — никто не взял" : "Назначен на координаторов — никто не взял"} <button class="btn small btn-take" data-act="claim">Взять в работу</button></div>` : ""}

    <dl class="kv facts">
      <dt>Срок</dt><dd>${active && inc.deadline ? `<span class="countdown" data-deadline="${esc(inc.deadline)}" data-start="${esc(inc.created_at || "")}" data-fc="${esc(fc ? fc.level : "")}"></span> <small>до ${esc(fmtDateTime(inc.deadline, nowMs))}</small>`
        : inc.deadline ? esc(fmtDateTime(inc.deadline, nowMs)) : `<span class="dim">срок не задан</span>`}</dd>
      ${fc ? `<dt>Прогноз</dt><dd><span class="forecast ${fc.level}">${FORECAST_TEXT[fc.level]}</span> <small>прошло ${Math.round(fc.progress * 100)}% срока${fc.eta ? " · решат ~" + esc(fmtWhen(fc.eta, nowMs)) : ""} · ${esc(fc.basis || "")}</small></dd>` : ""}
      <dt>Возник</dt><dd>${esc(fmtDateTime(inc.created_at, nowMs))}</dd>
      ${inc.resolved_at ? `<dt>Решён</dt><dd>${esc(fmtDateTime(inc.resolved_at, nowMs))}</dd>` : ""}
      <dt>Исполнитель</dt><dd>${esc(inc.team || "—")} / ${inc.assignee ? esc(inc.assignee) : `<span class="dim">сотрудник не назначен</span>`}</dd>
      <dt>Координатор</dt><dd>${claim ? `<b class="owner">в работе: ${esc(claim.by)}</b> · с ${esc(fmtTime(claim.at))}` : "никто не взял в работу"}</dd>
      <dt>Влияние / срочность</dt><dd>${esc(inc.impact || "—")} / ${esc(inc.urgency || "—")}</dd>
    </dl>

    <div class="drawer-btns">
      ${mine ? `<button class="btn small" data-act="release">Отпустить</button>`
        : claim ? `<button class="btn small" data-act="force" title="Сейчас в работе: ${esc(claim.by)}">Забрать себе</button>`
        : alert || !active ? "" : `<button class="btn small btn-take" data-act="claim">Взять в работу</button>`}
      ${canTransfer ? `<button class="btn small btn-primary" data-act="transfer">Передать</button>` : ""}
      ${active ? `<button class="btn small" data-act="escalate">Эскалировать</button>` : ""}
      <a class="btn small" href="${esc(inc.itsm_url || "#")}" target="_blank" rel="noopener" title="Статус, приоритет и срок меняются в ITSM">Открыть в ITSM ↗</a>
    </div>
    <form class="comment-form" data-act="comment"><input name="text" placeholder="Комментарий в журнал работ…" autocomplete="off"><button class="btn small">Добавить</button></form>

    ${recurrenceSection(inc.recurrence, nowMs)}

    ${aiSection(inc, snap)}

    ${action ? `<h3>Черновик ${action.kind === "route" ? "передачи" : "эскалации"} <span class="linklike" data-act="copy">скопировать</span></h3><div class="draft">${esc(action.draft)}</div>` : ""}

    <h3>История изменений</h3>
    ${historyHtml(history, nowMs)}

    <h3>Журнал работ</h3>
    ${inc.work_log.length ? `<div class="worklog">${inc.work_log.slice().reverse().map((w) => `
      <div class="wl"><span>${esc(fmtDateTime(w.at, nowMs))}</span><b>${esc(w.author || "")}</b>${w.action && ACTION_LABEL[w.action] ? ` <small>${ACTION_LABEL[w.action]}</small>` : ""}<div>${esc(w.text || "")}</div></div>`).join("")}</div>`
      : `<div class="pop-empty">записей нет</div>`}

    <h3>Описание</h3><div class="panel-text">${esc(inc.description || inc.short_description || "—")}</div>
    ${inc.solution && inc.state_kind !== "active" ? `<h3>Решение</h3><div class="panel-text">${esc(inc.solution)}</div>` : ""}

    <h3>Карточка</h3>
    <dl class="kv">
      <dt>Статус</dt><dd>${esc(inc.state)}${inc.state_reason && inc.state_reason !== "Нет" ? " · " + esc(inc.state_reason) : ""}</dd>
      <dt>Сервис</dt><dd>${esc(inc.service || "—")}</dd>
      <dt>Сценарий</dt><dd>${esc(inc.scenario || "—")}</dd>
      <dt>Тип сервиса</dt><dd>${esc(inc.service_type || "—")}</dd>
      <dt>Канал</dt><dd>${esc(inc.channel || "—")}</dd>
      <dt>Связи</dt><dd>${inc.cause_incident_id ? `Причина: ${open(inc.cause_incident_id)}${links.length ? ` · ${links.join(", ")}` : ""}` : links.join(", ") || "—"}</dd>
    </dl>`;

  const on = (sel, fn) => el.querySelectorAll(sel).forEach((b) => (b.onclick = fn));
  on("[data-act=close]", handlers.close);
  on("[data-act=claim]", () => handlers.claim(inc.id));
  on("[data-act=force]", () => handlers.claim(inc.id, true));
  on("[data-act=release]", () => handlers.release(inc.id));
  on("[data-act=transfer]", () => handlers.transfer(inc.id));
  on("[data-act=escalate]", () => handlers.escalate(inc.id, action && action.kind === "escalate" ? action.draft : null));
  on("[data-act=copy]", (e) => handlers.copy(action.draft, e.target));
  on("[data-act=copy-id]", (e) => handlers.copy(inc.id, e.target));
  on("[data-act=confirm-dups]", () => handlers.confirmDups(inc));
  el.querySelectorAll("[data-why]").forEach((b) => (b.onclick = () => {
    const k = `${inc.id}:${b.dataset.why}`;
    why.has(k) ? why.delete(k) : why.add(k);
    renderPanel(inc, snap, nowMs, handlers, history);
  }));
  el.querySelectorAll("[data-open]").forEach((s) => (s.onclick = () => handlers.open(s.dataset.open)));
  const form = el.querySelector("[data-act=comment]");
  form.onsubmit = (e) => {
    e.preventDefault();
    const t = form.text.value.trim();
    if (t) { handlers.comment(inc.id, t); form.text.value = ""; }
  };
  if (keep) { form.text.value = keep.v; if (keep.f) form.text.focus(); }
  bindFeedback(el, inc);
  el.scrollTop = scroll;
}
