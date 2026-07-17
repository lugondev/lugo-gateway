# Lugo Web Client — Màn Talk (Phase 1c) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Màn Talk thật — vòng tròn logo làm chỉ báo trạng thái phản ứng theo âm lượng thật, transcript lượt hiện tại, và điều khiển tối giản. Dựng trên lớp audio đã chạy và đã đo ở phase 1b.

**Architecture:** `LugoMark` là component thuần túy (chỉ nhận `state` + `level`, không biết WS/audio). `Talk` cắm `Conversation` vào nó. Lớp audio không đổi ngoài việc thêm đo mức âm lượng.

**Tech Stack:** React 19, SVG, Web Audio `AnalyserNode`, Be Vietnam Pro (fontsource), Vitest.

**Spec:** `docs/superpowers/specs/2026-07-16-lugo-web-client-design.md`

## Nền tảng đã có (đừng dựng lại)

Repo con `lugo-web-client` @ `0070ad5`, 35/35 test:
- `src/audio/conversation.ts` — `Conversation` với callback `onState(TalkState)`, `onUserText`, `onReplyText`, `onError`; `connect()`, `disconnect()`, `sendText()`, `abort()`. `TalkState = 'idle'|'connecting'|'listening'|'thinking'|'speaking'|'error'`.
- `src/audio/mic.ts` — `Mic.start(onFrame)`, `stop()`.
- `src/audio/player.ts` — `Player.push(packet)`, `stop()`, `playing`, `chunkDuration(frames, rate)`.
- `src/audio/capability.ts` — `checkAudioSupport()`.
- `src/theme.css` — token màu Lugo, `[data-surface='talk']` đã đảo sang nền tối.

## Nguyên tắc thiết kế (đọc trước khi code — plan này sẽ vô nghĩa nếu bỏ qua)

**Chấm là BẠN, vòng là LUGO.** Bảng nhận diện Lugo ghi rõ: vòng tròn hở = "sự lắng nghe", chấm nhỏ = "Bạn – trung tâm của mọi kết nối", khoảng hở = "cánh cửa kết nối". Quy tắc trạng thái suy ra trực tiếp: **ai đang hoạt động thì phần đó động.**

| Trạng thái | Vòng (Lugo) | Chấm (bạn) |
|---|---|---|
| `idle` | đứng yên, mờ 35% | đứng yên, mờ |
| `connecting` | mờ dần vào/ra chậm | đứng yên, mờ |
| `listening` | đứng yên, sáng vừa | **nở theo âm lượng mic của bạn** |
| `thinking` | **xoay đều** | đứng yên, mờ |
| `speaking` | **thở theo âm lượng tiếng trả lời** | đứng yên, mờ |
| `error` | đứng yên, màu danger | đứng yên, màu danger |

Đây là lý do phải đo âm lượng thật thay vì nhịp cố định: nhịp cố định chỉ là spinner sơn màu thương hiệu. Vòng tròn phản ứng theo tiếng nói mới là thứ phân biệt "chỉ báo trạng thái" với "bạn đồng hành".

**Không có bong bóng chat.** Đây là bạn đồng hành bằng giọng nói, không phải app nhắn tin. Mọi sản phẩm AI đều đổ ra một khung chat — đó là câu trả lời mặc định, và nó biến việc *nói* thành việc *đọc log*. Talk chỉ hiện **lượt hiện tại**: câu trả lời to ở giữa, câu bạn vừa nói nhỏ và mờ bên dưới. Lịch sử thuộc màn History (plan sau), không phải ở đây.

**Cam chỉ cho trạng thái hoạt động và hành động chính.** Không tô cam lên chữ, viền, nhãn. Vòng tròn khi hoạt động và nút chính — hết. Lỗi dùng `--lugo-danger` (đã có từ phase 1a).

**Chữ không nói điều vòng tròn đã nói.** Không thêm nhãn "Đang nghe..." bên dưới vòng tròn — vòng tròn đã nói rồi, thêm chữ là để hai thứ làm cùng một việc. Nhưng **trình đọc màn hình không thấy được vòng tròn**, nên trạng thái phải có ở `aria-live`. Hai đối tượng khác nhau, không phải trùng lặp.

## Global Constraints

- Palette đúng token đã có trong `theme.css`: `--lugo-ink` `#111111`, `--lugo-ink-soft` `#2A2A2A`, `--lugo-cream` `#F7F4EE`, `--lugo-cream-deep` `#E8E1D6`, `--lugo-accent` `#FF8A00`, `--lugo-accent-warm` `#FFC857`, `--lugo-danger`. **Không thêm màu mới.**
- **Không có role/admin trong UI.** Không màn quản trị, không kiểm tra role, không điều kiện theo role.
- `src/audio/` vẫn không được import React hay routing.
- Copy tiếng Việt, câu thường, động từ chủ động. Nút nói đúng việc nó làm.
- **Sàn chất lượng, không cần khoe:** responsive xuống mobile; focus bàn phím nhìn thấy được; `prefers-reduced-motion` được tôn trọng.
- Chạy `pnpm test` trong `lugo-web-client/`. Hiện 35 test pass — phải giữ nguyên.
- **Không** `git push`. Commit trong repo con.

---

### Task 1: Đo mức âm lượng

Vòng tròn phản ứng theo âm lượng thật cần một nguồn số. `AnalyserNode` cho cả mic lẫn player.

**Files:**
- Modify: `src/audio/mic.ts`
- Modify: `src/audio/player.ts`
- Modify: `src/audio/level.ts` (mới)
- Test: `src/audio/level.test.ts` (mới)

**Interfaces:**
- Produces:
  - `rmsToLevel(rms: number): number` — chuẩn hoá RMS thô về 0..1 dùng được cho UI
  - `smoothLevel(prev: number, next: number, attack: number, release: number): number`
  - `Mic.level: number` (getter, 0..1)
  - `Player.level: number` (getter, 0..1)

- [ ] **Step 1: Viết test thất bại**

Tạo `src/audio/level.test.ts`:

```ts
import { describe, expect, it } from 'vitest'
import { rmsToLevel, smoothLevel } from './level'

describe('rmsToLevel', () => {
  it('im lặng ra 0', () => {
    expect(rmsToLevel(0)).toBe(0)
  })

  it('nằm trong 0..1 với mọi đầu vào, kể cả vượt ngưỡng', () => {
    for (const v of [0, 0.001, 0.05, 0.3, 1, 5, 100]) {
      const l = rmsToLevel(v)
      expect(l).toBeGreaterThanOrEqual(0)
      expect(l).toBeLessThanOrEqual(1)
    }
  })

  it('đơn điệu tăng: to hơn thì level cao hơn', () => {
    expect(rmsToLevel(0.2)).toBeGreaterThan(rmsToLevel(0.02))
  })

  it('giọng nói bình thường ra mức thấy được, không dí sát 0', () => {
    // RMS giọng nói thường ~0.05-0.2. Nếu thang tuyến tính thì vòng tròn
    // gần như không nhúc nhích -- phải cảm nhận được.
    expect(rmsToLevel(0.1)).toBeGreaterThan(0.3)
  })
})

describe('smoothLevel', () => {
  it('lên nhanh (attack) để bắt kịp lúc bắt đầu nói', () => {
    const out = smoothLevel(0, 1, 0.5, 0.1)
    expect(out).toBeGreaterThan(0.4)
  })

  it('xuống chậm (release) để không giật cục giữa các âm tiết', () => {
    // Nếu xuống ngay lập tức, vòng tròn nhấp nháy loạn giữa từng âm tiết.
    const out = smoothLevel(1, 0, 0.5, 0.1)
    expect(out).toBeGreaterThan(0.8)
  })

  it('đứng yên khi không đổi', () => {
    expect(smoothLevel(0.5, 0.5, 0.5, 0.1)).toBeCloseTo(0.5)
  })
})
```

- [ ] **Step 2: Chạy test để xác nhận nó fail**

Run: `cd lugo-web-client && pnpm test`
Expected: FAIL — không tìm thấy module `./level`

- [ ] **Step 3: Implement level.ts**

Tạo `src/audio/level.ts`:

```ts
/** RMS thô -> mức 0..1 dùng được cho UI.
 *
 * Thang log chứ không tuyến tính: tai người nghe theo log, và RMS giọng nói
 * bình thường chỉ quanh 0.05-0.2 -- vẽ tuyến tính thì vòng tròn gần như không
 * nhúc nhích khi người dùng nói.
 */
export function rmsToLevel(rms: number): number {
  if (rms <= 0) return 0
  const db = 20 * Math.log10(rms)
  // -60dB (gần như im) -> 0 ; -6dB (to) -> 1
  const norm = (db + 60) / 54
  return Math.max(0, Math.min(1, norm))
}

/** Làm mượt: lên nhanh, xuống chậm.
 *
 * Xuống chậm là chủ ý: giọng nói có khoảng lặng giữa các âm tiết, và bám sát
 * chúng khiến vòng tròn nhấp nháy loạn thay vì thở.
 */
export function smoothLevel(prev: number, next: number, attack: number, release: number): number {
  const k = next > prev ? attack : release
  return prev + (next - prev) * k
}

/** Đọc mức tức thời từ một AnalyserNode. Trả 0 nếu chưa có node. */
export function readLevel(analyser: AnalyserNode | null, buf: Float32Array): number {
  if (!analyser) return 0
  analyser.getFloatTimeDomainData(buf)
  let sum = 0
  for (let i = 0; i < buf.length; i += 1) sum += buf[i] * buf[i]
  return rmsToLevel(Math.sqrt(sum / buf.length))
}
```

- [ ] **Step 4: Chạy test để xác nhận pass**

Run: `cd lugo-web-client && pnpm test`
Expected: PASS (7 test mới, tổng 42)

- [ ] **Step 5: Cắm analyser vào Mic**

Trong `src/audio/mic.ts`, thêm import:

```ts
import { readLevel, smoothLevel } from './level'
```

Thêm field vào class `Mic`:

```ts
  private analyser: AnalyserNode | null = null
  private buf = new Float32Array(1024)
  private _level = 0
```

Trong `start()`, sau khi tạo `source`, chèn analyser song song với worklet (KHÔNG nối tiếp — worklet vẫn phải nhận nguyên tín hiệu):

```ts
    this.analyser = this.ctx.createAnalyser()
    this.analyser.fftSize = 2048
    source.connect(this.analyser)
```

Thêm getter:

```ts
  /** Mức giọng NÓI CỦA BẠN, 0..1. Chấm trong logo là "bạn" -- nó nở theo cái này. */
  get level(): number {
    this._level = smoothLevel(this._level, readLevel(this.analyser, this.buf), 0.5, 0.12)
    return this._level
  }
```

Trong `stop()`, thêm `this.analyser = null` và `this._level = 0`.

- [ ] **Step 6: Cắm analyser vào Player**

Trong `src/audio/player.ts`, thêm import `readLevel, smoothLevel` từ `./level`, và các field tương tự:

```ts
  private analyser: AnalyserNode | null = null
  private buf = new Float32Array(1024)
  private _level = 0
```

Trong `ensure()`, sau khi tạo `this.ctx`:

```ts
    this.analyser = this.ctx.createAnalyser()
    this.analyser.fftSize = 2048
    this.analyser.connect(this.ctx.destination)
```

Trong `schedule()`, đổi `src.connect(ctx.destination)` thành `src.connect(this.analyser ?? ctx.destination)`.

Thêm getter:

```ts
  /** Mức tiếng LUGO ĐANG NÓI, 0..1. Vòng tròn thở theo cái này. */
  get level(): number {
    this._level = smoothLevel(this._level, readLevel(this.analyser, this.buf), 0.4, 0.1)
    return this._level
  }
```

Trong `stop()`, thêm `this.analyser = null` và `this._level = 0`.

- [ ] **Step 7: Lộ level qua Conversation**

Trong `src/audio/conversation.ts`, thêm getter vào class `Conversation`:

```ts
  /** Mức để vẽ vòng tròn: khi Lugo nói thì lấy theo tiếng nó, còn lại lấy theo
   * giọng bạn. Đúng quy tắc "ai hoạt động thì phần đó động". */
  get level(): number {
    return this.state === 'speaking' ? this.player.level : this.mic.level
  }
```

- [ ] **Step 8: Chạy test + build**

Run: `cd lugo-web-client && pnpm test && pnpm build`
Expected: 42 test pass, build sạch

- [ ] **Step 9: Commit**

```bash
cd lugo-web-client
git add src/audio/level.ts src/audio/level.test.ts src/audio/mic.ts src/audio/player.ts src/audio/conversation.ts
git commit -m "feat(audio): đo mức âm lượng cho chỉ báo trạng thái"
```

---

### Task 2: LugoMark — chữ ký của sản phẩm

Component thuần túy: nhận `state` + `level`, vẽ. Không biết gì về WS, audio, React context. Test được và xem được độc lập.

**Files:**
- Create: `src/components/LugoMark.tsx`
- Create: `src/components/LugoMark.css`

**Interfaces:**
- Produces: `<LugoMark state={TalkState} level={number} />`

- [ ] **Step 1: Implement LugoMark.tsx**

Hình học lấy từ logo: vòng tròn hở, khoảng hở ở phía trên-phải, chấm nằm trong khoảng hở đó.

Tạo `src/components/LugoMark.tsx`:

```tsx
import type { TalkState } from '../audio/conversation'
import './LugoMark.css'

const R = 38
const CIRC = 2 * Math.PI * R // 238.76
const GAP = 46 // độ dài khoảng hở trên chu vi -> ~70 độ
const DASH = CIRC - GAP

// Chấm nằm giữa khoảng hở: góc -45 độ, bán kính R.
const DOT_X = 50 + R * Math.cos(-Math.PI / 4)
const DOT_Y = 50 + R * Math.sin(-Math.PI / 4)

// Vì sao rotate(-10) chứ không phải số khác: <circle> trong SVG bắt đầu vẽ từ
// 3 giờ và đi thuận chiều kim đồng hồ. Với dasharray này, nét phủ 0..290.6 độ
// nên khoảng hở đã tự nằm ở tâm ~-35 độ (trên-phải). Xoay thêm -10 đưa tâm
// khoảng hở về đúng -45 độ, trùng vị trí chấm. Đổi R hay GAP thì phải tính lại.

export function LugoMark({ state, level }: { state: TalkState; level: number }) {
  // Chấm là BẠN: nở theo giọng bạn khi bạn đang nói.
  const dotScale = state === 'listening' ? 1 + level * 0.85 : 1
  // Vòng là LUGO: thở theo tiếng nó khi nó đang nói.
  const ringScale = state === 'speaking' ? 1 + level * 0.06 : 1

  return (
    <svg
      className="mark"
      data-state={state}
      viewBox="0 0 100 100"
      role="img"
      aria-hidden="true"
    >
      <g style={{ transform: `scale(${ringScale})`, transformOrigin: '50px 50px' }}>
        <circle
          className="mark__ring"
          cx="50"
          cy="50"
          r={R}
          fill="none"
          strokeWidth="9"
          strokeLinecap="round"
          strokeDasharray={`${DASH} ${GAP}`}
          transform="rotate(-10 50 50)"
        />
      </g>
      <circle
        className="mark__dot"
        cx={DOT_X}
        cy={DOT_Y}
        r="7"
        style={{ transform: `scale(${dotScale})`, transformOrigin: `${DOT_X}px ${DOT_Y}px` }}
      />
    </svg>
  )
}
```

- [ ] **Step 2: Implement LugoMark.css**

Tạo `src/components/LugoMark.css`:

```css
.mark {
  width: clamp(160px, 42vw, 240px);
  height: clamp(160px, 42vw, 240px);
  display: block;
}

.mark__ring {
  stroke: var(--lugo-cream);
  opacity: 0.35;
  transition: opacity 400ms ease, stroke 400ms ease;
  /* view-box chứ KHÔNG phải fill-box: transform-origin ở đây tính bằng toạ độ
     viewBox (50px 50px). fill-box sẽ lấy gốc theo hộp bao của chính hình, làm
     tâm xoay lệch đi. */
  transform-box: view-box;
}

.mark__dot {
  fill: var(--lugo-cream);
  opacity: 0.35;
  transition: opacity 400ms ease, fill 400ms ease;
  /* view-box chứ KHÔNG phải fill-box: transform-origin ở đây tính bằng toạ độ
     viewBox (50px 50px). fill-box sẽ lấy gốc theo hộp bao của chính hình, làm
     tâm xoay lệch đi. */
  transform-box: view-box;
}

/* listening: chấm (bạn) sống dậy. Vòng sáng vừa -- Lugo đang chú ý, chưa nói. */
.mark[data-state='listening'] .mark__ring { opacity: 0.5; }
.mark[data-state='listening'] .mark__dot {
  fill: var(--lugo-accent);
  opacity: 1;
}

/* thinking: vòng (Lugo) xoay. Chấm lặng -- lượt nói đã xong. */
.mark[data-state='thinking'] .mark__ring {
  opacity: 0.9;
  transform-origin: 50px 50px;
  animation: mark-spin 1.6s linear infinite;
}

/* speaking: vòng cam, thở theo âm lượng (scale do JS đặt theo level). */
.mark[data-state='speaking'] .mark__ring {
  stroke: var(--lugo-accent);
  opacity: 1;
}

/* connecting: thở chậm, chưa cam -- chưa có gì hoạt động thật. */
.mark[data-state='connecting'] .mark__ring {
  animation: mark-breathe 1.8s ease-in-out infinite;
}

.mark[data-state='error'] .mark__ring,
.mark[data-state='error'] .mark__dot {
  stroke: var(--lugo-danger);
  fill: var(--lugo-danger);
  opacity: 0.9;
}
.mark[data-state='error'] .mark__ring { fill: none; }

@keyframes mark-spin {
  to { transform: rotate(360deg); }
}

@keyframes mark-breathe {
  0%, 100% { opacity: 0.3; }
  50% { opacity: 0.7; }
}

/* Chuyển động phải phục vụ, không được ép. Bỏ xoay/thở; trạng thái vẫn đọc
   được qua màu và độ đậm. */
@media (prefers-reduced-motion: reduce) {
  .mark__ring,
  .mark__dot {
    animation: none !important;
    transition: opacity 200ms ease, stroke 200ms ease, fill 200ms ease;
  }
}
```

- [ ] **Step 3: Kiểm tra build**

Run: `cd lugo-web-client && pnpm build`
Expected: build sạch

- [ ] **Step 4: Commit**

```bash
cd lugo-web-client
git add src/components/LugoMark.tsx src/components/LugoMark.css
git commit -m "feat(ui): LugoMark — vòng là Lugo, chấm là bạn, ai hoạt động thì phần đó động"
```

---

### Task 3: Màn Talk

**Files:**
- Create: `src/routes/Talk.css`
- Modify: `src/routes/Talk.tsx` (đang là placeholder từ phase 1a — thay hẳn)
- Modify: `src/main.tsx` (nạp font)

**Interfaces:**
- Consumes: `Conversation` (phase 1b), `LugoMark` (Task 2), `checkAudioSupport` (phase 1b)
- Produces: `<Talk onLogout={() => void} />`

- [ ] **Step 1: Cài font**

```bash
cd lugo-web-client && pnpm add @fontsource-variable/be-vietnam-pro
```

Nếu package đó không tồn tại, dùng `pnpm add @fontsource/be-vietnam-pro` và import các weight 400/500/600.

Trong `src/main.tsx`, thêm import trước `./theme.css` nếu có, hoặc lên đầu:

```ts
import '@fontsource-variable/be-vietnam-pro'
```

Trong `src/theme.css`, thêm vào `body`:

```css
  font-family: 'Be Vietnam Pro Variable', 'Be Vietnam Pro', system-ui, sans-serif;
```

**Vì sao Be Vietnam Pro:** sản phẩm tiếng Việt, transcript đọc nhiều, và rất nhiều font phương Tây đặt dấu thanh tiếng Việt xấu hoặc va vào nhau. Font này thiết kế cho tiếng Việt. Self-host qua fontsource nên không gọi ra ngoài.

- [ ] **Step 2: Talk.css**

Tạo `src/routes/Talk.css`:

```css
.talk {
  min-height: 100dvh;
  display: grid;
  grid-template-rows: auto 1fr auto;
  gap: 24px;
  padding: 20px;
  background: var(--lugo-bg);
  color: var(--lugo-fg);
}

.talk__bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.talk__wordmark {
  font-size: 0.8125rem;
  font-weight: 600;
  letter-spacing: 0.35em;
  opacity: 0.5;
}

.talk__stage {
  display: grid;
  justify-items: center;
  align-content: center;
  gap: 40px;
  text-align: center;
}

/* Lượt hiện tại, không phải log. Cỡ chữ này là chủ ý: bạn đang NÓI chuyện,
   không phải đọc bản ghi. */
.talk__reply {
  font-size: clamp(1.375rem, 3.6vw, 2rem);
  line-height: 1.45;
  font-weight: 400;
  max-width: 22ch;
  margin: 0;
  min-height: 1.45em;
}

.talk__you {
  font-size: 0.9375rem;
  color: var(--lugo-cream-deep);
  opacity: 0.5;
  margin: 0;
  max-width: 32ch;
}

.talk__hint {
  font-size: 0.9375rem;
  opacity: 0.45;
  margin: 0;
  max-width: 26ch;
}

.talk__error {
  color: var(--lugo-danger);
  font-size: 0.9375rem;
  margin: 0;
  max-width: 30ch;
}

.talk__controls {
  display: flex;
  gap: 12px;
  justify-content: center;
  align-items: center;
}

.talk__btn {
  font: inherit;
  font-size: 0.9375rem;
  font-weight: 500;
  padding: 13px 28px;
  border-radius: 999px;
  cursor: pointer;
  border: 1px solid currentColor;
  background: none;
  color: var(--lugo-fg);
  opacity: 0.75;
  transition: opacity 160ms ease;
}
.talk__btn:hover { opacity: 1; }

/* Hành động chính -- một trong hai chỗ duy nhất được dùng cam. */
.talk__btn--primary {
  background: var(--lugo-accent-gradient);
  color: #111;
  border-color: transparent;
  opacity: 1;
}

.talk__btn:focus-visible {
  outline: 2px solid var(--lugo-accent-warm);
  outline-offset: 3px;
}

.sr-only {
  position: absolute;
  width: 1px; height: 1px;
  padding: 0; margin: -1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
  border: 0;
}
```

- [ ] **Step 3: Talk.tsx**

Thay toàn bộ `src/routes/Talk.tsx`:

```tsx
import { useEffect, useRef, useState } from 'react'
import { checkAudioSupport } from '../audio/capability'
import { Conversation, type TalkState } from '../audio/conversation'
import { LugoMark } from '../components/LugoMark'
import './Talk.css'

const STATE_LABEL: Record<TalkState, string> = {
  idle: 'Chưa kết nối',
  connecting: 'Đang kết nối',
  listening: 'Đang nghe',
  thinking: 'Đang nghĩ',
  speaking: 'Đang trả lời',
  error: 'Có lỗi',
}

export function Talk({ onLogout }: { onLogout: () => void }) {
  const [state, setState] = useState<TalkState>('idle')
  const [reply, setReply] = useState('')
  const [you, setYou] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [level, setLevel] = useState(0)
  const convRef = useRef<Conversation | null>(null)

  // Đọc level ~mỗi khung hình. Không đưa vào state của Conversation vì đây
  // thuần túy là chuyện vẽ -- lớp audio không cần biết có ai đang vẽ.
  useEffect(() => {
    let raf = 0
    const tick = () => {
      const c = convRef.current
      if (c) setLevel(c.level)
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [])

  useEffect(() => () => convRef.current?.disconnect(), [])

  async function start() {
    const support = checkAudioSupport()
    if (!support.ok) {
      // Nói thật thiếu gì, và nói cách sửa. Không "trình duyệt không hỗ trợ".
      setError(`Trình duyệt này thiếu ${support.missing.join(', ')}. Hãy mở bằng Chrome hoặc Edge bản mới, qua HTTPS.`)
      setState('error')
      return
    }
    setError(null)
    setReply('')
    setYou('')
    const conv = new Conversation({
      onState: setState,
      onUserText: setYou,
      onReplyText: (t) => setReply((prev) => (prev ? `${prev} ${t}` : t)),
      onError: (m) => setError(m),
    })
    convRef.current = conv
    await conv.connect()
  }

  function stop() {
    convRef.current?.disconnect()
    convRef.current = null
    setState('idle')
  }

  const live = state !== 'idle' && state !== 'error'

  return (
    <main className="talk" data-surface="talk">
      <div className="talk__bar">
        <span className="talk__wordmark">LUGO</span>
        <button className="talk__btn" onClick={onLogout}>
          Đăng xuất
        </button>
      </div>

      <div className="talk__stage">
        <LugoMark state={state} level={level} />

        {/* Vòng tròn đã nói trạng thái cho người nhìn thấy nó. Dòng này dành
            cho người dùng trình đọc màn hình -- khác đối tượng, không trùng việc. */}
        <p className="sr-only" aria-live="polite">
          {STATE_LABEL[state]}
        </p>

        {error ? (
          <p className="talk__error" role="alert">{error}</p>
        ) : reply ? (
          <p className="talk__reply">{reply}</p>
        ) : live ? (
          <p className="talk__hint">Cứ nói tự nhiên. Muốn ngắt lời thì cứ nói chen vào.</p>
        ) : (
          <p className="talk__hint">Nhấn để bắt đầu. Cứ nói như nói với một người bạn.</p>
        )}

        {you && !error && <p className="talk__you">{you}</p>}
      </div>

      <div className="talk__controls">
        {live ? (
          <button className="talk__btn" onClick={stop}>
            Dừng
          </button>
        ) : (
          <button className="talk__btn talk__btn--primary" onClick={start}>
            Bắt đầu nói
          </button>
        )}
      </div>
    </main>
  )
}
```

**Chú ý về copy:** nút nói đúng việc nó làm ("Bắt đầu nói" → "Dừng"). Màn trống là lời mời hành động, không phải câu tâm trạng. Lỗi nói thiếu cái gì và sửa thế nào, không xin lỗi, không mơ hồ.

- [ ] **Step 4: Reply phải reset mỗi lượt**

`onReplyText` cộng dồn các câu trong một lượt (server gửi từng câu). Nhưng lượt mới phải xoá lượt cũ, nếu không câu trả lời cứ dài mãi.

Trong `Talk.tsx`, thêm vào `start()` khi tạo `Conversation` — sửa `onState`:

```tsx
      onState: (s) => {
        setState(s)
        // Lượt mới bắt đầu -> xoá lượt cũ. Không làm thế thì các lượt dính
        // vào nhau thành một khối chữ dài vô tận.
        if (s === 'thinking') setReply('')
      },
```

- [ ] **Step 5: Build + test**

Run: `cd lugo-web-client && pnpm test && pnpm build`
Expected: 42 test pass, build sạch

- [ ] **Step 6: Commit**

```bash
cd lugo-web-client
git add src/routes/Talk.tsx src/routes/Talk.css src/main.tsx src/theme.css package.json pnpm-lock.yaml
git commit -m "feat(ui): màn Talk — lượt hiện tại, không phải log chat"
```

---

### Task 4: Xác minh thật

**Files:**
- Create: `lugo-web-client/verify-talk.mjs`

- [ ] **Step 1: Chụp ảnh từng trạng thái**

Tạo `lugo-web-client/verify-talk.mjs`:

```js
// Chụp Talk ở từng trạng thái. Ảnh là thứ duy nhất chứng minh được thiết kế
// trông đúng -- test không nhìn được.
import { chromium } from 'playwright'

const b = await chromium.launch({
  args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'],
})
const p = await b.newPage({ viewport: { width: 420, height: 860 } })
const errors = []
p.on('pageerror', (e) => errors.push(String(e)))

await p.goto('http://localhost:5173/')
await p.fill('input[aria-label="Tên đăng nhập"]', 'e2e-user')
await p.fill('input[aria-label="Mật khẩu"]', 'pw12345678')
await p.click('button[type="submit"]')
await p.waitForTimeout(1500)
await p.screenshot({ path: 'shots/talk-idle.png' })

await p.click('text=Bắt đầu nói')
await p.waitForTimeout(3000)
await p.screenshot({ path: 'shots/talk-listening.png' })

await p.evaluate(() => (window).__conv?.sendText?.('Xin chao'))
await p.waitForTimeout(2500)
await p.screenshot({ path: 'shots/talk-thinking.png' })
await p.waitForTimeout(6000)
await p.screenshot({ path: 'shots/talk-speaking.png' })

console.log('lỗi trang:', errors.length ? errors : 'không có')
console.log('state hiện tại:', await p.textContent('[aria-live]'))
await b.close()
```

Chạy gateway (`.venv/bin/uvicorn app.main:app --app-dir apps/api_gateway --port 8000`) và `pnpm dev`, rồi `mkdir -p shots && node verify-talk.mjs`.

- [ ] **Step 2: NHÌN vào ảnh và tự phê bình**

Đọc từng ảnh vừa chụp. Kiểm tra thật:
- Vòng tròn có ra hình logo Lugo không (vòng hở, chấm nằm trong khoảng hở ở trên-phải)? Nếu khoảng hở lệch chỗ, chỉnh `rotate(-55 50 50)` cho khớp.
- Chấm có nằm ĐÚNG trong khoảng hở không, hay đè lên nét vòng?
- Ở `listening`, chấm có cam và vòng có mờ hơn không?
- Ở `speaking`, vòng có cam không?
- Cam có rò rỉ ra chỗ nào ngoài vòng-khi-hoạt-động và nút chính không?
- Chữ tiếng Việt có dấu thanh đặt đúng, không va vào nhau không?
- Ở 420px, có gì tràn hay chật không?

**Sửa những gì bạn thấy sai, chụp lại, nhìn lại.** Đừng báo "xong" khi chưa nhìn ảnh.

- [ ] **Step 3: Kiểm tra reduced-motion**

```js
const p2 = await b.newPage({ viewport: { width: 420, height: 860 } })
await p2.emulateMedia({ reducedMotion: 'reduce' })
```
Xác nhận vòng không xoay ở `thinking` mà vẫn phân biệt được trạng thái qua màu/độ đậm.

- [ ] **Step 4: Kiểm tra bàn phím**

Tab qua các nút — focus phải nhìn thấy được (viền cam ở `:focus-visible`).

- [ ] **Step 5: Commit**

```bash
cd lugo-web-client
echo "shots/" >> .gitignore
git add verify-talk.mjs .gitignore
git commit -m "test(ui): chụp ảnh Talk từng trạng thái để tự phê bình"
```

## Ngoài phạm vi plan này

- Nav 4 mục (Talk/History/Devices/Tools) — chưa có 3 màn kia thì nav là vỏ rỗng
- History, Devices, Tools
- Chọn profile/giọng
- Ô nhập chữ trong UI (`sendText` đã có, chưa lộ ra)
- Tự nối lại khi rớt mạng
