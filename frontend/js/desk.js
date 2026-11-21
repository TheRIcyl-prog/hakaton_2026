// Диалоги АРМ: передача в команду и подтверждение эскалации.
import { esc } from "./format.js";
import { api } from "./api.js";
import { aiEnabled } from "./ai-mode.js";

const modal = () => document.getElementById("modal");

export function closeModal() {
  modal().classList.add("hidden");
  modal().innerHTML = "";
}

// Передать инцидент в команду. Рекомендованные команды — сверху с долей похожих случаев.
// Конфликт правок (409): инцидент изменили, пока окно было открыто — ничего не перезаписано
function conflictText(ex) {
  const ch = ex.data && ex.data.changes;
  return ch && ch.length ? `Инцидент только что изменили: ${ch.map((c) => c.text).join("; ")}. Правка не сохранена.` : ex.message;
}

export function openTransfer(inc, snap, { onDone, copy, reopen }) {
  const useAI = aiEnabled(snap);
  const route = useAI && inc.ai?.route?.team ? inc.ai.route : null;
  const pct = (x) => x.confidence_text || `${Math.round((x.score ?? x.confidence ?? 0) * 100)}%`;
  const suggested = route ? [{ team: route.team, label: pct(route) }, ...(route.alternatives || []).map((a) => ({ team: a.team, label: pct(a) }))] : [];
  const action = useAI && (snap.actions || []).find((a) => a.incident_id === inc.id && a.kind === "route");
  const others = (snap.teams || []).filter((t) => t !== inc.team && !suggested.some((s) => s.team === t));
  const first = suggested.find((s) => s.team !== inc.team);

  modal().innerHTML = `
    <form class="modal-card" id="transfer-form">
      <h2>Передать в команду</h2>
      <div class="modal-sub">${esc(inc.product || "")} · ${esc(inc.title)}<br>${esc(inc.id)} · сейчас: <b>${esc(inc.team || "—")}</b></div>
      ${suggested.length ? `<div class="modal-label"><span class="ai-tag">ИИ</span> рекомендация по похожим случаям</div>
        <div class="team-options">${suggested.filter((s) => s.team !== inc.team).map((s, i) => `
          <label class="team-option"><input type="radio" name="team" value="${esc(s.team)}" ${s === first ? "checked" : ""}>
            <span><b>${esc(s.team)}</b><small>${esc(s.label)}${i === 0 && route && route.reason ? " · " + esc(route.reason) : ""}</small></span></label>`).join("")}
        </div>` : ""}
      <label for="transfer-team" class="modal-label">${first ? "Или другая команда" : "Команда"}</label>
      <select id="transfer-team" name="other"${first ? "" : " required"}><option value="">— выбрать из списка —</option>${others.map((t) => `<option>${esc(t)}</option>`).join("")}</select>
      <div class="modal-label">Исполнитель (необязательно)</div>
      <input name="assignee" placeholder="ФИО инженера, если известен" autocomplete="off">
      <div class="modal-label">Комментарий в журнал</div>
      <textarea name="comment" rows="2" placeholder="Например: подтвердили по телефону, дежурный в курсе"></textarea>
      ${action?.draft ? '<label class="check"><input type="checkbox" name="copy" checked> Скопировать текст передачи для чата/почты</label>' : ""}
      <div class="modal-error hidden"></div>
      <div class="modal-btns"><button type="button" class="btn" data-act="cancel">Отмена</button><button class="btn btn-primary">Передать</button></div>
    </form>`;
  modal().classList.remove("hidden");
  const form = document.getElementById("transfer-form");
  form.querySelector("[data-act=cancel]").onclick = closeModal;
  form.other.onchange = () => { if (form.other.value) form.querySelectorAll("input[name=team]").forEach((r) => (r.checked = false)); };
  form.onsubmit = async (e) => {
    e.preventDefault();
    const team = form.other.value || (form.querySelector("input[name=team]:checked") || {}).value;
    const err = form.querySelector(".modal-error");
    if (!team) { err.textContent = "Выберите команду"; err.classList.remove("hidden"); return; }
    const button = e.submitter || form.querySelector(".btn-primary");
    if (button.disabled) return;
    button.disabled = true;
    try {
      await api.assign(inc.id, team, form.comment.value.trim() || null, form.assignee.value.trim() || null, inc.hash);
      if (form.copy?.checked && action?.draft) {
        const text = route ? action.draft.split(route.team).join(team) : action.draft.replace("<команда>", team);
        await copy(text).catch(() => {});
      }
      if (form.isConnected) closeModal();
      onDone(`Передан в «${team}»`);
    } catch (ex) {
      err.textContent = conflictText(ex);
      if (ex.data && ex.data.conflict && reopen) {
        const b = document.createElement("button");
        b.type = "button"; b.className = "btn small"; b.textContent = "Обновить";
        b.onclick = () => { closeModal(); reopen(); };
        err.append(" ", b);
      }
      err.classList.remove("hidden");
    } finally {
      button.disabled = false;
    }
  };
  setTimeout(() => (form.querySelector("input[name=team]:checked") || form.other).focus(), 50);
}

// Эскалация: комментарий в журнал + копия текста эскалации
export function openEscalate(inc, draft, { onDone, copy }) {
  modal().innerHTML = `
    <form class="modal-card" id="esc-form">
      <h2>Эскалировать</h2>
      <div class="modal-sub">${esc(inc.product || "")} · ${esc(inc.id)} · команда <b>${esc(inc.team || "—")}</b></div>
      <div class="modal-label">Кому и почему (в журнал работ)</div>
      <textarea name="comment" rows="3">Эскалировано руководителю группы «${esc(inc.team || "")}»: ${esc(inc.forecast && inc.forecast.level === "overdue" ? "срок нарушен" : "срок под угрозой")}.</textarea>
      ${draft ? `<label class="check"><input type="checkbox" name="copy" checked> Скопировать текст эскалации</label>` : ""}
      <div class="modal-error hidden"></div>
      <div class="modal-btns"><button type="button" class="btn" data-act="cancel">Отмена</button><button class="btn btn-primary">Эскалировать</button></div>
    </form>`;
  modal().classList.remove("hidden");
  const form = document.getElementById("esc-form");
  form.querySelector("[data-act=cancel]").onclick = closeModal;
  form.onsubmit = async (e) => {
    e.preventDefault();
    const button = e.submitter || form.querySelector(".btn-primary");
    if (button.disabled) return;
    button.disabled = true;
    try {
      await api.escalate(inc.id, form.comment.value.trim() || null, inc.hash);
      if (draft && form.copy?.checked) await copy(draft).catch(() => {});
      if (form.isConnected) closeModal();
      onDone("Эскалация записана в журнал");
    } catch (ex) {
      const err = form.querySelector(".modal-error");
      err.textContent = ex.message;
      err.classList.remove("hidden");
    } finally {
      button.disabled = false;
    }
  };
}
