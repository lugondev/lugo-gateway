# Web Client Profile Selection & Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let web-client users pick which profile a conversation runs under, and create/edit/delete their own profiles, using the existing backend `/v1/profiles` CRUD.

**Architecture:** All work is in the `lugo-web-client` submodule (React + Vite, state-based routing, no router lib). New `api/profiles.ts` + `api/tts.ts` wrap the backend. `Talk.tsx` gains a profile `<select>` that adds `?profile=` to the conversation WebSocket. A new `Profiles` screen (added to `Nav`/`App`) lists shared templates + the user's own profiles and opens a full `ProfileEditor` form. Pure, bug-prone logic (response parsing, param building, form serialization, header parsing) is extracted into testable functions; UI wiring is covered by testing-library component tests.

**Tech Stack:** TypeScript, React 18, Vite, Vitest, @testing-library/react, jsdom.

## Global Constraints

- All source paths below are relative to the `lugo-web-client/` submodule root. All git commits happen **inside** `lugo-web-client` (it is its own repo/submodule).
- Every network call goes through `apiFetch` from `src/api/client.ts` (adds bearer + refresh). Never call `fetch` directly in feature code.
- Backend `GET /v1/profiles` returns `{success, data: {<name>: <masked profile>}}`; a profile with `owner_id === null` is a shared template (Clone only), `owner_id !== null` is the caller's own (Edit/Delete).
- `llm.api_key` comes back masked as `"***"`. The editor loads it as `""` and NEVER sends `"***"`; a blank `api_key` on save means "keep the stored key" (backend behavior).
- `PUT /v1/profiles/{name}` is a full replace — the editor is a full form, so every field is always sent.
- Run tests with `npx vitest run <path>` from `lugo-web-client/`. Run the whole suite with `npm test`.
- Match existing code style: Vietnamese comments are fine and idiomatic here; keep files small and single-purpose.

---

### Task 1: `api/profiles.ts` — types + CRUD + error surfacing

**Files:**
- Create: `src/api/profiles.ts`
- Test: `src/api/profiles.test.ts`

**Interfaces:**
- Consumes: `apiFetch` from `src/api/client.ts`.
- Produces:
  - Types `LlmConfig`, `SttConfig`, `TtsConfig`, `McpServer`, `MemoryConfig`, `SessionConfig`, `Profile`, `ProfileInput` (`= Omit<Profile, 'owner_id'>`), `LlmOption`.
  - `listProfiles(): Promise<Profile[]>`
  - `getProfile(name: string): Promise<Profile>`
  - `createProfile(p: ProfileInput): Promise<Profile>`
  - `updateProfile(name: string, p: ProfileInput): Promise<Profile>`
  - `deleteProfile(name: string): Promise<void>`
  - `cloneProfile(name: string, newName: string): Promise<Profile>`
  - `listLlmOptions(): Promise<LlmOption[]>`

- [ ] **Step 1: Write the failing test**

`src/api/profiles.test.ts`:

```tsx
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  cloneProfile, createProfile, deleteProfile, getProfile,
  listLlmOptions, listProfiles, updateProfile,
} from './profiles'
import { saveTokens } from './tokens'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status, headers: { 'Content-Type': 'application/json' },
  })
}

const SHARED = { name: 'esp32', owner_id: null, nickname: 'ESP32' }
const MINE = { name: 'mine', owner_id: 'u1', nickname: '' }

describe('profiles api', () => {
  beforeEach(() => {
    localStorage.clear()
    saveTokens('acc', 'ref')
    vi.restoreAllMocks()
  })

  it('listProfiles turns the name-keyed dict into a sorted array', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      jsonResponse({ success: true, data: { mine: MINE, esp32: SHARED } })))
    const list = await listProfiles()
    expect(list.map((p) => p.name)).toEqual(['esp32', 'mine']) // sorted by nickname||name
    expect(list.find((p) => p.name === 'esp32')?.owner_id).toBeNull()
  })

  it('listProfiles goes through apiFetch (bearer attached)', async () => {
    const f = vi.fn().mockResolvedValue(jsonResponse({ success: true, data: {} }))
    vi.stubGlobal('fetch', f)
    await listProfiles()
    expect(new Headers(f.mock.calls[0][1].headers).get('Authorization')).toBe('Bearer acc')
  })

  it('createProfile POSTs the payload and returns the created profile', async () => {
    const f = vi.fn().mockResolvedValue(jsonResponse({ success: true, data: MINE }))
    vi.stubGlobal('fetch', f)
    const p = await createProfile({ name: 'mine' } as never)
    expect(f.mock.calls[0][0]).toContain('/v1/profiles')
    expect(f.mock.calls[0][1].method).toBe('POST')
    expect(p.name).toBe('mine')
  })

  it('updateProfile PUTs to the name path', async () => {
    const f = vi.fn().mockResolvedValue(jsonResponse({ success: true, data: MINE }))
    vi.stubGlobal('fetch', f)
    await updateProfile('mine', { name: 'mine' } as never)
    expect(f.mock.calls[0][0]).toContain('/v1/profiles/mine')
    expect(f.mock.calls[0][1].method).toBe('PUT')
  })

  it('deleteProfile DELETEs the name path', async () => {
    const f = vi.fn().mockResolvedValue(jsonResponse({ success: true }))
    vi.stubGlobal('fetch', f)
    await deleteProfile('mine')
    expect(f.mock.calls[0][0]).toContain('/v1/profiles/mine')
    expect(f.mock.calls[0][1].method).toBe('DELETE')
  })

  it('cloneProfile POSTs new_name to the clone path', async () => {
    const f = vi.fn().mockResolvedValue(jsonResponse({ success: true, data: MINE }))
    vi.stubGlobal('fetch', f)
    await cloneProfile('esp32', 'mine')
    expect(f.mock.calls[0][0]).toContain('/v1/profiles/esp32/clone')
    expect(JSON.parse(f.mock.calls[0][1].body)).toEqual({ new_name: 'mine' })
  })

  it('getProfile returns the single profile', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ success: true, data: MINE })))
    expect((await getProfile('mine')).name).toBe('mine')
  })

  it('listLlmOptions returns the options array', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({
      success: true, data: [{ id: 'x', engine: 'openai', model_id: 'gpt', label: 'GPT' }],
    })))
    expect((await listLlmOptions())[0].engine).toBe('openai')
  })

  it('surfaces the server error text; maps 409 to a name-taken message', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      jsonResponse({ detail: "'mine' already exists" }, 409)))
    await expect(createProfile({ name: 'mine' } as never)).rejects.toThrow(/already exists|taken/i)
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd lugo-web-client && npx vitest run src/api/profiles.test.ts`
Expected: FAIL — `Cannot find module './profiles'`.

- [ ] **Step 3: Write minimal implementation**

`src/api/profiles.ts`:

```ts
import { apiFetch } from './client'

export interface LlmConfig { base_url: string; api_key: string; model: string; engine: string }
export interface TtsConfig { profile_name: string }
export interface SttConfig { profile: string; engine: string; language: string; model: string }
export interface McpServer { name: string; url: string; headers: Record<string, string>; enabled: boolean }
export interface MemoryConfig {
  enabled: boolean; mode: string; top_k: number; extractor_model: string; embed_model: string
  compaction_threshold: number; max_facts: number; dedup_threshold: number
}
export interface SessionConfig { idle_timeout_s: number }

export interface Profile {
  name: string
  owner_id: string | null
  nickname: string
  llm: LlmConfig
  system_prompt: string
  voice_optimized: boolean
  stt: SttConfig
  tts: TtsConfig
  mcp_servers: McpServer[]
  memory: MemoryConfig
  session: SessionConfig
}

export type ProfileInput = Omit<Profile, 'owner_id'>
export interface LlmOption { id: string; engine: string; model_id: string; label: string }

async function errorFrom(resp: Response): Promise<Error> {
  let msg = ''
  try {
    const body = await resp.json()
    const raw = body?.error ?? body?.detail
    if (typeof raw === 'string') msg = raw
  } catch {
    // body không phải JSON -- rơi xuống thông báo mặc định
  }
  if (resp.status === 409) return new Error(msg || 'That name is already taken.')
  return new Error(msg || `Server returned error ${resp.status}`)
}

async function jsonData<T>(resp: Response): Promise<T> {
  if (!resp.ok) throw await errorFrom(resp)
  return (await resp.json()).data as T
}

export async function listProfiles(): Promise<Profile[]> {
  const resp = await apiFetch('/v1/profiles')
  const dict = await jsonData<Record<string, Profile>>(resp)
  return Object.values(dict ?? {}).sort(
    (a, b) => (a.nickname || a.name).localeCompare(b.nickname || b.name))
}

export async function getProfile(name: string): Promise<Profile> {
  return jsonData<Profile>(await apiFetch(`/v1/profiles/${encodeURIComponent(name)}`))
}

export async function createProfile(p: ProfileInput): Promise<Profile> {
  return jsonData<Profile>(await apiFetch('/v1/profiles', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(p),
  }))
}

export async function updateProfile(name: string, p: ProfileInput): Promise<Profile> {
  return jsonData<Profile>(await apiFetch(`/v1/profiles/${encodeURIComponent(name)}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(p),
  }))
}

export async function deleteProfile(name: string): Promise<void> {
  const resp = await apiFetch(`/v1/profiles/${encodeURIComponent(name)}`, { method: 'DELETE' })
  if (!resp.ok) throw await errorFrom(resp)
}

export async function cloneProfile(name: string, newName: string): Promise<Profile> {
  return jsonData<Profile>(await apiFetch(`/v1/profiles/${encodeURIComponent(name)}/clone`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ new_name: newName }),
  }))
}

export async function listLlmOptions(): Promise<LlmOption[]> {
  return jsonData<LlmOption[]>(await apiFetch('/v1/profiles/llm-options'))
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd lugo-web-client && npx vitest run src/api/profiles.test.ts`
Expected: PASS (all cases).

- [ ] **Step 5: Commit**

```bash
cd lugo-web-client
git add src/api/profiles.ts src/api/profiles.test.ts
git commit -m "feat(api): profiles CRUD client (list/get/create/update/delete/clone/llm-options)"
```

---

### Task 2: `api/tts.ts` — list TTS profiles for the editor dropdown

**Files:**
- Create: `src/api/tts.ts`
- Test: `src/api/tts.test.ts`

**Interfaces:**
- Consumes: `apiFetch`.
- Produces: `TtsProfileSummary { name: string; nickname?: string }`, `listTtsProfiles(): Promise<TtsProfileSummary[]>`.

- [ ] **Step 1: Write the failing test**

`src/api/tts.test.ts`:

```tsx
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { listTtsProfiles } from './tts'
import { saveTokens } from './tokens'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

describe('tts api', () => {
  beforeEach(() => { localStorage.clear(); saveTokens('acc', 'ref'); vi.restoreAllMocks() })

  it('listTtsProfiles maps the name-keyed dict to a sorted array of names', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({
      success: true, data: { 'co-host': { nickname: 'Co Host' }, 'nu-tre': {} },
    })))
    const list = await listTtsProfiles()
    expect(list.map((t) => t.name)).toEqual(['co-host', 'nu-tre']) // 'Co Host' < 'nu-tre'
  })

  it('hits /v1/tts/profiles through apiFetch', async () => {
    const f = vi.fn().mockResolvedValue(jsonResponse({ success: true, data: {} }))
    vi.stubGlobal('fetch', f)
    await listTtsProfiles()
    expect(f.mock.calls[0][0]).toContain('/v1/tts/profiles')
    expect(new Headers(f.mock.calls[0][1].headers).get('Authorization')).toBe('Bearer acc')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd lugo-web-client && npx vitest run src/api/tts.test.ts`
Expected: FAIL — `Cannot find module './tts'`.

- [ ] **Step 3: Write minimal implementation**

`src/api/tts.ts`:

```ts
import { apiFetch } from './client'

export interface TtsProfileSummary { name: string; nickname?: string }

export async function listTtsProfiles(): Promise<TtsProfileSummary[]> {
  const resp = await apiFetch('/v1/tts/profiles')
  if (!resp.ok) throw new Error(`Server returned error ${resp.status}`)
  const dict = ((await resp.json()).data ?? {}) as Record<string, { nickname?: string }>
  return Object.entries(dict)
    .map(([name, v]) => ({ name, nickname: v?.nickname }))
    .sort((a, b) => (a.nickname || a.name).localeCompare(b.nickname || b.name))
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd lugo-web-client && npx vitest run src/api/tts.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd lugo-web-client
git add src/api/tts.ts src/api/tts.test.ts
git commit -m "feat(api): list TTS profiles for the profile editor dropdown"
```

---

### Task 3: `conversation.ts` — `buildParams(profile?)` + a `profile` on `Conversation`

**Files:**
- Modify: `src/audio/conversation.ts` (replace the `PARAMS` const at lines 12-19; extend the constructor at lines 35-37; use `buildParams` in `connect` at line 62)
- Test: `src/audio/conversation.test.ts` (append cases)

**Interfaces:**
- Consumes: nothing new.
- Produces: `buildParams(profile?: string): URLSearchParams`; `Conversation` constructor becomes `constructor(cb?: ConversationCallbacks, profile?: string)`.

- [ ] **Step 1: Write the failing test** (append to `src/audio/conversation.test.ts`)

```tsx
import { buildParams } from './conversation'

describe('buildParams', () => {
  it('keeps the base audio params', () => {
    const p = buildParams()
    expect(p.get('audio_out')).toBe('opus')
    expect(p.get('output')).toBe('audio,text')
    expect(p.get('sample_rate')).toBe('16000')
    expect(p.get('output_sample_rate')).toBe('24000')
  })

  it('adds profile only when given', () => {
    expect(buildParams('esp32').get('profile')).toBe('esp32')
    expect(buildParams().has('profile')).toBe(false)
    expect(buildParams('').has('profile')).toBe(false) // empty string = no profile
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd lugo-web-client && npx vitest run src/audio/conversation.test.ts`
Expected: FAIL — `buildParams` is not exported.

- [ ] **Step 3: Write minimal implementation**

In `src/audio/conversation.ts`, replace the `PARAMS` const (lines 12-19) with:

```ts
export function buildParams(profile?: string): URLSearchParams {
  const p = new URLSearchParams({
    // Opus qua chính socket đã xác thực: audio_out=url sẽ trỏ vào /artifacts,
    // vốn KHÔNG có auth -- ai có URL cũng nghe được hội thoại.
    audio_out: 'opus',
    output: 'audio,text',
    sample_rate: '16000',
    output_sample_rate: '24000',
  })
  if (profile) p.set('profile', profile)
  return p
}
```

Add a `profile` field and extend the constructor (lines ~33-37):

```ts
  private cb: ConversationCallbacks
  private profile?: string

  constructor(cb: ConversationCallbacks = {}, profile?: string) {
    this.cb = cb
    this.profile = profile
  }
```

In `connect()` (line ~62), replace `` `/v1/conversation/stream?${PARAMS}` `` with:

```ts
    this.ws = new WebSocket(wsUrl(ApiUrl(''), `/v1/conversation/stream?${buildParams(this.profile)}`), [
      'bearer',
      token,
    ])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd lugo-web-client && npx vitest run src/audio/conversation.test.ts`
Expected: PASS (old `wsUrl` cases + new `buildParams` cases).

- [ ] **Step 5: Commit**

```bash
cd lugo-web-client
git add src/audio/conversation.ts src/audio/conversation.test.ts
git commit -m "feat(audio): Conversation sends ?profile= on the WS when a profile is selected"
```

---

### Task 4: Talk profile picker

**Files:**
- Create: `src/screens/talkProfile.ts` (pure helper)
- Test: `src/screens/talkProfile.test.ts`
- Modify: `src/screens/Talk.tsx`
- Test: `src/screens/Talk.test.tsx`

**Interfaces:**
- Consumes: `listProfiles` (Task 1), `Conversation` + constructor `profile` arg (Task 3).
- Produces: `resolveInitialProfile(saved: string | null, names: string[]): string` — the saved name if still present, else the first name, else `''`. `PROFILE_KEY = 'lugo.talkProfile'`.

- [ ] **Step 1: Write the failing test** (`src/screens/talkProfile.test.ts`)

```tsx
import { describe, expect, it } from 'vitest'
import { resolveInitialProfile } from './talkProfile'

describe('resolveInitialProfile', () => {
  it('keeps the saved selection when it still exists', () => {
    expect(resolveInitialProfile('b', ['a', 'b', 'c'])).toBe('b')
  })
  it('falls back to the first profile when nothing is saved', () => {
    expect(resolveInitialProfile(null, ['a', 'b'])).toBe('a')
  })
  it('falls back to the first profile when the saved one is gone', () => {
    expect(resolveInitialProfile('x', ['a', 'b'])).toBe('a')
  })
  it('returns empty string when there are no profiles', () => {
    expect(resolveInitialProfile('x', [])).toBe('')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd lugo-web-client && npx vitest run src/screens/talkProfile.test.ts`
Expected: FAIL — `Cannot find module './talkProfile'`.

- [ ] **Step 3: Write the helper** (`src/screens/talkProfile.ts`)

```ts
export const PROFILE_KEY = 'lugo.talkProfile'

/** Chọn profile ban đầu: giữ lựa chọn đã lưu nếu còn tồn tại, không thì lấy
 * cái đầu danh sách. '' nghĩa là không có profile nào (dùng default server). */
export function resolveInitialProfile(saved: string | null, names: string[]): string {
  if (saved && names.includes(saved)) return saved
  return names[0] ?? ''
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd lugo-web-client && npx vitest run src/screens/talkProfile.test.ts`
Expected: PASS.

- [ ] **Step 5: Wire the picker into `Talk.tsx`**

Add imports at the top of `src/screens/Talk.tsx`:

```tsx
import { listProfiles, type Profile } from '../api/profiles'
import { PROFILE_KEY, resolveInitialProfile } from './talkProfile'
```

Inside `Talk()`, after the existing `useState` calls, add:

```tsx
  const [profiles, setProfiles] = useState<Profile[]>([])
  const [profile, setProfile] = useState<string>('')

  useEffect(() => {
    let alive = true
    listProfiles()
      .then((list) => {
        if (!alive) return
        setProfiles(list)
        setProfile(resolveInitialProfile(localStorage.getItem(PROFILE_KEY), list.map((p) => p.name)))
      })
      .catch(() => { /* danh sách hỏng thì cứ để trống -> Start chạy bằng default server */ })
    return () => { alive = false }
  }, [])

  function chooseProfile(name: string): void {
    setProfile(name)
    localStorage.setItem(PROFILE_KEY, name)
  }
```

Change `start()` to pass the profile into `Conversation` (the `new Conversation({...})` call becomes two args):

```tsx
    convRef.current = conv
    // giữ nguyên callbacks ở trên; chỉ thêm profile đã chọn làm tham số thứ 2
    await conv.connect()
```

Update the `new Conversation({ ... })` line to `new Conversation({ ... }, profile || undefined)`.

Add the dropdown inside the `talk__bar` div (after the `LUGO` span):

```tsx
      <div className="talk__bar">
        <span className="talk__wordmark">LUGO</span>
        {profiles.length > 0 && (
          <label className="talk__profile">
            <span className="sr-only">Assistant</span>
            <select
              aria-label="Assistant"
              value={profile}
              disabled={live}
              onChange={(e) => chooseProfile(e.target.value)}
            >
              {profiles.map((p) => (
                <option key={p.name} value={p.name}>{p.nickname || p.name}</option>
              ))}
            </select>
          </label>
        )}
      </div>
```

- [ ] **Step 6: Write the failing component test** (`src/screens/Talk.test.tsx`)

```tsx
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api/profiles', () => ({
  listProfiles: vi.fn(),
}))
import { listProfiles } from '../api/profiles'
import { Talk } from './Talk'
import { PROFILE_KEY } from './talkProfile'

const LIST = [
  { name: 'esp32', owner_id: null, nickname: 'ESP32' },
  { name: 'rpi', owner_id: null, nickname: 'RPI' },
]

describe('Talk profile picker', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.mocked(listProfiles).mockResolvedValue(LIST as never)
  })

  it('auto-selects the first profile when nothing is saved', async () => {
    render(<Talk />)
    const select = (await screen.findByLabelText('Assistant')) as HTMLSelectElement
    expect(select.value).toBe('esp32')
  })

  it('restores the saved selection', async () => {
    localStorage.setItem(PROFILE_KEY, 'rpi')
    render(<Talk />)
    const select = (await screen.findByLabelText('Assistant')) as HTMLSelectElement
    expect(select.value).toBe('rpi')
  })

  it('persists the selection on change', async () => {
    render(<Talk />)
    const select = (await screen.findByLabelText('Assistant')) as HTMLSelectElement
    fireEvent.change(select, { target: { value: 'rpi' } })
    expect(localStorage.getItem(PROFILE_KEY)).toBe('rpi')
  })

  it('shows no dropdown when the list is empty', async () => {
    vi.mocked(listProfiles).mockResolvedValue([] as never)
    render(<Talk />)
    await waitFor(() => expect(listProfiles).toHaveBeenCalled())
    expect(screen.queryByLabelText('Assistant')).toBeNull()
  })
})
```

- [ ] **Step 7: Run the Talk tests**

Run: `cd lugo-web-client && npx vitest run src/screens/Talk.test.tsx`
Expected: PASS. (If jsdom lacks `requestAnimationFrame`, the raf loop is harmless; the picker assertions do not depend on it.)

- [ ] **Step 8: Add minimal styles** (append to `src/screens/Talk.css`)

```css
.talk__bar { display: flex; align-items: center; gap: 0.75rem; }
.talk__profile select {
  background: transparent; color: inherit; border: 1px solid currentColor;
  border-radius: 6px; padding: 2px 6px; font: inherit;
}
```

- [ ] **Step 9: Commit**

```bash
cd lugo-web-client
git add src/screens/talkProfile.ts src/screens/talkProfile.test.ts \
        src/screens/Talk.tsx src/screens/Talk.test.tsx src/screens/Talk.css
git commit -m "feat(talk): profile picker that sends ?profile= on the conversation WS"
```

---

### Task 5: Profile form helpers (pure)

**Files:**
- Create: `src/screens/profileForm.ts`
- Test: `src/screens/profileForm.test.ts`

**Interfaces:**
- Consumes: types from `api/profiles` (Task 1).
- Produces:
  - `emptyProfileInput(): ProfileInput` — a blank profile with backend-matching defaults.
  - `toEditableInput(p: Profile): ProfileInput` — strips `owner_id` and blanks `llm.api_key` (so a masked `"***"` is never echoed back).
  - `parseHeaders(text: string): Record<string, string>` — parses a JSON object of string values; throws `Error` on anything else.
  - `serializeHeaders(h: Record<string, string>): string` — pretty JSON (`{}` → `"{}"`).

- [ ] **Step 1: Write the failing test** (`src/screens/profileForm.test.ts`)

```tsx
import { describe, expect, it } from 'vitest'
import { emptyProfileInput, parseHeaders, serializeHeaders, toEditableInput } from './profileForm'
import type { Profile } from '../api/profiles'

describe('profileForm helpers', () => {
  it('emptyProfileInput has backend-matching defaults and no owner_id', () => {
    const e = emptyProfileInput()
    expect(e.memory.dedup_threshold).toBe(0.92)
    expect(e.session.idle_timeout_s).toBe(30)
    expect(e.memory.mode).toBe('all')
    expect('owner_id' in e).toBe(false)
  })

  it('toEditableInput blanks a masked api_key and drops owner_id', () => {
    const p = {
      ...emptyProfileInput(), owner_id: 'u1',
      llm: { base_url: '', api_key: '***', model: 'gpt', engine: 'openai' },
    } as unknown as Profile
    const e = toEditableInput(p)
    expect(e.llm.api_key).toBe('')       // never echo the mask back
    expect(e.llm.model).toBe('gpt')      // other llm fields preserved
    expect('owner_id' in e).toBe(false)
  })

  it('parseHeaders accepts a JSON object of strings', () => {
    expect(parseHeaders('{"X-Key":"v"}')).toEqual({ 'X-Key': 'v' })
    expect(parseHeaders('')).toEqual({})       // empty text = no headers
    expect(parseHeaders('  ')).toEqual({})
  })

  it('parseHeaders rejects non-objects and non-string values', () => {
    expect(() => parseHeaders('[1,2]')).toThrow()
    expect(() => parseHeaders('{"n":1}')).toThrow()
    expect(() => parseHeaders('nonsense')).toThrow()
  })

  it('serializeHeaders round-trips', () => {
    expect(parseHeaders(serializeHeaders({ a: 'b' }))).toEqual({ a: 'b' })
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd lugo-web-client && npx vitest run src/screens/profileForm.test.ts`
Expected: FAIL — `Cannot find module './profileForm'`.

- [ ] **Step 3: Write the helpers** (`src/screens/profileForm.ts`)

```ts
import type { Profile, ProfileInput } from '../api/profiles'

export function emptyProfileInput(): ProfileInput {
  return {
    name: '',
    nickname: '',
    llm: { base_url: '', api_key: '', model: '', engine: '' },
    system_prompt: '',
    voice_optimized: false,
    stt: { profile: '', engine: '', language: '', model: '' },
    tts: { profile_name: '' },
    mcp_servers: [],
    memory: {
      enabled: true, mode: 'all', top_k: 5, extractor_model: '', embed_model: '',
      compaction_threshold: 20, max_facts: 200, dedup_threshold: 0.92,
    },
    session: { idle_timeout_s: 30 },
  }
}

export function toEditableInput(p: Profile): ProfileInput {
  return {
    name: p.name,
    nickname: p.nickname,
    // Không bao giờ mang mask "***" quay lại server: để trống = giữ key cũ.
    llm: { ...p.llm, api_key: '' },
    system_prompt: p.system_prompt,
    voice_optimized: p.voice_optimized,
    stt: { ...p.stt },
    tts: { ...p.tts },
    mcp_servers: p.mcp_servers.map((s) => ({ ...s, headers: { ...s.headers } })),
    memory: { ...p.memory },
    session: { ...p.session },
  }
}

export function parseHeaders(text: string): Record<string, string> {
  if (!text.trim()) return {}
  const parsed = JSON.parse(text) // ném SyntaxError nếu JSON hỏng
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new Error('Headers must be a JSON object, e.g. {"X-Key": "value"}')
  }
  for (const v of Object.values(parsed)) {
    if (typeof v !== 'string') throw new Error('Every header value must be a string')
  }
  return parsed as Record<string, string>
}

export function serializeHeaders(h: Record<string, string>): string {
  return JSON.stringify(h ?? {}, null, 2)
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd lugo-web-client && npx vitest run src/screens/profileForm.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd lugo-web-client
git add src/screens/profileForm.ts src/screens/profileForm.test.ts
git commit -m "feat(profiles): pure form helpers (defaults, api_key blanking, header JSON parse)"
```

---

### Task 6: `ProfileEditor` — the full form

**Files:**
- Create: `src/screens/ProfileEditor.tsx`, `src/screens/Profiles.css`
- Test: `src/screens/ProfileEditor.test.tsx`

**Interfaces:**
- Consumes: `createProfile`, `updateProfile`, `listLlmOptions` (Task 1); `listTtsProfiles` (Task 2); `emptyProfileInput`, `toEditableInput`, `parseHeaders`, `serializeHeaders` (Task 5); `Button`, `TextInput`, `TextArea` from `src/ui/`.
- Produces: `ProfileEditor({ mode, initial, onDone, onCancel })` where `mode: 'create' | 'edit'`, `initial: ProfileInput`, `onDone: () => void`, `onCancel: () => void`. On save it calls `createProfile`/`updateProfile` then `onDone`.

- [ ] **Step 1: Write the failing test** (`src/screens/ProfileEditor.test.tsx`)

```tsx
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api/profiles', async (orig) => ({
  ...(await orig<typeof import('../api/profiles')>()),
  createProfile: vi.fn(),
  updateProfile: vi.fn(),
  listLlmOptions: vi.fn(),
}))
vi.mock('../api/tts', () => ({ listTtsProfiles: vi.fn() }))

import { createProfile, updateProfile, listLlmOptions } from '../api/profiles'
import { listTtsProfiles } from '../api/tts'
import { ProfileEditor } from './ProfileEditor'
import { emptyProfileInput, toEditableInput } from './profileForm'

beforeEach(() => {
  vi.mocked(listLlmOptions).mockResolvedValue([])
  vi.mocked(listTtsProfiles).mockResolvedValue([])
  vi.mocked(createProfile).mockResolvedValue({} as never)
  vi.mocked(updateProfile).mockResolvedValue({} as never)
})

it('create mode: New profile calls createProfile with the entered name', async () => {
  const onDone = vi.fn()
  render(<ProfileEditor mode="create" initial={emptyProfileInput()} onDone={onDone} onCancel={() => {}} />)
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'my-bot' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save' }))
  await waitFor(() => expect(createProfile).toHaveBeenCalled())
  expect(vi.mocked(createProfile).mock.calls[0][0].name).toBe('my-bot')
  await waitFor(() => expect(onDone).toHaveBeenCalled())
})

it('edit mode: name is readonly and api_key is never sent as ***', async () => {
  const loaded = toEditableInput({
    ...emptyProfileInput(), owner_id: 'u1',
    llm: { base_url: '', api_key: '***', model: 'gpt', engine: 'openai' },
    name: 'mine',
  } as never)
  render(<ProfileEditor mode="edit" initial={loaded} onDone={() => {}} onCancel={() => {}} />)
  expect((screen.getByLabelText('Name') as HTMLInputElement).readOnly).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: 'Save' }))
  await waitFor(() => expect(updateProfile).toHaveBeenCalled())
  expect(vi.mocked(updateProfile).mock.calls[0][1].llm.api_key).toBe('') // blank, not ***
})

it('adds and removes MCP server rows', async () => {
  render(<ProfileEditor mode="create" initial={emptyProfileInput()} onDone={() => {}} onCancel={() => {}} />)
  fireEvent.click(screen.getByRole('button', { name: 'Add MCP server' }))
  expect(screen.getByLabelText('MCP server 1 name')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Remove MCP server 1' }))
  expect(screen.queryByLabelText('MCP server 1 name')).toBeNull()
})

it('blocks save and shows an error when MCP headers JSON is invalid', async () => {
  render(<ProfileEditor mode="create" initial={emptyProfileInput()} onDone={() => {}} onCancel={() => {}} />)
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'x' } })
  fireEvent.click(screen.getByRole('button', { name: 'Add MCP server' }))
  fireEvent.change(screen.getByLabelText('MCP server 1 headers (JSON)'), { target: { value: 'not json' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save' }))
  expect(await screen.findByRole('alert')).toBeTruthy()
  expect(createProfile).not.toHaveBeenCalled()
})

it('surfaces a server error on save', async () => {
  vi.mocked(createProfile).mockRejectedValue(new Error('That name is already taken.'))
  render(<ProfileEditor mode="create" initial={emptyProfileInput()} onDone={() => {}} onCancel={() => {}} />)
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'dup' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save' }))
  expect(await screen.findByText(/already taken/i)).toBeTruthy()
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd lugo-web-client && npx vitest run src/screens/ProfileEditor.test.tsx`
Expected: FAIL — `Cannot find module './ProfileEditor'`.

- [ ] **Step 3: Write the component** (`src/screens/ProfileEditor.tsx`)

```tsx
import { useEffect, useState } from 'react'
import { createProfile, updateProfile, listLlmOptions, type LlmOption, type McpServer, type ProfileInput } from '../api/profiles'
import { listTtsProfiles, type TtsProfileSummary } from '../api/tts'
import { parseHeaders, serializeHeaders } from './profileForm'
import { Button } from '../ui/Button'
import './Profiles.css'

// Dùng <input>/<textarea> thô với aria-label thay vì ui/TextInput|TextArea:
// hai component đó tự render <label htmlFor=id> và onChange là event thuần
// (props = {label,id} & InputHTMLAttributes), không phải onChange(v). Raw +
// aria-label giữ form gọn và cho test query bằng getByLabelText.

const STT_PRESETS = ['', 'vi', 'en', 'multi', 'en_vi']

// Mỗi dòng MCP giữ headers dưới dạng chuỗi JSON để người dùng gõ tự do; chỉ
// parse khi Save (lỗi parse chặn Save, không làm hỏng state đang gõ).
type McpRow = Omit<McpServer, 'headers'> & { headersText: string }

export function ProfileEditor({
  mode, initial, onDone, onCancel,
}: {
  mode: 'create' | 'edit'
  initial: ProfileInput
  onDone: () => void
  onCancel: () => void
}) {
  const [form, setForm] = useState<ProfileInput>(initial)
  const [mcp, setMcp] = useState<McpRow[]>(
    initial.mcp_servers.map((s) => ({ ...s, headersText: serializeHeaders(s.headers) })))
  const [llmOptions, setLlmOptions] = useState<LlmOption[]>([])
  const [ttsProfiles, setTtsProfiles] = useState<TtsProfileSummary[]>([])
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    listLlmOptions().then(setLlmOptions).catch(() => setLlmOptions([]))
    listTtsProfiles().then(setTtsProfiles).catch(() => setTtsProfiles([]))
  }, [])

  function patch(p: Partial<ProfileInput>): void { setForm((f) => ({ ...f, ...p })) }

  async function save(): Promise<void> {
    setError(null)
    let mcp_servers: McpServer[]
    try {
      mcp_servers = mcp.map((r) => ({
        name: r.name, url: r.url, enabled: r.enabled, headers: parseHeaders(r.headersText),
      }))
    } catch (e) {
      setError(`MCP headers: ${(e as Error).message}`)
      return
    }
    const payload: ProfileInput = { ...form, mcp_servers }
    setSaving(true)
    try {
      if (mode === 'create') await createProfile(payload)
      else await updateProfile(form.name, payload)
      onDone()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="pe">
      {error && <p className="pe__error" role="alert">{error}</p>}

      <fieldset className="pe__group">
        <legend>Basic</legend>
        <label className="pe__field">Name
          <input aria-label="Name" value={form.name} readOnly={mode === 'edit'}
            onChange={(e) => patch({ name: e.target.value })} />
        </label>
        <label className="pe__field">Nickname
          <input aria-label="Nickname" value={form.nickname}
            onChange={(e) => patch({ nickname: e.target.value })} />
        </label>
        <label className="pe__check">
          <input type="checkbox" checked={form.voice_optimized}
            onChange={(e) => patch({ voice_optimized: e.target.checked })} />
          Voice optimized
        </label>
      </fieldset>

      <fieldset className="pe__group">
        <legend>LLM</legend>
        <label className="pe__field">Engine
          <input aria-label="LLM engine" list="llm-engines" value={form.llm.engine}
            onChange={(e) => patch({ llm: { ...form.llm, engine: e.target.value } })} />
        </label>
        <label className="pe__field">Model
          <input aria-label="LLM model" list="llm-models" value={form.llm.model}
            onChange={(e) => patch({ llm: { ...form.llm, model: e.target.value } })} />
        </label>
        <datalist id="llm-engines">
          {[...new Set(llmOptions.map((o) => o.engine))].map((e) => <option key={e} value={e} />)}
        </datalist>
        <datalist id="llm-models">
          {llmOptions.map((o) => <option key={o.id} value={o.model_id}>{o.label}</option>)}
        </datalist>
        <label className="pe__field">Base URL
          <input aria-label="LLM base_url" value={form.llm.base_url}
            onChange={(e) => patch({ llm: { ...form.llm, base_url: e.target.value } })} />
        </label>
        <label className="pe__field">API key
          <input aria-label="API key" type="password" placeholder="leave blank to keep existing"
            value={form.llm.api_key}
            onChange={(e) => patch({ llm: { ...form.llm, api_key: e.target.value } })} />
        </label>
      </fieldset>

      <fieldset className="pe__group">
        <legend>System prompt</legend>
        <textarea aria-label="System prompt" rows={4} value={form.system_prompt}
          onChange={(e) => patch({ system_prompt: e.target.value })} />
      </fieldset>

      <fieldset className="pe__group">
        <legend>STT</legend>
        <label className="pe__field">Preset
          <select aria-label="STT preset" value={form.stt.profile}
            onChange={(e) => patch({ stt: { ...form.stt, profile: e.target.value } })}>
            {STT_PRESETS.map((p) => <option key={p} value={p}>{p || '(server default)'}</option>)}
          </select>
        </label>
        <label className="pe__field">Engine
          <input aria-label="STT engine" value={form.stt.engine}
            onChange={(e) => patch({ stt: { ...form.stt, engine: e.target.value } })} />
        </label>
        <label className="pe__field">Language
          <input aria-label="STT language" value={form.stt.language}
            onChange={(e) => patch({ stt: { ...form.stt, language: e.target.value } })} />
        </label>
        <label className="pe__field">Model
          <input aria-label="STT model" value={form.stt.model}
            onChange={(e) => patch({ stt: { ...form.stt, model: e.target.value } })} />
        </label>
      </fieldset>

      <fieldset className="pe__group">
        <legend>TTS</legend>
        <label className="pe__field">Voice profile
          <select aria-label="TTS profile" value={form.tts.profile_name}
            onChange={(e) => patch({ tts: { profile_name: e.target.value } })}>
            <option value="">(server default)</option>
            {ttsProfiles.map((t) => <option key={t.name} value={t.name}>{t.nickname || t.name}</option>)}
          </select>
        </label>
      </fieldset>

      <fieldset className="pe__group">
        <legend>MCP servers</legend>
        {mcp.map((row, i) => (
          <div className="pe__mcp" key={i}>
            <label className="pe__field">Name
              <input aria-label={`MCP server ${i + 1} name`} value={row.name}
                onChange={(e) => setMcp((m) => m.map((r, j) => j === i ? { ...r, name: e.target.value } : r))} />
            </label>
            <label className="pe__field">URL
              <input aria-label={`MCP server ${i + 1} url`} value={row.url}
                onChange={(e) => setMcp((m) => m.map((r, j) => j === i ? { ...r, url: e.target.value } : r))} />
            </label>
            <label className="pe__check">
              <input type="checkbox" checked={row.enabled}
                onChange={(e) => setMcp((m) => m.map((r, j) => j === i ? { ...r, enabled: e.target.checked } : r))} />
              Enabled
            </label>
            <label className="pe__field">Headers (JSON)
              <textarea aria-label={`MCP server ${i + 1} headers (JSON)`} rows={2} value={row.headersText}
                onChange={(e) => setMcp((m) => m.map((r, j) => j === i ? { ...r, headersText: e.target.value } : r))} />
            </label>
            <Button variant="secondary" onClick={() => setMcp((m) => m.filter((_, j) => j !== i))}>
              {`Remove MCP server ${i + 1}`}
            </Button>
          </div>
        ))}
        <Button variant="secondary"
          onClick={() => setMcp((m) => [...m, { name: '', url: '', enabled: true, headersText: '{}' }])}>
          Add MCP server
        </Button>
      </fieldset>

      <fieldset className="pe__group">
        <legend>Memory</legend>
        <label className="pe__check">
          <input type="checkbox" checked={form.memory.enabled}
            onChange={(e) => patch({ memory: { ...form.memory, enabled: e.target.checked } })} />
          Enabled
        </label>
        <label className="pe__field">Mode
          <select aria-label="Memory mode" value={form.memory.mode}
            onChange={(e) => patch({ memory: { ...form.memory, mode: e.target.value } })}>
            <option value="all">all</option>
            <option value="semantic">semantic</option>
          </select>
        </label>
        <label className="pe__field">Top K
          <input aria-label="Memory top_k" type="number" value={form.memory.top_k}
            onChange={(e) => patch({ memory: { ...form.memory, top_k: Number(e.target.value) } })} />
        </label>
        <label className="pe__field">Extractor model
          <input aria-label="Memory extractor_model" value={form.memory.extractor_model}
            onChange={(e) => patch({ memory: { ...form.memory, extractor_model: e.target.value } })} />
        </label>
        <label className="pe__field">Embed model
          <input aria-label="Memory embed_model" value={form.memory.embed_model}
            onChange={(e) => patch({ memory: { ...form.memory, embed_model: e.target.value } })} />
        </label>
        <label className="pe__field">Compaction threshold
          <input aria-label="Memory compaction_threshold" type="number" value={form.memory.compaction_threshold}
            onChange={(e) => patch({ memory: { ...form.memory, compaction_threshold: Number(e.target.value) } })} />
        </label>
        <label className="pe__field">Max facts
          <input aria-label="Memory max_facts" type="number" value={form.memory.max_facts}
            onChange={(e) => patch({ memory: { ...form.memory, max_facts: Number(e.target.value) } })} />
        </label>
        <label className="pe__field">Dedup threshold
          <input aria-label="Memory dedup_threshold" type="number" step="0.01" value={form.memory.dedup_threshold}
            onChange={(e) => patch({ memory: { ...form.memory, dedup_threshold: Number(e.target.value) } })} />
        </label>
      </fieldset>

      <fieldset className="pe__group">
        <legend>Session</legend>
        <label className="pe__field">Idle timeout (s)
          <input aria-label="Session idle_timeout_s" type="number" value={form.session.idle_timeout_s}
            onChange={(e) => patch({ session: { idle_timeout_s: Number(e.target.value) } })} />
        </label>
      </fieldset>

      <div className="pe__actions">
        <Button variant="secondary" onClick={onCancel}>Cancel</Button>
        <Button variant="primary" disabled={saving} onClick={save}>Save</Button>
      </div>
    </div>
  )
}
```

Create `src/screens/Profiles.css` (shared by editor + list):

```css
.pe { display: flex; flex-direction: column; gap: 1rem; padding: 1rem; max-width: 640px; margin: 0 auto; }
.pe__group { display: flex; flex-direction: column; gap: 0.5rem; border: 1px solid currentColor; border-radius: 8px; padding: 0.75rem; }
.pe__field { display: flex; flex-direction: column; gap: 0.25rem; font-size: 0.85rem; }
.pe__check { display: flex; align-items: center; gap: 0.5rem; }
.pe__mcp { display: flex; flex-direction: column; gap: 0.4rem; border-top: 1px dashed currentColor; padding-top: 0.5rem; }
.pe__actions { display: flex; justify-content: flex-end; gap: 0.75rem; }
.pe__error { color: #b00020; }
.profiles { padding: 1rem; max-width: 640px; margin: 0 auto; display: flex; flex-direction: column; gap: 1rem; }
.profiles__row { display: flex; align-items: center; gap: 0.5rem; justify-content: space-between; }
.profiles__badge { font-size: 0.7rem; opacity: 0.7; }
```

Note: the editor deliberately uses raw `<input>`/`<textarea>` with `aria-label`
(the ui `TextInput`/`TextArea` render their own `<label htmlFor>` and take an
event-based `onChange`, which doesn't fit this dense form). `Button` variants
`primary`/`secondary` are used (confirmed to exist via `Button.test.tsx`).

- [ ] **Step 4: Run the test**

```bash
cd lugo-web-client
npx vitest run src/screens/ProfileEditor.test.tsx
```
Expected: PASS (all five cases).

- [ ] **Step 5: Commit**

```bash
cd lugo-web-client
git add src/screens/ProfileEditor.tsx src/screens/ProfileEditor.test.tsx src/screens/Profiles.css
git commit -m "feat(profiles): full profile editor form (create/update, all fieldsets)"
```

---

### Task 7: `Profiles` list screen + Nav/App wiring

**Files:**
- Create: `src/screens/Profiles.tsx`
- Test: `src/screens/Profiles.test.tsx`
- Modify: `src/components/Nav.tsx` (add `'profiles'` to `Screen` and `ITEMS`)
- Modify: `src/App.tsx` (add `profiles: Profiles` to `SCREENS`)

**Interfaces:**
- Consumes: `listProfiles`, `getProfile`, `deleteProfile`, `cloneProfile`, `type Profile` (Task 1); `emptyProfileInput`, `toEditableInput` (Task 5); `ProfileEditor` (Task 6); `ConfirmModal`, `Modal`, `TextInput`, `Button` from `src/ui/`.
- Produces: `Profiles` screen component (default-exported member used by `App`).

- [ ] **Step 1: Extend `Nav.tsx`**

In `src/components/Nav.tsx` change the `Screen` type and `ITEMS`:

```tsx
export type Screen = 'talk' | 'history' | 'devices' | 'tools' | 'profiles'
```

```tsx
const ITEMS: { id: Screen; label: string }[] = [
  { id: 'talk', label: 'Talk' },
  { id: 'history', label: 'History' },
  { id: 'profiles', label: 'Profiles' },
  { id: 'devices', label: 'Devices' },
  { id: 'tools', label: 'Tools' },
]
```

- [ ] **Step 2: Write the failing test** (`src/screens/Profiles.test.tsx`)

```tsx
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api/profiles', async (orig) => ({
  ...(await orig<typeof import('../api/profiles')>()),
  listProfiles: vi.fn(),
  getProfile: vi.fn(),
  deleteProfile: vi.fn(),
  cloneProfile: vi.fn(),
}))
// Editor is exercised in its own test; stub it here to keep this test on the list.
vi.mock('./ProfileEditor', () => ({ ProfileEditor: () => <div>editor</div> }))

import { listProfiles, deleteProfile } from '../api/profiles'
import { Profiles } from './Profiles'

const SHARED = { name: 'esp32', owner_id: null, nickname: 'ESP32', mcp_servers: [] }
const MINE = { name: 'mine', owner_id: 'u1', nickname: 'Mine', mcp_servers: [] }

beforeEach(() => {
  vi.mocked(listProfiles).mockResolvedValue([SHARED, MINE] as never)
  vi.mocked(deleteProfile).mockResolvedValue(undefined as never)
})

it('groups shared templates (Clone only) and my profiles (Edit/Delete)', async () => {
  render(<Profiles />)
  await screen.findByText('ESP32')
  // shared row: a Clone action, no Delete
  const shared = screen.getByText('ESP32').closest('.profiles__row') as HTMLElement
  expect(shared.querySelector('[data-act="clone"]')).toBeTruthy()
  expect(shared.querySelector('[data-act="delete"]')).toBeNull()
  // mine row: Edit + Delete
  const mine = screen.getByText('Mine').closest('.profiles__row') as HTMLElement
  expect(mine.querySelector('[data-act="edit"]')).toBeTruthy()
  expect(mine.querySelector('[data-act="delete"]')).toBeTruthy()
})

it('deletes my profile after confirming', async () => {
  render(<Profiles />)
  const mine = (await screen.findByText('Mine')).closest('.profiles__row') as HTMLElement
  fireEvent.click(mine.querySelector('[data-act="delete"]') as HTMLElement)
  // ConfirmModal's confirm label is unique ("Yes, delete") so it does not
  // collide with the row's own "Delete" button.
  fireEvent.click(await screen.findByRole('button', { name: 'Yes, delete' }))
  await waitFor(() => expect(deleteProfile).toHaveBeenCalledWith('mine'))
})
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd lugo-web-client && npx vitest run src/screens/Profiles.test.tsx`
Expected: FAIL — `Cannot find module './Profiles'`.

- [ ] **Step 4: Write `Profiles.tsx`**

```tsx
import { useEffect, useState } from 'react'
import {
  cloneProfile, deleteProfile, getProfile, listProfiles,
  type Profile, type ProfileInput,
} from '../api/profiles'
import { emptyProfileInput, toEditableInput } from './profileForm'
import { ProfileEditor } from './ProfileEditor'
import { Button } from '../ui/Button'
import { ConfirmModal } from '../ui/ConfirmModal'
import { Modal } from '../ui/Modal'
import './Profiles.css'

type Editing = { mode: 'create' | 'edit'; initial: ProfileInput } | null

export function Profiles() {
  const [profiles, setProfiles] = useState<Profile[]>([])
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState<Editing>(null)
  const [toDelete, setToDelete] = useState<string | null>(null)
  const [cloneOf, setCloneOf] = useState<string | null>(null)
  const [cloneName, setCloneName] = useState('')

  function refresh(): void {
    listProfiles().then(setProfiles).catch((e) => setError((e as Error).message))
  }
  useEffect(refresh, [])

  async function openEdit(name: string): Promise<void> {
    try { setEditing({ mode: 'edit', initial: toEditableInput(await getProfile(name)) }) }
    catch (e) { setError((e as Error).message) }
  }
  async function doDelete(): Promise<void> {
    if (!toDelete) return
    try { await deleteProfile(toDelete); refresh() }
    catch (e) { setError((e as Error).message) }
    finally { setToDelete(null) }
  }
  async function doClone(): Promise<void> {
    if (!cloneOf) return
    try { await cloneProfile(cloneOf, cloneName); refresh() }
    catch (e) { setError((e as Error).message); return }
    finally { setCloneOf(null); setCloneName('') }
  }

  if (editing) {
    return (
      <ProfileEditor mode={editing.mode} initial={editing.initial}
        onDone={() => { setEditing(null); refresh() }}
        onCancel={() => setEditing(null)} />
    )
  }

  const shared = profiles.filter((p) => p.owner_id === null)
  const mine = profiles.filter((p) => p.owner_id !== null)

  return (
    <main className="profiles">
      {error && <p role="alert" className="pe__error">{error}</p>}
      <div className="profiles__row">
        <h2>Profiles</h2>
        <Button variant="primary"
          onClick={() => setEditing({ mode: 'create', initial: emptyProfileInput() })}>New</Button>
      </div>

      <section>
        <h3>Mine</h3>
        {mine.map((p) => (
          <div className="profiles__row" key={p.name}>
            <span>{p.nickname || p.name}</span>
            <span>
              <button data-act="edit" className="btn btn--secondary" onClick={() => openEdit(p.name)}>Edit</button>
              <button data-act="clone" className="btn btn--secondary"
                onClick={() => { setCloneOf(p.name); setCloneName(`${p.name}-copy`) }}>Clone</button>
              <button data-act="delete" className="btn btn--danger" onClick={() => setToDelete(p.name)}>Delete</button>
            </span>
          </div>
        ))}
      </section>

      <section>
        <h3>Shared templates</h3>
        {shared.map((p) => (
          <div className="profiles__row" key={p.name}>
            <span>{p.nickname || p.name} <span className="profiles__badge">shared</span></span>
            <button data-act="clone" className="btn btn--secondary"
              onClick={() => { setCloneOf(p.name); setCloneName(`${p.name}-copy`) }}>Clone</button>
          </div>
        ))}
      </section>

      <ConfirmModal
        open={toDelete !== null}
        title={`Delete "${toDelete ?? ''}"?`}
        message="This permanently removes the profile."
        confirmLabel="Yes, delete"
        destructive
        onConfirm={doDelete}
        onCancel={() => setToDelete(null)}
      />

      <Modal open={cloneOf !== null} title={`Clone "${cloneOf ?? ''}"`} onClose={() => setCloneOf(null)}>
        <label className="pe__field">New name
          <input aria-label="Clone new name" value={cloneName}
            onChange={(e) => setCloneName(e.target.value)} />
        </label>
        <div className="pe__actions">
          <Button variant="secondary" onClick={() => setCloneOf(null)}>Cancel</Button>
          <Button variant="primary" onClick={doClone}>Clone</Button>
        </div>
      </Modal>
    </main>
  )
}
```

- [ ] **Step 5: (prop shapes already confirmed — no action)**

The call sites above match the real ui APIs:
`ConfirmModal({ open, title, message, confirmLabel, onConfirm, onCancel, destructive?, busy? })`
and `Modal({ open, onClose, title, children })`. Both render nothing when
`open` is false, so the confirm/clone controls only exist while their state is
set — which is why the delete test waits for the confirm button to appear.

- [ ] **Step 6: Wire `App.tsx`**

In `src/App.tsx` add the import and the `SCREENS` entry:

```tsx
import { Profiles } from './screens/Profiles'
```

```tsx
const SCREENS: Record<Screen, ComponentType> = {
  talk: Talk,
  history: History,
  profiles: Profiles,
  devices: Devices,
  tools: Tools,
}
```

- [ ] **Step 7: Run the Profiles test + full suite**

```bash
cd lugo-web-client
npx vitest run src/screens/Profiles.test.tsx
npm test
```
Expected: Profiles test PASS; whole suite green.

- [ ] **Step 8: Typecheck / build**

Run: `cd lugo-web-client && npx tsc -b`
Expected: no type errors. Fix any surfaced by the new files.

- [ ] **Step 9: Commit**

```bash
cd lugo-web-client
git add src/screens/Profiles.tsx src/screens/Profiles.test.tsx src/components/Nav.tsx src/App.tsx
git commit -m "feat(profiles): Profiles screen (list/clone/delete/new) + Nav/App wiring"
```

---

## Manual / e2e verification (after Task 7)

Not a unit test — needs the running gateway (restarted so it picks up the DB config) and a browser. The existing e2e harness lives in `scripts/e2e/`.

1. Start gateway + web client; log in.
2. Talk tab: confirm the **Assistant** dropdown lists profiles, defaults to the first, remembers the choice across reloads, and a conversation connects (STT/TTS follow the chosen profile — verify a Vietnamese profile transcribes via qwen3_asr).
3. Profiles tab: **New** → fill the form → Save → appears under "Mine". **Edit** → change nickname → Save persists. **Clone** a shared template → appears under "Mine". **Delete** → confirm → disappears. Trigger a duplicate name → the server error shows.
4. Optional: extend `scripts/e2e/talk.mjs` with a profile-selection assertion.

---

## Self-Review

**Spec coverage:**
- Talk picker (`?profile=`, localStorage, auto-first, disabled while live, empty-list fallback) → Tasks 3, 4. ✓
- `api/profiles.ts` full CRUD + error/409 → Task 1. ✓
- `api/tts.ts` → Task 2. ✓
- Profiles screen shared/mine + Clone/Delete(ConfirmModal)/New → Task 7. ✓
- Full editor (Basic/LLM/System/STT/TTS/MCP/Memory/Session), api_key blanking, name readonly, MCP headers JSON, full-replace round-trip → Tasks 5, 6. ✓
- Nav/App wiring → Task 7. ✓
- No backend changes → honored. ✓

**Placeholder scan:** none — every step has real code/commands.

**Type consistency:** `ProfileInput`, `Profile`, `McpServer`, `LlmOption`, `TtsProfileSummary` are defined in Tasks 1-2 and consumed unchanged. `Conversation(cb, profile)` two-arg form defined in Task 3, used in Task 4. The editor/list use raw `<input aria-label>`/`<textarea aria-label>` (not ui `TextInput`/`TextArea`, whose `onChange` is event-based and which render their own label). `ConfirmModal`/`Modal` call sites match the real props (`open`/`message`/`destructive` for ConfirmModal; `open` for Modal) — read from source, not assumed. ConfirmModal's confirm label is `"Yes, delete"`, distinct from the row's `"Delete"` button, so the delete test's role query is unambiguous.

**ui components read from source (no open assumptions):** `TextInput`/`TextArea` (`{label,id} & InputHTMLAttributes`), `Modal` (`{open,onClose,title,children}`), `ConfirmModal` (`{open,title,message,confirmLabel,onConfirm,onCancel,destructive?,busy?}`), `Button` (variants `primary`/`secondary`/`danger`/`ghost`).
