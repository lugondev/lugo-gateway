function activateSystemSubtab(subtab) {
  document.querySelectorAll(".subtab-item").forEach((b) => {
    b.classList.toggle("active", b.getAttribute("data-subtab") === subtab);
  });
  document.querySelectorAll(".subtab-pane").forEach((p) => {
    p.classList.toggle("active", p.id === `subtab-system-${subtab}`);
  });
}

export function initSystemSubtabs() {
  const buttons = document.querySelectorAll(".subtab-item");
  if (!buttons.length) return;

  const validSubtabs = Array.from(buttons).map((b) => b.getAttribute("data-subtab"));

  buttons.forEach((btn) => {
    btn.addEventListener("click", () => {
      const subtab = btn.getAttribute("data-subtab");
      activateSystemSubtab(subtab);
      const url = new URL(window.location.href);
      url.searchParams.set("systab", subtab);
      window.history.replaceState(null, "", url);
    });
  });

  const requestedSubtab = new URLSearchParams(window.location.search).get("systab");
  if (requestedSubtab && validSubtabs.includes(requestedSubtab)) {
    activateSystemSubtab(requestedSubtab);
  }
}
