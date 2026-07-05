import { el } from "./helpers.js";
import { loadRecommend } from "./model-recommender.js";
import { loadMcpServers } from "./mcp-servers.js";

export function initSidebar() {
  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.addEventListener("click", () => {
      const section = btn.getAttribute("data-section");
      document.querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("active", b === btn));
      document.querySelectorAll(".section").forEach((s) => {
        s.classList.toggle("active", s.id === `section-${section}`);
      });
      if (section === "models") loadRecommend();
      if (section === "mcp") loadMcpServers();
    });
  });

  const toggle = el("sidebar-toggle");
  if (toggle) {
    toggle.addEventListener("click", () => {
      el("sidebar").classList.toggle("collapsed");
    });
  }
}

