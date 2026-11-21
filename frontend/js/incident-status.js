import { closeModal } from "./desk.js";
import { esc } from "./format.js";

// Те же причины и целевые статусы, что в мини-ITSM. Переходы и названия приходят из /api/meta.
const REASONS = ["Постоянное решение", "Внешняя обработка", "Инцидент самоустранился", "Дубль", "Инцидент не подтверждён"];
const DONE = new Set([7, 10, 20]);

export async function openIncidentStatus(initial, { loadMeta, submit, reload, onSaved }) {
  const modal = document.getElementById("modal");
  modal.innerHTML = `
    <form class="modal-card status-form" aria-labelledby="arm-status-title">
      <h2 id="arm-status-title">Изменить статус</h2>
      <p class="modal-sub status-current"></p>
      <fieldset class="status-fields" disabled>
        <label for="arm-status-next">Новый статус</label>
        <select id="arm-status-next" name="state" required></select>
        <div class="status-reason hidden">
          <label for="arm-status-reason">Причина (обязательно)</label>
          <select id="arm-status-reason" name="reason" disabled></select>
        </div>
        <label for="arm-status-comment">Комментарий в журнал</label>
        <textarea id="arm-status-comment" name="comment" rows="3"></textarea>
      </fieldset>
      <div class="status-message" role="status">Загружаем допустимые переходы…</div>
      <div class="modal-btns">
        <button type="button" class="btn" data-cancel>Отмена</button>
        <button type="button" class="btn hidden" data-refresh>Обновить данные</button>
        <button type="submit" class="btn btn-primary" disabled>Сохранить</button>
      </div>
    </form>`;
  modal.classList.remove("hidden");
  const form = modal.querySelector("form");
  const fields = form.querySelector("fieldset");
  const current = form.querySelector(".status-current");
  const reasonRow = form.querySelector(".status-reason");
  const message = form.querySelector(".status-message");
  const retry = form.querySelector("[data-refresh]");
  const save = form.querySelector("[type=submit]");
  const cancel = form.querySelector("[data-cancel]");
  const { state: next, reason, comment } = form.elements;
  let inc = initial;
  let meta;
  let busy = false;
  let needsReload = false;
  let conflict = "";
  let transitions = [];

  function toggleReason() {
    const required = DONE.has(Number(next.value));
    reasonRow.classList.toggle("hidden", !required);
    reason.disabled = !required;
    reason.required = required;
  }

  function controls() {
    const locked = busy || needsReload || !transitions.length;
    fields.disabled = locked;
    save.disabled = locked;
    retry.classList.toggle("hidden", !needsReload && !!meta);
    retry.disabled = busy;
  }

  function renderOptions() {
    const selected = next.value;
    const selectedReason = reason.value;
    current.textContent = `${inc.id} · Текущий статус: ${inc.state || meta.states[inc.state_id]}`;
    transitions = meta.transitions[inc.state_id] || [];
    next.innerHTML = '<option value="">Выберите статус</option>' + transitions.map((id) => `<option value="${esc(id)}">${esc(meta.states[id])}</option>`).join("");
    next.value = transitions.some((id) => String(id) === selected) ? selected : "";
    const previous = inc.state_reason && inc.state_reason !== "Нет" ? inc.state_reason : "";
    const reasons = [...new Set([...REASONS, previous, selectedReason].filter(Boolean))];
    reason.innerHTML = '<option value="">Выберите причину</option>' + reasons.map((value) => `<option>${esc(value)}</option>`).join("");
    reason.value = selectedReason || previous;
    toggleReason();
  }

  async function load() {
    busy = true;
    controls();
    try {
      meta ||= await loadMeta();
      if (needsReload) inc = await reload(inc.id);
      if (!form.isConnected) return;
      needsReload = false;
      renderOptions();
      message.textContent = [conflict, !transitions.length && "Доступных переходов нет."].filter(Boolean).join(" ");
    } catch (error) {
      if (form.isConnected) message.textContent = `${conflict ? conflict + " " : ""}Не удалось загрузить данные. ${error.message}`;
    } finally {
      busy = false;
      controls();
      if (form.isConnected && !retry.classList.contains("hidden")) retry.focus();
      else if (form.isConnected && transitions.length) next.focus();
      else if (form.isConnected) cancel.focus();
    }
  }

  cancel.onclick = closeModal;
  retry.onclick = load;
  next.onchange = toggleReason;
  form.onsubmit = async (event) => {
    event.preventDefault();
    if (busy || needsReload || !next.value || !transitions.some((id) => String(id) === next.value)) return;
    const target = Number(next.value);
    const value = DONE.has(target) ? reason.value : null;
    if (DONE.has(target) && !value) { reason.reportValidity(); return; }
    busy = true;
    controls();
    message.textContent = "Сохраняем статус…";
    try {
      const saved = await submit(inc, target, comment.value.trim() || null, value);
      if (form.isConnected) closeModal();
      onSaved(saved);
    } catch (error) {
      if (error.status === 409) {
        const changes = Array.isArray(error.data?.changes) ? error.data.changes.map((change) => change.text).filter(Boolean) : [];
        conflict = `Конфликт. Статус не сохранён. ${changes.join(". ") || error.message}`;
        needsReload = true;
        if (form.isConnected) message.textContent = conflict;
        await load();
      } else if (form.isConnected) {
        message.textContent = `Не удалось изменить статус. ${error.message}`;
      }
    } finally {
      busy = false;
      controls();
    }
  };
  cancel.focus();
  await load();
}
