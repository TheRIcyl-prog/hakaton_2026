// Диалоги АРМ: передача в команду и подтверждение эскалации.
import { esc, shortId } from "./format.js";
import { api } from "./api.js";

const modal = () => document.getElementById("modal");

export function closeModal() {
  modal().classList.add("hidden");
  modal().innerHTML = "";
}

// Передать инцидент в команду. Рекомендованные команды — сверху с долей похожих случаев.
export function openTransfer(inc, snap, { onDone, copy }) {
  const route = inc.ai && inc.ai.route;
  const suggested = route ? [{ team: route.team, score: route.confidence }, ...route.alternatives] : [];
  const action = (snap.actions || []).find((a) => a.incident_id === inc.id && a.kind === "route");
  const others = (snap.teams || []).filter((t) => t !== inc.team && !suggested.some((s) => s.team === t));
  const first = suggested.find((s) => s.team !== inc.team);

  modal().innerHTML = `
    <form class="modal-card" id="transfer-form">
      <h2>Передать в команду</h2>
      <div class="modal-sub">${esc(shortId(inc.id))} · ${esc(inc.product || "")} · ${esc(inc.title)}<br>сейчас: <b>${esc(inc.team || "—")}</b></div>
      ${suggested.length ? `<div class="modal-label">Рекомендация по похожим случаям</div>
        <div class="team-options">${suggested.filter((s) => s.team !== inc.team).map((s, i) => `
          <label class="team-option"><input type="radio" name="team" value="${esc(s.team)}" ${s === first ? "checked" : ""}>
            <span><b>${esc(s.team)}</b><small>${Math.round(s.score * 100)}%${i === 0 && route ? " · " + esc(route.reason) : ""}</small></span></label>`).join("")}
        </div>` : `<div class="modal-sub">Похожих случаев нет — выберите команду из списка.</div>`}
      <div class="modal-label">Или другая команда</div>
      <select name="other"><option value="">— выбрать из списка —</option>${others.map((t) => `<option>${esc(t)}</option>`).join("")}</select>
      <div class="modal-label">Исполнитель (необязательно)</div>
      <input name="assignee" placeholder="ФИО инженера, если известен" autocomplete="off">
      <div class="modal-label">Комментарий в журнал</div>
      <textarea name="comment" rows="2" placeholder="Например: подтвердили по телефону, дежурный в курсе"></textarea>
      <label class="check"><input type="checkbox" name="copy" checked> Скопировать текст передачи для чата/почты</label>
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
    try {
      if (form.copy.checked && action) {
        const text = route ? action.draft.split(route.team).join(team) : action.draft.replace("<команда>", team);
        await copy(text);
      }
      await api.assign(inc.id, team, form.comment.value.trim() || null, form.assignee.value.trim() || null);
      closeModal();
      onDone(`Передан в «${team}»`);
    } catch (ex) {
      err.textContent = ex.message;
      err.classList.remove("hidden");
    }
  };
  setTimeout(() => (form.querySelector("input[name=team]:checked") || form.other).focus(), 50);
}

// Эскалация: комментарий в журнал + копия текста эскалации
export function openEscalate(inc, draft, { onDone, copy }) {
  modal().innerHTML = `
    <form class="modal-card" id="esc-form">
      <h2>Эскалировать</h2>
      <div class="modal-sub">${esc(shortId(inc.id))} · ${esc(inc.product || "")} · команда <b>${esc(inc.team || "—")}</b></div>
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
    try {
      if (draft && form.copy && form.copy.checked) await copy(draft);
      await api.escalate(inc.id, form.comment.value.trim() || null);
      closeModal();
      onDone("Эскалация записана в журнал");
    } catch (ex) {
      const err = form.querySelector(".modal-error");
      err.textContent = ex.message;
      err.classList.remove("hidden");
    }
  };
}
