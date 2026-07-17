# Lugo Web Client — Lớp Audio + WS (Phase 1b) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lớp audio + WS transport cho hội thoại realtime: thu mic → PCM16 16kHz đẩy lên socket, nhận Opus 24kHz về giải mã và phát, có barge-in. **Không có UI** — plan sau (1c) dựng màn Talk trên lớp này.

**Architecture:** Ba module tách bạch, không module nào biết React: `mic.ts` (thu), `player.ts` (giải mã + xếp lịch phát), `conversation.ts` (WS + máy trạng thái). Spec yêu cầu lớp audio test được độc lập với React — đó là lý do chia thế này.

**Tech Stack:** AudioWorklet (thu), WebCodecs `AudioDecoder` (giải mã Opus), Web Audio API (phát), Vitest.

**Spec:** `docs/superpowers/specs/2026-07-16-lugo-web-client-design.md`

## Giao thức thật của backend (đã đo từ code, không phải giả định)

Endpoint: `GET /v1/conversation/stream` (WebSocket).

**Xác thực:** subprotocol `["bearer", <access_token>]`. Server echo lại `"bearer"`. **Không** truyền token qua query string.

**Query params dùng ở plan này:**
- `audio_out=opus` — bắt buộc. Audio về dưới dạng binary frame trên chính socket đã xác thực, không sinh URL công khai nào. (`/artifacts` hiện KHÔNG có auth — đã xác minh bằng curl: `/artifacts/x.wav` trả 404 chứ không phải 401 — nên `audio_out=url` sẽ khiến audio hội thoại ai có URL cũng nghe được. Đó là lý do chọn opus.)
- `output=audio,text` — muốn cả audio lẫn text về.
- `sample_rate=16000` — mic vào.
- `output_sample_rate=24000` — audio ra.
- `session_id=<uuid>` — tùy chọn, để resume.

**Client → server:**
- Binary frame: PCM16 little-endian, 16kHz, mono.
- `{"type":"abort"}` — hủy lượt đang chạy (barge-in).
- `{"type":"text","text":"..."}` — nhập bằng chữ.
- `{"type":"reset"}`, `{"type":"flush"}`, `{"type":"end"}`.

**Server → client (JSON, khóa `event`):**

| event | payload | ý nghĩa |
|---|---|---|
| `engines_ready` | — | engine đã nạp xong, bắt đầu nói được |
| `speech_start` | — | server nghe thấy người dùng bắt đầu nói |
| `speech_end` | `speech_ms` | hết câu, sắp xử lý |
| `user_transcript` | `turn`, `text`, `engine` | STT ra chữ |
| `processing` | `turn` | đang nghĩ |
| `response_text` | `turn`, `chunk_index`, `text`, `responder` | một câu trả lời |
| `audio_start` | `turn`, `chunk_index`, `codec="opus"`, `sample_rate`, `frames` | sắp gửi `frames` packet Opus |
| `audio_end` | `turn`, `chunk_index` | hết packet của chunk này |
| `turn_done` | `turn`, có thể có `skipped` | xong lượt |
| `aborted` | `reason` | lượt bị hủy |
| `error` | `message` | lỗi |
| `warning` | `message` | cảnh báo |

**Binary frame nhận được** = một packet Opus thô (không bọc Ogg), thuộc `audio_start` gần nhất. Đếm đủ `frames` packet thì tới `audio_end`.

## Khả năng trình duyệt — ĐÃ ĐO THẬT, không phải giả định

Chạy Chromium 149 qua Playwright, trên `http://localhost` (secure context):

```
isSecureContext: true
AudioDecoder:    true
getUserMedia:    true
opus_24000:      true      <- cấu hình ta cần
opus_16000:      true
opus_48000:      true
```

**Cảnh báo về cách đo:** cùng phép thử đó chạy trên `about:blank` trả `AudioDecoder: false` và `getUserMedia: false` — **âm tính giả**, vì `about:blank` không phải secure context. Đừng feature-detect ngoài một trang được phục vụ thật, và đừng kết luận trình duyệt thiếu tính năng từ một phép đo như vậy.

**Ràng buộc triển khai suy ra từ đây: client BẮT BUỘC chạy trên HTTPS ở production** (hoặc `localhost` khi dev). Không có secure context thì cả WebCodecs lẫn mic đều không tồn tại, và Talk chết hoàn toàn — không phải suy giảm từ từ. Điều này phải vào cấu hình deploy trước khi client lên sóng.

## Global Constraints

- **Opus ra: 24000 Hz, mono, frame 60ms = 1440 samples.** PCM16 vào: **16000 Hz, mono**. Các số này lấy từ code server, dùng đúng.
- **`conversation_opus_pace = False`** (mặc định hiện tại) → server bắn cả loạt packet một lúc, **không** rải theo thời gian thực. Client PHẢI tự đệm và xếp lịch phát; không được cho rằng packet đến đúng nhịp phát.
- Không module nào trong `src/audio/` được import React hay routing. Chúng nhận callback, không tự điều hướng.
- Không hardcode URL. Base URL từ `import.meta.env.VITE_API_BASE_URL`; WS URL suy ra từ nó (`http→ws`, `https→wss`).
- Token lấy qua lớp đã có, không tự đọc localStorage.
- Chạy test: `pnpm test` trong `lugo-web-client/`.
- **Không** `git push`. Commit trong repo con `lugo-web-client/`.
- Repo con hiện ở commit `a98bab4`, 17/17 test pass.

## File Structure

| File | Trách nhiệm |
|---|---|
| `src/audio/capability.ts` | Kiểm tra trình duyệt có đủ khả năng không. Nơi duy nhất feature-detect. |
| `src/audio/pcm-worklet.ts` | Mã AudioWorklet (chạy trong worklet thread), gói dưới dạng chuỗi. |
| `src/audio/mic.ts` | Thu mic → PCM16 16kHz → callback. |
| `src/audio/player.ts` | Nhận packet Opus → giải mã → xếp lịch phát. Có `stop()` cho barge-in. |
| `src/audio/conversation.ts` | WS: nối, gửi audio, phân giải event, máy trạng thái, barge-in. |

---

### Task 1: Kiểm tra khả năng trình duyệt

**Rủi ro thật, phải xử lý trước:** WebCodecs `AudioDecoder` không có ở mọi trình duyệt. Nếu thiếu, cả lớp phát vô dụng. Phải phát hiện sớm và nói thật với người dùng, thay vì để micro bật lên rồi im lặng không nghe thấy gì.

**Files:**
- Create: `src/audio/capability.ts`
- Test: `src/audio/capability.test.ts`

**Interfaces:**
- Produces: `checkAudioSupport(): {ok: true} | {ok: false, missing: string[]}`

- [ ] **Step 1: Viết test thất bại**

Tạo `src/audio/capability.test.ts`:

```ts
import { afterEach, describe, expect, it, vi } from 'vitest'
import { checkAudioSupport } from './capability'

afterEach(() => vi.unstubAllGlobals())

describe('checkAudioSupport', () => {
  it('đủ khả năng thì ok', () => {
    vi.stubGlobal('AudioDecoder', class {})
    vi.stubGlobal('AudioContext', class {})
    vi.stubGlobal('navigator', { mediaDevices: { getUserMedia: () => {} } })
    expect(checkAudioSupport()).toEqual({ ok: true })
  })

  it('thiếu AudioDecoder thì báo rõ thiếu gì', () => {
    vi.stubGlobal('AudioDecoder', undefined)
    vi.stubGlobal('AudioContext', class {})
    vi.stubGlobal('navigator', { mediaDevices: { getUserMedia: () => {} } })
    const r = checkAudioSupport()
    expect(r.ok).toBe(false)
    expect(r.ok === false && r.missing).toContain('AudioDecoder')
  })

  it('thiếu getUserMedia thì báo rõ', () => {
    vi.stubGlobal('AudioDecoder', class {})
    vi.stubGlobal('AudioContext', class {})
    vi.stubGlobal('navigator', {})
    const r = checkAudioSupport()
    expect(r.ok).toBe(false)
    expect(r.ok === false && r.missing).toContain('getUserMedia')
  })

  it('thiếu nhiều thứ thì liệt kê hết, không dừng ở cái đầu', () => {
    vi.stubGlobal('AudioDecoder', undefined)
    vi.stubGlobal('AudioContext', undefined)
    vi.stubGlobal('navigator', {})
    const r = checkAudioSupport()
    expect(r.ok === false && r.missing.length).toBe(3)
  })
})
```

- [ ] **Step 2: Chạy test để xác nhận nó fail**

Run: `cd lugo-web-client && pnpm test`
Expected: FAIL — không tìm thấy module `./capability`

- [ ] **Step 3: Implement**

Tạo `src/audio/capability.ts`:

```ts
// Nơi DUY NHẤT feature-detect. Gom về một chỗ để khi một trình duyệt thiếu thứ
// gì đó, ta nói thật được là thiếu cái nào -- thay vì để mic bật lên rồi người
// dùng ngồi nói vào khoảng không.
export type AudioSupport = { ok: true } | { ok: false; missing: string[] }

export function checkAudioSupport(): AudioSupport {
  const missing: string[] = []
  // WebCodecs: cần để giải mã Opus server đẩy về qua socket.
  if (typeof (globalThis as any).AudioDecoder === 'undefined') missing.push('AudioDecoder')
  if (typeof (globalThis as any).AudioContext === 'undefined') missing.push('AudioContext')
  if (!(globalThis as any).navigator?.mediaDevices?.getUserMedia) missing.push('getUserMedia')
  return missing.length === 0 ? { ok: true } : { ok: false, missing }
}
```

- [ ] **Step 4: Chạy test để xác nhận pass**

Run: `cd lugo-web-client && pnpm test`
Expected: PASS (4 test mới + 17 cũ = 21)

- [ ] **Step 5: Xác minh THẬT trên trình duyệt (bắt buộc, không bỏ qua)**

Feature-detect trong jsdom không chứng minh được gì về trình duyệt thật. Tạo `lugo-web-client/probe.html`:

```html
<!doctype html>
<meta charset="utf-8">
<pre id="out">đang kiểm tra...</pre>
<script type="module">
const out = document.getElementById('out')
const lines = []
lines.push('AudioDecoder: ' + (typeof AudioDecoder !== 'undefined'))
lines.push('AudioContext: ' + (typeof AudioContext !== 'undefined'))
lines.push('getUserMedia: ' + !!navigator.mediaDevices?.getUserMedia)
if (typeof AudioDecoder !== 'undefined') {
  try {
    const cfg = { codec: 'opus', sampleRate: 24000, numberOfChannels: 1 }
    const sup = await AudioDecoder.isConfigSupported(cfg)
    lines.push('opus 24k mono decode: ' + sup.supported)
  } catch (e) {
    lines.push('opus check ném lỗi: ' + e)
  }
}
out.textContent = lines.join('\n')
</script>
```

Chạy `pnpm dev`, mở `/probe.html`, **chép lại kết quả thật vào report**.

Kỳ vọng (controller đã đo trên Chromium 149 + localhost): cả bốn dòng đều `true`. Nếu bạn thấy `false`, **kiểm tra `isSecureContext` trước khi kết luận** — đo ngoài secure context cho âm tính giả.

**Nếu `opus 24k mono decode` là false trên một trang được phục vụ thật với `isSecureContext: true`: DỪNG LẠI và báo cáo.** Đừng tự chọn phương án thay thế — chủ dự án đã cân nhắc và chọn Opus-qua-WS có ý thức (vì `/artifacts` không có auth), nên việc đổi hướng là quyết định của họ, không phải của bạn.

- [ ] **Step 6: Commit**

```bash
cd lugo-web-client
git add src/audio/capability.ts src/audio/capability.test.ts probe.html
git commit -m "feat(audio): feature-detect khả năng audio của trình duyệt"
```

---

### Task 2: Thu mic → PCM16 16kHz

**Files:**
- Create: `src/audio/pcm-worklet.ts`
- Create: `src/audio/mic.ts`
- Test: `src/audio/mic.test.ts`

**Interfaces:**
- Produces:
  - `floatToPcm16(input: Float32Array): ArrayBuffer` — export riêng để test được thuần túy
  - `class Mic { start(onFrame: (pcm: ArrayBuffer) => void): Promise<void>; stop(): void }`

- [ ] **Step 1: Viết test thất bại**

`AudioWorklet` và `getUserMedia` không tồn tại trong jsdom, nên **không** cố test `Mic` end-to-end ở đây — test phần thuần túy (chuyển đổi mẫu), là chỗ lỗi thật hay nấp. Xác minh `Mic` bằng tay trên trình duyệt ở Task 5.

Tạo `src/audio/mic.test.ts`:

```ts
import { describe, expect, it } from 'vitest'
import { floatToPcm16 } from './mic'

describe('floatToPcm16', () => {
  it('0 thành 0', () => {
    const out = new Int16Array(floatToPcm16(new Float32Array([0])))
    expect(out[0]).toBe(0)
  })

  it('biên +1 và -1 không tràn số', () => {
    const out = new Int16Array(floatToPcm16(new Float32Array([1, -1])))
    expect(out[0]).toBe(32767)
    expect(out[1]).toBe(-32768)
  })

  it('cắt ngưỡng giá trị ngoài [-1,1] thay vì để quấn vòng', () => {
    // Quấn vòng biến đỉnh sóng thành tiếng nổ lách tách -- lỗi kinh điển.
    const out = new Int16Array(floatToPcm16(new Float32Array([2, -2, 1.5])))
    expect(out[0]).toBe(32767)
    expect(out[1]).toBe(-32768)
    expect(out[2]).toBe(32767)
  })

  it('giữ đúng số mẫu', () => {
    const out = new Int16Array(floatToPcm16(new Float32Array(480)))
    expect(out.length).toBe(480)
  })

  it('little-endian', () => {
    const buf = floatToPcm16(new Float32Array([1]))
    const bytes = new Uint8Array(buf)
    // 32767 = 0x7FFF -> LE: FF 7F
    expect(bytes[0]).toBe(0xff)
    expect(bytes[1]).toBe(0x7f)
  })
})
```

- [ ] **Step 2: Chạy test để xác nhận nó fail**

Run: `cd lugo-web-client && pnpm test`
Expected: FAIL — không tìm thấy module `./mic`

- [ ] **Step 3: Implement worklet**

Tạo `src/audio/pcm-worklet.ts`:

```ts
// Mã chạy TRONG AudioWorklet thread. Gói dưới dạng chuỗi rồi nạp qua blob URL:
// worklet cần một file riêng, mà ta không muốn thêm một entry point vào build
// chỉ vì mấy dòng này.
export const PCM_WORKLET_SRC = `
class PcmCapture extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0] && inputs[0][0]
    if (ch && ch.length) {
      // Copy: buffer gốc được worklet tái sử dụng ngay sau khi process() trả về.
      this.port.postMessage(new Float32Array(ch))
    }
    return true
  }
}
registerProcessor('pcm-capture', PcmCapture)
`
```

- [ ] **Step 4: Implement mic.ts**

Tạo `src/audio/mic.ts`:

```ts
import { PCM_WORKLET_SRC } from './pcm-worklet'

const SAMPLE_RATE = 16000

export function floatToPcm16(input: Float32Array): ArrayBuffer {
  const out = new Int16Array(input.length)
  for (let i = 0; i < input.length; i += 1) {
    // Cắt ngưỡng TRƯỚC khi nhân: bỏ qua bước này thì giá trị ngoài [-1,1] quấn
    // vòng và đỉnh sóng thành tiếng nổ lách tách.
    const s = Math.max(-1, Math.min(1, input[i]))
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff
  }
  return out.buffer
}

export class Mic {
  private ctx: AudioContext | null = null
  private stream: MediaStream | null = null

  async start(onFrame: (pcm: ArrayBuffer) => void): Promise<void> {
    // Xin AudioContext đúng 16kHz để trình duyệt tự resample -- rẻ hơn và
    // đúng hơn là ta tự viết resampler.
    this.ctx = new AudioContext({ sampleRate: SAMPLE_RATE })
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    })

    const blob = new Blob([PCM_WORKLET_SRC], { type: 'application/javascript' })
    const url = URL.createObjectURL(blob)
    try {
      await this.ctx.audioWorklet.addModule(url)
    } finally {
      URL.revokeObjectURL(url)
    }

    const source = this.ctx.createMediaStreamSource(this.stream)
    const node = new AudioWorkletNode(this.ctx, 'pcm-capture')
    node.port.onmessage = (e) => onFrame(floatToPcm16(e.data as Float32Array))
    source.connect(node)
    // Worklet phải nối tới destination mới chạy, nhưng ta KHÔNG muốn nghe lại
    // tiếng mình -- nối qua một gain 0.
    const mute = this.ctx.createGain()
    mute.gain.value = 0
    node.connect(mute)
    mute.connect(this.ctx.destination)
  }

  stop(): void {
    this.stream?.getTracks().forEach((t) => t.stop())
    this.stream = null
    void this.ctx?.close()
    this.ctx = null
  }
}
```

- [ ] **Step 5: Chạy test để xác nhận pass**

Run: `cd lugo-web-client && pnpm test`
Expected: PASS (5 test mới, tổng 26)

- [ ] **Step 6: Commit**

```bash
cd lugo-web-client
git add src/audio/mic.ts src/audio/pcm-worklet.ts src/audio/mic.test.ts
git commit -m "feat(audio): thu mic ra PCM16 16kHz"
```

---

### Task 3: Giải mã Opus + xếp lịch phát

Đây là module khó nhất. Server bắn cả loạt packet (`opus_pace=False`), nên phát theo kiểu "nhận được là phát" sẽ chồng chéo hết lên nhau. Phải xếp lịch theo đồng hồ của AudioContext.

**Files:**
- Create: `src/audio/player.ts`
- Test: `src/audio/player.test.ts`

**Interfaces:**
- Produces:
  - `nextStartTime(now: number, cursor: number): number` — export riêng để test thuần túy
  - `class Player { push(packet: ArrayBuffer): void; stop(): void; get playing(): boolean }`

- [ ] **Step 1: Viết test thất bại**

`AudioDecoder` không có trong jsdom. Test phần thuần túy — logic xếp lịch, chính là chỗ lỗi nấp.

Tạo `src/audio/player.test.ts`:

```ts
import { describe, expect, it } from 'vitest'
import { nextStartTime } from './player'

describe('nextStartTime', () => {
  it('lần đầu (cursor sau lưng) thì phát ngay', () => {
    // cursor=0 nghĩa là chưa phát gì; now=5 -> phải phát ~ngay, không phải lúc 0
    expect(nextStartTime(5, 0)).toBeGreaterThanOrEqual(5)
  })

  it('nối liền khi cursor còn ở tương lai', () => {
    // Đang phát tới giây 10, giờ mới 5 -> chunk sau phải nối vào 10, KHÔNG phát đè
    expect(nextStartTime(5, 10)).toBe(10)
  })

  it('cursor tụt lại sau now thì bắt kịp về now', () => {
    // Máy lag/tab ngủ khiến cursor tụt lại. Phải phát ngay, không cố phát vào
    // quá khứ (Web Audio sẽ phát tất tật cùng lúc = tiếng ồn).
    expect(nextStartTime(20, 10)).toBeGreaterThanOrEqual(20)
  })

  it('không bao giờ trả thời điểm trong quá khứ', () => {
    for (const [now, cur] of [[0, 0], [100, 1], [3.3, 3.29], [1e6, 0]]) {
      expect(nextStartTime(now, cur)).toBeGreaterThanOrEqual(now)
    }
  })
})
```

- [ ] **Step 2: Chạy test để xác nhận nó fail**

Run: `cd lugo-web-client && pnpm test`
Expected: FAIL — không tìm thấy module `./player`

- [ ] **Step 3: Implement**

Tạo `src/audio/player.ts`:

```ts
const OUTPUT_SAMPLE_RATE = 24000

/** Thời điểm phát chunk kế tiếp trên đồng hồ AudioContext.
 *
 * Server gửi cả loạt packet một lúc (conversation_opus_pace=False), nên KHÔNG
 * được phát ngay khi nhận -- chúng sẽ chồng lên nhau. Nối đuôi theo cursor.
 * Nếu cursor đã tụt lại sau hiện tại (tab ngủ, máy lag) thì bắt kịp về now:
 * xếp lịch vào quá khứ khiến Web Audio phát tất cả cùng lúc thành tiếng ồn. */
export function nextStartTime(now: number, cursor: number): number {
  return Math.max(now, cursor)
}

export class Player {
  private ctx: AudioContext | null = null
  private decoder: AudioDecoder | null = null
  private cursor = 0
  private sources: AudioBufferSourceNode[] = []
  private timestamp = 0

  private ensure(): void {
    if (this.ctx) return
    this.ctx = new AudioContext()
    this.decoder = new AudioDecoder({
      output: (data: AudioData) => this.schedule(data),
      error: (e: Error) => console.error('opus decode', e),
    })
    this.decoder.configure({
      codec: 'opus',
      sampleRate: OUTPUT_SAMPLE_RATE,
      numberOfChannels: 1,
    })
  }

  private schedule(data: AudioData): void {
    const ctx = this.ctx
    if (!ctx) {
      data.close()
      return
    }
    const frames = data.numberOfFrames
    const pcm = new Float32Array(frames)
    data.copyTo(pcm, { planeIndex: 0, format: 'f32-planar' })
    data.close()

    const buf = ctx.createBuffer(1, frames, OUTPUT_SAMPLE_RATE)
    buf.copyToChannel(pcm, 0)
    const src = ctx.createBufferSource()
    src.buffer = buf
    src.connect(ctx.destination)

    const at = nextStartTime(ctx.currentTime, this.cursor)
    src.start(at)
    this.cursor = at + frames / OUTPUT_SAMPLE_RATE
    this.sources.push(src)
    src.onended = () => {
      this.sources = this.sources.filter((s) => s !== src)
    }
  }

  push(packet: ArrayBuffer): void {
    this.ensure()
    // Frame 60ms @ 24kHz. timestamp tính bằng micro giây.
    const chunk = new EncodedAudioChunk({
      type: 'key', // Opus: mọi frame đều độc lập
      timestamp: this.timestamp,
      data: packet,
    })
    this.timestamp += 60_000
    this.decoder?.decode(chunk)
  }

  get playing(): boolean {
    return this.sources.length > 0
  }

  /** Barge-in: im NGAY. Người dùng đã nói đè lên -- nghe tiếp là sai. */
  stop(): void {
    this.sources.forEach((s) => {
      try {
        s.stop()
      } catch {
        // đã dừng rồi
      }
    })
    this.sources = []
    this.cursor = 0
    this.timestamp = 0
    try {
      this.decoder?.close()
    } catch {
      // chưa configure
    }
    this.decoder = null
    void this.ctx?.close()
    this.ctx = null
  }
}
```

- [ ] **Step 4: Chạy test để xác nhận pass**

Run: `cd lugo-web-client && pnpm test`
Expected: PASS (4 test mới, tổng 30)

- [ ] **Step 5: Commit**

```bash
cd lugo-web-client
git add src/audio/player.ts src/audio/player.test.ts
git commit -m "feat(audio): giải mã Opus + xếp lịch phát, dừng ngay khi barge-in"
```

---

### Task 4: WS + máy trạng thái hội thoại

**Files:**
- Create: `src/audio/conversation.ts`
- Test: `src/audio/conversation.test.ts`

**Interfaces:**
- Consumes: `Mic` (Task 2), `Player` (Task 3), `getAccessToken` từ `../api/tokens`, `ApiUrl` từ `../api/client`
- Produces:
  - `wsUrl(path: string): string` — export riêng để test
  - `type TalkState = 'idle' | 'connecting' | 'listening' | 'thinking' | 'speaking' | 'error'`
  - `class Conversation` với `connect()`, `disconnect()`, `sendText(t)`, `abort()`, và callback `onState`, `onUserText`, `onReplyText`, `onError`

- [ ] **Step 1: Viết test thất bại**

Tạo `src/audio/conversation.test.ts`:

```ts
import { describe, expect, it } from 'vitest'
import { wsUrl } from './conversation'

describe('wsUrl', () => {
  it('http thành ws', () => {
    expect(wsUrl('http://localhost:8000', '/v1/conversation/stream')).toBe(
      'ws://localhost:8000/v1/conversation/stream',
    )
  })

  it('https thành wss', () => {
    expect(wsUrl('https://api.example.com', '/v1/conversation/stream')).toBe(
      'wss://api.example.com/v1/conversation/stream',
    )
  })

  it('không đụng tới phần còn lại của URL', () => {
    expect(wsUrl('https://api.example.com:8443/base', '/x')).toBe('wss://api.example.com:8443/base/x')
  })
})
```

- [ ] **Step 2: Chạy test để xác nhận nó fail**

Run: `cd lugo-web-client && pnpm test`
Expected: FAIL — không tìm thấy module `./conversation`

- [ ] **Step 3: Implement**

Tạo `src/audio/conversation.ts`:

```ts
import { ApiUrl } from '../api/client'
import { getAccessToken } from '../api/tokens'
import { Mic } from './mic'
import { Player } from './player'

export type TalkState = 'idle' | 'connecting' | 'listening' | 'thinking' | 'speaking' | 'error'

export function wsUrl(base: string, path: string): string {
  return `${base.replace(/^http/, 'ws')}${path}`
}

const PARAMS = new URLSearchParams({
  // Opus qua chính socket đã xác thực: audio_out=url sẽ trỏ vào /artifacts,
  // vốn KHÔNG có auth -- ai có URL cũng nghe được hội thoại.
  audio_out: 'opus',
  output: 'audio,text',
  sample_rate: '16000',
  output_sample_rate: '24000',
})

export interface ConversationCallbacks {
  onState?: (s: TalkState) => void
  onUserText?: (text: string) => void
  onReplyText?: (text: string) => void
  onError?: (message: string) => void
}

export class Conversation {
  private ws: WebSocket | null = null
  private mic = new Mic()
  private player = new Player()
  private state: TalkState = 'idle'

  constructor(private cb: ConversationCallbacks = {}) {}

  private setState(s: TalkState): void {
    if (this.state === s) return
    this.state = s
    this.cb.onState?.(s)
  }

  async connect(): Promise<void> {
    this.setState('connecting')
    const token = getAccessToken()
    if (!token) {
      this.setState('error')
      this.cb.onError?.('chưa đăng nhập')
      return
    }

    // Token đi qua subprotocol, KHÔNG qua query string: query string bị ghi vào
    // access log và lịch sử proxy.
    this.ws = new WebSocket(wsUrl(ApiUrl(''), `/v1/conversation/stream?${PARAMS}`), [
      'bearer',
      token,
    ])
    this.ws.binaryType = 'arraybuffer'

    this.ws.onmessage = (e) => this.onMessage(e)
    this.ws.onerror = () => {
      this.setState('error')
      this.cb.onError?.('mất kết nối')
    }
    this.ws.onclose = () => {
      this.mic.stop()
      this.player.stop()
      if (this.state !== 'error') this.setState('idle')
    }
    this.ws.onopen = async () => {
      await this.mic.start((pcm) => {
        if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(pcm)
      })
      this.setState('listening')
    }
  }

  private onMessage(e: MessageEvent): void {
    if (e.data instanceof ArrayBuffer) {
      this.player.push(e.data)
      return
    }
    let msg: Record<string, unknown>
    try {
      msg = JSON.parse(e.data as string)
    } catch {
      return
    }
    switch (msg.event) {
      case 'speech_start':
        // Barge-in: người dùng nói đè khi trợ lý đang nói -> im ngay và bảo
        // server bỏ lượt đang chạy. Không làm thế thì hai giọng chồng nhau.
        if (this.player.playing) {
          this.player.stop()
          this.send({ type: 'abort' })
        }
        this.setState('listening')
        break
      case 'speech_end':
      case 'processing':
        this.setState('thinking')
        break
      case 'user_transcript':
        this.cb.onUserText?.(String(msg.text ?? ''))
        break
      case 'response_text':
        this.cb.onReplyText?.(String(msg.text ?? ''))
        break
      case 'audio_start':
        this.setState('speaking')
        break
      case 'turn_done':
      case 'aborted':
        this.setState('listening')
        break
      case 'error':
        this.setState('error')
        this.cb.onError?.(String(msg.message ?? 'lỗi không rõ'))
        break
    }
  }

  private send(obj: unknown): void {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(obj))
  }

  sendText(text: string): void {
    this.send({ type: 'text', text })
    this.setState('thinking')
  }

  abort(): void {
    this.player.stop()
    this.send({ type: 'abort' })
  }

  disconnect(): void {
    this.mic.stop()
    this.player.stop()
    this.ws?.close()
    this.ws = null
    this.setState('idle')
  }
}
```

- [ ] **Step 4: Chạy test để xác nhận pass**

Run: `cd lugo-web-client && pnpm test`
Expected: PASS (3 test mới, tổng 33)

- [ ] **Step 5: Commit**

```bash
cd lugo-web-client
git add src/audio/conversation.ts src/audio/conversation.test.ts
git commit -m "feat(audio): WS hội thoại + máy trạng thái + barge-in"
```

---

### Task 5: Xác minh thật với gateway thật

Không có UI nên xác minh bằng một trang thử tối giản. **Đây là task quan trọng nhất của plan** — mọi test ở trên đều chạy trong jsdom, nơi không có audio thật.

**Files:**
- Create: `lugo-web-client/talk-probe.html`

- [ ] **Step 1: Trang thử**

Tạo `lugo-web-client/talk-probe.html`:

```html
<!doctype html>
<meta charset="utf-8">
<button id="go">Nối & nói</button>
<button id="stop">Dừng</button>
<pre id="log"></pre>
<script type="module">
import { Conversation } from '/src/audio/conversation.ts'
import { login } from '/src/api/auth.ts'

const log = (m) => (document.getElementById('log').textContent += m + '\n')
const conv = new Conversation({
  onState: (s) => log('state: ' + s),
  onUserText: (t) => log('bạn: ' + t),
  onReplyText: (t) => log('lugo: ' + t),
  onError: (e) => log('LỖI: ' + e),
})
document.getElementById('go').onclick = async () => {
  await login('e2e-user', 'pw12345678')
  await conv.connect()
}
document.getElementById('stop').onclick = () => conv.disconnect()
</script>
```

- [ ] **Step 2: Chạy gateway thật**

```bash
cd /Users/lugon/code/speech-text-transformer
.venv/bin/uvicorn app.main:app --app-dir apps/api_gateway --port 8000
```

Nếu chưa có user `e2e-user`:
```bash
curl -s -X POST localhost:8000/api/auth/signup -H 'Content-Type: application/json' \
  -d '{"username":"e2e-user","password":"pw12345678"}'
```

- [ ] **Step 3: Chạy client và thử bằng tai**

```bash
cd lugo-web-client && cp -n .env.example .env; pnpm dev
```

Mở `/talk-probe.html`, bấm "Nối & nói", cho phép mic, rồi kiểm tra và **chép kết quả thật vào report**:

- [ ] WS nối được (state: connecting → listening). Nếu 4401 → token/subprotocol sai.
- [ ] DevTools → Network → WS → Frames: thấy binary frame **đi lên** (mic) đều đặn.
- [ ] Nói một câu → thấy `bạn: <chữ>` đúng những gì bạn nói.
- [ ] **Nghe thấy tiếng trả lời** — đây là điều test không chứng minh được.
- [ ] Tiếng trả lời **không** chồng chéo/nhiễu/vấp (chứng tỏ xếp lịch đúng).
- [ ] **Barge-in:** nói đè khi trợ lý đang nói → tiếng im NGAY, state về listening.
- [ ] Không có URL `/artifacts` nào trong tab Network (chứng tỏ đi đúng đường opus).

**Nếu nghe thấy tiếng nổ lách tách, tiếng chồng nhau, hay im lặng: DỪNG và báo cáo cùng những gì bạn quan sát được.** Đừng vá bừa — trong 3 chỗ (mic, giải mã, xếp lịch) thì triệu chứng khác nhau chỉ về đúng một chỗ, và đoán mò sẽ giấu mất nguyên nhân.

- [ ] **Step 3b: Kiểm chứng KHÁCH QUAN (không dựa vào tai)**

Tai không đo được, và người kế tiếp đọc report của bạn không nghe lại được. Đo bằng số.

Cài Playwright nếu chưa có: `cd lugo-web-client && pnpm add -D playwright && npx playwright install chromium`

Tạo `lugo-web-client/verify-audio.mjs`:

```js
// Chạy Talk thật trong Chromium với mic giả, rồi ĐO thay vì nghe.
import { chromium } from 'playwright'

const b = await chromium.launch({
  args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'],
})
const p = await b.newPage()

const artifactHits = []
p.on('request', (r) => {
  if (r.url().includes('/artifacts/')) artifactHits.push(r.url())
})

// Bẫy AudioDecoder để đếm frame và đo RMS -- phải cài TRƯỚC khi trang chạy.
await p.addInitScript(() => {
  window.__decoded = []
  const Orig = window.AudioDecoder
  window.AudioDecoder = class extends Orig {
    constructor(init) {
      super({
        error: init.error,
        output: (data) => {
          const pcm = new Float32Array(data.numberOfFrames)
          data.copyTo(pcm, { planeIndex: 0, format: 'f32-planar' })
          let sum = 0
          for (const v of pcm) sum += v * v
          window.__decoded.push({
            frames: data.numberOfFrames,
            rate: data.sampleRate,
            rms: Math.sqrt(sum / pcm.length),
          })
          init.output(data)
        },
      })
    }
  }
})

await p.goto('http://localhost:5173/talk-probe.html')
await p.click('#go')
await p.waitForTimeout(25000) // đủ cho mic giả -> STT -> LLM -> TTS

const d = await p.evaluate(() => window.__decoded)
const log = await p.textContent('#log')

const totalFrames = d.reduce((s, x) => s + x.frames, 0)
const loud = d.filter((x) => x.rms > 0.001).length
console.log('log:\n' + log)
console.log('chunk giải mã:', d.length)
console.log('tổng frame:', totalFrames, '=', (totalFrames / 24000).toFixed(2), 'giây audio')
console.log('sample rate khác 24000:', d.filter((x) => x.rate !== 24000).length)
console.log('chunk KHÔNG im lặng:', loud, '/', d.length)
console.log('request /artifacts (phải là 0):', artifactHits.length)

await b.close()
```

Chạy: `cd lugo-web-client && node verify-audio.mjs` (cần `pnpm dev` và gateway đang chạy).

**Chép output thật vào report.** Ngưỡng phải đạt:
- `chunk giải mã` > 0 — không thì giải mã hỏng hoặc audio không về.
- `sample rate khác 24000` = **0** — khác 0 nghĩa là lệch tần số, và lệch tần số cho ra giọng chipmunk hoặc giọng trầm rề mà test đơn vị không bao giờ bắt được.
- `chunk KHÔNG im lặng` gần bằng tổng — toàn im lặng nghĩa là giải mã ra rác.
- `request /artifacts` = **0** — khác 0 nghĩa là đang đi nhầm đường `audio_url` công khai, đúng thứ ta chọn opus để tránh.
- `tổng frame` quy ra giây phải hợp lý so với độ dài câu trả lời trong `log`.

- [ ] **Step 4: Commit**

```bash
cd lugo-web-client
git add talk-probe.html
git commit -m "test(audio): trang thử hội thoại thật"
```

## Ngoài phạm vi plan này

- Màn Talk thật (vòng tròn logo làm chỉ báo trạng thái, nav 4 mục) — plan 1c
- History, Devices, Tools
- Chọn profile/giọng nói
- Nhập bằng chữ ở UI (`sendText` đã có, chưa có ô nhập)
- Bảo vệ `/artifacts` (vấn đề có sẵn; plan này né bằng cách dùng opus, không sửa nó)
- Tự nối lại khi rớt mạng
