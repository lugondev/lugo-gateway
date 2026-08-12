import { el } from "./helpers.js";
import { loadHome } from "./home.js";
import { loadRecommend } from "./model-recommender.js";
import { loadMcpServers } from "./mcp-servers.js";
import { loadUsers } from "./users.js";
import { loadMyDevices } from "./devices.js";
import { loadModelRegistry } from "./model-registry.js";
import { loadProviders } from "./providers.js";
import { loadUsage } from "./usage.js";
import { loadPricing } from "./pricing.js";
import { loadMyUsage } from "./usage-me.js";
import { loadQuotas } from "./quotas.js";
import { fetchAuthStatus } from "./session.js";

function activateSection(section) {
  document.querySelectorAll(".nav-item").forEach((b) => {
    b.classList.toggle("active", b.getAttribute("data-section") === section);
  });
  document.querySelectorAll(".section").forEach((s) => {
    s.classList.toggle("active", s.id === `section-${section}`);
  });
  if (section === "home") loadHome();
  if (section === "models") loadRecommend();
  if (section === "mcp") loadMcpServers();
  if (section === "users") loadUsers();
  if (section === "devices") loadMyDevices();
  if (section === "model-registry") loadModelRegistry();
  if (section === "providers") loadProviders();
  if (section === "usage") loadUsage();
  if (section === "pricing") loadPricing();
  if (section === "my-usage") loadMyUsage();
  if (section === "quotas") loadQuotas();
}

export async function initSidebar() {
  const status = await fetchAuthStatus();
  if (status.authenticated && status.role === "admin") {
    document.querySelectorAll(".admin-only").forEach((li) => {
      li.classList.remove("admin-only");
    });
  }

  // Scoped to [data-section]: plugin nav items (injected by plugins-nav.js)
  // are also `.nav-item` but have no section to switch to — they open their
  // own page instead, wired up independently by that module.
  const validSections = Array.from(document.querySelectorAll(".nav-item[data-section]")).map((b) =>
    b.getAttribute("data-section")
  );

  document.querySelectorAll(".nav-item[data-section]").forEach((btn) => {
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
