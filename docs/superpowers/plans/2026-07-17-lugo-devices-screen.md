# Lugo Web Client — Màn Devices (Phase 1d) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Người dùng tự ghép thiết bị Lugo của mình, xem danh sách, đặt tên, và gỡ khi không dùng nữa. Đóng vòng lặp "mua thiết bị → ghép → nói chuyện".

**Architecture:** `src/api/devices.ts` là lớp gọi API (qua `apiFetch`, không tự `fetch`). `src/lib/time.ts` là hàm thuần tính thời gian tương đối. `Devices.tsx` chỉ là UI. Nav xuất hiện lần đầu vì giờ đã có hai màn thật.

**Spec:** `docs/superpowers/specs/2026-07-16-lugo-web-client-design.md`

## Nền tảng đã có (đừng dựng lại)

Repo con `lugo-web-client` @ `98a1b9b`, 42/42 test:
- `src/api/client.ts` — `apiFetch(path, init?)` gắn bearer, tự refresh khi 401, dedupe refresh, báo `onAuthLost`. **Mọi lời gọi API phải đi qua đây.**
- `src/api/auth.ts` — `login`, `logout`, `isAuthed`.
- `src/routes/Talk.tsx` — màn Talk đã chạy.
- `src/theme.css` — token màu; `[data-surface='talk']` đảo sang nền tối.
- `src/components/LugoMark.tsx` — chữ ký, chỉ dùng ở Talk.

## API thật của backend (đã đọc code + xác minh bằng curl)

Vừa sửa ở commit `517e52b`: ba route này trước đó đọc thẳng `request.session` nên **luôn trả 401 cho bearer**. Giờ đã dùng `current_user_id()`. Xác minh bằng curl với token thật: `/v1/devices/mine` → 200, `/v1/devices` (admin) → 403, không token → 401.

| Endpoint | Body | Trả về |
|---|---|---|
| `GET /v1/devices/mine` | — | `{success, data: Device[]}` |
| `POST /v1/devices/mine/{id}/revoke` | — | `{success: true}`, hoặc 404 nếu không phải của bạn |
| `POST /v1/devices/pair/claim` | `{code, name}` | `{success, data: Device}` |

`Device` = `{id, user_id, name, serial, created_at, last_seen_at, revoked}` (thời gian là chuỗi ISO hoặc `null`).

**Luồng ghép (quan trọng để viết copy cho đúng):**
1. Thiết bị ESP32 tự gọi `/v1/devices/pair/init` với serial của nó → nhận **mã 6 chữ số**, hiện lên màn hình thiết bị.
2. Người dùng gõ mã đó vào web + đặt tên → SPA gọi `pair/claim`.
3. Thiết bị poll `/v1/devices/pair/status` → nhận token của nó.

Mã là **6 chữ số** (`f"{secrets.randbelow(1_000_000):06d}"`), **TTL 10 phút**.

Lỗi có thể gặp từ `pair/claim` (đều là 4xx có `error`):
- Mã sai/hết hạn → `pairing code is invalid or expired`
- Phần cứng đã ghép rồi → `a device with this hardware is already paired; revoke it first`

## Nguyên tắc thiết kế

**Không vẽ chấm xanh "Online".** `last_seen_at` chỉ được cập nhật khi thiết bị mở WebSocket — nó là *"lần cuối thấy"*, **không phải** tín hiệu hiện diện thật. Một chấm xanh dựa trên nó là lời nói dối ngay khi thiết bị rớt mạng 30 giây trước, và người dùng sẽ tin nó rồi đi tìm lỗi nhầm chỗ. Hiển thị "lần cuối thấy" dạng tương đối ("2 giờ trước") là sự thật, và cũng đủ để trả lời câu hỏi thật của người dùng.

Ngoại lệ duy nhất: nếu `last_seen_at` trong vòng **90 giây**, nói "Đang hoạt động" — khoảng đó đủ hẹp để không thành lời nói dối.

**Nền kem, không phải nền tối.** Spec: nền tối cho Talk (màn dùng lâu, buổi tối), nền kem cho các màn đọc-nhiều. Devices là danh sách — kem. **Không** đặt `data-surface="talk"`.

**Cam chỉ cho hành động chính.** Ở màn này, hành động chính là "Ghép thiết bị". Nút gỡ dùng `--lugo-danger`, không phải cam.

**Nav chỉ liệt kê màn có thật.** Spec vẽ nav 4 mục, nhưng History và Tools chưa tồn tại. Nav trỏ tới màn không có là nói dối người dùng. Nav mọc dần theo số màn thật: giờ là Talk + Devices.

## Global Constraints

- Chỉ dùng token màu đã có trong `theme.css`. **Không thêm màu mới.**
- **Không role/admin trong UI.** Không gọi `/v1/devices` (endpoint admin) — nó sẽ 403 và đúng ra phải thế.
- Mọi lời gọi API qua `apiFetch`. **Không** `fetch` trực tiếp, **không** tự đọc token.
- Copy tiếng Việt, câu thường, động từ chủ động. Nút nói đúng việc nó làm.
- Responsive xuống mobile; focus bàn phím nhìn thấy được; `prefers-reduced-motion` tôn trọng.
- Chạy `pnpm test` trong `lugo-web-client/`. Hiện 42 test pass — phải giữ.
- **Không** `git push`. Commit trong repo con.

## File Structure

| File | Trách nhiệm |
|---|---|
| `src/lib/time.ts` | Thời gian tương đối tiếng Việt. Thuần túy, test được. |
| `src/api/devices.ts` | Gọi 3 endpoint devices. Không biết React. |
| `src/routes/Devices.tsx` + `.css` | UI danh sách + form ghép. |
| `src/components/Nav.tsx` + `.css` | Nav giữa các màn có thật. |
| `src/App.tsx` | Thêm định tuyến giữa Talk và Devices. |

---

### Task 1: Thời gian tương đối + lớp API devices

**Files:**
- Create: `src/lib/time.ts`, `src/lib/time.test.ts`
- Create: `src/api/devices.ts`, `src/api/devices.test.ts`

**Interfaces:**
- Produces:
  - `relativeTime(iso: string | null, now?: number): string`
  - `isRecentlyActive(iso: string | null, now?: number): boolean` — true nếu trong 90 giây
  - `type Device = {id, user_id, name, serial, created_at, last_seen_at, revoked}`
  - `listDevices(): Promise<Device[]>`
  - `claimDevice(code: string, name: string): Promise<Device>`
  - `revokeDevice(id: string): Promise<void>`

- [ ] **Step 1: Viết test thất bại cho time.ts**

Tạo `src/lib/time.test.ts`:

```ts
import { describe, expect, it } from 'vitest'
import { isRecentlyActive, relativeTime } from './time'

const NOW = Date.parse('2026-07-17T12:00:00Z')
const ago = (sec: number) => new Date(NOW - sec * 1000).toISOString()

describe('relativeTime', () => {
  it('null thì nói thẳng là chưa từng thấy', () => {
    expect(relativeTime(null, NOW)).toBe('chưa kết nối lần nào')
  })

  it('vài giây trước', () => {
    expect(relativeTime(ago(5), NOW)).toBe('vừa xong')
  })

  it('phút', () => {
    expect(relativeTime(ago(120), NOW)).toBe('2 phút trước')
  })

  it('giờ', () => {
    expect(relativeTime(ago(3 * 3600), NOW)).toBe('3 giờ trước')
  })

  it('ngày', () => {
    expect(relativeTime(ago(2 * 86400), NOW)).toBe('2 ngày trước')
  })

  it('thời gian trong tương lai không ra số âm', () => {
    // Lệch đồng hồ giữa máy chủ và trình duyệt là chuyện thường.
    // "-3 phút trước" làm người dùng nghĩ app hỏng.
    expect(relativeTime(new Date(NOW + 60000).toISOString(), NOW)).toBe('vừa xong')
  })

  it('chuỗi rác không làm sập, trả về thông báo trung thực', () => {
    expect(relativeTime('không-phải-ngày', NOW)).toBe('chưa kết nối lần nào')
  })
})

describe('isRecentlyActive', () => {
  it('trong 90 giây thì đang hoạt động', () => {
    expect(isRecentlyActive(ago(30), NOW)).toBe(true)
  })

  it('quá 90 giây thì không', () => {
    // Đây là ranh giới giữa sự thật và lời nói dối: last_seen chỉ được cập nhật
    // khi thiết bị mở WS, nên nói "đang hoạt động" cho một mốc cũ là bịa.
    expect(isRecentlyActive(ago(200), NOW)).toBe(false)
  })

  it('null thì không', () => {
    expect(isRecentlyActive(null, NOW)).toBe(false)
  })
})
```

- [ ] **Step 2: Chạy test để xác nhận nó fail**

Run: `cd lugo-web-client && pnpm test`
Expected: FAIL — không tìm thấy module `./time`

- [ ] **Step 3: Implement time.ts**

Tạo `src/lib/time.ts`:

```ts
const RECENT_MS = 90_000

function parse(iso: string | null): number | null {
  if (!iso) return null
  const t = Date.parse(iso)
  return Number.isNaN(t) ? null : t
}

/** "lần cuối thấy" dạng người đọc được.
 *
 * Cố ý KHÔNG nói "online": last_seen_at chỉ được cập nhật khi thiết bị mở WS,
 * nên nó là dấu vết quá khứ, không phải hiện diện thật.
 */
export function relativeTime(iso: string | null, now: number = Date.now()): string {
  const t = parse(iso)
  if (t === null) return 'chưa kết nối lần nào'

  // Kẹp về 0: lệch đồng hồ server/trình duyệt là chuyện thường, và
  // "-3 phút trước" khiến người dùng tưởng app hỏng.
  const sec = Math.max(0, Math.floor((now - t) / 1000))
  if (sec < 60) return 'vừa xong'
  if (sec < 3600) return `${Math.floor(sec / 60)} phút trước`
  if (sec < 86400) return `${Math.floor(sec / 3600)} giờ trước`
  return `${Math.floor(sec / 86400)} ngày trước`
}

export function isRecentlyActive(iso: string | null, now: number = Date.now()): boolean {
  const t = parse(iso)
  if (t === null) return false
  return now - t <= RECENT_MS && now - t >= -RECENT_MS
}
```

- [ ] **Step 4: Viết test thất bại cho devices.ts**

Tạo `src/api/devices.test.ts`:

```ts
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { claimDevice, listDevices, revokeDevice } from './devices'
import { saveTokens } from './tokens'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const DEVICE = {
  id: 'd1', user_id: 'u1', name: 'Loa bếp', serial: 'ABC123',
  created_at: '2026-07-17T10:00:00Z', last_seen_at: null, revoked: false,
}

describe('devices api', () => {
  beforeEach(() => {
    localStorage.clear()
    saveTokens('acc', 'ref')
    vi.restoreAllMocks()
  })

  it('listDevices trả mảng device', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ success: true, data: [DEVICE] })))
    const list = await listDevices()
    expect(list).toHaveLength(1)
    expect(list[0].name).toBe('Loa bếp')
  })

  it('listDevices gọi đúng endpoint CỦA TÔI, không phải endpoint admin', async () => {
    const f = vi.fn().mockResolvedValue(jsonResponse({ success: true, data: [] }))
    vi.stubGlobal('fetch', f)
    await listDevices()
    // /v1/devices là endpoint admin -- bearer sẽ 403 và đúng ra phải thế.
    expect(f.mock.calls[0][0]).toContain('/v1/devices/mine')
  })

  it('listDevices gắn bearer (tức đi qua apiFetch, không tự fetch)', async () => {
    const f = vi.fn().mockResolvedValue(jsonResponse({ success: true, data: [] }))
    vi.stubGlobal('fetch', f)
    await listDevices()
    expect(new Headers(f.mock.calls[0][1].headers).get('Authorization')).toBe('Bearer acc')
  })

  it('claimDevice gửi code và name', async () => {
    const f = vi.fn().mockResolvedValue(jsonResponse({ success: true, data: DEVICE }))
    vi.stubGlobal('fetch', f)
    const d = await claimDevice('123456', 'Loa bếp')
    expect(f.mock.calls[0][0]).toContain('/v1/devices/pair/claim')
    expect(JSON.parse(f.mock.calls[0][1].body)).toEqual({ code: '123456', name: 'Loa bếp' })
    expect(d.id).toBe('d1')
  })

  it('claimDevice giữ nguyên thông báo lỗi của server', async () => {
    // Server phân biệt "mã sai" với "phần cứng đã ghép rồi" -- hai lỗi đó cần
    // hai cách xử lý khác nhau. Nuốt mất thông tin đó là hại người dùng.
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      jsonResponse({ success: false, error: 'pairing code is invalid or expired' }, 400),
    ))
    await expect(claimDevice('000000', 'X')).rejects.toThrow(/invalid or expired/)
  })

  it('revokeDevice gọi đúng đường của user', async () => {
    const f = vi.fn().mockResolvedValue(jsonResponse({ success: true }))
    vi.stubGlobal('fetch', f)
    await revokeDevice('d1')
    expect(f.mock.calls[0][0]).toContain('/v1/devices/mine/d1/revoke')
    expect(f.mock.calls[0][1].method).toBe('POST')
  })

  it('revokeDevice ném lỗi khi server từ chối', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ detail: 'not found' }, 404)))
    await expect(revokeDevice('nope')).rejects.toThrow()
  })
})
```

- [ ] **Step 5: Chạy test để xác nhận nó fail**

Run: `cd lugo-web-client && pnpm test`
Expected: FAIL — không tìm thấy module `./devices`

- [ ] **Step 6: Implement devices.ts**

Tạo `src/api/devices.ts`:

```ts
import { apiFetch } from './client'

export type Device = {
  id: string
  user_id: string
  name: string
  serial: string
  created_at: string | null
  last_seen_at: string | null
  revoked: boolean
}

/** Lấy thông báo lỗi của server ra, giữ nguyên chữ.
 *
 * Server phân biệt "mã sai/hết hạn" với "phần cứng đã ghép rồi" -- hai tình
 * huống cần hai hành động khác nhau. Thay bằng một câu chung chung là lấy mất
 * của người dùng thứ họ cần để tự sửa.
 */
async function errorFrom(resp: Response): Promise<Error> {
  try {
    const body = await resp.json()
    const msg = body?.error ?? body?.detail
    if (typeof msg === 'string' && msg) return new Error(msg)
  } catch {
    // body không phải JSON -- rơi xuống thông báo mặc định
  }
  return new Error(`Máy chủ trả về lỗi ${resp.status}`)
}

export async function listDevices(): Promise<Device[]> {
  // /v1/devices/mine, KHÔNG phải /v1/devices -- cái sau là endpoint admin và
  // bearer sẽ nhận 403, đúng như thiết kế.
  const resp = await apiFetch('/v1/devices/mine')
  if (!resp.ok) throw await errorFrom(resp)
  const body = await resp.json()
  return body.data as Device[]
}

export async function claimDevice(code: string, name: string): Promise<Device> {
  const resp = await apiFetch('/v1/devices/pair/claim', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code, name }),
  })
  if (!resp.ok) throw await errorFrom(resp)
  const body = await resp.json()
  return body.data as Device
}

export async function revokeDevice(id: string): Promise<void> {
  const resp = await apiFetch(`/v1/devices/mine/${encodeURIComponent(id)}/revoke`, {
    method: 'POST',
  })
  if (!resp.ok) throw await errorFrom(resp)
}
```

- [ ] **Step 7: Chạy test để xác nhận pass**

Run: `cd lugo-web-client && pnpm test`
Expected: PASS (17 test mới, tổng 59)

- [ ] **Step 8: Commit**

```bash
cd lugo-web-client
git add src/lib/time.ts src/lib/time.test.ts src/api/devices.ts src/api/devices.test.ts
git commit -m "feat(devices): lớp API + thời gian tương đối"
```

---

### Task 2: Màn Devices

**Files:**
- Create: `src/routes/Devices.tsx`, `src/routes/Devices.css`

**Interfaces:**
- Consumes: `listDevices`, `claimDevice`, `revokeDevice`, `Device` (Task 1); `relativeTime`, `isRecentlyActive` (Task 1)
- Produces: `<Devices />`

- [ ] **Step 1: Devices.css**

Tạo `src/routes/Devices.css`:

```css
.dev {
  min-height: 100dvh;
  padding: 20px 20px 88px;
  max-width: 34rem;
  margin: 0 auto;
}

.dev__h {
  font-size: 1.5rem;
  font-weight: 600;
  margin: 8px 0 4px;
}

.dev__sub {
  margin: 0 0 24px;
  opacity: 0.6;
  font-size: 0.9375rem;
  line-height: 1.5;
}

.dev__list {
  list-style: none;
  padding: 0;
  margin: 0 0 28px;
  display: grid;
  gap: 10px;
}

.dev__item {
  border: 1px solid var(--lugo-cream-deep);
  border-radius: 12px;
  padding: 14px 16px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.dev__name {
  font-weight: 500;
  margin: 0 0 2px;
}

.dev__meta {
  font-size: 0.8125rem;
  opacity: 0.55;
  margin: 0;
}

.dev__serial {
  font-size: 0.75rem;
  opacity: 0.4;
  margin: 2px 0 0;
  letter-spacing: 0.04em;
}

.dev__empty {
  border: 1px dashed var(--lugo-cream-deep);
  border-radius: 12px;
  padding: 24px 20px;
  text-align: center;
  font-size: 0.9375rem;
  line-height: 1.6;
  opacity: 0.7;
  margin: 0 0 28px;
}

.dev__form {
  display: grid;
  gap: 10px;
}

.dev__code {
  font-size: 1.5rem;
  letter-spacing: 0.3em;
  text-align: center;
  font-variant-numeric: tabular-nums;
}

.dev__input {
  font: inherit;
  padding: 12px 14px;
  border-radius: 10px;
  border: 1px solid var(--lugo-cream-deep);
  background: transparent;
  color: inherit;
}

.dev__input:focus-visible {
  outline: 2px solid var(--lugo-accent-warm);
  outline-offset: 2px;
  border-color: transparent;
}

.dev__btn {
  font: inherit;
  font-size: 0.9375rem;
  font-weight: 500;
  padding: 12px 20px;
  border-radius: 999px;
  border: 1px solid currentColor;
  background: none;
  color: inherit;
  cursor: pointer;
  opacity: 0.75;
}
.dev__btn:hover { opacity: 1; }
.dev__btn:focus-visible {
  outline: 2px solid var(--lugo-accent-warm);
  outline-offset: 3px;
}

/* Hành động chính của màn này -- chỗ duy nhất được dùng cam ở đây. */
.dev__btn--primary {
  background: var(--lugo-accent-gradient);
  color: #111;
  border-color: transparent;
  opacity: 1;
}

.dev__btn--danger {
  border-color: var(--lugo-danger);
  color: var(--lugo-danger);
  font-size: 0.8125rem;
  padding: 8px 14px;
}

.dev__err {
  color: var(--lugo-danger);
  font-size: 0.9375rem;
  margin: 0;
}
```

- [ ] **Step 2: Devices.tsx**

Tạo `src/routes/Devices.tsx`:

```tsx
import { useEffect, useState } from 'react'
import { claimDevice, listDevices, revokeDevice, type Device } from '../api/devices'
import { isRecentlyActive, relativeTime } from '../lib/time'
import './Devices.css'

export function Devices() {
  const [items, setItems] = useState<Device[]>([])
  const [code, setCode] = useState('')
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [confirming, setConfirming] = useState<string | null>(null)

  async function refresh() {
    try {
      setItems(await listDevices())
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Không tải được danh sách thiết bị')
    }
  }

  useEffect(() => {
    void refresh()
  }, [])

  async function pair(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await claimDevice(code.trim(), name.trim())
      setCode('')
      setName('')
      await refresh()
    } catch (err) {
      // Giữ nguyên chữ của server: nó phân biệt "mã sai" với "phần cứng đã
      // ghép rồi", và hai lỗi đó cần hai hành động khác nhau.
      setError(err instanceof Error ? err.message : 'Ghép không thành công')
    } finally {
      setBusy(false)
    }
  }

  async function remove(id: string) {
    setError(null)
    try {
      await revokeDevice(id)
      setConfirming(null)
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Gỡ không thành công')
    }
  }

  const active = items.filter((d) => !d.revoked)

  return (
    <main className="dev">
      <h1 className="dev__h">Thiết bị</h1>
      <p className="dev__sub">Thiết bị đã ghép sẽ nói chuyện với Lugo bằng tài khoản của bạn.</p>

      {active.length === 0 ? (
        <p className="dev__empty">
          Chưa có thiết bị nào. Bật thiết bị Lugo lên — nó sẽ hiện một mã gồm 6 chữ số. Nhập mã đó
          xuống dưới.
        </p>
      ) : (
        <ul className="dev__list">
          {active.map((d) => (
            <li className="dev__item" key={d.id}>
              <div>
                <p className="dev__name">{d.name}</p>
                <p className="dev__meta">
                  {isRecentlyActive(d.last_seen_at)
                    ? 'Đang hoạt động'
                    : `Lần cuối thấy: ${relativeTime(d.last_seen_at)}`}
                </p>
                <p className="dev__serial">{d.serial}</p>
              </div>
              {confirming === d.id ? (
                <div style={{ display: 'flex', gap: 6 }}>
                  <button className="dev__btn dev__btn--danger" onClick={() => remove(d.id)}>
                    Gỡ thật
                  </button>
                  <button className="dev__btn" onClick={() => setConfirming(null)}>
                    Thôi
                  </button>
                </div>
              ) : (
                <button className="dev__btn dev__btn--danger" onClick={() => setConfirming(d.id)}>
                  Gỡ
                </button>
              )}
            </li>
          ))}
        </ul>
      )}

      <form className="dev__form" onSubmit={pair}>
        <input
          className="dev__input dev__code"
          aria-label="Mã 6 chữ số"
          value={code}
          onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
          placeholder="000000"
          inputMode="numeric"
          autoComplete="one-time-code"
        />
        <input
          className="dev__input"
          aria-label="Tên thiết bị"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Đặt tên, ví dụ: Loa bếp"
        />
        {error && (
          <p className="dev__err" role="alert">
            {error}
          </p>
        )}
        <button
          className="dev__btn dev__btn--primary"
          type="submit"
          disabled={busy || code.length !== 6 || !name.trim()}
        >
          {busy ? 'Đang ghép...' : 'Ghép thiết bị'}
        </button>
      </form>
    </main>
  )
}
```

**Chú ý về copy:** màn trống là lời mời hành động và nói rõ phải làm gì tiếp ("Bật thiết bị lên — nó sẽ hiện một mã"). Nút gỡ hỏi lại vì gỡ là hành động phá hoại: thiết bị sẽ mất quyền và phải ghép lại. "Gỡ thật" chứ không phải "OK" — nút xác nhận nói đúng việc nó làm.

- [ ] **Step 3: Build + test**

Run: `cd lugo-web-client && pnpm test && pnpm build`
Expected: 59 test pass, build sạch

- [ ] **Step 4: Commit**

```bash
cd lugo-web-client
git add src/routes/Devices.tsx src/routes/Devices.css
git commit -m "feat(devices): màn ghép và quản lý thiết bị"
```

---

### Task 3: Nav

Giờ mới có hai màn thật nên nav mới có nghĩa.

**Files:**
- Create: `src/components/Nav.tsx`, `src/components/Nav.css`
- Modify: `src/App.tsx`
- Modify: `src/routes/Talk.tsx` (bỏ nút Đăng xuất — nó chuyển sang Nav)

**Interfaces:**
- Produces: `<Nav current={Screen} onGo={(s: Screen) => void} onLogout={() => void} />`, `type Screen = 'talk' | 'devices'`

- [ ] **Step 1: Nav.css**

Tạo `src/components/Nav.css`:

```css
.nav {
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  display: flex;
  justify-content: center;
  gap: 4px;
  padding: 10px 12px calc(10px + env(safe-area-inset-bottom));
  background: color-mix(in srgb, var(--lugo-bg) 92%, transparent);
  backdrop-filter: blur(8px);
  border-top: 1px solid color-mix(in srgb, currentColor 12%, transparent);
}

.nav__btn {
  font: inherit;
  font-size: 0.875rem;
  font-weight: 500;
  padding: 10px 18px;
  min-height: 40px;
  border: 0;
  border-radius: 999px;
  background: none;
  color: inherit;
  opacity: 0.5;
  cursor: pointer;
}
.nav__btn:hover { opacity: 0.8; }

/* Màn đang xem. KHÔNG dùng cam: cam dành cho trạng thái hoạt động và hành
   động chính, còn "bạn đang ở đây" thì không phải hành động. */
.nav__btn[aria-current='page'] {
  opacity: 1;
  background: color-mix(in srgb, currentColor 10%, transparent);
}

.nav__btn:focus-visible {
  outline: 2px solid var(--lugo-accent-warm);
  outline-offset: 2px;
}

.nav__spacer { flex: 1; max-width: 24px; }
```

- [ ] **Step 2: Nav.tsx**

Tạo `src/components/Nav.tsx`:

```tsx
import './Nav.css'

export type Screen = 'talk' | 'devices'

// Chỉ liệt kê màn CÓ THẬT. Spec vẽ nav 4 mục, nhưng History và Tools chưa tồn
// tại -- nav trỏ tới màn không có là nói dối người dùng. Thêm vào khi có thật.
const ITEMS: { id: Screen; label: string }[] = [
  { id: 'talk', label: 'Nói' },
  { id: 'devices', label: 'Thiết bị' },
]

export function Nav({
  current,
  onGo,
  onLogout,
}: {
  current: Screen
  onGo: (s: Screen) => void
  onLogout: () => void
}) {
  return (
    <nav className="nav" aria-label="Điều hướng chính">
      {ITEMS.map((it) => (
        <button
          key={it.id}
          className="nav__btn"
          aria-current={current === it.id ? 'page' : undefined}
          onClick={() => onGo(it.id)}
        >
          {it.label}
        </button>
      ))}
      <span className="nav__spacer" />
      <button className="nav__btn" onClick={onLogout}>
        Đăng xuất
      </button>
    </nav>
  )
}
```

- [ ] **Step 3: App.tsx**

Thay `src/App.tsx`:

```tsx
import { useEffect, useState } from 'react'
import './theme.css'
import { isAuthed, logout } from './api/auth'
import { onAuthLost } from './api/client'
import { Nav, type Screen } from './components/Nav'
import { Devices } from './routes/Devices'
import { Login } from './routes/Login'
import { Talk } from './routes/Talk'

export default function App() {
  const [authed, setAuthed] = useState(isAuthed())
  const [screen, setScreen] = useState<Screen>('talk')

  useEffect(() => {
    onAuthLost(() => setAuthed(false))
  }, [])

  if (!authed) return <Login onDone={() => setAuthed(true)} />

  function signOut() {
    logout()
    setAuthed(false)
    setScreen('talk')
  }

  return (
    <>
      {screen === 'talk' ? <Talk /> : <Devices />}
      <Nav current={screen} onGo={setScreen} onLogout={signOut} />
    </>
  )
}
```

- [ ] **Step 4: Bỏ nút Đăng xuất khỏi Talk**

Trong `src/routes/Talk.tsx`:
- Đổi chữ ký thành `export function Talk()` — bỏ prop `onLogout`.
- Xoá nút "Đăng xuất" trong `.talk__bar` (nó chuyển sang Nav; để hai chỗ cùng làm một việc là thừa).
- Giữ nguyên wordmark trong `.talk__bar`.
- Thêm khoảng đệm đáy để Nav không che nút chính: trong `Talk.css`, đổi `padding: 20px` thành `padding: 20px 20px 88px`.

- [ ] **Step 5: Build + test**

Run: `cd lugo-web-client && pnpm test && pnpm build`
Expected: 59 test pass, build sạch

- [ ] **Step 6: Commit**

```bash
cd lugo-web-client
git add src/components/Nav.tsx src/components/Nav.css src/App.tsx src/routes/Talk.tsx src/routes/Talk.css
git commit -m "feat(ui): nav giữa Nói và Thiết bị"
```

---

### Task 4: Xác minh thật

- [ ] **Step 1: Chạy gateway + client**

```bash
cd /Users/lugon/code/speech-text-transformer
.venv/bin/uvicorn app.main:app --app-dir apps/api_gateway --port 8000
# terminal khác:
cd lugo-web-client && pnpm dev
```

- [ ] **Step 2: Tạo một mã ghép thật (giả làm thiết bị ESP32)**

```bash
curl -s -X POST localhost:8000/v1/devices/pair/init \
  -H 'Content-Type: application/json' -d '{"serial":"TEST-SERIAL-001"}'
```
Chép `code` 6 chữ số trong kết quả.

- [ ] **Step 3: Ghép qua UI và chụp ảnh**

Tạo `lugo-web-client/verify-devices.mjs`:

```js
import { chromium } from 'playwright'
const b = await chromium.launch()
const p = await b.newPage({ viewport: { width: 420, height: 860 }, deviceScaleFactor: 2 })
const errors = []
p.on('pageerror', (e) => errors.push(String(e)))

// mã truyền qua biến môi trường: PAIR_CODE=123456 node verify-devices.mjs
const code = process.env.PAIR_CODE
if (!code) throw new Error('thiếu PAIR_CODE')

await p.goto('http://localhost:5173/')
await p.fill('input[aria-label="Tên đăng nhập"]', 'e2e-user')
await p.fill('input[aria-label="Mật khẩu"]', 'pw12345678')
await p.click('button[type="submit"]')
await p.waitForTimeout(1500)

await p.click('text=Thiết bị')
await p.waitForTimeout(800)
await p.screenshot({ path: 'shots/dev-empty.png' })

await p.fill('input[aria-label="Mã 6 chữ số"]', code)
await p.fill('input[aria-label="Tên thiết bị"]', 'Loa bếp')
await p.click('text=Ghép thiết bị')
await p.waitForTimeout(1500)
await p.screenshot({ path: 'shots/dev-paired.png' })

await p.click('text=Gỡ')
await p.waitForTimeout(400)
await p.screenshot({ path: 'shots/dev-confirm.png' })

console.log('lỗi trang:', errors.length ? errors : 'không có')
await b.close()
```

Chạy: `PAIR_CODE=<mã> node verify-devices.mjs`

- [ ] **Step 4: NHÌN vào ảnh**

- Thiết bị có hiện ra sau khi ghép không (tên, "chưa kết nối lần nào", serial)?
- Màn trống có nói rõ phải làm gì không?
- Nút "Gỡ" bấm vào có hỏi lại không?
- Nav có che mất nút "Ghép thiết bị" không?
- Nền có phải kem (không phải nền tối) không?
- Cam có rò ra ngoài nút "Ghép thiết bị" không?
- Ở 420px có gì tràn không?

**Sửa những gì thấy sai, chụp lại, nhìn lại.**

- [ ] **Step 5: Thử lỗi thật**

Nhập mã sai (`000000`) → phải hiện đúng chữ của server ("pairing code is invalid or expired"), không phải câu chung chung.

- [ ] **Step 6: Commit**

```bash
cd lugo-web-client
git add verify-devices.mjs
git commit -m "test(devices): xác minh luồng ghép thật"
```

## Ngoài phạm vi plan này

- History, Tools (và mục nav của chúng)
- Đổi tên thiết bị đã ghép (API chưa có endpoint)
- Tự làm mới danh sách theo chu kỳ — hiện chỉ tải khi vào màn và sau khi ghép/gỡ. Cố ý: thêm polling là thêm rủi ro mà chưa có ai cần.
- Hiện diện realtime (backend chỉ có `last_seen_at`, không có kênh presence)
