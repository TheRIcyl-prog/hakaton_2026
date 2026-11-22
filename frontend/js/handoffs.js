import { api } from "./api.js";
import { esc, fmtDateTime } from "./format.js";

function summary(rows) {
  if (!rows.length) return "<p>Активных задач для передачи нет.</p>";
  return `<ul class="handoff-list">${rows.map((inc) => `<li>
    <button type="button" class="linklike" data-handoff-incident="${esc(inc.id)}">${esc(inc.id)} · ${esc(inc.title)}</button>
    <div>${[inc.outage && `Сбой «${inc.outage}»`, inc.product, inc.team, inc.coordinator].filter(Boolean).map(esc).join(" · ")}</div>
    <small>${inc.signals.map(esc).join(" · ")}${inc.deadline ? ` · SLA до ${esc(fmtDateTime(inc.deadline, Date.now()))} МСК` : ""}</small>
    ${inc.last_action?.text ? `<div class="outage-description">Последнее действие · ${esc(inc.last_action.text)}</div>` : ""}
  </li>`).join("")}</ul>`;
}

export function mountHandoffs(el, open, signal) {
  const form = el.querySelector("#handoff-form");
  const recipient = form.elements.recipient;
  const submit = form.querySelector('button[type="submit"]');
  const preview = el.querySelector("[data-handoff-preview]");
  const status = el.querySelector("#handoff-status");
  const items = el.querySelector("#handoff-items");
  let visible = false;
  let timer;
  let busy = false;
  let read;
  let lastItems;
  let lastPreview;
  let lastRecipients;

  async function refresh() {
    clearTimeout(timer);
    if (!visible || read || signal.aborted) return;
    const request = new AbortController();
    read = request;
    try {
      const result = await api.handoffs(request.signal);
      if (read !== request) return;
      const choices = '<option value="">Выберите координатора</option>' + result.recipients.map((user) =>
        `<option value="${esc(user.id)}">${esc(user.name)}</option>`).join("");
      if (lastRecipients !== choices) {
        const selected = recipient.value;
        recipient.innerHTML = choices;
        if (result.recipients.some((user) => user.id === selected)) recipient.value = selected;
        lastRecipients = choices;
      }
      recipient.disabled = submit.disabled = busy || !result.recipients.length;
      if (!result.recipients.length) status.textContent = "Для передачи смены нужна вторая учётная запись.";
      preview.querySelector("summary").textContent = `Сводка смены · ${result.preview.incidents.length}`;
      const previewMarkup = summary(result.preview.incidents);
      if (lastPreview !== previewMarkup) {
        preview.querySelector("div").innerHTML = previewMarkup;
        lastPreview = previewMarkup;
      }
      const markup = result.items.map((item) => {
        const incoming = item.to_id === result.me;
        return `<article class="outage-card corners">
          <h2>${incoming ? "От" : "Кому передано ·"} ${esc(incoming ? item.from_name : item.to_name)}</h2>
          <div class="outage-description">${esc(fmtDateTime(item.at, Date.now()))} МСК · ${item.accepted_at
            ? `Принято ${esc(fmtDateTime(item.accepted_at, Date.now()))} МСК` : "Ожидает приёма"}</div>
          <p class="handoff-note">${esc(item.note)}</p>
          <details data-handoff="${esc(item.id)}"><summary>Переданные задачи · ${item.incidents.length}</summary>
            ${summary(item.incidents)}</details>
          ${incoming && !item.accepted_at ? `<button type="button" class="btn btn-primary small" data-handoff-ack="${esc(item.id)}">Принять смену</button>` : ""}
        </article>`;
      }).join("") || "<p>Передач смены пока нет.</p>";
      if (lastItems !== markup) {
        const expanded = new Set([...items.querySelectorAll("details[open]")].map((node) => node.dataset.handoff));
        items.innerHTML = markup;
        for (const node of items.querySelectorAll("details")) node.open = expanded.has(node.dataset.handoff);
        lastItems = markup;
      }
    } catch (error) {
      if (!request.signal.aborted && !signal.aborted) status.textContent = error.message;
    } finally {
      if (read === request) {
        read = null;
        if (visible && !signal.aborted) timer = setTimeout(refresh, 5000);
      }
    }
  }

  function update(next) {
    if (visible === next) return;
    visible = next;
    el.classList.toggle("hidden", !visible);
    if (visible) refresh();
    else {
      clearTimeout(timer);
      read?.abort();
      read = null;
    }
  }

  async function save(action, message, after = () => {}) {
    if (busy) return;
    busy = true;
    status.textContent = "";
    submit.disabled = true;
    try {
      await action();
      after();
      status.textContent = message;
    } catch (error) {
      status.textContent = error.message;
    } finally {
      busy = false;
      read?.abort();
      read = null;
      submit.disabled = recipient.disabled;
      await refresh();
    }
  }

  form.onsubmit = (event) => {
    event.preventDefault();
    const note = form.elements.note.value.trim();
    if (!note) { status.textContent = "Укажите следующий шаг."; return; }
    save(() => api.sendHandoff(recipient.value, note), "Смена передана, ожидаем подтверждения.", () => form.reset());
  };
  el.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.dataset.handoffIncident) open(button.dataset.handoffIncident);
    else if (button.dataset.handoffAck && !busy) {
      button.disabled = true;
      save(() => api.acknowledgeHandoff(button.dataset.handoffAck), "Приём смены подтверждён.")
        .finally(() => { button.disabled = false; });
    }
  });
  signal.addEventListener("abort", () => update(false), { once: true });
  return { update };
}
