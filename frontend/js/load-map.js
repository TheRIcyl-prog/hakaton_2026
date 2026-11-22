import { esc, fmtTime, fmtWhen } from "./format.js";

export function mountLoadMap(el, openIncident) {
  let selected;
  let markup;

  function setVisible(visible, snap) {
    el.classList.toggle("hidden", !visible);
    if (!visible) return;
    const products = new Map();
    const groups = snap.groups;
    for (const { key, ids } of groups) {
      for (const id of new Set(ids)) {
        const inc = snap.incidents[id];
        // Тот же критерий, что у snap.counts: короткие решённые строки не входят в счётчик.
        if (!inc || inc.resolved_in.includes(key)) continue;
        const product = inc.product || "Продукт не указан";
        if (!products.has(product)) products.set(product, new Map());
        const cells = products.get(product);
        if (!cells.has(key)) cells.set(key, []);
        cells.get(key).push(inc);
      }
    }

    const names = [...products.keys()].sort((a, b) => a.localeCompare(b, "ru"));
    const chosen = selected && products.get(selected.product)?.get(selected.group);
    const groupTitle = chosen && groups.find((group) => group.key === selected.group)?.title;
    const next = `
      <header><h2>Карта нагрузки</h2><small>Текущий снимок · ${fmtTime(snap.now)} МСК</small></header>
      <div class="load-map-layout">
        <div class="load-map-table">
          <table>
            <thead><tr><th scope="col">Продукт</th>${groups.map((group) => `<th scope="col">${esc(group.title)}</th>`).join("")}</tr></thead>
            <tbody>${names.map((product) => `<tr><th scope="row">${esc(product)}</th>${groups.map(({ key, title }) => {
              const count = products.get(product).get(key)?.length || 0;
              const active = chosen && selected.product === product && selected.group === key;
              return `<td>${count ? `<button type="button" class="load-cell" data-group="${esc(key)}" data-product="${esc(product)}" aria-label="${esc(product)} · ${esc(title)} · ${count}" aria-pressed="${!!active}">${count}</button>` : "0"}</td>`;
            }).join("")}</tr>`).join("")}</tbody>
          </table>
          ${names.length ? "" : "<p>В рабочих группах нет инцидентов.</p>"}
        </div>
        ${chosen ? `<section class="load-list" aria-label="Инциденты выбранной ячейки"><h3>${esc(selected.product)} · ${esc(groupTitle)} (${chosen.length})</h3>
          <ul>${chosen.map((inc) => `<li>
            <button type="button" class="linklike" data-incident="${esc(inc.id)}">${esc(inc.title || inc.id)}</button>
            <small>${esc(inc.id)} · ${esc(fmtWhen(inc.created_at, Date.parse(snap.now)))} · ${esc(inc.state)}${inc.deadline ? ` · SLA до ${esc(fmtWhen(inc.deadline, Date.parse(snap.now)))}` : ""}</small>
          </li>`).join("")}</ul></section>` : ""}
      </div>`;
    if (next === markup) return;
    const focused = el.contains(document.activeElement) && document.activeElement.dataset;
    markup = next;
    el.innerHTML = next;
    if (focused) {
      const button = focused.incident
        ? el.querySelector(`[data-incident="${CSS.escape(focused.incident)}"]`)
        : [...el.querySelectorAll(".load-cell")].find(({ dataset }) => dataset.product === focused.product && dataset.group === focused.group);
      (button || document.getElementById("load-map-toggle")).focus({ preventScroll: true });
    }
    el.onclick = (event) => {
      const button = event.target.closest("button");
      if (button?.dataset.incident) openIncident(button.dataset.incident);
      else if (button?.dataset.product) {
        selected = { product: button.dataset.product, group: button.dataset.group };
        setVisible(true, snap);
      }
    };
  }

  return { setVisible };
}
