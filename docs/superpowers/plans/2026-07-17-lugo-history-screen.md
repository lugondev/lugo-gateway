# Lugo Web Client — Màn History (Phase 1e) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Người dùng xem lại các cuộc trò chuyện cũ của mình — danh sách phiên, transcript đầy đủ, và xoá phiên không muốn giữ.

**Architecture:** `src/api/history.ts` gọi API (qua `apiFetch`). `History.tsx` là UI hai tầng: danh sách → chi tiết một phiên. Không thêm router thư viện — `useState` là đủ cho hai tầng.

**Spec:** `docs/superpowers/specs/2026-07-16-lugo-web-client-design.md`

## KHÔNG có audio phát lại — và vì sao

Spec bản đầu hứa History có audio phát lại. **Đã hoãn có chủ đích** (spec đã sửa ở `5c5d99f`). Ba lý do độc lập, mỗi cái đủ để chặn:

1. `get_messages()` trả đúng `{turn, role, content}` — thuần văn bản, không cột nào tham chiếu audio.
2. Artifacts bị dọn sau 24 giờ (`artifacts_ttl_hours = 24.0`).
3. **Luồng web client không sinh file audio nào cả** — ta chọn `audio_out=opus` nên audio đi thẳng qua WebSocket và không bao giờ ghi ra đĩa.

**Đừng cố thêm nút phát.** Không có gì để phát. Nếu bạn thấy mình đang tìm `audio_url` trong dữ liệu message thì dừng lại — nó không tồn tại.

## Nền tảng đã có (đừng dựng lại)

Repo con `lugo-web-client` @ `391daed`, 62/62 test:
- `src/api/client.ts` — `apiFetch(path, init?)`. **Mọi lời gọi API phải đi qua đây.**
- `src/api/devices.ts` — mẫu tốt để bắt chước: `errorFrom()` lấy chữ lỗi server, `friendlyDeviceError()` dịch sang tiếng Việt hành động được, lỗi lạ trả nguyên văn.
- `src/lib/time.ts` — `relativeTime(iso, now?)`, `isRecentlyActive(iso, now?)`.
- `src/components/Nav.tsx` — `type Screen = 'talk' | 'devices'`, `ITEMS` liệt kê màn có thật.
- `src/routes/Devices.tsx` — mẫu bố cục cho màn nền kem.

## API thật (đã đọc code, và đã curl xác minh `/v1/sessions` → 200 với bearer)

`sessions.py` dùng đúng `current_user_id`/`current_role`, nên bearer chạy được. Vì đường bearer **luôn** là `role="user"`, `_scope_user_id()` luôn trả về id của chính người gọi — người dùng chỉ bao giờ thấy phiên của mình. Không cần lọc ở client.

| Endpoint | Trả về |
|---|---|
| `GET /v1/sessions?limit=20&offset=0` | `{success, data: SessionRow[]}` |
| `GET /v1/sessions/{id}` | `{success, data: Session & {messages: Message[]}}`, 404 nếu không phải của bạn |
| `DELETE /v1/sessions/{id}` | `{success, data: {id, deleted}}`, 404 nếu không phải của bạn |

- `SessionRow` = `{id, profile_id, user_id, created_at, ended_at, meta, message_count, preview}` (`preview` = 80 ký tự đầu của câu người dùng nói đầu tiên; thời gian là ISO hoặc `null`).
- `Message` = `{turn, role, content}`. `role` là `"user"` hoặc `"assistant"`.

**Sắp xếp:** server đã trả theo `created_at DESC` (mới nhất trước). **Đừng sắp lại ở client.**

**404 nghĩa là "không phải của bạn HOẶC không tồn tại"** — server cố ý không phân biệt hai cái đó. Đừng viết copy khẳng định phiên "đã bị xoá"; ta không biết.

## Global Constraints

- Chỉ dùng token màu đã có trong `theme.css`. **Không thêm màu mới.** Nền kem (màn đọc-nhiều) — **không** đặt `data-surface="talk"`.
- Cam chỉ cho hành động chính. Ở màn này **không có hành động chính nào** — đọc là việc chính, còn xoá là phá hoại. Nên **màn này không có gì màu cam**. Nút xoá dùng `--lugo-danger`.
- **Không role/admin trong UI.**
- Mọi lời gọi API qua `apiFetch`. Không `fetch` trực tiếp, không tự đọc token.
- Copy tiếng Việt, câu thường, động từ chủ động. **Lỗi phải là tiếng Việt** — một vòng trước đã lỡ hiện chuỗi tiếng Anh của server cho người dùng cuối; đừng lặp lại.
- Padding đáy `88px` để nav cố định không che nội dung.
- Responsive xuống mobile; focus bàn phím nhìn thấy được.
- Chạy `pnpm test` trong `lugo-web-client/`. Hiện 62 test pass — phải giữ.
- **Không** `git push`. Commit trong repo con.

## File Structure

| File | Trách nhiệm |
|---|---|
| `src/api/history.ts` | Gọi 3 endpoint sessions. Không biết React. |
| `src/routes/History.tsx` + `.css` | Danh sách + chi tiết phiên. |
| `src/components/Nav.tsx` | Thêm mục "Lịch sử". |
| `src/App.tsx` | Thêm định tuyến. |

---

### Task 1: Lớp API history

**Files:**
- Create: `src/api/history.ts`, `src/api/history.test.ts`

**Interfaces:**
- Produces:
  - `type Message = {turn: number, role: string, content: string}`
  - `type SessionRow = {id, profile_id, user_id, created_at, ended_at, meta, message_count, preview}`
  - `type SessionDetail = SessionRow & {messages: Message[]}`
  - `listSessions(limit?: number, offset?: number): Promise<SessionRow[]>`
  - `getSession(id: string): Promise<SessionDetail>`
  - `deleteSession(id: string): Promise<void>`

- [ ] **Step 1: Viết test thất bại**

Tạo `src/api/history.test.ts`:

```ts
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { deleteSession, getSession, listSessions } from './history'
import { saveTokens } from './tokens'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const ROW = {
  id: 's1', profile_id: 'p', user_id: 'u1',
  created_at: '2026-07-17T10:00:00Z', ended_at: null, meta: {},
  message_count: 4, preview: 'Xin chào Lugo',
}

describe('history api', () => {
  beforeEach(() => {
    localStorage.clear()
    saveTokens('acc', 'ref')
    vi.restoreAllMocks()
  })

  it('listSessions trả mảng phiên', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ success: true, data: [ROW] })))
    const rows = await listSessions()
    expect(rows).toHaveLength(1)
    expect(rows[0].preview).toBe('Xin chào Lugo')
  })

  it('listSessions gắn bearer (tức đi qua apiFetch)', async () => {
    const f = vi.fn().mockResolvedValue(jsonResponse({ success: true, data: [] }))
    vi.stubGlobal('fetch', f)
    await listSessions()
    expect(new Headers(f.mock.calls[0][1].headers).get('Authorization')).toBe('Bearer acc')
  })

  it('listSessions truyền limit và offset', async () => {
    const f = vi.fn().mockResolvedValue(jsonResponse({ success: true, data: [] }))
    vi.stubGlobal('fetch', f)
    await listSessions(50, 100)
    expect(String(f.mock.calls[0][0])).toContain('limit=50')
    expect(String(f.mock.calls[0][0])).toContain('offset=100')
  })

  it('getSession trả cả messages', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      jsonResponse({ success: true, data: { ...ROW, messages: [{ turn: 1, role: 'user', content: 'hi' }] } }),
    ))
    const d = await getSession('s1')
    expect(d.messages).toHaveLength(1)
    expect(d.messages[0].role).toBe('user')
  })

  it('getSession 404 ném lỗi TIẾNG VIỆT, không phải chuỗi của server', async () => {
    // Người dùng cuối là người Việt. Một vòng trước ta đã lỡ hiện
    // "pairing code is invalid or expired" thẳng vào mặt họ.
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ detail: "Session 's1' not found" }, 404)))
    await expect(getSession('s1')).rejects.toThrow(/không tìm thấy|không còn/i)
  })

  it('deleteSession gọi đúng method DELETE', async () => {
    const f = vi.fn().mockResolvedValue(jsonResponse({ success: true, data: { id: 's1', deleted: true } }))
    vi.stubGlobal('fetch', f)
    await deleteSession('s1')
    expect(String(f.mock.calls[0][0])).toContain('/v1/sessions/s1')
    expect(f.mock.calls[0][1].method).toBe('DELETE')
  })

  it('id được encode để không vỡ URL', async () => {
    const f = vi.fn().mockResolvedValue(jsonResponse({ success: true, data: {} }))
    vi.stubGlobal('fetch', f)
    await deleteSession('a/b c')
    expect(String(f.mock.calls[0][0])).toContain('a%2Fb%20c')
  })

  it('deleteSession ném lỗi khi server từ chối', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ detail: 'nope' }, 404)))
    await expect(deleteSession('s1')).rejects.toThrow()
  })
})
```

- [ ] **Step 2: Chạy test để xác nhận nó fail**

Run: `cd lugo-web-client && pnpm test`
Expected: FAIL — không tìm thấy module `./history`

- [ ] **Step 3: Implement**

Tạo `src/api/history.ts`:

```ts
import { apiFetch } from './client'

export type Message = { turn: number; role: string; content: string }

export type SessionRow = {
  id: string
  profile_id: string
  user_id: string | null
  created_at: string | null
  ended_at: string | null
  meta: Record<string, unknown>
  message_count: number
  preview: string
}

export type SessionDetail = SessionRow & { messages: Message[] }

/** Lỗi tiếng Việt cho người dùng cuối.
 *
 * Server trả 404 cho CẢ "không tồn tại" LẪN "không phải của bạn" -- nó cố ý
 * không phân biệt. Nên copy ở đây không được khẳng định phiên "đã bị xoá":
 * ta không biết điều đó.
 */
async function errorFrom(resp: Response): Promise<Error> {
  if (resp.status === 404) {
    return new Error('Không tìm thấy cuộc trò chuyện này. Có thể nó đã bị xoá.')
  }
  if (resp.status === 401 || resp.status === 403) {
    return new Error('Phiên đăng nhập đã hết hạn. Hãy đăng nhập lại.')
  }
  return new Error(`Máy chủ trả về lỗi ${resp.status}`)
}

export async function listSessions(limit = 20, offset = 0): Promise<SessionRow[]> {
  const resp = await apiFetch(`/v1/sessions?limit=${limit}&offset=${offset}`)
  if (!resp.ok) throw await errorFrom(resp)
  const body = await resp.json()
  return body.data as SessionRow[]
}

export async function getSession(id: string): Promise<SessionDetail> {
  const resp = await apiFetch(`/v1/sessions/${encodeURIComponent(id)}`)
  if (!resp.ok) throw await errorFrom(resp)
  const body = await resp.json()
  return body.data as SessionDetail
}

export async function deleteSession(id: string): Promise<void> {
  const resp = await apiFetch(`/v1/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' })
  if (!resp.ok) throw await errorFrom(resp)
}
```

- [ ] **Step 4: Chạy test để xác nhận pass**

Run: `cd lugo-web-client && pnpm test`
Expected: PASS (8 test mới, tổng 70)

- [ ] **Step 5: Commit**

```bash
cd lugo-web-client
git add src/api/history.ts src/api/history.test.ts
git commit -m "feat(history): lớp API phiên trò chuyện"
```

---

### Task 2: Màn History

Hai tầng: danh sách → chi tiết. `useState` là đủ, **không thêm thư viện router**.

**Files:**
- Create: `src/routes/History.tsx`, `src/routes/History.css`

- [ ] **Step 1: History.css**

Tạo `src/routes/History.css`:

```css
.his {
  min-height: 100dvh;
  padding: 20px 20px 88px;
  max-width: 34rem;
  margin: 0 auto;
}

.his__h {
  font-size: 1.5rem;
  font-weight: 600;
  margin: 8px 0 4px;
}

.his__sub {
  margin: 0 0 24px;
  opacity: 0.6;
  font-size: 0.9375rem;
  line-height: 1.5;
}

.his__list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: grid;
  gap: 10px;
}

.his__row {
  width: 100%;
  text-align: left;
  font: inherit;
  color: inherit;
  border: 1px solid var(--lugo-cream-deep);
  border-radius: 12px;
  padding: 14px 16px;
  background: none;
  cursor: pointer;
  display: grid;
  gap: 4px;
}
.his__row:hover { border-color: color-mix(in srgb, currentColor 30%, transparent); }
.his__row:focus-visible {
  outline: 2px solid var(--lugo-accent-warm);
  outline-offset: 2px;
}

.his__preview {
  font-weight: 500;
  margin: 0;
  /* Cắt ở một dòng: đây là danh sách để quét mắt, không phải để đọc. */
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.his__meta {
  font-size: 0.8125rem;
  opacity: 0.55;
  margin: 0;
}

.his__empty {
  border: 1px dashed var(--lugo-cream-deep);
  border-radius: 12px;
  padding: 24px 20px;
  text-align: center;
  font-size: 0.9375rem;
  line-height: 1.6;
  opacity: 0.7;
}

.his__err {
  color: var(--lugo-danger);
  font-size: 0.9375rem;
}

/* --- chi tiết --- */

.his__bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 16px;
}

.his__btn {
  font: inherit;
  font-size: 0.875rem;
  padding: 8px 14px;
  min-height: 40px;
  border-radius: 999px;
  border: 1px solid currentColor;
  background: none;
  color: inherit;
  cursor: pointer;
  opacity: 0.7;
}
.his__btn:hover { opacity: 1; }
.his__btn:focus-visible {
  outline: 2px solid var(--lugo-accent-warm);
  outline-offset: 3px;
}

.his__btn--danger {
  border-color: var(--lugo-danger);
  color: var(--lugo-danger);
}

.his__turns {
  display: grid;
  gap: 18px;
  margin: 0;
}

.his__turn { margin: 0; }

/* Nhãn ai nói. Không dùng bong bóng chat -- đây là bản ghi để đọc lại,
   không phải một cuộc chat đang diễn ra. */
.his__who {
  font-size: 0.75rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  opacity: 0.45;
  margin: 0 0 4px;
}

.his__said {
  margin: 0;
  line-height: 1.6;
  white-space: pre-wrap;
}

.his__turn--user .his__said { opacity: 0.65; }
```

- [ ] **Step 2: History.tsx**

Tạo `src/routes/History.tsx`:

```tsx
import { useEffect, useState } from 'react'
import { deleteSession, getSession, listSessions, type SessionDetail, type SessionRow } from '../api/history'
import { relativeTime } from '../lib/time'
import './History.css'

function Detail({ id, onBack, onDeleted }: { id: string; onBack: () => void; onDeleted: () => void }) {
  const [data, setData] = useState<SessionDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [confirming, setConfirming] = useState(false)

  useEffect(() => {
    let alive = true
    getSession(id)
      .then((d) => alive && setData(d))
      .catch((e) => alive && setError(e instanceof Error ? e.message : 'Không tải được'))
    return () => {
      alive = false
    }
  }, [id])

  async function remove() {
    try {
      await deleteSession(id)
      onDeleted()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Xoá không thành công')
    }
  }

  return (
    <main className="his">
      <div className="his__bar">
        <button className="his__btn" onClick={onBack}>
          Quay lại
        </button>
        {confirming ? (
          <span style={{ display: 'flex', gap: 6 }}>
            <button className="his__btn his__btn--danger" onClick={remove}>
              Xoá thật
            </button>
            <button className="his__btn" onClick={() => setConfirming(false)}>
              Thôi
            </button>
          </span>
        ) : (
          <button className="his__btn his__btn--danger" onClick={() => setConfirming(true)}>
            Xoá
          </button>
        )}
      </div>

      {error && (
        <p className="his__err" role="alert">
          {error}
        </p>
      )}

      {data && data.messages.length === 0 && (
        <p className="his__empty">Cuộc trò chuyện này không có nội dung nào.</p>
      )}

      {data && data.messages.length > 0 && (
        <div className="his__turns">
          {data.messages.map((m, i) => (
            <div className={`his__turn his__turn--${m.role}`} key={`${m.turn}-${i}`}>
              <p className="his__who">{m.role === 'user' ? 'Bạn' : 'Lugo'}</p>
              <p className="his__said">{m.content}</p>
            </div>
          ))}
        </div>
      )}
    </main>
  )
}

export function History() {
  const [rows, setRows] = useState<SessionRow[]>([])
  const [open, setOpen] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function refresh() {
    try {
      // Server đã sắp theo created_at DESC -- không sắp lại ở client.
      setRows(await listSessions(50))
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Không tải được lịch sử')
    }
  }

  useEffect(() => {
    void refresh()
  }, [])

  if (open) {
    return (
      <Detail
        id={open}
        onBack={() => setOpen(null)}
        onDeleted={() => {
          setOpen(null)
          void refresh()
        }}
      />
    )
  }

  return (
    <main className="his">
      <h1 className="his__h">Lịch sử</h1>
      <p className="his__sub">Những gì bạn và Lugo đã nói với nhau.</p>

      {error && (
        <p className="his__err" role="alert">
          {error}
        </p>
      )}

      {!error && rows.length === 0 ? (
        <p className="his__empty">Chưa có cuộc trò chuyện nào. Sang mục Nói để bắt đầu.</p>
      ) : (
        <ul className="his__list">
          {rows.map((r) => (
            <li key={r.id}>
              <button className="his__row" onClick={() => setOpen(r.id)}>
                <p className="his__preview">{r.preview || 'Không có nội dung'}</p>
                <p className="his__meta">
                  {relativeTime(r.created_at)} · {r.message_count} tin nhắn
                </p>
              </button>
            </li>
          ))}
        </ul>
      )}
    </main>
  )
}
```

**Chú ý:** không có nút phát audio ở đâu cả — không có gì để phát (xem đầu plan). Nhãn "BẠN"/"LUGO" thay cho bong bóng chat: đây là bản ghi để đọc lại, không phải cuộc chat đang diễn ra.

- [ ] **Step 3: Build + test**

Run: `cd lugo-web-client && pnpm test && pnpm build`
Expected: 70 test pass, build sạch

- [ ] **Step 4: Commit**

```bash
cd lugo-web-client
git add src/routes/History.tsx src/routes/History.css
git commit -m "feat(history): màn xem lại cuộc trò chuyện"
```

---

### Task 3: Nối vào Nav

**Files:**
- Modify: `src/components/Nav.tsx`
- Modify: `src/App.tsx`

- [ ] **Step 1: Nav**

Trong `src/components/Nav.tsx`:
- Đổi `export type Screen = 'talk' | 'devices'` thành `'talk' | 'history' | 'devices'`.
- Thêm vào `ITEMS`, **giữa** talk và devices: `{ id: 'history', label: 'Lịch sử' }`.

Thứ tự này có lý do: Nói là việc chính, Lịch sử là thứ bạn xem sau khi nói, Thiết bị là cấu hình — thưa dần theo tần suất dùng.

**Vẫn không thêm Tools** — nó chưa tồn tại, và nav trỏ tới màn không có là nói dối người dùng.

- [ ] **Step 2: App**

Trong `src/App.tsx`, import `History` và thêm nhánh:

```tsx
      {screen === 'talk' ? <Talk /> : screen === 'history' ? <History /> : <Devices />}
```

- [ ] **Step 3: Build + test**

Run: `cd lugo-web-client && pnpm test && pnpm build`
Expected: 70 test pass, build sạch

- [ ] **Step 4: Commit**

```bash
cd lugo-web-client
git add src/components/Nav.tsx src/App.tsx
git commit -m "feat(ui): thêm Lịch sử vào nav"
```

---

### Task 4: Xác minh thật

- [ ] **Step 1: Tạo lịch sử thật**

Chạy gateway + `pnpm dev`. Rồi tạo vài phiên thật bằng cách nói chuyện qua trang thử đã có:

```bash
cd lugo-web-client && PAIR_CODE=x node - <<'EOF'
import { chromium } from 'playwright'
const b = await chromium.launch({ args: ['--use-fake-ui-for-media-stream','--use-fake-device-for-media-stream'] })
const p = await b.newPage()
await p.goto('http://localhost:5173/talk-probe.html')
await p.click('#go'); await p.waitForTimeout(3000)
await p.click('#say'); await p.waitForTimeout(25000)
await b.close()
EOF
```

Chạy hai lần để có hai phiên. (Hoặc nói chuyện thật qua màn Talk.)

- [ ] **Step 2: Chụp và nhìn**

Tạo `lugo-web-client/verify-history.mjs`:

```js
import { chromium } from 'playwright'
const b = await chromium.launch()
const p = await b.newPage({ viewport: { width: 420, height: 860 }, deviceScaleFactor: 2 })
const errors = []
p.on('pageerror', (e) => errors.push(String(e)))
p.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text().slice(0, 110)) })

await p.goto('http://localhost:5173/')
await p.fill('input[aria-label="Tên đăng nhập"]', 'e2e-user')
await p.fill('input[aria-label="Mật khẩu"]', 'pw12345678')
await p.click('button[type="submit"]')
await p.waitForTimeout(1500)

await p.click('text=Lịch sử')
await p.waitForTimeout(1200)
console.log('số phiên:', await p.locator('.his__row').count())
await p.screenshot({ path: 'shots/his-list.png' })

if (await p.locator('.his__row').count() > 0) {
  await p.locator('.his__row').first().click()
  await p.waitForTimeout(1200)
  console.log('số lượt trong transcript:', await p.locator('.his__turn').count())
  await p.screenshot({ path: 'shots/his-detail.png' })
  await p.click('text=Xoá')
  await p.waitForTimeout(400)
  console.log('có hỏi lại trước khi xoá?', (await p.locator('text=Xoá thật').count()) > 0)
  await p.screenshot({ path: 'shots/his-confirm.png' })
}
console.log('lỗi trang:', errors.length ? errors : 'không có')
await b.close()
```

Chạy: `node verify-history.mjs`

- [ ] **Step 3: NHÌN vào ảnh**

- Danh sách có hiện preview + thời gian tương đối + số tin nhắn không?
- Bấm vào một phiên có ra transcript đúng không? Nhãn "BẠN"/"LUGO" rõ chứ?
- **Không** có nút phát audio nào chứ? (Không được có — không có gì để phát.)
- Nút Xoá có hỏi lại không?
- Nền có phải kem không? Có gì màu cam không? (**Không được có** — màn này không có hành động chính.)
- Nav có che nội dung không? Nav có đủ 3 mục Nói / Lịch sử / Thiết bị không?
- Ở 420px có gì tràn không? Preview dài có bị cắt gọn một dòng không?

**Sửa những gì thấy sai, chụp lại, nhìn lại.**

- [ ] **Step 4: Thử lỗi thật**

Xoá một phiên rồi bấm lại vào nó (dùng nút back của trình duyệt hoặc gọi lại `getSession` với id đã xoá) → phải hiện **tiếng Việt**, không phải chuỗi của server.

- [ ] **Step 5: Commit**

```bash
cd lugo-web-client
git add verify-history.mjs
git commit -m "test(history): xác minh danh sách và transcript thật"
```

## Ngoài phạm vi plan này

- **Audio phát lại** — xem đầu plan; cần thay đổi ở backend, không phải việc UI
- Tools (và mục nav của nó)
- Phân trang / tải thêm — hiện lấy 50 phiên gần nhất. Thêm khi có người thật chạm trần.
- Tìm kiếm trong lịch sử
- Xoá hàng loạt (`/v1/sessions/bulk_delete` có sẵn, chưa lộ ra UI)
