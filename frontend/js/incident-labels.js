import { esc } from "./format.js";

const LABELS = [
  ["waiting_external", "Ждём внешних"],
  ["aftermath", "Устранить последствия"],
  ["clients_affected", "Задеты клиенты"],
  ["reassigned", "Перекидывание"],
];

export function incidentLabels(inc, limit = Infinity) {
  const names = new Set(inc.labels || []);
  const tags = [];
  const recurrence = inc.recurrence;
  if (recurrence || names.has("recurrence")) {
    const strong = recurrence?.level === "strong";
    const serial = recurrence ? ` · ${esc(recurrence.count + 1)}-й за неделю` : "";
    tags.push(`<span class="incident-label recurrence-badge${strong ? " strong" : ""}"${strong ? ' title="Сбой повторился после постоянного решения"' : ""}>${strong ? "Сильный повтор" : "Повтор"}${serial}</span>`);
  }
  for (const [name, text] of LABELS) {
    if (names.has(name)) tags.push(`<span class="incident-label ${name}">${text}</span>`);
  }
  return tags.slice(0, limit).join("");
}
