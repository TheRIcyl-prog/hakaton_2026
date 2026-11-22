(() => {
  const key = "mi-dashboard:theme";
  const root = document.documentElement;
  const defaultTheme = document.currentScript.dataset.defaultTheme || "light";

  function apply(theme) {
    const dark = (theme || defaultTheme) === "dark";
    root.dataset.theme = dark ? "dark" : "light";
    document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
      button.setAttribute("aria-pressed", String(dark));
    });
  }

  try { apply(localStorage.getItem(key)); }
  catch { apply(null); }

  document.addEventListener("DOMContentLoaded", () => {
    apply(root.dataset.theme);
    document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
      button.addEventListener("click", () => {
        const theme = root.dataset.theme === "dark" ? "light" : "dark";
        apply(theme);
        try { localStorage.setItem(key, theme); } catch { /* тема действует до перезагрузки */ }
      });
    });
  });
  window.addEventListener("storage", (event) => {
    if (event.key === key || event.key === null) apply(event.newValue);
  });
})();
