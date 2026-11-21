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
  let request = null;
  let timer;
  async function fetchSnapshot(force) {
    try {
      const headers = !force && etag ? { "If-None-Match": etag } : {};
      const r = await fetch("/api/snapshot", { headers, cache: "no-store" });
      if (r.status === 200) {
        const snap = await r.json();
        onSnap(snap);
        etag = r.headers.get("ETag");
      } else if (r.status !== 304 || force) {
        throw new Error(`ошибка ${r.status}`);
      }
      onStatus(true);
      return true;
    } catch {
      onStatus(false);
      return false;
    }
  }
  // Принудительное обновление идёт после уже начатого опроса, чтобы старый ответ не перезаписал новый снимок.
  function tick(force = false) {
    clearTimeout(timer);
    const pending = request ? request.then(() => fetchSnapshot(force)) : fetchSnapshot(force);
    request = pending;
    pending.then(() => {
      if (request !== pending) return;
      request = null;
      timer = setTimeout(tick, intervalMs);
    });
    return pending;
  }
  tick();
  return () => tick(true);
}

async function send(method, url, body, options) {
  const r = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
    ...options,
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
  queue: (by, signal) => send("GET", `/api/queue${by ? `?by=${encodeURIComponent(by)}` : ""}`, undefined, { signal, cache: "no-store" }),
  queuePreference: (by, id, operation) => send("POST", "/api/queue/preferences", { by, id, operation }),
  // «Взять в работу» снимает тревогу у всех (кнопки «Принял» нет). Уже у другого → 409 с data.coordinator.
  // При перехвате привязываем подтверждение к версии, которую видел координатор.
  claim: async (id, force, expected_hash) => {
    if (force && !expected_hash) throw new Error("Обновите карточку перед перехватом инцидента");
    return send("POST", `/api/incidents/${encodeURIComponent(id)}/claim`, { by: getMe(), force: force || undefined, expected_hash });
  },
  release: (id) => send("DELETE", `/api/incidents/${encodeURIComponent(id)}/claim?by=${encodeURIComponent(getMe())}`),
  // expected_hash: инцидент изменился, пока координатор смотрел → 409 {conflict, changes}, правка не сохраняется
  assign: (id, team, comment, assignee, hash) => send("POST", `/api/itsm/${encodeURIComponent(id)}/assign`, { team, comment, assignee, by: getMe(), expected_hash: hash || undefined }),
  state: (id, state_id, comment, reason, expected_hash) => send("POST", `/api/itsm/${encodeURIComponent(id)}/state`, { state_id, comment, reason, expected_hash, by: getMe() }),
  priority: (id, priority_id, recalc_deadline, comment, expected_hash) => send("POST", `/api/itsm/${encodeURIComponent(id)}/priority`, { priority_id, recalc_deadline, comment, expected_hash, by: getMe() }),
  deadline: (id, deadline, comment, expected_hash) => send("POST", `/api/itsm/${encodeURIComponent(id)}/deadline`, { deadline, comment, expected_hash, by: getMe() }),
  comment: (id, text, expected_hash) => send("POST", `/api/itsm/${encodeURIComponent(id)}/comment`, { text, expected_hash, by: getMe() }),
  escalate: (id, comment, expected_hash) => send("POST", `/api/itsm/${encodeURIComponent(id)}/escalate`, { comment, expected_hash, by: getMe() }),
  confirmDups: (master_id, ids) => send("POST", "/api/duplicates/confirm", { master_id, ids, by: getMe() }),
  confirmOutage: (candidate_id) => send("POST", "/api/outages/confirm", { candidate_id, by: getMe() }),
  outageCandidate: (id) => send("GET", `/api/outage-candidates/${encodeURIComponent(id)}`),
  excludeOutageCandidateMember: (candidateId, incidentId) => send("DELETE",
    `/api/outage-candidates/${encodeURIComponent(candidateId)}/incidents/${encodeURIComponent(incidentId)}`, { by: getMe() }),
  outage: (id) => send("GET", `/api/outages/${encodeURIComponent(id)}`),
  outageAction: (id, fields) => send("POST", `/api/outages/${encodeURIComponent(id)}/actions`, { ...fields, by: getMe() }),
  excludeOutageMember: (outageId, incidentId, hash, incidentIds) => send("DELETE",
    `/api/outages/${encodeURIComponent(outageId)}/incidents/${encodeURIComponent(incidentId)}`,
    { by: getMe(), expected_hash: hash, expected_incident_ids: incidentIds }),
  meta: () => send("GET", "/api/meta"),
  incident: (id) => send("GET", `/api/incidents/${encodeURIComponent(id)}`),
  aiFeedback: (id, fields) => send("POST", `/api/incidents/${encodeURIComponent(id)}/ai-feedback`, fields),
};
