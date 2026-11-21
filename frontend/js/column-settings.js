import { esc } from "./format.js";

const PREFIX = "mi-dashboard:arm-columns:";

export function orderGroups(groups, order) {
  const ranks = new Map(order.map((key, index) => [key, index]));
  return [...groups].sort((a, b) => (ranks.get(a.key) ?? order.length) - (ranks.get(b.key) ?? order.length));
}

export function mountColumnSettings(dialog, { getName, onChange }) {
  const list = dialog.querySelector("#column-order");
  const collapse = dialog.querySelector("#collapse-empty");
  const profile = dialog.querySelector("#column-settings-profile");
  const fallback = new Map();
  let groups = [];
  let groupKey = "";
  let key;
  let settings;

  function paint(focusKey, direction) {
    collapse.checked = settings.collapse;
    const ordered = orderGroups(groups, settings.order);
    list.innerHTML = ordered.map((group, i) => `<li data-group="${esc(group.key)}">
      <span>${esc(group.title)}</span>
      <button type="button" class="btn small" data-step="-1" aria-label="Выше · ${esc(group.title)}"${i === 0 ? " disabled" : ""}>Выше</button>
      <button type="button" class="btn small" data-step="1" aria-label="Ниже · ${esc(group.title)}"${i === ordered.length - 1 ? " disabled" : ""}>Ниже</button>
    </li>`).join("");
    const row = [...list.children].find((item) => item.dataset.group === focusKey);
    if (row) (row.querySelector(`[data-step="${direction}"]:enabled`) || row.querySelector("button:enabled"))?.focus();
  }

  function sync(force = false) {
    const name = getName().trim();
    const selected = PREFIX + (name ? "by:" + name : "local");
    if (key === selected && !force) return false;
    key = selected;
    let saved = fallback.get(key);
    try { saved = JSON.parse(localStorage.getItem(key)); } catch { /* локальное хранилище недоступно */ }
    settings = {
      collapse: typeof saved?.collapse === "boolean" ? saved.collapse : true,
      order: Array.isArray(saved?.order) ? [...new Set(saved.order.filter((id) => typeof id === "string"))] : [],
    };
    profile.textContent = name ? `Настройки координатора · ${name}` : "Локальный профиль";
    return true;
  }

  function save(next, focusKey, step) {
    settings = next;
    fallback.set(key, next);
    try { localStorage.setItem(key, JSON.stringify(next)); } catch { /* настройки действуют до перезагрузки */ }
    paint(focusKey, step);
    onChange();
  }

  collapse.onchange = () => save({ ...settings, collapse: collapse.checked });
  dialog.querySelector("#column-order-reset").onclick = () => save({ ...settings, order: [] });
  list.onclick = (event) => {
    const button = event.target.closest("[data-step]");
    if (!button) return;
    const id = button.closest("li").dataset.group;
    const step = Number(button.dataset.step);
    const order = orderGroups(groups, settings.order).map((group) => group.key);
    const from = order.indexOf(id);
    const to = from + step;
    if (from < 0 || to < 0 || to >= order.length) return;
    [order[from], order[to]] = [order[to], order[from]];
    save({ ...settings, order }, id, step);
  };
  window.addEventListener("storage", (event) => {
    if (event.key === null || event.key === key) { sync(true); paint(); onChange(); }
  });
  return {
    settings(nextGroups = groups) {
      const nextKey = JSON.stringify(nextGroups.map(({ key, title }) => [key, title]));
      const profileChanged = sync();
      if (nextKey !== groupKey || profileChanged) {
        groups = nextGroups.map(({ key, title }) => ({ key, title }));
        groupKey = nextKey;
        paint();
      }
      return settings;
    },
    open() { this.settings(); dialog.showModal(); },
  };
}
