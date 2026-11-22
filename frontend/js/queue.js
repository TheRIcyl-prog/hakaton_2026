import { api, getMe } from "./api.js";
import { closeModal } from "./desk.js";
import { esc } from "./format.js";
import { aiEnabled, modelOutage, outageTitle } from "./ai-mode.js";

const REFRESH_MS = 5000; // Отсрочка может истечь без новой версии снимка.
const KINDS = {
  claim: ["Новый критический", "Взять в работу"],
  route: ["Передача", "Передать"],
  escalate: ["Эскалация", "Эскалировать"],
  escalation_ack: ["Эскалация ждёт приёма", "Открыть эскалацию"],
  escalation_followup: ["Эскалация без ответа", "Проверить эскалацию"],
  outage: ["Возможный сбой", "Проверить состав"],
  dynamics: ["Динамика", "Посмотреть"],
  aftermath: ["Последствия", "Проверить последствия"],
  deadline: ["Срок", "Посмотреть"],
  recurrence: ["Повтор", "Прошлые случаи"],
};
const REVIEW_KINDS = new Set(["dynamics", "aftermath", "deadline", "recurrence", "escalation_ack", "escalation_followup"]);
const PRIORITY = { critical: "Критический", high: "Высокий", medium: "Средний", low: "Низкий" };
const REMINDER_KINDS = new Set(["deadline", "recurrence"]);

function button(action, title, primary = false, disabled = false) {
  return `<button type="button" class="btn small${primary ? " btn-primary" : ""}" data-queue-act="${action}"${disabled ? " disabled" : ""}>${title}</button>`;
}

function itsmLink(value) {
  const url = value && URL.parse(value, location.origin);
  return url && (url.protocol === "https:" || url.protocol === "http:")
    ? `<a class="btn small" href="${esc(url.href)}" target="_blank" rel="noopener">ITSM</a>` : "";
}

function renderItem(item, scope, candidate, snap) {
  const [kind, title] = KINDS[item.kind] || ["Задача", "Открыть"];
  const inc = item.incident;
  const useAI = aiEnabled(snap);
  const coordinator = inc && (snap?.incidents?.[inc.id]?.coordinator?.name || snap?.claims?.[inc.id]?.by);
  const review = REVIEW_KINDS.has(item.kind);
  const outage = item.kind === "outage";
  const manual = item.kind === "route" && !useAI;
  const confidence = useAI && item.kind === "route" && Number.isFinite(item.confidence) ? `Уверенность ${Math.round(item.confidence * 100)}%` : "";
  const taskTitle = manual ? "Выбрать команду и передать" : outage && !useAI ? (candidate ? outageTitle(candidate, useAI) : kind) : item.title;
  const reason = manual || (outage && !useAI && (!candidate || modelOutage(candidate, useAI))) ? "" : item.reason;
  const busy = scope.busy.has(item.id);
  const primary = outage
    ? item.outage_id && button("review-outage", title, true, busy)
    : item.incident_id && !review && KINDS[item.kind] && button(item.kind, title, true, busy);
  const incidentTitle = inc?.title || taskTitle || kind;
  return `<article class="action${inc?.priority_level === "critical" ? " crit" : item.kind === "escalate" ? " escalate" : ""}" data-queue-id="${esc(item.id)}"${inc ? ` data-incident-id="${esc(inc.id)}"` : ""}>
    <div class="queue-priority"><span class="card-prio prio-${esc(inc?.priority_level || "unknown")}">${esc(PRIORITY[inc?.priority_level] || kind)}</span></div>
    <div class="queue-task">
      <div class="action-title" title="${esc(incidentTitle)}">${esc(incidentTitle)}</div>
      <div class="queue-incident">${[inc?.id, inc?.product, kind].filter(Boolean).map(esc).join(" · ")}</div>
    </div>
    <div class="queue-owner">${esc(coordinator || (inc ? "Не назначен" : "—"))}</div>
    <div class="queue-sla">${inc?.deadline ? `<span class="countdown-mini" data-deadline="${esc(inc.deadline)}"></span><small class="queue-sla-label"></small>` : "Срок не задан"}</div>
    <div class="queue-actions">
      ${primary || (inc ? button("open", review ? title : "Открыть", false, busy) : "")}
      <details class="queue-menu">
        <summary class="btn small" aria-label="Действия по задаче">⋯</summary>
        <fieldset class="action-btns"${busy ? " disabled" : ""}>
          ${inc && primary ? button("open", "Открыть карточку") : ""}
          ${review && scope.reviewed.has(item.id) ? button("reviewed", "Просмотрено") : ""}
          ${useAI && item.draft ? button("copy", "Текст") : ""}
          ${itsmLink(inc?.itsm_url)}
          ${button("snooze", "Отложить на 15 мин")}
          ${button("dismiss", outage ? "Нет" : "Не моё")}
          ${taskTitle && taskTitle !== incidentTitle ? `<div class="action-reason">${esc(taskTitle)}</div>` : ""}
          ${reason || confidence ? `<div class="action-reason">${[reason, confidence].filter(Boolean).map(esc).join(" · ")}</div>` : ""}
          ${candidate ? `<p class="queue-incident">Состав · ${esc(candidate.incident_ids.join(", "))}</p>` : ""}
        </fieldset>
      </details>
    </div>
  </article>`;
}

export function mountQueue(el, { getSnapshot, open, copy, signal, toast, reviewOutage, refreshSnapshot, tickCountdowns }) {
  const counter = document.getElementById("queue-count");
  const list = el.querySelector("#queue-items");
  const status = el.querySelector("#queue-status");
  const retry = el.querySelector("#queue-retry");
  const reminders = el.querySelector("#queue-reminders");
  const total = el.querySelector("#queue-total");
  const priority = el.querySelector("#queue-priority");
  let selected;

  function select(id) {
    selected = id;
    for (const row of list.querySelectorAll("[data-incident-id]")) {
      const open = row.querySelector('[data-queue-act="open"]');
      if (id === row.dataset.incidentId) open.setAttribute("aria-current", "true");
      else open.removeAttribute("aria-current");
    }
  }
  const modal = document.getElementById("modal");
  let scope;
  let read;
  let readAgain = false;
  let timer;
  let markup = "";

  function paint() {
    const message = scope.error || (scope.loaded ? "" : "Загружаем очередь…");
    if (status.textContent !== message) status.textContent = message;
    status.parentElement.hidden = !message;
    retry.hidden = !scope.error;
    const reminderCount = scope.items.filter((item) => REMINDER_KINDS.has(item.kind)).length;
    const primaryCount = scope.items.length - reminderCount;
    reminders.hidden = !reminderCount;
    reminders.setAttribute("aria-expanded", String(scope.showReminders));
    reminders.textContent = scope.showReminders ? "Скрыть сроки и повторы" : `Показать сроки и повторы (${reminderCount})`;
    const visibleCount = scope.showReminders ? scope.items.length : primaryCount;
    counter.textContent = visibleCount || "";

    const snap = getSnapshot();
    const candidates = new Map((snap?.outage_candidates || []).map((candidate) => [candidate.id, candidate]));
    const visible = scope.items.filter((item) => (scope.showReminders || !REMINDER_KINDS.has(item.kind))
      && (!priority.value || item.incident?.priority_level === priority.value));
    total.textContent = scope.loaded ? `Задач в очереди: ${visibleCount}${priority.value ? ` · показано: ${visible.length}` : ""}` : "";
    const next = visible.map((item) => renderItem(item, scope, candidates.get(item.outage_id), snap)).join("")
      || (scope.loaded && !scope.error ? `<p class="actions-empty">${priority.value ? "Нет задач с выбранным приоритетом" : reminderCount ? "Остались проверки сроков и прошлых случаев" : "В очереди пока нет задач"}</p>` : "");
    if (next === markup) return;
    const previous = document.activeElement;
    const focusedId = list.contains(previous) ? previous.closest("[data-queue-id]")?.dataset.queueId : null;
    const expanded = new Set([...list.querySelectorAll("details[open]")].map((d) => d.closest("[data-queue-id]").dataset.queueId));
    markup = next;
    list.innerHTML = next;
    for (const row of list.children) {
      const menu = row.querySelector("details");
      if (!menu) continue;
      menu.open = expanded.has(row.dataset.queueId);
      if (row.dataset.queueId === focusedId) {
        const focus = [...row.querySelectorAll("button:enabled, a[href], summary")].find((node) =>
          node.tagName === previous.tagName && node.dataset.queueAct === previous.dataset.queueAct && node.textContent === previous.textContent);
        (focus || menu.querySelector("summary")).focus({ preventScroll: true });
      }
    }
    if (focusedId && document.activeElement === document.body) priority.focus({ preventScroll: true });
    select(selected);
    tickCountdowns();
  }

  function cancelRead() {
    read?.abort();
    read = null;
    readAgain = false;
  }

  function refresh(urgent = false) {
    if (signal.aborted) return;
    clearTimeout(timer);
    const by = getMe();
    if (scope?.by !== by) {
      cancelRead();
      if (modal.querySelector("#queue-action-form")) closeModal();
      scope = { by, items: [], busy: new Set(), reviewed: new Set(), loaded: false, error: "", showReminders: false };
    } else if (urgent) {
      cancelRead();
    }
    paint();
    if (read) { readAgain = true; return; }
    const request = new AbortController();
    read = request;
    api.queue(request.signal).then(({ items }) => {
      if (read !== request) return;
      scope.items = items;
      const ids = new Set(items.map((item) => item.id));
      for (const id of scope.reviewed) if (!ids.has(id)) scope.reviewed.delete(id);
      scope.error = "";
      scope.loaded = true;
      paint();
    }).catch((e) => {
      if (read !== request) return;
      scope.error = `Не удалось обновить очередь. ${e.message}`;
      paint();
    }).finally(() => {
      if (read !== request) return;
      read = null;
      if (readAgain) { readAgain = false; refresh(); }
      else timer = setTimeout(refresh, REFRESH_MS);
    });
  }

  function available(owner, id) {
    return owner === scope && owner.by === getMe() && owner.items.some((item) => item.id === id);
  }

  async function apply(item, write, text, { domain = false, form } = {}) {
    const owner = scope;
    if (!available(owner, item.id) || owner.busy.has(item.id)) return;
    owner.busy.add(item.id);
    if (form) form.querySelector("button[type=submit]").disabled = true;
    paint();
    try {
      await write();
      finish();
      if (owner === scope) toast(text);
    } catch (e) {
      if (e.status === 409 || e.status === 404) {
        finish();
        if (owner === scope) toast(`Задача изменилась. ${e.message}`, "bad");
      } else if (owner === scope) {
        if (form?.isConnected) {
          const error = form.querySelector(".modal-error");
          error.textContent = e.message;
          error.classList.remove("hidden");
        } else {
          toast(`Действие не выполнено. ${e.message}`, "bad");
        }
      }
    } finally {
      owner.busy.delete(item.id);
      if (form?.isConnected) form.querySelector("button[type=submit]").disabled = false;
      if (owner === scope) paint();
    }

    function finish() {
      if (form?.isConnected) closeModal();
      if (owner === scope) {
        owner.items = owner.items.filter((entry) => entry.id !== item.id);
        owner.reviewed.delete(item.id);
      }
      // Ответ очереди, полученный до записи, не должен возвращать задачу в список.
      refresh(true);
      if (domain) refreshSnapshot();
    }
  }

  async function openAction(item) {
    const owner = scope;
    if (owner.busy.has(item.id)) return;
    owner.busy.add(item.id);
    paint();
    try {
      const inc = await api.incident(item.incident_id);
      if (!available(owner, item.id) || el.classList.contains("hidden") || !modal.classList.contains("hidden")) return;
      const route = item.kind === "route";
      const title = route ? "Передать в команду" : "Эскалировать";
      const snap = getSnapshot();
      const suggestion = aiEnabled(snap) ? item.target_team : null;
      const teams = [...new Set([suggestion, ...(snap?.teams || [])])].filter((team) => team && team !== inc.team);
      modal.innerHTML = `<form class="modal-card queue-action-form" id="queue-action-form">
        <h2>${title}</h2>
        <div class="modal-sub">${esc(inc.id)} · ${esc(inc.title)} · ${esc(inc.team || "Команда не указана")}</div>
        ${route ? `<label class="modal-label" for="queue-team">Команда</label>
          <select name="team" id="queue-team" required><option value="">Выберите команду</option>${teams.map((team) => `<option${team === suggestion ? " selected" : ""}>${esc(team)}</option>`).join("")}</select>
          <label class="modal-label" for="queue-assignee">Исполнитель (необязательно)</label><input id="queue-assignee" name="assignee" autocomplete="off">` : ""}
        <label class="modal-label" for="queue-comment">Комментарий в журнал</label>
        <textarea name="comment" rows="3" id="queue-comment">${route ? "" : esc(item.reason || "")}</textarea>
        <div class="modal-error hidden" role="status"></div>
        <div class="modal-btns"><button type="button" class="btn" data-cancel>Отмена</button><button type="submit" class="btn btn-primary">${route ? "Передать" : title}</button></div>
      </form>`;
      modal.classList.remove("hidden");
      const form = modal.querySelector("form");
      form.querySelector("[data-cancel]").onclick = closeModal;
      form.onsubmit = (event) => {
        event.preventDefault();
        if (!available(owner, item.id)) {
          closeModal();
          toast("Задача изменилась. Очередь обновляется.", "bad");
          refresh(true);
          refreshSnapshot();
          return;
        }
        const data = new FormData(form);
        const team = data.get("team");
        const comment = data.get("comment").trim() || null;
        const write = route
          ? () => api.assign(inc.id, team, comment, data.get("assignee").trim() || null, inc.hash)
          : () => api.escalate(inc.id, comment, inc.hash);
        apply(item, write, route ? `Передан в «${team}»` : "Эскалация записана в журнал", { domain: true, form });
      };
      form.querySelector(route ? "select" : "textarea").focus();
    } catch (e) {
      if (owner === scope) toast(`Не удалось открыть ${item.incident_id}. ${e.message}`, "bad");
      if (e.status === 409 || e.status === 404) refresh(true);
    } finally {
      owner.busy.delete(item.id);
      if (owner === scope) paint();
    }
  }

  list.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-queue-act]");
    if (!button) return;
    const owner = scope;
    const id = button.closest("[data-queue-id]").dataset.queueId;
    if (!available(owner, id) || owner.busy.has(id)) return;
    const item = owner.items.find((entry) => entry.id === id);
    const action = button.dataset.queueAct;
    if (action === "open") {
      const isCurrent = () => available(owner, id) && !el.classList.contains("hidden");
      if (await open(item.incident_id, isCurrent) && REVIEW_KINDS.has(item.kind) && isCurrent()) {
        owner.reviewed.add(id);
        paint();
      }
    } else if (action === "copy") {
      copy(item.draft, button);
    } else if (action === "route" || action === "escalate") {
      openAction(item);
    } else if (action === "claim") {
      apply(item, () => api.claim(item.incident_id), "Взято в работу", { domain: true });
    } else if (action === "review-outage") {
      reviewOutage(item.outage_id);
    } else if (action === "snooze" || action === "dismiss" || (action === "reviewed" && owner.reviewed.has(id))) {
      const operation = action === "snooze" ? "snooze" : "dismiss";
      apply(item, () => api.queuePreference(id, operation), operation === "snooze" ? "Отложено на 15 минут" : "Убрано из вашей очереди");
    }
  });
  reminders.onclick = () => { scope.showReminders = !scope.showReminders; paint(); };
  retry.onclick = () => refresh(true);
  priority.onchange = () => { if (scope) paint(); };
  signal.addEventListener("abort", () => {
    clearTimeout(timer);
    cancelRead();
    scope = null;
  }, { once: true });
  return { refresh, select };
}
