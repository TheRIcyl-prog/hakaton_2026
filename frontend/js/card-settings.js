const FIELDS = [
  ["product", "Продукт", false],
  ["created", "Время регистрации", false],
  ["executor", "Команда и исполнитель", false],
  ["state", "Статус", false],
  ["deadline", "Срок", true],
  ["labels", "Метки", true],
  ["summary", "Краткая суть", false],
  ["ai", "Рекомендации ИИ", false],
  ["updates", "Недавние изменения", false],
];
const PREFIX = "mi-dashboard:card-fields:";
const defaults = Object.fromEntries(FIELDS.map(([key, , value]) => [key, value]));

export function mountCardSettings(dialog, { getName, onChange }) {
  const profile = dialog.querySelector("#card-settings-profile");
  const container = dialog.querySelector("#card-settings-fields");
  const fallback = new Map();
  const controls = FIELDS.map(([key, label]) => {
    const row = document.createElement("label");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.name = key;
    row.append(input, label);
    container.append(row);
    return input;
  });
  let selected;
  let fields;

  function sync(force = false) {
    const name = getName().trim();
    const key = PREFIX + (name ? "by:" + name : "local");
    if (key === selected && !force) return;
    selected = key;
    let saved = fallback.get(key);
    try { saved = JSON.parse(localStorage.getItem(key)); } catch { /* локальное хранилище недоступно */ }
    fields = Object.fromEntries(FIELDS.map(([id, , value]) => [id, typeof saved?.[id] === "boolean" ? saved[id] : value]));
    profile.textContent = name ? `Настройки координатора · ${name}` : "Локальный профиль";
    controls.forEach((input) => { input.checked = fields[input.name]; });
  }

  function save(value) {
    fields = value;
    fallback.set(selected, value);
    try { localStorage.setItem(selected, JSON.stringify(value)); } catch { /* настройки действуют до перезагрузки */ }
    onChange();
  }

  container.addEventListener("change", (event) => {
    const input = event.target;
    save({ ...fields, [input.name]: input.checked });
  });
  dialog.querySelector("#card-settings-reset").onclick = () => {
    controls.forEach((input) => { input.checked = defaults[input.name]; });
    save({ ...defaults });
  };
  window.addEventListener("storage", (event) => {
    if (event.key === null || event.key === selected) { sync(true); onChange(); }
  });
  return {
    fields() { sync(); return fields; },
    setAiEnabled(enabled) {
      for (const input of controls) {
        if (input.name === "summary" || input.name === "ai") input.parentElement.classList.toggle("hidden", !enabled);
      }
    },
    open() { sync(); dialog.showModal(); },
  };
}
