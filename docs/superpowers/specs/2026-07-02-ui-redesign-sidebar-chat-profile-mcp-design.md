# UI Redesign: Sidebar Layout + Unified Chat + Profile Editor + MCP Tab

**Date:** 2026-07-02  
**Status:** Approved

---

## Overview

Full frontend redesign of `apps/api_gateway/app/static/` (vanilla HTML/CSS/JS, no framework).

Goals:
1. Replace horizontal tab bar with a fixed left sidebar + header + footer layout.
2. Merge the separate "Conversation" and "LLM Chat" tabs into a single **Chat** section with a mode selector.
3. Add an inline profile editor (2-column card, inspired by xiaozhi Role Configuration) inside the Chat section.
4. Add a new **MCP** section for managing global MCP servers.

Backend APIs already exist — this is a purely frontend change.

---

## 1. Global Layout

```
┌─────────────────────────────────────────────────────────────┐
│ HEADER  [≡] Speech Text Transformer     [●STT][●TTS][●LLM] │
├──────────────┬──────────────────────────────────────────────┤
│  SIDEBAR     │  CONTENT (overflow-y: auto)                 │
│  220px fixed │                                              │
│              │  <active section rendered here>             │
│  ● Chat      │                                              │
│    STT        │                                              │
│    TTS        │                                              │
│    Models     │                                              │
│    MCP        │                                              │
│    System     │                                              │
├──────────────┴──────────────────────────────────────────────┤
│ FOOTER  vX.Y  ● LLM  ● STT  ● TTS  (live dots)            │
└─────────────────────────────────────────────────────────────┘
```

### Header
- Left: hamburger icon (collapses sidebar on narrow screens) + app name "Speech Text Transformer"
- Right: three compact status badges `● STT`, `● TTS`, `● LLM` — green = ready, red = error/unavailable
- Populated on load from `GET /v1/status`

### Sidebar
- Fixed 220px, full viewport height minus header/footer
- Nav items: Chat, STT, TTS, Models, MCP, System
- Active item highlighted; click switches content pane
- Collapsed to icon-only strip on narrow viewports (≤768px) or when hamburger toggled

### Footer
- Version string (from `GET /v1/status` or hardcoded)
- Three live status dots (same data as header badges)
- Minimal height (~32px)

---

## 2. Chat Section (merged Conversation + LLM Chat)

### 2a. Mode Selector
Segment button row at top of section:

```
[Text→Text] [Voice→Voice] [Voice→Text] [Text→Voice]
```

| Mode | Input | Output | Uses |
|------|-------|--------|------|
| Text→Text | textarea | text dialogue | `/v1/conversation/chat` |
| Voice→Voice | mic | audio playback | existing Conversation flow (VAD + STT + LLM + TTS) |
| Voice→Text | mic | text transcript | STT only (stream) |
| Text→Voice | textarea | audio playback | TTS synthesis |

### 2b. Profile Bar
Directly below mode selector:

```
[Profile: (none) ▾]  [Edit]  [+ New Profile]
```

- Dropdown lists all profiles from `GET /v1/profiles`; option "(none)" uses server defaults
- **Edit**: expands the edit panel pre-filled with the selected profile
- **+ New Profile**: expands the edit panel in create mode (empty fields)
- When a profile is selected, all chat API calls append `?profile=<name>`

### 2c. Profile Edit Panel (inline, expandable)
Appears between profile bar and dialogue area. Hidden by default, shown on Edit/New.

```
┌── Profile Configuration ──────────────────────────────────────┐
│  LEFT COLUMN                │  RIGHT COLUMN                   │
│  ─────────────────          │  ─────────────────              │
│  Name                       │  LLM Base URL                   │
│  [_____________________]    │  [_________________________]    │
│                             │  LLM Model                      │
│  System Prompt              │  [_________________________]    │
│  [                     ]    │  LLM API Key                    │
│  [                     ]    │  [•••••••••••••••••••••••••]    │
│  [                     ]    │                                 │
│  [                     ]    │  TTS Engine   [select ▾]        │
│                             │  TTS Voice    [select ▾]        │
│  MCP Servers (per-profile)  │                                 │
│  ☑ server-a  localhost:8080 │  [Save Configuration]           │
│  ☐ server-b  localhost:9090 │  [Cancel]                       │
│  (from global MCP list)     │  [Delete Profile]  ← edit only │
└───────────────────────────────────────────────────────────────┘
```

**Fields:**
- **Name** — profile identifier (slug, no spaces); read-only when editing existing
- **System Prompt** — textarea, ~5 rows
- **LLM Base URL** — text input (e.g. `https://api.openai.com/v1`)
- **LLM Model** — text input (e.g. `gpt-4o-mini`)
- **LLM API Key** — password input; sent to server, never stored in browser
- **TTS Engine** — select populated from `GET /v1/status` TTS engines
- **TTS Voice** — select shown only when engine = vieneu; populated dynamically
- **MCP Servers** — checklist of global MCP servers (from `GET /v1/mcp/servers`); checked ones are included in `mcp_servers` array on save

**Actions:**
- **Save Configuration** → `PUT /v1/profiles/{name}` (edit) or `POST /v1/profiles` (new)
- **Cancel** → collapse panel, no change
- **Delete Profile** → `DELETE /v1/profiles/{name}`, collapse panel, clear selection

### 2d. Dialogue Area
- Chat history displayed as bubbles (user = right, assistant = left)
- Shows audio waveform placeholder for Voice→Voice mode replies
- Reset button clears history

### 2e. Input Area (mode-dependent)
**Text→Text:**
```
[textarea: "Type a message…"]  [Send]
```
Sends to `POST /v1/conversation/chat?profile=<name>` (streaming SSE if available, else JSON).

**Voice→Voice / Voice→Text:**
```
[● Start Mic]  [■ Stop]  [status: idle]
```
Uses existing WebSocket STT stream + conversation flow.

**Text→Voice:**
```
[textarea]  [Synthesize & Play]
```
Calls TTS endpoint and plays result inline.

---

## 3. MCP Section (new)

Global MCP server registry — not profile-specific. Profiles reference servers from this list.

```
Global MCP Servers

┌──────────────────────────────────────────────────────────────┐
│ Name       URL                         Status    Actions     │
│ ─────────────────────────────────────────────────────────    │
│ my-server  http://localhost:8080/mcp   ✓ 3 tools  [Test][✕] │
│ remote-ai  https://mcp.example.com    ✗ unreachable [Test][✕]│
└──────────────────────────────────────────────────────────────┘

Add Server
Name: [____________]  URL: [____________________________]  [Add]
```

**API calls:**
- List: `GET /v1/mcp/servers`
- Add: `POST /v1/mcp/servers`
- Delete: `DELETE /v1/mcp/servers/{name}` (also invalidates pool connection)
- Test: `GET /v1/mcp/servers/{name}/tools` → shows tool count or error inline

---

## 4. Retained Sections (unchanged content, new wrapper)

| Section | Content |
|---------|---------|
| **STT** | Batch (upload/record) + Streaming WebSocket — unchanged |
| **TTS** | Batch + SSE Stream — unchanged |
| **Models** | STT/TTS/LLM model download & activate — remove the "Conversation LLM (Ollama)" section that's now in Chat |
| **System** | Status grid + STT preprocessing — unchanged |

---

## 5. Styling

- Keep existing `Chakra Petch` + `IBM Plex Mono` fonts and dark theme (`styles.css`)
- Sidebar background: slightly darker than content area (`--bg-sidebar`)
- Profile edit panel: card with `border: 1px solid var(--border)`, `border-radius: 10px`, `padding: 20px`
- Status dots: `●` character, color toggled via CSS class (`status-ok` = green, `status-err` = red, `status-warn` = orange)
- Mode segment buttons: reuse existing `.seg` / `.seg-btn` component
- Responsive: sidebar collapses to icon strip at `≤768px`; edit panel stacks to single column

---

## 6. Files Changed

| File | Change |
|------|--------|
| `static/index.html` | Full rewrite — sidebar shell + all 6 sections |
| `static/app.js` | Refactor — add profile CRUD, MCP CRUD, mode switching, sidebar nav |
| `static/styles.css` | Extend — sidebar, header, footer, edit panel, status dots |

No backend changes required.

---

## 7. Out of Scope

- Profile template chips (xiaozhi "Template" feature) — not in current Profile model
- Vision Model / Memory Model / Small LLM fields — not in current Profile model
- Intent Recognition separate from MCP — MCP servers cover this
- Mobile-first layout — responsive sidebar collapse is sufficient

---

## 8. Testing

- [ ] Sidebar nav switches sections correctly
- [ ] Header status badges update on load
- [ ] Profile dropdown lists all profiles; "(none)" uses defaults
- [ ] Edit panel opens/closes; Save calls correct API (PUT vs POST)
- [ ] MCP server add/delete/test works
- [ ] Chat Text→Text mode sends message and displays reply
- [ ] Voice→Voice mode starts/stops mic, plays back audio
- [ ] Mode switching hides/shows correct input controls
- [ ] Responsive: sidebar collapses at ≤768px
