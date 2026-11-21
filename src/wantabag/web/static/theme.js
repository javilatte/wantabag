"use strict";
(() => {
  const KEY = "wantabag-theme";
  const root = document.documentElement;
  const apply = (t) => {
    if (t === "light" || t === "dark") root.dataset.theme = t;
    else delete root.dataset.theme;
  };
  try { apply(localStorage.getItem(KEY)); } catch {}

  document.addEventListener("DOMContentLoaded", () => {
    const current = root.dataset.theme || "auto";
    for (const input of document.querySelectorAll('input[name="theme"]')) {
      input.checked = input.value === current;
      input.addEventListener("change", () => {
        apply(input.value);
        try {
          if (input.value === "auto") localStorage.removeItem(KEY);
          else localStorage.setItem(KEY, input.value);
        } catch {}
      });
    }
  });
})();
