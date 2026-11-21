import { api } from "./api.js";
import { esc, plural } from "./format.js";

const SOURCES = { rules: "Правила сопоставления", gigachat: "GigaChat", cache: "GigaChat (кэш)" };
const countLabel = (n) => `${n} ${plural(n, "инцидент", "инцидента", "инцидентов")}`;

function deadline(outage) {
  if (outage.status === "resolved") return `<span class="outage-resolved">Решён</span>`;
  if (!outage.deadline) return `<span>Срок не задан</span>`;
  return `<span class="countdown" data-deadline="${esc(outage.deadline)}"></span>`;
}

export function renderWallOutages(el, snap, alarmIds) {
  const outages = snap.outages || [];
  el.classList.toggle("hidden", !outages.length);
  if (!outages.length) { el.innerHTML = ""; return; }

  const affected = new Set();
  const entries = outages.map((outage) => {
    const critical = outage.incident_ids.filter((id) => alarmIds.has(id));
    critical.forEach((id) => affected.add(id));
    return { outage, alarm: critical.length > 0 };
  });
  const byDeadline = (outage) => Date.parse(outage.deadline) || Infinity;
  entries.sort((a, b) => Number(b.alarm) - Number(a.alarm)
    || Number(a.outage.status === "resolved") - Number(b.outage.status === "resolved")
    || (b.outage.priority_id || 0) - (a.outage.priority_id || 0)
    || byDeadline(a.outage) - byDeadline(b.outage));
  el.innerHTML = `<div class="wall-outages-head"><h2>Сбои сейчас</h2>
    ${affected.size ? `<strong class="outage-alarm-label">! Критические в сбоях · ${affected.size} не взято</strong>` : ""}</div>
    <div class="wall-outages-grid">${entries.slice(0, 4).map(({ outage, alarm }) => `
      <article class="wall-outage${outage.status === "resolved" ? " resolved" : ""}${alarm ? " critical" : ""}">
        <strong title="${esc(outage.name)}">${esc(outage.name)}</strong>
        <div class="wall-outage-meta"><span>${countLabel(outage.incident_ids.length)} · ${outage.products.length} ${plural(outage.products.length, "продукт", "продукта", "продуктов")}</span>
          ${deadline(outage)}</div>
      </article>`).join("")}</div>`;
}

export function mountOutages(el, { open, refresh, needName, toast }) {
  const list = el.querySelector("#outage-list");
  const status = el.querySelector("#outage-status");
  status.tabIndex = -1;
  const refreshButton = el.querySelector("#outage-refresh");
  const details = new Map();
  let snap;
  let markup = "";
  let busy = false;
  let refreshRequired = false;
  let snapshotMessage = "";
  let opening = 0;

  function members(outage, detail, candidate) {
    const ids = detail?.outage?.incident_ids || outage.incident_ids;
    const rows = ids.map((id) => ({ id, inc: detail.incidents.get(id) || snap.incidents[id] }));
    const included = new Set(ids);
    const parents = new Set(rows.map(({ inc }) => inc?.cause_incident_id).filter((id) => included.has(id)));
    return `<ul class="outage-members">${rows.map(({ id, inc }) => {
      const label = inc && [inc.product, inc.title].filter(Boolean).join(" · ");
      const cause = inc?.cause_incident_id;
      const relation = included.has(cause) ? `Следствие ${cause}` : parents.has(id) ? "Причина цепочки" : "";
      const canExclude = candidate || detail.incidents.has(id);
      const disabled = busy || refreshRequired ? " disabled" : "";
      return `<li${canExclude ? ' class="action-btns"' : ""}><button type="button" class="outage-member" data-incident="${esc(id)}"><b>${esc(id)}</b>
        ${label || relation ? `<span>${esc([relation, label].filter(Boolean).join(" · "))}</span>` : ""}</button>
        ${canExclude ? `<button type="button" class="btn small" data-exclude="${esc(id)}" aria-label="Исключить ${esc(id)} ${candidate ? "из предложения" : "из сбоя"}"${disabled}>Исключить</button>` : ""}</li>`;
    }).join("")}</ul>`;
  }

  function card(outage, candidate) {
    const detail = details.get(outage.id);
    let composition = "";
    if (detail?.outage) composition = members(outage, detail, candidate);
    else if (detail?.error) composition = `<p class="outage-error">${esc(detail.error)}</p><button class="btn small" data-retry>Повторить</button>`;
    else if (detail) composition = `<p class="outage-description">Загружаем состав…</p>`;
    const source = SOURCES[outage.source];
    const key = `${candidate ? "candidate" : "confirmed"}:${outage.id}`;
    return `<article class="outage-card" data-id="${esc(outage.id)}" data-kind="${candidate ? "candidate" : "confirmed"}">
      <div class="outage-card-head"><h3>${esc(outage.name)}</h3><div class="outage-deadline">${deadline(outage)}</div></div>
      <div class="outage-description">${countLabel(outage.incident_ids.length)}${outage.products.length ? ` · ${esc(outage.products.join(", "))}` : ""}</div>
      ${outage.reason ? `<p>${esc(outage.reason)}</p>` : ""}
      ${source ? `<div class="outage-description">Источник · ${source}</div>` : ""}
      <details data-key="${esc(key)}"><summary>Состав · ${detail?.outage?.incident_ids.length ?? outage.incident_ids.length}</summary>${composition}
        ${!candidate && detail?.outage?.status === "active" ? outageControls() : ""}</details>
      ${candidate ? `<button class="btn btn-primary small" data-confirm${busy || refreshRequired ? " disabled" : ""}>Да, это один сбой</button>` : ""}
    </article>`;
  }

  function outageControls() {
    const disabled = busy || refreshRequired ? " disabled" : "";
    return `<div class="action-btns" aria-label="Действия над активными заявками сбоя">
      <button type="button" class="btn small" data-operation="claim"${disabled}>Взять весь сбой</button>
      <button type="button" class="btn small" data-operation="escalate"${disabled}>Эскалировать сбой</button>
      <select class="select" data-outage-team aria-label="Команда для сбоя"${disabled}>
        <option value="">Выберите команду</option>
        ${(snap.teams || []).map((team) => `<option value="${esc(team)}">${esc(team)}</option>`).join("")}
      </select>
      <button type="button" class="btn small" data-operation="assign"${disabled}>Передать активные заявки</button>
    </div>`;
  }

  function paint() {
    if (!snap) return;
    const candidates = snap.outage_candidates || [];
    const outages = snap.outages || [];
    const next = [
      candidates.length ? `<h2>Предложения</h2>${candidates.map((outage) => card(outage, true)).join("")}` : "",
      outages.length ? `<h2>Подтверждённые сбои</h2>${outages.map((outage) => card(outage, false)).join("")}` : "",
    ].join("") || `<p class="outage-description">Подтверждённых сбоев и предложений пока нет</p>`;
    if (next === markup) return;
    const expanded = new Set([...list.querySelectorAll("details[open]")].map((node) => node.dataset.key));
    const focused = document.activeElement;
    const focusedKey = focused?.closest("details")?.dataset.key;
    markup = next;
    list.innerHTML = next;
    for (const node of list.querySelectorAll("details")) {
      if (expanded.has(node.dataset.key)) node.open = true;
      if (node.dataset.key === focusedKey) {
        const member = [...node.querySelectorAll("[data-incident], [data-exclude]")].find((button) => !button.disabled
          && (button.hasAttribute("data-incident") ? button.dataset.incident === focused.dataset.incident
            : button.dataset.exclude === focused.dataset.exclude));
        (member || node.querySelector("summary")).focus({ preventScroll: true });
      }
    }
  }

  async function loadDetails(id, candidate) {
    if (details.has(id)) return;
    const entry = {};
    details.set(id, entry);
    paint();
    try {
      const outage = await (candidate ? api.outageCandidate(id) : api.outage(id));
      entry.incidents = new Map(outage.incidents.map((inc) => [inc.id, inc]));
      entry.outage = outage;
    } catch (e) {
      entry.error = `Не удалось загрузить состав. ${e.message}`;
    }
    if (details.get(id) === entry) { paint(); }
  }

  async function refreshSnapshot() {
    busy = true;
    refreshButton.classList.add("hidden");
    status.textContent = `${snapshotMessage} Обновляем снимок…`;
    paint();
    const updated = await refresh();
    busy = false;
    refreshRequired = !updated;
    status.textContent = `${snapshotMessage} ${updated ? "Снимок обновлён." : "Не удалось обновить снимок."}`;
    refreshButton.classList.toggle("hidden", updated);
    paint();
  }

  async function confirmCandidate(id) {
    if (busy || refreshRequired || !snap.outage_candidates?.some((candidate) => candidate.id === id) || needName()) return;
    busy = true;
    status.textContent = "Подтверждаем сбой…";
    paint();
    try {
      await api.confirmOutage(id);
      snapshotMessage = "Сбой подтверждён.";
    } catch (e) {
      if (e.status !== 409) {
        busy = false;
        status.textContent = `Не удалось подтвердить сбой. ${e.message}`;
        paint();
        return;
      }
      snapshotMessage = "Предложение или состав изменились. Проверьте актуальные данные.";
    }
    await refreshSnapshot();
  }

  async function actOnOutage(id, operation, team) {
    if (busy || refreshRequired || needName()) return;
    const detail = details.get(id);
    if (!detail?.outage || detail.outage.status !== "active") return;
    if (operation === "assign" && !team) { status.textContent = "Сначала выберите команду."; return; }
    if (operation === "assign" && !window.confirm(`Передать все активные заявки сбоя в «${team}»?`)) return;
    const expected_hashes = Object.fromEntries([...detail.incidents.values()]
      .filter((inc) => inc.state_kind === "active").map((inc) => [inc.id, inc.hash]));
    busy = true;
    paint();
    try {
      const outage = await api.outageAction(id, { operation, team: team || null, expected_hashes });
      details.set(id, { outage, incidents: new Map(outage.incidents.map((inc) => [inc.id, inc])) });
      snapshotMessage = operation === "claim" ? "Активные заявки взяты в работу." : operation === "assign"
        ? "Активные заявки переданы." : "Эскалация записана в журнал.";
    } catch (e) {
      if (e.status !== 409) { busy = false; status.textContent = e.message; paint(); return; }
      details.delete(id);
      snapshotMessage = e.message || "Сбой изменился. Проверьте актуальный состав.";
    }
    await refreshSnapshot();
  }

  async function excludeMember(id, incidentId) {
    if (busy || refreshRequired || needName()) return;
    const detail = details.get(id);
    const member = detail?.incidents.get(incidentId);
    if (!member) return;
    const lastPair = detail.outage.incident_ids.length === 2;
    const prompt = lastPair ? `Исключить ${incidentId}? В сбое останется одна заявка, поэтому группа будет распущена.`
      : `Исключить ${incidentId} из сбоя? Заявка останется в дашборде.`;
    if (!window.confirm(prompt)) return;
    busy = true;
    status.textContent = `Исключаем ${incidentId}…`;
    paint();
    try {
      const { outage } = await api.excludeOutageMember(id, incidentId, member.hash, detail.outage.incident_ids);
      if (outage) details.set(id, { outage, incidents: new Map(outage.incidents.map((inc) => [inc.id, inc])) });
      else details.delete(id);
      snapshotMessage = outage ? `${incidentId} исключён из сбоя.` : `Группа распущена. Заявки остались в дашборде.`;
    } catch (e) {
      if (e.status !== 409 && e.status !== 404) {
        busy = false;
        status.textContent = `Не удалось исключить заявку. ${e.message}`;
        paint();
        return;
      }
      details.delete(id);
      snapshotMessage = "Состав изменился. Проверьте актуальные данные.";
    }
    await refreshSnapshot();
    if (document.activeElement === document.body) status.focus({ preventScroll: true });
  }

  async function excludeCandidateMember(id, incidentId) {
    if (busy || refreshRequired || needName()) return;
    if (!window.confirm(`Исключить ${incidentId} из предложения? Это изменит состав для всех координаторов.`)) return;
    busy = true;
    status.textContent = `Исключаем ${incidentId} из предложения…`;
    paint();
    let replacement;
    try {
      ({ candidate: replacement } = await api.excludeOutageCandidateMember(id, incidentId));
      snapshotMessage = replacement ? `${incidentId} исключён. Проверьте новый состав.`
        : `${incidentId} исключён. Предложение больше не подходит для подтверждения.`;
    } catch (error) {
      if (error.status !== 409 && error.status !== 404) {
        busy = false;
        status.textContent = `Не удалось исключить заявку. ${error.message}`;
        paint();
        return;
      }
      details.delete(id);
      snapshotMessage = "Предложение изменилось. Проверьте актуальный состав.";
    }
    await refreshSnapshot();
    if (!replacement || !reviewCandidate(replacement.id)) status.focus({ preventScroll: true });
  }

  function reviewCandidate(id) {
    const summary = list.querySelector(`[data-kind="candidate"][data-id="${CSS.escape(id)}"] summary`);
    if (!summary) return false;
    summary.parentElement.open = true;
    summary.focus();
    summary.scrollIntoView({ block: "nearest" });
    return true;
  }

  list.addEventListener("toggle", (event) => {
    const node = event.target;
    if (!node.open || !list.contains(node)) return;
    const card = node.closest("[data-kind]");
    if (card) loadDetails(card.dataset.id, card.dataset.kind === "candidate");
  }, true);

  list.addEventListener("click", async (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    const { id, kind } = button.closest("[data-kind]").dataset;
    const isCandidate = kind === "candidate";
    if (button.hasAttribute("data-confirm")) { confirmCandidate(id); return; }
    if (button.dataset.exclude) {
      if (isCandidate) excludeCandidateMember(id, button.dataset.exclude);
      else excludeMember(id, button.dataset.exclude);
      return;
    }
    if (button.dataset.operation) {
      actOnOutage(id, button.dataset.operation, button.closest("[data-kind]").querySelector("[data-outage-team]")?.value);
      return;
    }
    if (button.hasAttribute("data-retry")) { details.delete(id); loadDetails(id, isCandidate); return; }
    const incidentId = button.dataset.incident;
    const request = ++opening;
    try {
      const inc = snap.incidents[incidentId] || (!isCandidate && details.get(id)?.incidents?.get(incidentId)) || await api.incident(incidentId);
      if (request === opening && !el.classList.contains("hidden")) open(incidentId, inc);
    } catch (e) {
      if (request === opening) toast(`Не удалось открыть ${incidentId}. ${e.message}`, "bad");
    }
  });
  refreshButton.onclick = refreshSnapshot;

  function render(next) {
    if (snap !== next) {
      const old = new Map((snap?.outages || []).map((outage) => [outage.id, JSON.stringify(outage.incident_ids)]));
      for (const outage of next.outages || []) {
        if (old.get(outage.id) !== JSON.stringify(outage.incident_ids)) details.delete(outage.id);
        old.delete(outage.id);
      }
      for (const id of old.keys()) details.delete(id);
      const candidates = new Set((next.outage_candidates || []).map(({ id }) => id));
      for (const candidate of snap?.outage_candidates || []) if (!candidates.has(candidate.id)) details.delete(candidate.id);
      snap = next;
    }
    paint();
    for (const node of list.querySelectorAll('details[open]')) {
      const { id, kind } = node.closest("[data-kind]").dataset;
      loadDetails(id, kind === "candidate");
    }
  }
  return { render, reviewCandidate };
}
