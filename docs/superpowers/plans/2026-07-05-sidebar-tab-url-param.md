# Sidebar Tab URL Param Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reflect the active sidebar tab in the URL's `?tab=` query param so reloading or sharing a link preserves which section is open.

**Architecture:** `sidebar-nav.js`'s click handler and init logic both funnel through one `activateSection(section)` helper; the click handler additionally calls `history.replaceState` to update `?tab=` without adding a browser-history entry; init reads `?tab=` from `location.search` to pick the starting section.

**Tech Stack:** Vanilla JS (`URLSearchParams`, `history.replaceState`), no new dependencies.

## Global Constraints

- Valid `tab` values are exactly the six existing `data-section` values: `chat`, `stt`, `tts`, `models`, `mcp`, `system`.
- Use `history.replaceState`, not `pushState` — no new browser-history entries per tab click.
- No JS test harness exists in this project — verify manually in a browser (established pattern from the 2026-07-05 module-split work).

---

### Task 1: URL-synced sidebar tab

**Files:**
- Modify: `apps/api_gateway/app/static/js/sidebar-nav.js` (currently 22 lines, shown in full below)

**Interfaces:**
- Produces: `initSidebar()` (same exported name/signature as today — no other file imports anything else from this module).

Current file content, for reference:

```js
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
```

- [ ] **Step 1: Replace the file with the URL-synced version**

Write the full new content to `apps/api_gateway/app/static/js/sidebar-nav.js`:

```js
import { el } from "./helpers.js";
import { loadRecommend } from "./model-recommender.js";
import { loadMcpServers } from "./mcp-servers.js";

function activateSection(section) {
  document.querySelectorAll(".nav-item").forEach((b) => {
    b.classList.toggle("active", b.getAttribute("data-section") === section);
  });
  document.querySelectorAll(".section").forEach((s) => {
    s.classList.toggle("active", s.id === `section-${section}`);
  });
  if (section === "models") loadRecommend();
  if (section === "mcp") loadMcpServers();
}

export function initSidebar() {
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
```

- [ ] **Step 2: Syntax-check the file**

Run: `node --check apps/api_gateway/app/static/js/sidebar-nav.js`
Expected: no output (exit code 0)

- [ ] **Step 3: Start the dev server**

Run: `make start`
Expected: `started gateway (pid ...) http://0.0.0.0:8000/ui`

- [ ] **Step 4: Manually verify in a browser**

1. Open `http://localhost:8000/ui?tab=stt` — the STT tab must be active on load (not the default Chat tab), and the URL must still read `?tab=stt`.
2. Click through each of the other five tabs (Chat, TTS, Models, MCP, System) one at a time. After each click, confirm the address bar's `tab` param updates to match (e.g. clicking "Models" → URL becomes `...?tab=models`) and the Models/MCP tabs still trigger their data load (Models list / MCP server list visibly populate).
3. After clicking through several tabs, press the browser's Back button once. Expected: it navigates away from `/ui` entirely (e.g. back to whatever page was open before), NOT to a previously-active tab — confirming `replaceState` didn't add history entries.
4. Open `http://localhost:8000/ui?tab=bogus` (an invalid section name) — expected: falls back to the default active tab (Chat), no JS console errors.
5. Open `http://localhost:8000/ui` (no `tab` param at all) — expected: default Chat tab active, no console errors, and clicking a tab still updates the URL as in step 2.

- [ ] **Step 5: Stop the dev server**

Run: `make stop`

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/static/js/sidebar-nav.js
git commit -m "feat: reflect active sidebar tab in the URL's ?tab= param"
```
