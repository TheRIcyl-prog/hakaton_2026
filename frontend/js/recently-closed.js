import { api } from "./api.js";
import { aiEnabled, outageTitle } from "./ai-mode.js";
import { esc, fmtDateTime, plural } from "./format.js";

const RECHECK_MS = 5000;

function entry(row, kind, snap) {
  const at = `<time datetime="${esc(row.at)}">${esc(fmtDateTime(row.at, Date.parse(snap.now)))}</time>`;
  if (kind === "incidents") {
    return `<article class="outage-card corners">
      <div class="recent-entry-head">
        <div><h3>${esc(row.title || row.id)}</h3>
          <p>${[row.id, row.product, row.team].filter(Boolean).map(esc).join(" · ")}</p>
          ${at}
        </div>
        <button type="button" class="btn" data-recent-incident="${esc(row.id)}">Открыть карточку</button>
      </div>
    </article>`;
  }
  return `<article class="outage-card corners">
    <div class="recent-entry-head">
      <div><h3>${esc(outageTitle(row, aiEnabled(snap)))}</h3>
        <p>${row.count} ${plural(row.count, "заявка", "заявки", "заявок")}${row.products.length ? " · " + esc(row.products.join(", ")) : ""}</p>
      </div>
      ${at}
    </div>
    <details class="recent-details" data-recent-outage="${esc(row.id)}">
      <summary>Состав</summary><div class="recent-members"></div>
    </details>
  </article>`;
}

export function mountRecentlyClosed(panel, open, signal) {
  const list = panel.querySelector(".recent-list");
  const status = panel.querySelector(".recent-status");
  const pages = panel.querySelector(".recent-pages");
  let snap, version, active = false, request, timer, data;
  let kind = "incidents", offset = 0;

  function pause() {
    clearTimeout(timer);
    request?.abort();
    request = null;
  }

  async function loadMembers(details) {
    const host = details.querySelector(".recent-members");
    if (host.dataset.loaded) return;
    host.dataset.loaded = "true";
    host.textContent = "Загружаем состав…";
    try {
      const outage = await api.outage(details.dataset.recentOutage);
      if (!host.isConnected) return;
      host.innerHTML = `<ul class="outage-members">${outage.incidents.map(inc =>
        `<li><button type="button" class="outage-member" data-recent-incident="${esc(inc.id)}">
          <b>${esc(inc.id)}</b> <span>${esc(inc.title || inc.product || "—")}</span> <span>${esc(inc.state || "")}</span>
        </button></li>`).join("")}</ul>`;
    } catch {
      delete host.dataset.loaded;
      host.innerHTML = '<p class="recent-status">Состав не загрузился. <button type="button" class="btn small" data-recent-retry>Повторить</button></p>';
    }
  }

  function paint() {
    status.textContent = data.total ? "" : (kind === "incidents"
      ? "За последние 3 часа закрытых инцидентов нет."
      : "За последние 3 часа завершённых сбоев нет.");
    pages.hidden = data.previous_offset == null && data.next_offset == null;
    for (const button of pages.querySelectorAll("button")) {
      button.disabled = data[button.dataset.recentPage] == null;
    }
    const markup = data.items.map(row => entry(row, kind, snap)).join("");
    if (list.dataset.markup === markup) return;
    const expanded = new Set([...list.querySelectorAll("details[open]")].map(details => details.dataset.recentOutage));
    list.dataset.markup = markup;
    list.innerHTML = markup;
    for (const details of list.querySelectorAll("details")) {
      details.open = expanded.has(details.dataset.recentOutage);
      details.ontoggle = () => { if (details.open) loadMembers(details); };
    }
  }

  async function refresh() {
    if (!active || request || signal.aborted) return;
    clearTimeout(timer);
    const current = new AbortController();
    request = current;
    try {
      const result = await api.recentlyClosed(kind, offset, current.signal);
      if (request !== current) return;
      if (offset && !result.items.length) {
        offset = 0;
        request = null;
        return refresh();
      }
      data = result;
      paint();
    } catch {
      if (request === current) status.textContent = "Список не обновился. Повторим автоматически.";
    } finally {
      if (request === current) {
        request = null;
        if (active) timer = setTimeout(refresh, RECHECK_MS);
      }
    }
  }

  function switchPage(nextOffset) {
    pause();
    offset = nextOffset;
    data = null;
    list.replaceChildren();
    delete list.dataset.markup;
    pages.hidden = true;
    status.textContent = "Загружаем записи…";
    refresh();
  }

  panel.addEventListener("click", event => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.dataset.recentIncident) return open(button.dataset.recentIncident);
    if (button.hasAttribute("data-recent-retry")) return loadMembers(button.closest("details"));
    const tab = button.dataset.recentKind;
    if (tab && tab !== kind) {
      kind = tab;
      for (const control of panel.querySelectorAll("[data-recent-kind]")) {
        const selected = control === button;
        control.classList.toggle("active", selected);
        control.setAttribute("aria-pressed", String(selected));
      }
      switchPage(0);
    }
    const page = data?.[button.dataset.recentPage];
    if (page != null) switchPage(page);
  });

  signal.addEventListener("abort", pause, { once: true });
  return {
    update(visible, snapshot) {
      snap = snapshot;
      panel.classList.toggle("hidden", !visible);
      if (!visible) {
        if (active) pause();
        active = false;
        return;
      }
      const refreshNeeded = !active || version !== snapshot.version;
      active = true;
      version = snapshot.version;
      if (!data) status.textContent = "Загружаем записи…";
      if (refreshNeeded) refresh();
    },
  };
}
