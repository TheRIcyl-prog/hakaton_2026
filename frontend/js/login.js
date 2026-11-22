import { activateSession, announceSessionChange, loadProfile, returnPath } from "./session.js";

const form = document.getElementById("login-form");
const login = document.getElementById("login");
const password = document.getElementById("password");
const button = document.getElementById("login-submit");
const message = document.getElementById("login-message");
const overlay = document.getElementById("start-overlay");
const workspace = document.getElementById("arm-session");
const params = new URLSearchParams(location.search);
const target = returnPath(params.get("return"));

async function open(profile) {
  if (target !== "/") {
    location.replace(target);
    return;
  }
  if (params.has("return") || params.has("expired")) history.replaceState(null, "", "/");
  const signal = activateSession(profile, () => {
    workspace.hidden = workspace.inert = true;
    workspace.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close());
  });
  document.getElementById("me-label").textContent = profile.display_name;
  await import("./main.js");
  if (signal.aborted) return;
  workspace.hidden = workspace.inert = false;
  overlay.hidden = true;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (button.disabled) return;
  button.disabled = true;
  button.textContent = "Входим…";
  message.textContent = "";
  try {
    const response = await fetch("/api/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ login: login.value.trim(), password: password.value }),
    });
    if (response.status === 401) {
      message.textContent = "Неверный логин или пароль";
      password.focus();
      return;
    }
    if (!response.ok) throw new Error();
    password.value = "";
    announceSessionChange();
    await open(await response.json());
  } catch {
    message.textContent = "Не удалось войти. Попробуйте ещё раз";
  } finally {
    password.value = "";
    button.disabled = false;
    button.textContent = "Войти";
  }
});

const profile = await loadProfile().catch(() => {
  message.textContent = "Не удалось проверить сессию. Попробуйте войти ещё раз";
  return null;
});
if (profile) await open(profile);
else {
  overlay.hidden = false;
  if (params.has("expired") && !message.textContent) message.textContent = "Войдите снова";
  login.focus();
}
