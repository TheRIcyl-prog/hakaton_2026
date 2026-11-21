export const aiEnabled = (snapshot) => snapshot?.ai_status?.provider !== "off";

export function modelOutage(outage, enabled) {
  return !enabled && ["gigachat", "cache"].includes(outage.source);
}

export function outageTitle(outage, enabled) {
  if (!modelOutage(outage, enabled)) return outage.name;
  return [outage.status ? "Сбой" : "Возможный сбой", ...outage.products].join(" · ");
}
