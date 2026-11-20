// Опрос снимка с ETag и вызовы действий. Общий для АРМ, видеостены и ITSM.
const ME_KEY = "mi-dashboard:me";

export function getMe() {
  try { return localStorage.getItem(ME_KEY) || ""; } catch { return ""; }
}
export function setMe(name) {
  try { localStorage.setItem(ME_KEY, name); } catch { /* приватный режим */ }
}

// Цикл опроса: onSnap(snap) при изменениях, onStatus(online) на каждом шаге
export function startPolling(onSnap, onStatus, intervalMs = 1000) {
  let etag = null;
  async function tick() {
    try {
      const r = await fetch("/api/snapshot", { headers: etag ? { "If-None-Match": etag } : {}, cache: "no-store" });
      if (r.status === 200) {
        etag = r.headers.get("ETag");
        onSnap(await r.json());
      }
      onStatus(true);
    } catch {
      onStatus(false);
    }
    setTimeout(tick, intervalMs);
  }
  tick();
}

async function send(method, url, body) {
  const r = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await r.json(); } catch { /* пустой ответ */ }
  if (!r.ok) {
    const err = new Error((data && data.detail) || `ошибка ${r.status}`);
    err.status = r.status;
    err.data = data;
    throw err;
  }
  return data;
}

export const api = {
  // «Взять в работу» снимает тревогу у всех (кнопки «Принял» нет). Уже у другого → 409 с data.coordinator.
  // expected_hash не шлём: «взять» не меняет полей инцидента, конфликт тут только мешал бы.
  claim: (id, force) => send("POST", `/api/incidents/${encodeURIComponent(id)}/claim`, { by: getMe(), force: force || undefined }),
  release: (id) => send("DELETE", `/api/incidents/${encodeURIComponent(id)}/claim?by=${encodeURIComponent(getMe())}`),
  assign: (id, team, comment, assignee) => send("POST", `/api/itsm/${encodeURIComponent(id)}/assign`, { team, comment, assignee, by: getMe() }),
  state: (id, state_id, comment, reason) => send("POST", `/api/itsm/${encodeURIComponent(id)}/state`, { state_id, comment, reason, by: getMe() }),
  priority: (id, priority_id, recalc_deadline, comment) => send("POST", `/api/itsm/${encodeURIComponent(id)}/priority`, { priority_id, recalc_deadline, comment, by: getMe() }),
  deadline: (id, deadline, comment) => send("POST", `/api/itsm/${encodeURIComponent(id)}/deadline`, { deadline, comment, by: getMe() }),
  comment: (id, text) => send("POST", `/api/itsm/${encodeURIComponent(id)}/comment`, { text, by: getMe() }),
  escalate: (id, comment) => send("POST", `/api/itsm/${encodeURIComponent(id)}/escalate`, { comment, by: getMe() }),
  meta: () => send("GET", "/api/meta"),
  incident: (id) => send("GET", `/api/incidents/${encodeURIComponent(id)}`),
};
