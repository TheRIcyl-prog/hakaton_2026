import { api, getMe } from "./api.js";
import { esc } from "./format.js";

const TYPES = { route: "Кому передать", summary: "Суть", risk: "Риск", dynamics: "Динамика" };
const drafts = new WeakMap();

export function feedbackMarkup(inc, snap) {
  const ai = inc.ai || {};
  const types = Object.keys(TYPES).filter((kind) => ai[kind]
    && (kind !== "dynamics" || ai.dynamics.flag) && (kind !== "risk" || ai.risk.level !== "low"));
  if (!types.length) return "";
  const teams = (snap?.teams || []).filter((team) => team !== ai.route?.team && team !== "AutoClosedMi" &&
    team !== "КЦ ИТ Координаторы Массовых Инцидентов");
  return `<details class="ai-box" data-ai-feedback><summary>Неверно</summary>
    <form class="comment-form"><label>Подсказка <select class="select" name="kind">${types.map((kind) =>
      `<option value="${kind}">${TYPES[kind]}</option>`).join("")}</select></label>
      <label data-team-label${types[0] === "route" ? "" : ' class="hidden"'}>Команда <select class="select" name="team">
        <option value="">Выберите команду</option>${teams.map((team) => `<option value="${esc(team)}">${esc(team)}</option>`).join("")}</select></label>
      <button class="btn small" type="submit">Сохранить</button></form><div data-feedback-status role="status"></div></details>`;
}

export function rememberFeedback(host, same) {
  const draft = drafts.get(host);
  if (!same) { drafts.delete(host); return; }
  const box = host.querySelector("[data-ai-feedback]");
  if (!draft || !box) return;
  draft.open = box.open;
  draft.kind = box.querySelector("[name=kind]").value;
  draft.team = box.querySelector("[name=team]").value;
}

export function bindFeedback(host, incident) {
  const box = host.querySelector("[data-ai-feedback]");
  if (!box) return;
  const form = box.querySelector("form");
  const kind = form.elements.namedItem("kind");
  const team = form.elements.namedItem("team");
  const draft = drafts.get(host) || { open: false, kind: "", team: "", message: "", pending: false };
  drafts.set(host, draft);
  box.open = draft.open;
  if (Array.from(kind.options).some((option) => option.value === draft.kind)) kind.value = draft.kind;
  if (Array.from(team.options).some((option) => option.value === draft.team)) team.value = draft.team;
  kind.onchange = () => box.querySelector("[data-team-label]").classList.toggle("hidden", kind.value !== "route");
  kind.onchange();
  const paintStatus = () => {
    if (drafts.get(host) !== draft) return;
    const current = host.querySelector("[data-ai-feedback]");
    if (!current) return;
    current.querySelector("[data-feedback-status]").textContent = draft.message;
    current.querySelector("button").disabled = draft.pending;
  };
  paintStatus();
  form.onsubmit = async (event) => {
    event.preventDefault();
    if (draft.pending) return;
    const by = getMe();
    const isRoute = kind.value === "route";
    if (!by || (isRoute && !team.value)) {
      draft.message = by ? "Укажите подходящую команду." : "Сначала представьтесь в АРМ.";
      paintStatus();
      return;
    }
    draft.pending = true;
    draft.message = "";
    paintStatus();
    try {
      const result = await api.aiFeedback(incident.id, {
        by, kind: kind.value, expected_hash: incident.hash,
        corrected_team: isRoute ? team.value : null, expected_team: isRoute ? incident.ai.route.team : null,
      });
      draft.message = result.saved ? "Исправление сохранено." : "Это исправление уже учтено.";
    } catch (error) {
      draft.message = error.status ? error.message : "Не удалось связаться с сервером.";
    } finally {
      draft.pending = false;
      paintStatus();
    }
  };
}
