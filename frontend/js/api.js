// Опрос снимка с ETag и вызовы действий.
import { getSessionSignal, sessionFetch } from "./session.js";
export { getMe } from "./session.js";

const SNAPSHOT_POLL_MS = 400;

// Цикл опроса: onSnap(snap) при изменениях, onStatus(online) на каждом шаге
export function startPolling(onSnap, onStatus, signal) {
  const read = signal ? sessionFetch : fetch;
  const url = signal ? "/api/workstation/snapshot" : "/api/snapshot";
  let etag = null;
  let request = null;
  let timer;
  async function fetchSnapshot(force) {
    try {
      const headers = !force && etag ? { "If-None-Match": etag } : {};
      const r = await read(url, { headers, cache: "no-store", signal });
      if (r.status === 200) {
        const snap = await r.json();
        snap.now = r.headers.get("X-Dashboard-Now") || snap.now;
        signal?.throwIfAborted();
        onSnap(snap);
        etag = r.headers.get("ETag");
      } else if (r.status !== 304 || force) {
        throw new Error(`ошибка ${r.status}`);
      }
      onStatus(true);
      return true;
    } catch {
      if (!signal?.aborted) onStatus(false);
      return false;
    }
  }
  // Принудительное обновление идёт после уже начатого опроса, чтобы старый ответ не перезаписал новый снимок.
  function tick(force = false) {
    clearTimeout(timer);
    if (signal?.aborted) return Promise.resolve(false);
    const pending = request ? request.then(() => fetchSnapshot(force)) : fetchSnapshot(force);
    request = pending;
    pending.then(() => {
      if (request !== pending) return;
      request = null;
      if (!signal?.aborted) timer = setTimeout(tick, SNAPSHOT_POLL_MS);
    });
    return pending;
  }
  signal?.addEventListener("abort", () => clearTimeout(timer), { once: true });
  tick();
  return () => tick(true);
}

async function send(method, url, body, options) {
  const r = await sessionFetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
    ...options,
  });
  let data = null;
  try { data = await r.json(); } catch { /* пустой ответ */ }
  getSessionSignal().throwIfAborted();
  options?.signal?.throwIfAborted();
  if (!r.ok) {
    const err = new Error((data && data.detail) || `ошибка ${r.status}`);
    err.status = r.status;
    err.data = data;
    throw err;
  }
  return data;
}

export const api = {
  search: (params) => send("GET", `/api/search?${params}`),
  recentlyClosed: (kind, offset, signal) => send("GET", `/api/recently-closed?${new URLSearchParams({ kind, offset })}`, undefined, { signal }),
  importFile: (text) => send("POST", "/api/import", undefined, { body: text, headers: { "Content-Type": "text/plain; charset=utf-8" } }),
  queue: (signal) => send("GET", "/api/queue", undefined, { signal }),
  handoffs: (signal) => send("GET", "/api/handoffs", undefined, { signal }),
  sendHandoff: (recipient_id, note) => send("POST", "/api/handoffs", { recipient_id, note }),
  acknowledgeHandoff: (id) => send("POST", `/api/handoffs/${encodeURIComponent(id)}/acknowledge`),
  queuePreference: (id, operation) => send("POST", "/api/queue/preferences", { id, operation }),
  // «Взять в работу» снимает тревогу у всех (кнопки «Принял» нет). Уже у другого → 409 с data.coordinator.
  // При перехвате привязываем подтверждение к версии, которую видел координатор.
  claim: async (id, force, expected_hash) => {
    if (force && !expected_hash) throw new Error("Обновите карточку перед перехватом инцидента");
    return send("POST", `/api/incidents/${encodeURIComponent(id)}/claim`, { force: force || undefined, expected_hash });
  },
  release: (id, hash) => send("DELETE", `/api/incidents/${encodeURIComponent(id)}/claim${hash ? `?expected_hash=${encodeURIComponent(hash)}` : ""}`),
  // expected_hash: инцидент изменился, пока координатор смотрел → 409 {conflict, changes}, правка не сохраняется
  assign: (id, team, comment, assignee, hash) => send("POST", `/api/itsm/${encodeURIComponent(id)}/assign`, { team, comment, assignee, expected_hash: hash || undefined }),
  state: (id, state_id, comment, reason, expected_hash) => send("POST", `/api/itsm/${encodeURIComponent(id)}/state`, { state_id, comment, reason, expected_hash }),
  priority: (id, priority_id, recalc_deadline, comment, expected_hash) => send("POST", `/api/itsm/${encodeURIComponent(id)}/priority`, { priority_id, recalc_deadline, comment, expected_hash }),
  deadline: (id, deadline, comment, expected_hash) => send("POST", `/api/itsm/${encodeURIComponent(id)}/deadline`, { deadline, comment, expected_hash }),
  comment: (id, text, expected_hash) => send("POST", `/api/itsm/${encodeURIComponent(id)}/comment`, { text, expected_hash }),
  escalate: (id, comment, expected_hash) => send("POST", `/api/itsm/${encodeURIComponent(id)}/escalate`, { comment, expected_hash }),
  acknowledgeEscalation: (id, request_id, expected_hash) => send("POST",
    `/api/itsm/${encodeURIComponent(id)}/escalation/acknowledge`, { request_id, expected_hash }),
  confirmDups: (master_id, ids) => send("POST", "/api/duplicates/confirm", { master_id, ids }),
  confirmOutage: (candidate_id) => send("POST", "/api/outages/confirm", { candidate_id }),
  outageCandidate: (id) => send("GET", `/api/outage-candidates/${encodeURIComponent(id)}`),
  excludeOutageCandidateMember: (candidateId, incidentId) => send("DELETE",
    `/api/outage-candidates/${encodeURIComponent(candidateId)}/incidents/${encodeURIComponent(incidentId)}`),
  outage: (id) => send("GET", `/api/outages/${encodeURIComponent(id)}`),
  outageAction: (id, fields) => send("POST", `/api/outages/${encodeURIComponent(id)}/actions`, fields),
  addOutageMember: (outageId, incidentId, hash, incidentIds) => send("POST",
    `/api/outages/${encodeURIComponent(outageId)}/incidents/${encodeURIComponent(incidentId)}`,
    { expected_hash: hash, expected_incident_ids: incidentIds }),
  excludeOutageMember: (outageId, incidentId, hash, incidentIds) => send("DELETE",
    `/api/outages/${encodeURIComponent(outageId)}/incidents/${encodeURIComponent(incidentId)}`,
    { expected_hash: hash, expected_incident_ids: incidentIds }),
  meta: () => send("GET", "/api/meta"),
  incident: (id) => send("GET", `/api/incidents/${encodeURIComponent(id)}`),
  aiFeedback: (id, fields) => send("POST", `/api/incidents/${encodeURIComponent(id)}/ai-feedback`, fields),
};
