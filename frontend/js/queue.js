import { api, getMe } from "./api.js";
import { closeModal } from "./desk.js";
import { esc } from "./format.js";

const REFRESH_MS = 5000; // Отсрочка может истечь без новой версии снимка.
const KINDS = {
  claim: ["Новый критический", "Взять в работу"],
  route: ["Передача", "Передать"],
  escalate: ["Эскалация", "Эскалировать"],
  outage: ["Возможный сбой", "Проверить состав"],
  dynamics: ["Динамика", "Посмотреть"],
  aftermath: ["Последствия", "Проверить последствия"],
  deadline: ["Срок", "Посмотреть"],
  recurrence: ["Повтор", "Прошлые случаи"],
};
const REVIEW_KINDS = new Set(["dynamics", "aftermath", "deadline", "recurrence"]);

function button(action, title, primary = false) {
  return `<button type="button" class="btn small${primary ? " btn-primary" : ""}" data-queue-act="${action}">${title}</button>`;
}

function itsmLink(value) {
  const url = value && URL.parse(value, location.origin);
  return url && (url.protocol === "https:" || url.protocol === "http:")
    ? `<a class="btn small" href="${esc(url.href)}" target="_blank" rel="noopener">ITSM</a>` : "";
}

function renderItem(item, scope, candidate) {
  const [kind, title] = KINDS[item.kind] || ["Задача", "Открыть"];
  const inc = item.incident;
  const review = REVIEW_KINDS.has(item.kind);
  const outage = item.kind === "outage";
  const confidence = item.kind === "route" && Number.isFinite(item.confidence) ? `Уверенность ${Math.round(item.confidence * 100)}%` : "";
  const primary = outage
    ? item.outage_id && button("review-outage", title, true)
    : item.incident_id && button(review || !KINDS[item.kind] ? "open" : item.kind, title, true);
  return `<article class="action${inc?.priority_level === "critical" ? " crit" : item.kind === "escalate" ? " escalate" : ""}" data-queue-id="${esc(item.id)}">
    <div class="action-head"><span>${kind}${inc?.product ? ` · ${esc(inc.product)}` : ""}</span>
      ${inc?.deadline ? `<span class="countdown-mini" data-deadline="${esc(inc.deadline)}"></span>` : ""}</div>
    <div class="action-title">${esc(item.title || inc?.title || kind)}</div>
    ${inc ? `<div class="queue-incident">${esc(inc.id)}${inc.title && inc.title !== item.title ? ` · ${esc(inc.title)}` : ""}</div>` : ""}
    ${item.reason || confidence ? `<div class="action-reason">${[item.reason, confidence].filter(Boolean).map(esc).join(" · ")}</div>` : ""}
    ${candidate ? `<details class="queue-composition"><summary>Состав · ${esc(candidate.incident_ids.length)}</summary>
      <div>${esc(candidate.incident_ids.join(", "))}</div></details>` : ""}
    <fieldset class="action-btns"${scope.busy.has(item.id) ? " disabled" : ""}>
      ${primary || ""}
      ${outage ? button("dismiss", "Нет") : item.incident_id && !review && KINDS[item.kind] ? button("open", "Подробнее") : ""}
      ${review && scope.reviewed.has(item.id) ? button("reviewed", "Просмотрено") : ""}
      ${item.draft ? button("copy", "Текст") : ""}
      ${itsmLink(inc?.itsm_url)}
      ${button("snooze", "Отложить на 15 мин")}
      ${outage ? "" : button("dismiss", "Не моё")}
    </fieldset>
  </article>`;
}

export function mountQueue(el, { getSnapshot, open, copy, needName, toast, reviewOutage, refreshSnapshot, tickCountdowns }) {
  const counter = document.getElementById("queue-count");
  const list = el.querySelector("#queue-items");
  const status = el.querySelector("#queue-status");
  const retry = el.querySelector("#queue-retry");
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
    counter.textContent = scope.items.length || "";
    const candidates = new Map((getSnapshot()?.outage_candidates || []).map((candidate) => [candidate.id, candidate]));
    const next = scope.items.map((item) => renderItem(item, scope, candidates.get(item.outage_id))).join("")
      || (scope.loaded && !scope.error ? `<p class="actions-empty">В очереди пока нет задач</p>` : "");
    if (next === markup) return;
    const expanded = new Set([...list.querySelectorAll("details[open]")].map((d) => d.closest("[data-queue-id]").dataset.queueId));
    markup = next;
    list.innerHTML = next;
    for (const node of list.querySelectorAll("details")) node.open = expanded.has(node.closest("[data-queue-id]").dataset.queueId);
    tickCountdowns();
  }

  function cancelRead() {
    read?.abort();
    read = null;
    readAgain = false;
  }

  function refresh(urgent = false) {
    clearTimeout(timer);
    const by = getMe();
    if (scope?.by !== by) {
      cancelRead();
      if (modal.querySelector("#queue-action-form")) closeModal();
      scope = { by, items: [], busy: new Set(), reviewed: new Set(), loaded: false, error: "" };
      paint();
    } else if (urgent) {
      cancelRead();
    }
    if (read) { readAgain = true; return; }
    const request = new AbortController();
    read = request;
    api.queue(by, request.signal).then(({ items }) => {
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
    if (!available(owner, item.id) || owner.busy.has(item.id) || needName()) return;
    owner.busy.add(item.id);
    if (form) form.querySelector("button[type=submit]").disabled = true;
    paint();
    try {
      await write(owner.by);
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
    if (needName() || owner.busy.has(item.id)) return;
    owner.busy.add(item.id);
    paint();
    try {
      const inc = await api.incident(item.incident_id);
      if (!available(owner, item.id) || el.classList.contains("hidden") || !modal.classList.contains("hidden")) return;
      const route = item.kind === "route";
      const title = route ? "Передать в команду" : "Эскалировать";
      const teams = [...new Set([item.target_team, ...(getSnapshot()?.teams || [])])].filter((team) => team && team !== inc.team);
      modal.innerHTML = `<form class="modal-card queue-action-form" id="queue-action-form">
        <h2>${title}</h2>
        <div class="modal-sub">${esc(inc.id)} · ${esc(inc.title)} · ${esc(inc.team || "Команда не указана")}</div>
        ${route ? `<label class="modal-label" for="queue-team">Команда</label>
          <select name="team" id="queue-team" required><option value="">Выберите команду</option>${teams.map((team) => `<option${team === item.target_team ? " selected" : ""}>${esc(team)}</option>`).join("")}</select>
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
          : () => api.escalate(inc.id, comment);
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
      apply(item, (by) => api.queuePreference(by, id, operation), operation === "snooze" ? "Отложено на 15 минут" : "Убрано из вашей очереди");
    }
  });
  retry.onclick = () => refresh(true);
  return { refresh, syncBy: () => { if (scope?.by !== getMe()) refresh(); } };
}
