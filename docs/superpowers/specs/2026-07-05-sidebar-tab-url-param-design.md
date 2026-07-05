# Design: keep the active sidebar tab in the URL

## Context

`apps/api_gateway/app/static/js/sidebar-nav.js`'s `initSidebar()` switches the active
`.section`/`.nav-item` on click but never touches the URL, so reloading `/ui` or sharing a link
always lands on the default tab (Chat).

## Scope

- Add a `?tab=<section>` query param (values match `data-section`: `chat`, `stt`, `tts`,
  `models`, `mcp`, `system`).
- On init, read `tab` from `location.search`; if it names an existing nav item, activate that
  tab instead of the HTML default. Otherwise keep today's default (whatever `.nav-item` already
  has `active` in the HTML — currently Chat).
- On each tab click, update the URL's `tab` param via `history.replaceState` (no new browser
  history entry — Back still leaves `/ui` in one step).
- No change to the existing per-tab side effects (`loadRecommend()` on Models,
  `loadMcpServers()` on MCP) — they fire the same way whether the tab was reached by click or by
  URL param on load.

## Not in scope

- No `pushState`/back-button-cycles-through-tabs behavior.
- No change to any other query param or to the sidebar collapse/expand toggle.

## Implementation sketch

One file, `apps/api_gateway/app/static/js/sidebar-nav.js`: factor the "activate section X" logic
(currently inline in the click handler) into a small `activateSection(section)` helper used by
both the click handler and the init-time URL read; click handler additionally calls
`history.replaceState` with the updated `tab` param.

## Testing

No JS test harness in this project (established in the 2026-07-05 module-split work) — verified
manually in a browser: load `/ui?tab=stt` and confirm the STT tab is active on load; click
through tabs and confirm the URL's `tab` param updates without adding history entries; confirm
Back after several tab clicks leaves `/ui` in one step, not one step per prior tab.
