# Lugo Web Client — Khung SPA + Auth (Phase 1a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dựng repo con `lugo-web-client` (React SPA + Vite) gắn vào repo cha làm submodule đúng chuẩn, với lớp API client giữ token + tự refresh, và màn hình đăng nhập chạy thật против API gateway.

**Architecture:** Repo git độc lập, build ra static, không cần Node runtime ở prod. Một lớp API client duy nhất biết về token — mọi màn hình gọi qua nó, không màn nào tự `fetch`. Đăng nhập xong vào thẳng Talk (Talk là plan sau; plan này chỉ để một placeholder có bảo vệ route).

**Tech Stack:** React 19, Vite, TypeScript, Vitest (test lớp API client), pnpm.

**Spec:** `docs/superpowers/specs/2026-07-16-lugo-web-client-design.md`

**Phụ thuộc đã có (nhánh này fork từ `65be211`, đã chứa phase 0):**
- `POST /api/auth/token` `{username, password}` → `{success, data: {access_token, refresh_token, expires_in}}`
- `POST /api/auth/refresh` `{refresh_token}` → `{success, data: {access_token, expires_in}}`
- Mọi endpoint `/v1/*` nhận `Authorization: Bearer <access_token>`
- Bearer hỏng/hết hạn → **401 JSON dứt khoát**, không fallback. 401/403 có mang header CORS.

## Global Constraints

- **Auth chỉ dùng một phương thức, không fallback.** Client không bao giờ gửi cookie. Không đặt `credentials: 'include'` ở bất kỳ đâu — backend đã tắt `allow_credentials`, nên gửi cookie chỉ làm request bị trình duyệt chặn.
- **Chỉ lớp API client biết về token.** Không component/màn hình nào đọc token trực tiếp hay tự gọi `fetch`. Đây là ranh giới module chính của spec.
- **Không có role trong UI.** Không render bất cứ thứ gì liên quan quản trị. Không có màn hình admin. Backend enforce quyền, client không cần biết.
- Access token TTL 3600s (backend trả `expires_in`; dùng giá trị đó, **không** hardcode 3600 ở client).
- Palette Lugo, dùng đúng các mã này: nền tối `#111111` / `#2A2A2A`, nền sáng `#F7F4EE` / `#E8E1D6`, accent gradient `#FF8A00` → `#FFC857`. Cam **chỉ** cho trạng thái hoạt động và hành động chính.
- Base URL của API phải cấu hình được qua env (`VITE_API_BASE_URL`), **không** hardcode — client chạy domain khác API.
- Chạy lệnh client từ trong `lugo-web-client/`: `pnpm test`, `pnpm build`.
- **Không** `git push`. **Không** tạo repo GitHub. **Không** chạy `gh`. Repo con là local-only ở plan này.
- Repo con là repo git RIÊNG. Commit code client vào repo con; commit `.gitmodules` + gitlink vào repo cha. Đây là hai commit khác nhau ở hai repo khác nhau.

## File Structure

| File | Trách nhiệm |
|---|---|
| `lugo-web-client/` | Repo git riêng (submodule) |
| `lugo-web-client/src/api/tokens.ts` | Lưu/đọc/xoá token. Chỉ nơi này chạm storage. |
| `lugo-web-client/src/api/client.ts` | `apiFetch` — gắn bearer, tự refresh khi 401, gọi lại. Chỉ nơi này chạm token khi gửi request. |
| `lugo-web-client/src/api/auth.ts` | `login()`, `logout()` — gọi `/api/auth/token`. |
| `lugo-web-client/src/theme.css` | Biến CSS cho palette Lugo (light + dark). |
| `lugo-web-client/src/routes/Login.tsx` | Màn hình đăng nhập. |
| `lugo-web-client/src/routes/Talk.tsx` | Placeholder có bảo vệ route (nội dung thật ở plan sau). |
| `lugo-web-client/src/App.tsx` | Routing + guard. |
| `.gitmodules` (repo cha) | Đăng ký submodule — thứ mà `esp32-assistant` đang thiếu. |

---

### Task 1: Dựng repo con + khung Vite

**Files:**
- Create: `lugo-web-client/` (repo git riêng, scaffold Vite)
- Create: `lugo-web-client/.env.example`

**Interfaces:**
- Produces: một repo Vite+React+TS chạy được, có Vitest, `VITE_API_BASE_URL` đọc được từ env.

- [ ] **Step 1: Scaffold**

Từ thư mục gốc repo cha (`/Users/lugon/code/speech-text-transformer`):

```bash
pnpm create vite lugo-web-client --template react-ts
cd lugo-web-client
pnpm install
pnpm add -D vitest @testing-library/react @testing-library/jest-dom jsdom
```

- [ ] **Step 2: Cấu hình Vitest**

Tạo `lugo-web-client/vitest.config.ts`:

```ts
import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    environment: 'jsdom',
    globals: true,
  },
})
```

Thêm vào `lugo-web-client/package.json` phần `scripts`:

```json
    "test": "vitest run",
```

- [ ] **Step 3: env mẫu**

Tạo `lugo-web-client/.env.example`:

```
# API gateway chạy ở domain khác. Không có giá trị mặc định trong code --
# client phải biết nó gọi đi đâu.
VITE_API_BASE_URL=http://localhost:8000
```

- [ ] **Step 4: Kiểm tra khung chạy**

```bash
cd lugo-web-client && pnpm build
```
Expected: build thành công, có thư mục `dist/`.

```bash
cd lugo-web-client && pnpm test
```
Expected: Vitest chạy, báo "No test files found" (chưa có test — đúng ở bước này).

- [ ] **Step 5: Khởi tạo repo git riêng và commit**

```bash
cd lugo-web-client
git init -b main
git add -A
git commit -m "chore: scaffold Vite + React + TS + Vitest"
```

**Chưa** commit gì ở repo cha — việc đăng ký submodule là Task 5, sau khi repo con có nội dung thật.

---

### Task 2: Lớp lưu token

**Files:**
- Create: `lugo-web-client/src/api/tokens.ts`
- Test: `lugo-web-client/src/api/tokens.test.ts`

**Interfaces:**
- Produces:
  - `saveTokens(access: string, refresh: string): void`
  - `getAccessToken(): string | null`
  - `getRefreshToken(): string | null`
  - `clearTokens(): void`

- [ ] **Step 1: Viết test thất bại**

Tạo `lugo-web-client/src/api/tokens.test.ts`:

```ts
import { beforeEach, describe, expect, it } from 'vitest'
import { clearTokens, getAccessToken, getRefreshToken, saveTokens } from './tokens'

describe('tokens', () => {
  beforeEach(() => localStorage.clear())

  it('lưu rồi đọc lại được', () => {
    saveTokens('acc', 'ref')
    expect(getAccessToken()).toBe('acc')
    expect(getRefreshToken()).toBe('ref')
  })

  it('trả null khi chưa có gì', () => {
    expect(getAccessToken()).toBeNull()
    expect(getRefreshToken()).toBeNull()
  })

  it('clearTokens xoá cả hai', () => {
    saveTokens('acc', 'ref')
    clearTokens()
    expect(getAccessToken()).toBeNull()
    expect(getRefreshToken()).toBeNull()
  })
})
```

- [ ] **Step 2: Chạy test để xác nhận nó fail**

Run: `cd lugo-web-client && pnpm test`
Expected: FAIL — không tìm thấy module `./tokens`

- [ ] **Step 3: Implement**

Tạo `lugo-web-client/src/api/tokens.ts`:

```ts
// Nơi DUY NHẤT chạm vào storage của token. Mọi thứ khác đi qua client.ts.
//
// Token nằm trong localStorage nên XSS đọc được -- đây là cái giá đã chấp nhận
// khi chọn bearer thay vì BFF (xem spec). Access token TTL 1h giới hạn thiệt hại.
const ACCESS_KEY = 'lugo.access_token'
const REFRESH_KEY = 'lugo.refresh_token'

export function saveTokens(access: string, refresh: string): void {
  localStorage.setItem(ACCESS_KEY, access)
  localStorage.setItem(REFRESH_KEY, refresh)
}

export function getAccessToken(): string | null {
  return localStorage.getItem(ACCESS_KEY)
}

export function getRefreshToken(): string | null {
  return localStorage.getItem(REFRESH_KEY)
}

export function clearTokens(): void {
  localStorage.removeItem(ACCESS_KEY)
  localStorage.removeItem(REFRESH_KEY)
}
```

- [ ] **Step 4: Chạy test để xác nhận pass**

Run: `cd lugo-web-client && pnpm test`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit (trong repo con)**

```bash
cd lugo-web-client
git add src/api/tokens.ts src/api/tokens.test.ts
git commit -m "feat(api): lớp lưu token"
```

---

### Task 3: apiFetch — gắn bearer và tự refresh

Đây là lớp khó nhất của plan. Backend trả **401 JSON dứt khoát** khi token hỏng/hết hạn (không fallback), nên 401 là tín hiệu rõ ràng để refresh.

**Files:**
- Create: `lugo-web-client/src/api/client.ts`
- Test: `lugo-web-client/src/api/client.test.ts`

**Interfaces:**
- Consumes: `getAccessToken`, `getRefreshToken`, `saveTokens`, `clearTokens` (Task 2)
- Produces:
  - `apiFetch(path: string, init?: RequestInit): Promise<Response>`
  - `ApiUrl(path: string): string`
  - `onAuthLost(cb: () => void): void` — đăng ký callback khi refresh thất bại

- [ ] **Step 1: Viết test thất bại**

Tạo `lugo-web-client/src/api/client.test.ts`:

```ts
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { apiFetch, onAuthLost } from './client'
import { getAccessToken, saveTokens } from './tokens'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('apiFetch', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.restoreAllMocks()
  })

  it('gắn Authorization: Bearer từ token đã lưu', async () => {
    saveTokens('acc-1', 'ref-1')
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)

    await apiFetch('/v1/sessions')

    const headers = new Headers(fetchMock.mock.calls[0][1].headers)
    expect(headers.get('Authorization')).toBe('Bearer acc-1')
  })

  it('KHÔNG bao giờ gửi cookie', async () => {
    saveTokens('acc-1', 'ref-1')
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)

    await apiFetch('/v1/sessions')

    // backend tắt allow_credentials; gửi cookie chỉ khiến browser chặn response
    expect(fetchMock.mock.calls[0][1].credentials).not.toBe('include')
  })

  it('gặp 401 thì refresh rồi gọi lại request', async () => {
    saveTokens('expired', 'ref-1')
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ success: false, error: 'login required' }, 401))
      .mockResolvedValueOnce(
        jsonResponse({ success: true, data: { access_token: 'acc-2', expires_in: 3600 } }),
      )
      .mockResolvedValueOnce(jsonResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)

    const resp = await apiFetch('/v1/sessions')

    expect(resp.status).toBe(200)
    expect(fetchMock).toHaveBeenCalledTimes(3)
    expect(fetchMock.mock.calls[1][0]).toContain('/api/auth/refresh')
    // request gọi lại phải mang token MỚI, không phải token cũ
    const retryHeaders = new Headers(fetchMock.mock.calls[2][1].headers)
    expect(retryHeaders.get('Authorization')).toBe('Bearer acc-2')
    expect(getAccessToken()).toBe('acc-2')
  })

  it('refresh thất bại thì xoá token và báo auth lost', async () => {
    saveTokens('expired', 'bad-ref')
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ success: false }, 401))
      .mockResolvedValueOnce(jsonResponse({ success: false, error: 'invalid refresh token' }, 401))
    vi.stubGlobal('fetch', fetchMock)

    const lost = vi.fn()
    onAuthLost(lost)

    const resp = await apiFetch('/v1/sessions')

    expect(resp.status).toBe(401)
    expect(getAccessToken()).toBeNull()
    expect(lost).toHaveBeenCalledOnce()
  })

  it('không refresh vòng lặp: 401 sau khi đã refresh thì bỏ cuộc', async () => {
    saveTokens('expired', 'ref-1')
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ success: false }, 401))
      .mockResolvedValueOnce(
        jsonResponse({ success: true, data: { access_token: 'acc-2', expires_in: 3600 } }),
      )
      .mockResolvedValueOnce(jsonResponse({ success: false }, 401))
    vi.stubGlobal('fetch', fetchMock)

    const resp = await apiFetch('/v1/sessions')

    expect(resp.status).toBe(401)
    // 3 lần: request gốc, refresh, retry. KHÔNG được refresh lần nữa.
    expect(fetchMock).toHaveBeenCalledTimes(3)
  })

  it('nhiều request cùng gặp 401 chỉ refresh MỘT lần', async () => {
    saveTokens('expired', 'ref-1')
    let refreshCalls = 0
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (String(url).includes('/api/auth/refresh')) {
        refreshCalls += 1
        return jsonResponse({ success: true, data: { access_token: 'acc-2', expires_in: 3600 } })
      }
      const auth = new Headers(init?.headers).get('Authorization')
      return auth === 'Bearer acc-2' ? jsonResponse({ ok: true }) : jsonResponse({}, 401)
    })
    vi.stubGlobal('fetch', fetchMock)

    const results = await Promise.all([apiFetch('/v1/a'), apiFetch('/v1/b'), apiFetch('/v1/c')])

    expect(refreshCalls).toBe(1)
    expect(results.every((r) => r.status === 200)).toBe(true)
  })
})
```

**Tại sao test cuối đáng giá:** ba request song song hết hạn cùng lúc. Nếu mỗi request tự refresh, ta có ba lần gọi `/api/auth/refresh` cùng lúc — hôm nay chỉ là lãng phí, nhưng khoảnh khắc backend bật refresh-token rotation thì hai lần sau dùng token đã bị xoay và cả hai hỏng. `sharedRefresh()` tồn tại chính vì điều này; test này là thứ duy nhất ghim nó.

- [ ] **Step 2: Chạy test để xác nhận nó fail**

Run: `cd lugo-web-client && pnpm test`
Expected: FAIL — không tìm thấy module `./client`

- [ ] **Step 3: Implement**

Tạo `lugo-web-client/src/api/client.ts`:

```ts
import { clearTokens, getAccessToken, getRefreshToken, saveTokens } from './tokens'

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? ''

export function ApiUrl(path: string): string {
  return `${BASE_URL}${path}`
}

let authLostCb: (() => void) | null = null

export function onAuthLost(cb: () => void): void {
  authLostCb = cb
}

// Một refresh đang bay thì mọi request 401 khác cùng chờ nó, thay vì mỗi
// request tự refresh -- ba request song song hết hạn cùng lúc không được biến
// thành ba lần refresh.
let refreshInFlight: Promise<string | null> | null = null

async function refreshAccessToken(): Promise<string | null> {
  const refresh = getRefreshToken()
  if (!refresh) return null

  const resp = await fetch(ApiUrl('/api/auth/refresh'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ refresh_token: refresh }),
  })
  if (!resp.ok) return null

  const body = await resp.json()
  const access = body?.data?.access_token
  if (!access) return null

  saveTokens(access, refresh)
  return access
}

function sharedRefresh(): Promise<string | null> {
  if (!refreshInFlight) {
    refreshInFlight = refreshAccessToken().finally(() => {
      refreshInFlight = null
    })
  }
  return refreshInFlight
}

function withAuth(init: RequestInit, token: string | null): RequestInit {
  const headers = new Headers(init.headers)
  if (token) headers.set('Authorization', `Bearer ${token}`)
  // Không set credentials: backend tắt allow_credentials, và client này không
  // dùng cookie -- auth chỉ một phương thức, không fallback.
  return { ...init, headers }
}

export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const resp = await fetch(ApiUrl(path), withAuth(init, getAccessToken()))
  if (resp.status !== 401) return resp

  // 401 ở đây luôn nghĩa là token không dùng được: backend không fallback sang
  // danh tính khác, nên đây là tín hiệu refresh rõ ràng.
  const access = await sharedRefresh()
  if (!access) {
    clearTokens()
    authLostCb?.()
    return resp
  }

  // Gọi lại đúng MỘT lần. 401 tiếp nghĩa là hết cách -- không lặp vô hạn.
  const retry = await fetch(ApiUrl(path), withAuth(init, access))
  if (retry.status === 401) {
    clearTokens()
    authLostCb?.()
  }
  return retry
}
```

- [ ] **Step 4: Chạy test để xác nhận pass**

Run: `cd lugo-web-client && pnpm test`
Expected: PASS toàn bộ file `client.test.ts`

- [ ] **Step 5: Commit (trong repo con)**

```bash
cd lugo-web-client
git add src/api/client.ts src/api/client.test.ts
git commit -m "feat(api): apiFetch gắn bearer, tự refresh, không lặp vô hạn"
```

---

### Task 4: Đăng nhập + guard + palette

**Files:**
- Create: `lugo-web-client/src/api/auth.ts`
- Create: `lugo-web-client/src/theme.css`
- Create: `lugo-web-client/src/routes/Login.tsx`
- Create: `lugo-web-client/src/routes/Talk.tsx`
- Modify: `lugo-web-client/src/App.tsx`
- Test: `lugo-web-client/src/api/auth.test.ts`

**Interfaces:**
- Consumes: `apiFetch`, `ApiUrl`, `onAuthLost` (Task 3); `saveTokens`, `clearTokens`, `getAccessToken` (Task 2)
- Produces: `login(username, password): Promise<void>` (ném `Error` khi sai), `logout(): void`, `isAuthed(): boolean`

- [ ] **Step 1: Viết test thất bại**

Tạo `lugo-web-client/src/api/auth.test.ts`:

```ts
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { isAuthed, login, logout } from './auth'
import { getAccessToken, getRefreshToken } from './tokens'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('auth', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.restoreAllMocks()
  })

  it('login lưu cả hai token', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        jsonResponse({
          success: true,
          data: { access_token: 'acc', refresh_token: 'ref', expires_in: 3600 },
        }),
      ),
    )

    await login('toan', 'pw12345678')

    expect(getAccessToken()).toBe('acc')
    expect(getRefreshToken()).toBe('ref')
    expect(isAuthed()).toBe(true)
  })

  it('login gọi /api/auth/token, KHÔNG gọi /api/auth/login', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ success: true, data: { access_token: 'a', refresh_token: 'r', expires_in: 3600 } }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await login('toan', 'pw12345678')

    // /api/auth/login là lối cookie của admin webui -- client này không dùng
    expect(fetchMock.mock.calls[0][0]).toContain('/api/auth/token')
  })

  it('sai mật khẩu thì ném lỗi và không lưu token', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(jsonResponse({ success: false, error: 'invalid username or password' }, 401)),
    )

    await expect(login('toan', 'sai')).rejects.toThrow()
    expect(getAccessToken()).toBeNull()
  })

  it('logout xoá token', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        jsonResponse({ success: true, data: { access_token: 'a', refresh_token: 'r', expires_in: 3600 } }),
      ),
    )
    await login('toan', 'pw12345678')
    logout()
    expect(isAuthed()).toBe(false)
  })
})
```

- [ ] **Step 2: Chạy test để xác nhận nó fail**

Run: `cd lugo-web-client && pnpm test`
Expected: FAIL — không tìm thấy module `./auth`

- [ ] **Step 3: Implement auth.ts**

Tạo `lugo-web-client/src/api/auth.ts`:

```ts
import { ApiUrl } from './client'
import { clearTokens, getAccessToken, saveTokens } from './tokens'

export async function login(username: string, password: string): Promise<void> {
  // /api/auth/token, KHÔNG phải /api/auth/login -- cái sau là lối cookie của
  // admin webui và cố ý tách biệt khỏi client này.
  const resp = await fetch(ApiUrl('/api/auth/token'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  if (!resp.ok) {
    throw new Error('Sai tên đăng nhập hoặc mật khẩu')
  }
  const body = await resp.json()
  const { access_token, refresh_token } = body.data
  saveTokens(access_token, refresh_token)
}

export function logout(): void {
  clearTokens()
}

export function isAuthed(): boolean {
  return getAccessToken() !== null
}
```

- [ ] **Step 4: Chạy test để xác nhận pass**

Run: `cd lugo-web-client && pnpm test`
Expected: PASS toàn bộ (tokens + client + auth)

- [ ] **Step 5: Palette**

Tạo `lugo-web-client/src/theme.css`:

```css
/* Bộ nhận diện Lugo. Cam CHỈ dùng cho trạng thái hoạt động và hành động
   chính -- cam dùng cho mọi thứ thì không còn báo hiệu gì. */
:root {
  --lugo-ink: #111111;
  --lugo-ink-soft: #2a2a2a;
  --lugo-cream: #f7f4ee;
  --lugo-cream-deep: #e8e1d6;
  --lugo-accent: #ff8a00;
  --lugo-accent-warm: #ffc857;
  --lugo-accent-gradient: linear-gradient(135deg, #ff8a00, #ffc857);

  --lugo-bg: var(--lugo-cream);
  --lugo-fg: var(--lugo-ink);
}

/* Talk là màn dùng lâu, thường buổi tối, và logo trắng trên nền tối là bản
   mạnh nhất của bộ nhận diện -> nền tối là mặc định ở đó. */
[data-surface='talk'] {
  --lugo-bg: var(--lugo-ink);
  --lugo-fg: var(--lugo-cream);
}

body {
  margin: 0;
  background: var(--lugo-bg);
  color: var(--lugo-fg);
  font-family: system-ui, -apple-system, sans-serif;
}
```

- [ ] **Step 6: Login.tsx**

Tạo `lugo-web-client/src/routes/Login.tsx`:

```tsx
import { useState, type FormEvent } from 'react'
import { login } from '../api/auth'

export function Login({ onDone }: { onDone: () => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await login(username, password)
      onDone()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Đăng nhập thất bại')
    } finally {
      setBusy(false)
    }
  }

  return (
    <main style={{ display: 'grid', placeItems: 'center', minHeight: '100vh', padding: 24 }}>
      <form onSubmit={submit} style={{ display: 'grid', gap: 12, width: 'min(320px, 100%)' }}>
        <h1 style={{ margin: 0, fontSize: 28, letterSpacing: '0.2em' }}>LUGO</h1>
        <input
          aria-label="Tên đăng nhập"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          placeholder="Tên đăng nhập"
          autoComplete="username"
        />
        <input
          aria-label="Mật khẩu"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="Mật khẩu"
          autoComplete="current-password"
        />
        {error && <p role="alert" style={{ color: 'var(--lugo-accent)', margin: 0 }}>{error}</p>}
        <button type="submit" disabled={busy} style={{ background: 'var(--lugo-accent-gradient)', border: 0, padding: 12, borderRadius: 8, color: '#111', fontWeight: 600 }}>
          {busy ? 'Đang vào...' : 'Vào'}
        </button>
      </form>
    </main>
  )
}
```

- [ ] **Step 7: Talk.tsx (placeholder)**

Tạo `lugo-web-client/src/routes/Talk.tsx`:

```tsx
// Placeholder. Nội dung thật (WS realtime, VAD, barge-in, vòng tròn logo làm
// chỉ báo trạng thái) thuộc plan sau -- plan này chỉ dựng khung + auth.
export function Talk({ onLogout }: { onLogout: () => void }) {
  return (
    <main data-surface="talk" style={{ minHeight: '100vh', display: 'grid', placeItems: 'center' }}>
      <div style={{ display: 'grid', gap: 16, justifyItems: 'center' }}>
        <p style={{ opacity: 0.6 }}>Talk — sắp có</p>
        <button onClick={onLogout} style={{ background: 'none', border: '1px solid currentColor', color: 'inherit', padding: '8px 16px', borderRadius: 8 }}>
          Đăng xuất
        </button>
      </div>
    </main>
  )
}
```

- [ ] **Step 8: App.tsx — guard**

Thay toàn bộ `lugo-web-client/src/App.tsx`:

```tsx
import { useEffect, useState } from 'react'
import './theme.css'
import { isAuthed, logout } from './api/auth'
import { onAuthLost } from './api/client'
import { Login } from './routes/Login'
import { Talk } from './routes/Talk'

export default function App() {
  const [authed, setAuthed] = useState(isAuthed())

  // Refresh thất bại ở bất kỳ request nào -> quay về Login. Đây là lý do
  // client.ts có onAuthLost thay vì tự điều hướng: lớp API không biết gì về UI.
  useEffect(() => {
    onAuthLost(() => setAuthed(false))
  }, [])

  if (!authed) return <Login onDone={() => setAuthed(true)} />
  return (
    <Talk
      onLogout={() => {
        logout()
        setAuthed(false)
      }}
    />
  )
}
```

- [ ] **Step 9: Build + test**

```bash
cd lugo-web-client && pnpm test && pnpm build
```
Expected: test PASS toàn bộ, build thành công.

- [ ] **Step 10: Commit (trong repo con)**

```bash
cd lugo-web-client
git add -A
git commit -m "feat: đăng nhập bằng bearer token + guard + palette Lugo"
```

---

### Task 5: Đăng ký submodule đúng chuẩn ở repo cha

Đây là chỗ `esp32-assistant` làm sai: nó được commit ở mode `160000` (gitlink) nhưng repo **không có `.gitmodules`**, nên ai clone mới sẽ nhận thư mục rỗng và không có URL nào để biết phải lấy gì. Task này làm đúng.

**Files:**
- Create: `.gitmodules` (repo cha)
- Modify: `.gitignore` (repo cha) — nếu cần

**Interfaces:**
- Produces: `.gitmodules` đăng ký `lugo-web-client` + gitlink tương ứng.

- [ ] **Step 1: Kiểm tra repo cha có đang ignore thư mục con không**

```bash
cd /Users/lugon/code/speech-text-transformer
git check-ignore -v lugo-web-client || echo "không bị ignore -- tốt"
```

Nếu bị ignore, gỡ dòng tương ứng trong `.gitignore` (submodule phải track được).

- [ ] **Step 2: Tạo .gitmodules**

Repo GitHub chưa tồn tại (chủ dự án sẽ tạo sau). Dùng đường dẫn tương đối làm placeholder có chủ đích, và ghi rõ để không ai tưởng là quên:

Tạo `/Users/lugon/code/speech-text-transformer/.gitmodules`:

```
[submodule "lugo-web-client"]
	path = lugo-web-client
	url = ./lugo-web-client
```

- [ ] **Step 3: Thêm gitlink**

```bash
cd /Users/lugon/code/speech-text-transformer
git add .gitmodules
git add lugo-web-client
git -c core.fileMode=false status --short
```
Expected: thấy `A  .gitmodules` và `A  lugo-web-client` (mode 160000).

Xác nhận mode gitlink:
```bash
git ls-files -s lugo-web-client
```
Expected: dòng bắt đầu bằng `160000`.

- [ ] **Step 4: Xác nhận khác hẳn esp32-assistant**

```bash
git config -f .gitmodules --list
```
Expected: in ra `submodule.lugo-web-client.path` và `submodule.lugo-web-client.url` — tức submodule ĐƯỢC đăng ký, khác với `esp32-assistant` hiện không có mục nào.

- [ ] **Step 5: Commit (repo cha)**

```bash
cd /Users/lugon/code/speech-text-transformer
git commit -m "feat(web-client): đăng ký lugo-web-client làm submodule

url dùng đường dẫn tương đối vì repo GitHub chưa được tạo -- đây là
placeholder có chủ đích, đổi thành URL thật khi remote tồn tại. Khác với
esp32-assistant (gitlink không có .gitmodules), submodule này được đăng ký
đàng hoàng nên clone mới biết phải lấy gì."
```

---

## Xác minh cuối (không phải test tự động)

- [ ] **Chạy client thật với gateway thật**

```bash
# Terminal 1 -- gateway
.venv/bin/uvicorn app.main:app --app-dir apps/api_gateway --port 8000

# Terminal 2 -- client
cd lugo-web-client
cp .env.example .env
pnpm dev
```

Mở URL Vite in ra, rồi kiểm tra:
- Đăng nhập bằng một user có sẵn → vào được màn Talk placeholder.
- Sai mật khẩu → hiện lỗi, không vào.
- Mở DevTools → Network: request `/api/auth/token` **không** có `Cookie` header, response **không** có `Set-Cookie`.
- Bấm Đăng xuất → quay về Login; localStorage sạch token.
- **Kiểm tra CORS thật:** client ở `localhost:5173` gọi API ở `localhost:8000` là cross-origin thật sự. Nếu request bị chặn, đó là tín hiệu CORS chưa đúng — báo lại, đừng lách bằng proxy của Vite (proxy sẽ che mất đúng thứ ta cần kiểm chứng).

- [ ] **Kiểm tra admin webui không bị ảnh hưởng:** mở `http://localhost:8000/static/index.html`, đăng nhập, xem vài tab.

## Ngoài phạm vi plan này

- Talk thật (WS realtime, VAD, barge-in, chỉ báo trạng thái bằng vòng tròn logo)
- History, Devices, Tools
- Tạo repo GitHub và đổi url submodule thành URL thật
- Deploy client
- Test tự động cho UI (spec đã chốt: giai đoạn đầu chỉ test lớp API + audio)
