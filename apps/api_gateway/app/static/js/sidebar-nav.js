import { el } from "./helpers.js";
import { loadRecommend } from "./model-recommender.js";
import { loadMcpServers } from "./mcp-servers.js";
import { loadUsers } from "./users.js";
import { loadMyDevices } from "./devices.js";
import { loadModelRegistry } from "./model-registry.js";
import { loadProviders } from "./providers.js";
import { fetchAuthStatus } from "./session.js";

function activateSection(section) {
  document.querySelectorAll(".nav-item").forEach((b) => {
    b.classList.toggle("active", b.getAttribute("data-section") === section);
  });
  document.querySelectorAll(".section").forEach((s) => {
    s.classList.toggle("active", s.id === `section-${section}`);
  });
  if (section === "models") loadRecommend();
  if (section === "mcp") loadMcpServers();
  if (section === "users") loadUsers();
  if (section === "devices") loadMyDevices();
  if (section === "model-registry") loadModelRegistry();
  if (section === "providers") loadProviders();
}

export async function initSidebar() {
  const status = await fetchAuthStatus();
  if (status.authenticated && status.role === "admin") {
    document.querySelectorAll(".admin-only").forEach((li) => {
      li.classList.remove("admin-only");
    });
  }

  const validSections = Array.from(document.querySelectorAll(".nav-item")).map((b) =>
    b.getAttribute("data-section")
  );

  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.addEventListener("click", () => {
      const section = btn.getAttribute("data-section");
      activateSection(section);
      const url = new URL(window.location.href);
      url.searchParams.set("tab", section);
      window.history.replaceState(null, "", url);
    });
  });

  const requestedTab = new URLSearchParams(window.location.search).get("tab");
  if (requestedTab && validSections.includes(requestedTab)) {
    activateSection(requestedTab);
  }

  const toggle = el("sidebar-toggle");
  if (toggle) {
    toggle.addEventListener("click", () => {
      el("sidebar").classList.toggle("collapsed");
    });
  }
}
