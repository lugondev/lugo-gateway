# Lugo Web Client — Màn Tools (Phase 1f) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hai công cụ thủ công: đổi một file ghi âm thành chữ, và đọc một đoạn chữ thành tiếng. Màn cuối của bộ 4.

**Architecture:** `src/api/tools.ts` gọi API (qua `apiFetch`). `Tools.tsx` là UI hai khối độc lập nhau.

**Spec:** `docs/superpowers/specs/2026-07-16-lugo-web-client-design.md`

## Ba quyết định đã chốt — đọc trước khi code

**1. KHÔNG có ô chọn engine/model/voice.** Spec ghi Tools là "dạng đơn giản hoá của batch hiện có". Chọn engine STT là mối bận tâm của quản trị viên; để nó lọt vào app người dùng cuối là đúng thứ nguyên tắc "không role trong UI" muốn tránh. Server đã có mặc định của nó (`system_config.engines.default_stt_engine`, `default_tts_engine`) — dùng chúng. **Đừng gọi `/v1/stt/engines` hay `/v1/tts/voices`.** Chúng tồn tại và giờ đã có auth, nhưng không phải việc của màn này.

**2. Dùng `audio_url` — và đây là sự thiếu nhất quán CÓ CHỦ ĐÍCH.** `POST /v1/tts/synthesize` trả `audio_url` trỏ vào `/artifacts`, vốn **không có auth** (đã đo: `/artifacts/x.wav` → 404 chứ không 401). Ta chọn `audio_out=opus` cho Talk chính là để né chỗ này. Ở đây chấp nhận vì: audio là đoạn chữ do chính người dùng vừa gõ ra, không phải cuộc trò chuyện riêng tư; và `/v1/tts/synthesize` không có đường Opus. **Ghi vào spec như phơi nhiễm đã biết, đừng lặng lẽ ship.**

**3. Chỉ tải file lên, chưa ghi âm.** `MediaRecorder` cho ra webm/opus, mà `provider.transcribe_bytes()` chưa chắc nhận được định dạng đó — chưa đo nên chưa hứa. Ghi âm là việc sau.

## Nền tảng đã có (đừng dựng lại)

Repo con `lugo-web-client` @ `ab384b7`, 70/70 test:
- `src/api/client.ts` — `apiFetch(path, init?)`. **Mọi lời gọi API qua đây.**
- `src/api/devices.ts`, `src/api/history.ts` — hai mẫu tốt để bắt chước.
- `src/components/Nav.tsx` — `type Screen = 'talk' | 'history' | 'devices'`.
- `src/routes/Devices.tsx` / `History.tsx` — mẫu bố cục màn nền kem.

## API thật (đã đọc code; đã curl xác minh auth ở `6269e58`)

**Vừa siết ở `6269e58`:** `/v1/stt` và `/v1/tts` trước đó **không có auth** — ai cũng gọi được, chạy suy luận ML và tiêu tiền API của chủ dự án. Giờ nằm trong `_USER_PREFIXES`. Đã curl: không token → 401; có bearer → 200.

| Endpoint | Gửi | Nhận |
|---|---|---|
| `POST /v1/stt/transcribe` | multipart: `audio` (file). Các field khác (`engine`, `language`, `denoise`, `vad`, `segment`) đều optional — **bỏ trống hết**, server dùng mặc định. | `{success, data: {engine, text, is_final, confidence}}` |
| `POST /v1/tts/synthesize` | JSON `{text}`. Các field khác optional — **chỉ gửi `text`**. | `{success, data: {engine, sample_rate, audio_url, duration_seconds, job_id, text}}` |

**Quan trọng về `TTSRequest`:** `engine` có default `"omnivoice"` trong schema. Nếu client **không** gửi `engine`, server dùng `"omnivoice"` — **không phải** engine mặc định trong system config. Đó là hành vi của schema, không phải bug; nhưng nghĩa là bỏ trống `engine` KHÔNG có nghĩa "để server tự chọn". Task 4 phải kiểm chứng bằng gateway thật xem `{text}` trần có ra tiếng không. **Nếu 500 vì omnivoice chưa cài: DỪNG và báo cáo** — đừng tự chọn engine khác, đó là quyết định của chủ dự án.

**Lỗi:** STT trả 400 (RuntimeError) hoặc 500 kèm `detail`; TTS trả lỗi qua `AppError` → `{success: false, error}`.

## Global Constraints

- Chỉ token màu đã có. **Không thêm màu mới.** Nền kem — **không** đặt `data-surface="talk"`.
- Cam **chỉ** cho hành động chính. Màn này có **hai** hành động chính (một cho mỗi công cụ) — cả hai được dùng cam. Không tô cam gì khác.
- **Không role/admin trong UI.** Không ô chọn engine.
- Mọi lời gọi qua `apiFetch`. Không `fetch` trực tiếp, không tự đọc token.
- **Lỗi phải là tiếng Việt.** Một vòng trước đã lỡ hiện chuỗi tiếng Anh của server cho người dùng cuối.
- Padding đáy `88px` cho nav cố định.
- Responsive; focus bàn phím nhìn thấy được.
- Chạy `pnpm test` trong `lugo-web-client/`. Hiện 70 test pass — phải giữ.
- **Không** `git push`. Commit trong repo con.

---

### Task 1: Lớp API tools

**Files:**
- Create: `src/api/tools.ts`, `src/api/tools.test.ts`

**Interfaces:**
- Produces:
  - `transcribeFile(file: File): Promise<string>` — trả text
  - `synthesize(text: string): Promise<{audioUrl: string; durationSeconds: number | null}>`

- [ ] **Step 1: Viết test thất bại**

Tạo `src/api/tools.test.ts`:

```ts
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { synthesize, transcribeFile } from './tools'
import { saveTokens } from './tokens'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('tools api', () => {
  beforeEach(() => {
    localStorage.clear()
    saveTokens('acc', 'ref')
    vi.restoreAllMocks()
  })

  it('transcribeFile trả text', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      jsonResponse({ success: true, data: { engine: 'x', text: 'xin chào', is_final: true } }),
    ))
    expect(await transcribeFile(new File(['x'], 'a.wav'))).toBe('xin chào')
  })

  it('transcribeFile gửi multipart với field tên "audio"', async () => {
    const f = vi.fn().mockResolvedValue(jsonResponse({ success: true, data: { text: 'ok' } }))
    vi.stubGlobal('fetch', f)
    await transcribeFile(new File(['x'], 'a.wav'))
    expect(String(f.mock.calls[0][0])).toContain('/v1/stt/transcribe')
    const body = f.mock.calls[0][1].body as FormData
    expect(body).toBeInstanceOf(FormData)
    expect(body.get('audio')).toBeInstanceOf(File)
  })

  it('transcribeFile KHÔNG tự đặt Content-Type', async () => {
    // Trình duyệt phải tự sinh boundary cho multipart. Tự đặt Content-Type
    // là làm hỏng boundary và server sẽ không parse được.
    const f = vi.fn().mockResolvedValue(jsonResponse({ success: true, data: { text: 'ok' } }))
    vi.stubGlobal('fetch', f)
    await transcribeFile(new File(['x'], 'a.wav'))
    const h = new Headers(f.mock.calls[0][1].headers)
    expect(h.get('Content-Type')).toBeNull()
  })

  it('transcribeFile gắn bearer (đi qua apiFetch)', async () => {
    const f = vi.fn().mockResolvedValue(jsonResponse({ success: true, data: { text: 'ok' } }))
    vi.stubGlobal('fetch', f)
    await transcribeFile(new File(['x'], 'a.wav'))
    expect(new Headers(f.mock.calls[0][1].headers).get('Authorization')).toBe('Bearer acc')
  })

  it('transcribeFile lỗi thì ném tiếng Việt', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ detail: 'STT failed (vosk): boom' }, 500)))
    await expect(transcribeFile(new File(['x'], 'a.wav'))).rejects.toThrow(/không|thất bại/i)
  })

  it('synthesize gửi CHỈ text', async () => {
    const f = vi.fn().mockResolvedValue(jsonResponse({
      success: true, data: { engine: 'e', sample_rate: 24000, audio_url: '/artifacts/a.wav', duration_seconds: 1.5 },
    }))
    vi.stubGlobal('fetch', f)
    const r = await synthesize('xin chào')
    expect(String(f.mock.calls[0][0])).toContain('/v1/tts/synthesize')
    // Không gửi engine/voice: chọn engine là việc của quản trị, không phải
    // của người dùng cuối.
    expect(JSON.parse(f.mock.calls[0][1].body)).toEqual({ text: 'xin chào' })
    expect(r.audioUrl).toContain('/artifacts/a.wav')
    expect(r.durationSeconds).toBe(1.5)
  })

  it('synthesize trả URL tuyệt đối để thẻ audio dùng được', async () => {
    // audio_url của server là đường dẫn tương đối. Client chạy ở domain KHÁC,
    // nên để nguyên sẽ trỏ vào chính domain của client -> 404.
    const f = vi.fn().mockResolvedValue(jsonResponse({
      success: true, data: { audio_url: '/artifacts/a.wav', duration_seconds: null },
    }))
    vi.stubGlobal('fetch', f)
    const r = await synthesize('x')
    expect(r.audioUrl.startsWith('http')).toBe(true)
  })

  it('synthesize lỗi thì ném tiếng Việt', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ success: false, error: 'engine not found' }, 400)))
    await expect(synthesize('x')).rejects.toThrow(/không|thất bại/i)
  })
})
```

Test `trả URL tuyệt đối` là quan trọng nhất: client chạy domain khác API, nên `/artifacts/a.wav` để nguyên sẽ trỏ vào domain của client.

- [ ] **Step 2: Chạy test để xác nhận nó fail**

Run: `cd lugo-web-client && pnpm test`
Expected: FAIL — không tìm thấy module `./tools`

- [ ] **Step 3: Implement**

Tạo `src/api/tools.ts`:

```ts
import { ApiUrl, apiFetch } from './client'

async function viError(resp: Response, fallback: string): Promise<Error> {
  if (resp.status === 401 || resp.status === 403) {
    return new Error('Phiên đăng nhập đã hết hạn. Hãy đăng nhập lại.')
  }
  return new Error(fallback)
}

export async function transcribeFile(file: File): Promise<string> {
  const body = new FormData()
  body.append('audio', file)
  // Chỉ gửi file. engine/language/denoise/vad đều optional -> server dùng mặc
  // định của nó. Chọn engine là việc của quản trị, không phải người dùng cuối.
  //
  // KHÔNG đặt Content-Type: trình duyệt phải tự sinh boundary cho multipart.
  const resp = await apiFetch('/v1/stt/transcribe', { method: 'POST', body })
  if (!resp.ok) {
    throw await viError(resp, 'Không nhận dạng được file này. Thử file wav hoặc mp3 khác xem sao.')
  }
  const json = await resp.json()
  return (json.data?.text ?? '') as string
}

export async function synthesize(text: string): Promise<{
  audioUrl: string
  durationSeconds: number | null
}> {
  const resp = await apiFetch('/v1/tts/synthesize', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
  })
  if (!resp.ok) {
    throw await viError(resp, 'Không đọc được đoạn này. Thử lại sau ít phút.')
  }
  const json = await resp.json()
  const url = json.data?.audio_url as string | undefined
  if (!url) throw new Error('Máy chủ không trả về audio.')
  return {
    // audio_url là đường dẫn tương đối của API. Client chạy domain KHÁC, nên
    // phải ghép base URL vào, không thì thẻ <audio> trỏ vào domain client -> 404.
    audioUrl: url.startsWith('http') ? url : ApiUrl(url),
    durationSeconds: (json.data?.duration_seconds ?? null) as number | null,
  }
}
```

- [ ] **Step 4: Chạy test để xác nhận pass**

Run: `cd lugo-web-client && pnpm test`
Expected: PASS (9 test mới, tổng 79)

- [ ] **Step 5: Commit**

```bash
cd lugo-web-client
git add src/api/tools.ts src/api/tools.test.ts
git commit -m "feat(tools): lớp API chuyển file thành chữ và chữ thành tiếng"
```

---

### Task 2: Màn Tools

**Files:**
- Create: `src/routes/Tools.tsx`, `src/routes/Tools.css`

- [ ] **Step 1: Tools.css**

Tạo `src/routes/Tools.css`:

```css
.tool {
  min-height: 100dvh;
  padding: 20px 20px 88px;
  max-width: 34rem;
  margin: 0 auto;
}

.tool__h {
  font-size: 1.5rem;
  font-weight: 600;
  margin: 8px 0 4px;
}

.tool__sub {
  margin: 0 0 28px;
  opacity: 0.6;
  font-size: 0.9375rem;
  line-height: 1.5;
}

.tool__card {
  border: 1px solid var(--lugo-cream-deep);
  border-radius: 12px;
  padding: 18px 16px;
  margin-bottom: 16px;
  display: grid;
  gap: 12px;
}

.tool__card-h {
  font-size: 1rem;
  font-weight: 600;
  margin: 0;
}

.tool__hint {
  font-size: 0.8125rem;
  opacity: 0.55;
  margin: 0;
  line-height: 1.5;
}

.tool__file {
  font: inherit;
  font-size: 0.875rem;
}
.tool__file:focus-visible {
  outline: 2px solid var(--lugo-accent-warm);
  outline-offset: 2px;
}

.tool__area {
  font: inherit;
  padding: 12px 14px;
  border-radius: 10px;
  border: 1px solid var(--lugo-cream-deep);
  background: transparent;
  color: inherit;
  resize: vertical;
  min-height: 96px;
}
.tool__area:focus-visible {
  outline: 2px solid var(--lugo-accent-warm);
  outline-offset: 2px;
  border-color: transparent;
}

.tool__btn {
  font: inherit;
  font-size: 0.9375rem;
  font-weight: 500;
  padding: 12px 20px;
  min-height: 44px;
  border-radius: 999px;
  border: 0;
  cursor: pointer;
  /* Mỗi công cụ có một hành động chính -- đây là hai chỗ duy nhất dùng cam. */
  background: var(--lugo-accent-gradient);
  color: #111;
}
.tool__btn:disabled {
  background: none;
  border: 1px solid currentColor;
  color: inherit;
  opacity: 0.4;
  cursor: default;
}
.tool__btn:focus-visible {
  outline: 2px solid var(--lugo-accent-warm);
  outline-offset: 3px;
}

.tool__out {
  margin: 0;
  padding: 12px 14px;
  border-radius: 10px;
  background: color-mix(in srgb, currentColor 5%, transparent);
  line-height: 1.6;
  white-space: pre-wrap;
}

.tool__err {
  color: var(--lugo-danger);
  font-size: 0.9375rem;
  margin: 0;
}

.tool__audio { width: 100%; }
```

- [ ] **Step 2: Tools.tsx**

Tạo `src/routes/Tools.tsx`:

```tsx
import { useState } from 'react'
import { synthesize, transcribeFile } from '../api/tools'
import './Tools.css'

function ToText() {
  const [file, setFile] = useState<File | null>(null)
  const [text, setText] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function run() {
    if (!file) return
    setBusy(true)
    setError(null)
    setText('')
    try {
      const t = await transcribeFile(file)
      setText(t || '(không nghe ra chữ nào)')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Không nhận dạng được')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="tool__card">
      <h2 className="tool__card-h">Ghi âm thành chữ</h2>
      <p className="tool__hint">Chọn một file wav hoặc mp3. Lugo sẽ nghe và gõ lại.</p>
      <input
        className="tool__file"
        type="file"
        accept="audio/*"
        aria-label="Chọn file ghi âm"
        onChange={(e) => {
          setFile(e.target.files?.[0] ?? null)
          setText('')
          setError(null)
        }}
      />
      {error && (
        <p className="tool__err" role="alert">
          {error}
        </p>
      )}
      {text && <p className="tool__out">{text}</p>}
      <button className="tool__btn" onClick={run} disabled={!file || busy}>
        {busy ? 'Đang nghe...' : 'Chuyển thành chữ'}
      </button>
    </section>
  )
}

function ToVoice() {
  const [input, setInput] = useState('')
  const [url, setUrl] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function run() {
    setBusy(true)
    setError(null)
    setUrl(null)
    try {
      const r = await synthesize(input.trim())
      setUrl(r.audioUrl)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Không đọc được')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="tool__card">
      <h2 className="tool__card-h">Chữ thành tiếng</h2>
      <p className="tool__hint">Gõ gì đó, Lugo sẽ đọc lên.</p>
      <textarea
        className="tool__area"
        aria-label="Đoạn chữ cần đọc"
        value={input}
        onChange={(e) => setInput(e.target.value)}
        placeholder="Hôm nay trời đẹp quá..."
      />
      {error && (
        <p className="tool__err" role="alert">
          {error}
        </p>
      )}
      {url && <audio className="tool__audio" controls src={url} autoPlay />}
      <button className="tool__btn" onClick={run} disabled={!input.trim() || busy}>
        {busy ? 'Đang đọc...' : 'Đọc lên'}
      </button>
    </section>
  )
}

export function Tools() {
  return (
    <main className="tool">
      <h1 className="tool__h">Công cụ</h1>
      <p className="tool__sub">Hai việc lặt vặt, không cần mở cuộc trò chuyện.</p>
      <ToText />
      <ToVoice />
    </main>
  )
}
```

- [ ] **Step 3: Build + test**

Run: `cd lugo-web-client && pnpm test && pnpm build`
Expected: 79 test pass, build sạch

- [ ] **Step 4: Commit**

```bash
cd lugo-web-client
git add src/routes/Tools.tsx src/routes/Tools.css
git commit -m "feat(tools): màn công cụ thủ công"
```

---

### Task 3: Nối vào Nav

**Files:**
- Modify: `src/components/Nav.tsx`, `src/App.tsx`

- [ ] **Step 1: Nav**

Trong `src/components/Nav.tsx`:
- Đổi `Screen` thành `'talk' | 'history' | 'devices' | 'tools'`.
- Thêm `{ id: 'tools', label: 'Công cụ' }` vào **cuối** `ITEMS`.

Giờ nav mới đủ 4 mục như spec vẽ — vì cả 4 màn đã tồn tại thật. Đây là lần đầu điều đó đúng.

- [ ] **Step 2: App**

Trong `src/App.tsx`, import `Tools` và thêm nhánh. Với 4 màn, chuỗi ternary lồng nhau bắt đầu khó đọc — dùng bản đồ:

```tsx
const SCREENS: Record<Screen, () => JSX.Element> = {
  talk: Talk,
  history: History,
  devices: Devices,
  tools: Tools,
}
```
rồi render `{(() => { const S = SCREENS[screen]; return <S /> })()}` hoặc cách tương đương gọn hơn tuỳ bạn. Giữ đúng hành vi hiện tại; đừng đổi gì khác.

- [ ] **Step 3: Build + test**

Run: `cd lugo-web-client && pnpm test && pnpm build`
Expected: 79 test pass, build sạch

- [ ] **Step 4: Commit**

```bash
cd lugo-web-client
git add src/components/Nav.tsx src/App.tsx
git commit -m "feat(ui): thêm Công cụ vào nav — đủ 4 màn"
```

---

### Task 4: Xác minh thật

**Rủi ro đã biết cần kiểm chứng ở đây:** `TTSRequest.engine` mặc định `"omnivoice"` trong schema. Gửi `{text}` trần nghĩa là server dùng omnivoice, **không phải** engine mặc định trong system config. Nếu omnivoice chưa cài, `synthesize` sẽ lỗi.

- [ ] **Step 1: Kiểm bằng curl trước khi động vào UI**

```bash
cd /Users/lugon/code/speech-text-transformer
.venv/bin/uvicorn app.main:app --app-dir apps/api_gateway --port 8000 &
TOK=$(curl -s -X POST localhost:8000/api/auth/token -H 'Content-Type: application/json' \
  -d '{"username":"e2e-user","password":"pw12345678"}' \
  | .venv/bin/python -c "import sys,json; print(json.load(sys.stdin)['data']['access_token'])")
curl -s -X POST localhost:8000/v1/tts/synthesize -H "Authorization: Bearer $TOK" \
  -H 'Content-Type: application/json' -d '{"text":"xin chao"}' | head -c 300
```

**Nếu lỗi (engine chưa cài / 500): DỪNG và báo cáo.** Đừng tự chọn engine khác — engine nào là mặc định cho người dùng cuối là quyết định của chủ dự án, không phải của bạn.

- [ ] **Step 2: Chụp và nhìn**

Tạo `lugo-web-client/verify-tools.mjs`:

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

await p.click('text=Công cụ')
await p.waitForTimeout(700)
await p.screenshot({ path: 'shots/tools-empty.png' })
console.log('nav co du 4 muc?', await p.locator('.nav__tabs button').count())

await p.fill('textarea[aria-label="Đoạn chữ cần đọc"]', 'Xin chao, day la Lugo.')
await p.click('text=Đọc lên')
await p.waitForTimeout(12000)
const src = await p.locator('audio').getAttribute('src').catch(() => null)
console.log('audio src:', src)
console.log('src co tuyet doi (tro dung API) khong?', String(src).startsWith('http://localhost:8000'))
await p.screenshot({ path: 'shots/tools-voice.png' })

console.log('loi trang:', errors.length ? errors : 'khong co')
await b.close()
```

Chạy: `node verify-tools.mjs`

- [ ] **Step 3: NHÌN vào ảnh và kiểm**

- Thẻ `<audio>` có `src` **tuyệt đối** trỏ tới API (`http://localhost:8000/artifacts/...`) không? Nếu là đường dẫn tương đối thì nó trỏ vào domain client và sẽ 404 — đây là lỗi dễ mắc nhất của màn này.
- Có nghe được không (kiểm `duration` của thẻ audio > 0)?
- Nav có đủ **4** mục không?
- Nền kem chứ? Cam chỉ ở **hai** nút chính chứ?
- Nút bị disabled khi chưa nhập gì chứ?
- Ở 420px có gì tràn không? Nav có che nút không?

**Sửa những gì thấy sai, chụp lại, nhìn lại.**

- [ ] **Step 4: Thử lỗi thật**

Tải lên một file không phải audio (ví dụ một file .txt đổi tên thành .wav) → phải hiện **tiếng Việt**, không phải `STT failed (vosk): ...`.

- [ ] **Step 5: Commit**

```bash
cd lugo-web-client
git add verify-tools.mjs
git commit -m "test(tools): xác minh chuyển chữ thành tiếng thật"
```

## Ngoài phạm vi plan này

- **Ghi âm trực tiếp từ mic** — `MediaRecorder` cho ra webm/opus, chưa đo xem `transcribe_bytes()` có nhận không. Đừng hứa trước khi đo.
- Chọn engine/model/voice — là mối bận tâm của quản trị, không phải người dùng cuối
- Tải file audio về máy
- Hàng đợi nhiều file
- **Phơi nhiễm `/artifacts`**: audio sinh ra ở đây nằm ở URL công khai không auth. Chấp nhận vì nội dung là chữ người dùng tự gõ. Ghi vào spec.
