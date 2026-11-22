let profile = null;
let controller;
let lockPage;
let sessionChannel;

export const getMe = () => profile?.display_name || "";
export const getSessionSignal = () => controller.signal;

export function returnPath(value) {
  if (!value || !/^\/itsm\/[^/?#\\]+$/.test(value)) return "/";
  try {
    const id = decodeURIComponent(value.slice("/itsm/".length));
    if (id === "." || id === ".." || /[/\\\u0000-\u001f\u007f]/u.test(id)) return "/";
    return `/itsm/${encodeURIComponent(id)}`;
  } catch { return "/"; }
}

export function loginLocation(message) {
  const params = new URLSearchParams();
  const target = returnPath(location.pathname);
  if (target !== "/") params.set("return", target);
  if (message === "expired") params.set("expired", "1");
  return params.size ? `/?${params}` : "/";
}

export async function loadProfile() {
  const response = await fetch("/api/auth/me", { cache: "no-store" });
  if (response.status === 401) return null;
  if (!response.ok) throw new Error("Не удалось проверить сессию");
  return response.json();
}

export function activateSession(current, onLock) {
  profile = current;
  lockPage = onLock;
  controller = new AbortController();
  if ("BroadcastChannel" in window) {
    sessionChannel = new BroadcastChannel("coordinator-session");
    sessionChannel.onmessage = () => leave("expired");
  }
  window.addEventListener("pagehide", (event) => {
    if (event.persisted) discard();
  });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) location.reload();
  });
  return controller.signal;
}

export function announceSessionChange() {
  if (!("BroadcastChannel" in window)) return;
  const channel = new BroadcastChannel("coordinator-session");
  channel.postMessage(null);
  channel.close();
}

function discard() {
  if (!profile) return false;
  profile = null;
  controller.abort();
  sessionChannel?.close();
  lockPage();
  return true;
}

function leave(message) {
  if (discard()) location.replace(loginLocation(message));
}

export async function sessionFetch(url, options = {}) {
  if (!profile) throw new DOMException("Сессия завершена", "AbortError");
  const signal = options.signal ? AbortSignal.any([controller.signal, options.signal]) : controller.signal;
  const response = await fetch(url, { cache: "no-store", ...options, signal });
  if (response.status === 401) leave("expired");
  signal.throwIfAborted();
  return response;
}

export async function logout() {
  const response = await fetch("/api/auth/logout", { method: "POST", cache: "no-store" });
  if (response.status !== 204 && response.status !== 401) {
    throw new Error("Не удалось выйти. Попробуйте ещё раз.");
  }
  announceSessionChange();
  leave();
}
