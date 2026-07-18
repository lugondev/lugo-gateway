# Lugo Web Client — English Copy + Shared UI System (Phase 1g) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Switch all user-facing copy to English, and replace the per-screen duplicated form styling with one small set of shared, polished, accessible primitives (Button, Input, TextArea, Card, Modal) — keeping the Lugo identity.

**Architecture:** New `src/ui/` holds the primitives + one `ui.css`. Every screen re-points at them; the duplicated per-screen `__btn/__input/__area/__card` rules go away. The two destructive confirm flows (remove device, delete conversation) become real modal dialogs.

**Spec:** `docs/superpowers/specs/2026-07-16-lugo-web-client-design.md`

## Two owner decisions locked

- **Keep the Lugo identity** (cream surface, ink text, orange reserved for primary actions, pills, cards). Standardize and polish — do NOT move to a neutral/generic look.
- **Real modal dialogs** for confirmations (backdrop, focus trap, Esc, `aria-modal`), not inline button swaps.

## Scope of "English"

- **Every user-visible string → English.** Screens, buttons, placeholders, hints, empty states, `aria-live` state labels, and the error-mapping functions.
- **`relativeTime()` is user-facing too** — its output strings become English.
- **Keep Be Vietnam Pro** (renders Latin cleanly; no font churn).
- **Code comments stay in Vietnamese** — internal notes, not shipped. Do not translate them; keep diffs focused on what users see.
- **No i18n framework.** Straight string replacement.

## The test-update rule (read before Task 3)

Several existing tests assert the Vietnamese copy on purpose (they were the guard that kept raw server strings out of the UI). When the copy becomes English, those assertions **must be updated to the new English text, in the same task that changes the copy.** This is a legitimate update to intentionally-changed copy — NOT the forbidden "weaken a test to make it pass." The distinction: you are changing what the code says AND what the test checks, together, deliberately. Do not delete a test to avoid updating it.

Known Vietnamese-asserting tests (grep for more; do not trust this list to be complete):
- `src/api/auth.test.ts` — `rejects.toThrow(/không hợp lệ/)`
- `src/api/devices.test.ts` — `toContain('Mã không đúng hoặc đã hết hạn')`, friendlyDeviceError cases
- `src/api/history.test.ts` — `rejects.toThrow(/không tìm thấy|không còn/i)`
- `src/api/tools.test.ts` — `rejects.toThrow(/không|thất bại/i)`
- `src/lib/time.test.ts` — `'vừa xong'`, `'2 phút trước'`, `'chưa kết nối lần nào'`, etc.

## Nền tảng đã có (đừng dựng lại)

Repo con `lugo-web-client` @ `f85d795`, 78/78 test. Token màu đầy đủ trong `src/theme.css` (`--lugo-ink`, `--lugo-cream`, `--lugo-cream-deep`, `--lugo-accent`, `--lugo-accent-warm`, `--lugo-accent-gradient`, `--lugo-danger`, và `[data-surface='talk']` đảo nền tối). `box-sizing: border-box` đã reset toàn cục.

## Global Constraints

- Chỉ token màu đã có trong `theme.css`. **Không thêm màu mới.**
- Cam **chỉ** cho hành động chính và trạng thái hoạt động. Không nút phụ/huỷ/nav nào được cam.
- Mọi lời gọi API vẫn qua `apiFetch`; component không tự fetch.
- Responsive xuống **320px**; focus bàn phím nhìn thấy được; `prefers-reduced-motion` tôn trọng.
- Chạy `pnpm test` + `pnpm build` trong `lugo-web-client/`.
- **Không** `git push`. Commit trong repo con.

## File Structure

| File | Trách nhiệm |
|---|---|
| `src/ui/ui.css` | Toàn bộ style dùng chung: `.btn`, `.field`, `.input`, `.textarea`, `.card`, `.modal`. |
| `src/ui/Button.tsx` | Nút, các variant primary/secondary/danger/ghost. |
| `src/ui/TextInput.tsx`, `src/ui/TextArea.tsx` | Ô nhập có nhãn + trạng thái lỗi. |
| `src/ui/Card.tsx` | Khung card nền kem. |
| `src/ui/Modal.tsx`, `src/ui/ConfirmModal.tsx` | Dialog a11y + dialog xác nhận. |
| các `src/routes/*` + `src/components/Nav.tsx` | Re-point sang primitives + English. |

---

### Task 1: UI primitives — Button, TextInput, TextArea, Card

**Files:**
- Create: `src/ui/ui.css`, `src/ui/Button.tsx`, `src/ui/TextInput.tsx`, `src/ui/TextArea.tsx`, `src/ui/Card.tsx`
- Test: `src/ui/Button.test.tsx`

**Interfaces:**
- `<Button variant="primary"|"secondary"|"danger"|"ghost" size?="md"|"sm" fullWidth? ...buttonProps>`
- `<TextInput label id value onChange error? ...inputProps>`
- `<TextArea label id value onChange error? ...taProps>`
- `<Card>...</Card>`

- [ ] **Step 1: ui.css**

Tạo `src/ui/ui.css`:

```css
/* Nút dùng chung. Cam CHỈ ở variant primary -- các variant khác không bao giờ
   cam (kỷ luật của bộ nhận diện Lugo). */
.btn {
  font: inherit;
  font-weight: 500;
  border-radius: 999px;
  cursor: pointer;
  border: 1px solid transparent;
  transition: opacity 160ms ease, background 160ms ease;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  min-height: 44px;
  padding: 12px 22px;
}
.btn:focus-visible {
  outline: 2px solid var(--lugo-accent-warm);
  outline-offset: 3px;
}
.btn:disabled { cursor: default; opacity: 0.45; }

.btn--sm { min-height: 40px; padding: 8px 16px; font-size: 0.875rem; }
.btn--full { width: 100%; }

.btn--primary { background: var(--lugo-accent-gradient); color: #111; }
.btn--primary:disabled { background: none; border-color: currentColor; color: inherit; }

.btn--secondary { background: none; border-color: currentColor; color: inherit; opacity: 0.8; }
.btn--secondary:hover:not(:disabled) { opacity: 1; }

.btn--danger { background: none; border-color: var(--lugo-danger); color: var(--lugo-danger); }
.btn--danger:hover:not(:disabled) { background: color-mix(in srgb, var(--lugo-danger) 8%, transparent); }

.btn--ghost { background: none; border-color: transparent; color: inherit; opacity: 0.6; }
.btn--ghost:hover:not(:disabled) { opacity: 1; }

/* Trường nhập có nhãn. */
.field { display: grid; gap: 6px; }
.field__label { font-size: 0.8125rem; font-weight: 500; opacity: 0.7; }

.input, .textarea {
  font: inherit;
  width: 100%;
  padding: 12px 14px;
  border-radius: 10px;
  border: 1px solid var(--lugo-cream-deep);
  background: transparent;
  color: inherit;
}
.textarea { resize: vertical; min-height: 96px; }
.input:focus-visible, .textarea:focus-visible {
  outline: 2px solid var(--lugo-accent-warm);
  outline-offset: 2px;
  border-color: transparent;
}
.input[aria-invalid='true'], .textarea[aria-invalid='true'] {
  border-color: var(--lugo-danger);
}
.field__error { color: var(--lugo-danger); font-size: 0.875rem; margin: 0; }

.card {
  border: 1px solid var(--lugo-cream-deep);
  border-radius: 12px;
  padding: 16px;
}
```

- [ ] **Step 2: Button + test (TDD)**

Tạo `src/ui/Button.test.tsx`:

```tsx
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { Button } from './Button'

describe('Button', () => {
  it('renders its label and forwards onClick handler shape', () => {
    render(<Button variant="primary">Save</Button>)
    expect(screen.getByRole('button', { name: 'Save' })).toBeTruthy()
  })

  it('applies the variant class', () => {
    render(<Button variant="danger">Delete</Button>)
    expect(screen.getByRole('button').className).toContain('btn--danger')
  })

  it('disabled forwards to the element', () => {
    render(<Button variant="primary" disabled>X</Button>)
    expect((screen.getByRole('button') as HTMLButtonElement).disabled).toBe(true)
  })

  it('defaults type to button, not submit', () => {
    // A stray submit inside a form would submit it. Default must be safe.
    render(<Button variant="secondary">Cancel</Button>)
    expect((screen.getByRole('button') as HTMLButtonElement).type).toBe('button')
  })
})
```

Nếu `@testing-library/jest-dom` chưa được nạp, các assertion trên vẫn chạy được vì chỉ dùng thuộc tính DOM thường, không dùng matcher `toBeInTheDocument`.

Tạo `src/ui/Button.tsx`:

```tsx
import type { ButtonHTMLAttributes } from 'react'
import './ui.css'

type Variant = 'primary' | 'secondary' | 'danger' | 'ghost'

export function Button({
  variant,
  size = 'md',
  fullWidth = false,
  className = '',
  type = 'button',
  ...rest
}: { variant: Variant; size?: 'md' | 'sm'; fullWidth?: boolean } & ButtonHTMLAttributes<HTMLButtonElement>) {
  const cls = [
    'btn',
    `btn--${variant}`,
    size === 'sm' ? 'btn--sm' : '',
    fullWidth ? 'btn--full' : '',
    className,
  ]
    .filter(Boolean)
    .join(' ')
  // type mặc định "button": một <button> lạc trong <form> mà type="submit"
  // (mặc định của HTML) sẽ submit form ngoài ý muốn.
  return <button type={type} className={cls} {...rest} />
}
```

- [ ] **Step 3: TextInput, TextArea, Card**

Tạo `src/ui/TextInput.tsx`:

```tsx
import type { InputHTMLAttributes } from 'react'
import './ui.css'

export function TextInput({
  label,
  id,
  error,
  className = '',
  ...rest
}: { label: string; id: string; error?: string | null } & InputHTMLAttributes<HTMLInputElement>) {
  return (
    <div className="field">
      <label className="field__label" htmlFor={id}>
        {label}
      </label>
      <input
        id={id}
        className={`input ${className}`}
        aria-invalid={error ? 'true' : undefined}
        {...rest}
      />
      {error && (
        <p className="field__error" role="alert">
          {error}
        </p>
      )}
    </div>
  )
}
```

Tạo `src/ui/TextArea.tsx` (giống hệt nhưng `<textarea className="textarea">`):

```tsx
import type { TextareaHTMLAttributes } from 'react'
import './ui.css'

export function TextArea({
  label,
  id,
  error,
  className = '',
  ...rest
}: { label: string; id: string; error?: string | null } & TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <div className="field">
      <label className="field__label" htmlFor={id}>
        {label}
      </label>
      <textarea
        id={id}
        className={`textarea ${className}`}
        aria-invalid={error ? 'true' : undefined}
        {...rest}
      />
      {error && (
        <p className="field__error" role="alert">
          {error}
        </p>
      )}
    </div>
  )
}
```

Tạo `src/ui/Card.tsx`:

```tsx
import type { HTMLAttributes } from 'react'
import './ui.css'

export function Card({ className = '', ...rest }: HTMLAttributes<HTMLDivElement>) {
  return <div className={`card ${className}`} {...rest} />
}
```

- [ ] **Step 4: Nếu chưa có, cài Testing Library**

`@testing-library/react` có thể đã có (Task phase 1a cài `@testing-library/react`, `jsdom`). Kiểm tra `package.json`. Nếu thiếu: `pnpm add -D @testing-library/react`.

- [ ] **Step 5: Test + build**

Run: `cd lugo-web-client && pnpm test && pnpm build`
Expected: test cũ (78) + Button (4) đều pass; build sạch.

- [ ] **Step 6: Commit**

```bash
cd lugo-web-client
git add src/ui/ package.json pnpm-lock.yaml
git commit -m "feat(ui): shared Button/Input/TextArea/Card primitives"
```

---

### Task 2: Modal + ConfirmModal (accessible)

This is the a11y-critical component. It gets real unit tests.

**Files:**
- Create: `src/ui/Modal.tsx`, `src/ui/ConfirmModal.tsx`, and modal styles appended to `src/ui/ui.css`
- Test: `src/ui/Modal.test.tsx`

**Interfaces:**
- `<Modal open onClose title>{children}</Modal>`
- `<ConfirmModal open title message confirmLabel onConfirm onCancel destructive? busy?>`

- [ ] **Step 1: Modal styles → append to `src/ui/ui.css`**

```css
.modal__backdrop {
  position: fixed;
  inset: 0;
  background: color-mix(in srgb, var(--lugo-ink) 55%, transparent);
  display: grid;
  place-items: center;
  padding: 20px;
  z-index: 50;
}

.modal {
  /* Modal luôn dùng bề mặt kem, kể cả khi mở từ màn Talk nền tối -- nó là một
     lớp nổi riêng, không thuộc nền phía sau. */
  background: var(--lugo-cream);
  color: var(--lugo-ink);
  --lugo-danger: #c9372c;
  border-radius: 16px;
  padding: 22px;
  width: min(380px, 100%);
  display: grid;
  gap: 14px;
  box-shadow: 0 20px 60px -20px rgba(0, 0, 0, 0.5);
}

.modal__title { font-size: 1.125rem; font-weight: 600; margin: 0; }
.modal__body { margin: 0; line-height: 1.55; opacity: 0.8; }
.modal__actions { display: flex; gap: 10px; justify-content: flex-end; margin-top: 4px; }

@media (prefers-reduced-motion: no-preference) {
  .modal { animation: modal-in 160ms ease; }
  @keyframes modal-in {
    from { transform: translateY(8px); opacity: 0; }
    to { transform: none; opacity: 1; }
  }
}
```

- [ ] **Step 2: Write the failing tests (TDD)**

Tạo `src/ui/Modal.test.tsx`:

```tsx
import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { Modal } from './Modal'

describe('Modal', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('renders nothing when closed', () => {
    render(<Modal open={false} onClose={() => {}} title="X">body</Modal>)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('is a labelled modal dialog when open', () => {
    render(<Modal open onClose={() => {}} title="Remove device">body</Modal>)
    const dlg = screen.getByRole('dialog')
    expect(dlg.getAttribute('aria-modal')).toBe('true')
    // title is wired via aria-labelledby, not just visually present
    const labelledby = dlg.getAttribute('aria-labelledby')
    expect(labelledby).toBeTruthy()
    expect(document.getElementById(labelledby!)?.textContent).toBe('Remove device')
  })

  it('Escape closes it', () => {
    const onClose = vi.fn()
    render(<Modal open onClose={onClose} title="X">body</Modal>)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledOnce()
  })

  it('clicking the backdrop closes it', () => {
    const onClose = vi.fn()
    render(<Modal open onClose={onClose} title="X">body</Modal>)
    fireEvent.click(screen.getByTestId('modal-backdrop'))
    expect(onClose).toHaveBeenCalledOnce()
  })

  it('clicking INSIDE the dialog does NOT close it', () => {
    const onClose = vi.fn()
    render(<Modal open onClose={onClose} title="X"><button>inside</button></Modal>)
    fireEvent.click(screen.getByRole('button', { name: 'inside' }))
    expect(onClose).not.toHaveBeenCalled()
  })

  it('moves focus into the dialog on open', () => {
    render(<Modal open onClose={() => {}} title="X"><button>act</button></Modal>)
    // focus should be within the dialog, not left on <body>
    expect(screen.getByRole('dialog').contains(document.activeElement)).toBe(true)
  })
})
```

- [ ] **Step 3: Run to confirm RED**

Run: `cd lugo-web-client && pnpm test`
Expected: FAIL — module `./Modal` not found.

- [ ] **Step 4: Implement Modal**

Tạo `src/ui/Modal.tsx`:

```tsx
import { useEffect, useId, useRef, type ReactNode } from 'react'
import './ui.css'

export function Modal({
  open,
  onClose,
  title,
  children,
}: {
  open: boolean
  onClose: () => void
  title: string
  children: ReactNode
}) {
  const dialogRef = useRef<HTMLDivElement>(null)
  const titleId = useId()

  useEffect(() => {
    if (!open) return

    // Trả focus về nơi cũ khi đóng: người dùng bàn phím không bị "mất chỗ".
    const previouslyFocused = document.activeElement as HTMLElement | null

    // Đưa focus vào dialog. Ưu tiên phần tử focus được đầu tiên, nếu không thì
    // chính dialog (nó có tabIndex=-1).
    const focusables = dialogRef.current?.querySelectorAll<HTMLElement>(
      'button, [href], input, textarea, select, [tabindex]:not([tabindex="-1"])',
    )
    ;(focusables && focusables.length ? focusables[0] : dialogRef.current)?.focus()

    // Khoá cuộn nền.
    const prevOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        onClose()
        return
      }
      if (e.key !== 'Tab') return
      // Focus trap: giữ Tab quẩn trong dialog.
      const items = dialogRef.current?.querySelectorAll<HTMLElement>(
        'button, [href], input, textarea, select, [tabindex]:not([tabindex="-1"])',
      )
      if (!items || items.length === 0) {
        e.preventDefault()
        return
      }
      const first = items[0]
      const last = items[items.length - 1]
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = prevOverflow
      previouslyFocused?.focus()
    }
  }, [open, onClose])

  if (!open) return null

  return (
    <div
      className="modal__backdrop"
      data-testid="modal-backdrop"
      onClick={onClose}
    >
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        ref={dialogRef}
        // Bấm bên trong không được lan ra backdrop (backdrop đóng modal).
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="modal__title" id={titleId}>
          {title}
        </h2>
        {children}
      </div>
    </div>
  )
}
```

- [ ] **Step 5: ConfirmModal**

Tạo `src/ui/ConfirmModal.tsx`:

```tsx
import { Button } from './Button'
import { Modal } from './Modal'

export function ConfirmModal({
  open,
  title,
  message,
  confirmLabel,
  onConfirm,
  onCancel,
  destructive = false,
  busy = false,
}: {
  open: boolean
  title: string
  message: string
  confirmLabel: string
  onConfirm: () => void
  onCancel: () => void
  destructive?: boolean
  busy?: boolean
}) {
  return (
    <Modal open={open} onClose={onCancel} title={title}>
      <p className="modal__body">{message}</p>
      <div className="modal__actions">
        <Button variant="ghost" size="sm" onClick={onCancel} disabled={busy}>
          Cancel
        </Button>
        <Button
          variant={destructive ? 'danger' : 'primary'}
          size="sm"
          onClick={onConfirm}
          disabled={busy}
        >
          {confirmLabel}
        </Button>
      </div>
    </Modal>
  )
}
```

- [ ] **Step 6: Run to confirm GREEN**

Run: `cd lugo-web-client && pnpm test`
Expected: 6 Modal tests + all prior pass.

- [ ] **Step 7: Commit**

```bash
cd lugo-web-client
git add src/ui/Modal.tsx src/ui/ConfirmModal.tsx src/ui/Modal.test.tsx src/ui/ui.css
git commit -m "feat(ui): accessible Modal + ConfirmModal (focus trap, Esc, backdrop)"
```

---

### Task 3: English copy for the leaf layers (time + errors) + update their tests

Isolate the pure string+test change here so the screen migrations stay about structure.

**Files:**
- Modify: `src/lib/time.ts` + `src/lib/time.test.ts`
- Modify: `src/api/{auth,devices,history,tools}.ts` + their `.test.ts`

**English strings — use exactly these:**

`time.ts`:
- `null` → `'never connected'`
- `< 60s` → `'just now'`
- minutes → `'{n} min ago'` (e.g. `'2 min ago'`)
- hours → `'{n} h ago'`
- days → `'{n} d ago'`

`devices.ts` `friendlyDeviceError`:
- invalid/expired → `'That code is wrong or expired. Codes last 10 minutes — restart the device to get a new one.'`
- already paired → `'This device is already paired to an account. Remove it from the list above before pairing again.'`
- else → raw server text (unchanged)

`history.ts`:
- 404 → `'This conversation could not be found. It may have been deleted.'`
- 401/403 → `'Your session has expired. Please sign in again.'`
- else → `'Server returned error {status}'`

`tools.ts`:
- STT failure → `'Could not transcribe this file. Try a different wav or mp3.'`
- TTS failure → `'Could not read this out. Try again in a moment.'`
- 401/403 → `'Your session has expired. Please sign in again.'`
- missing audio → `'The server returned no audio.'`

`auth.ts` (login):
- bad login → `'Wrong username or password'`
- malformed 200 → `'The server returned invalid data'`

- [ ] **Step 1: Change the strings**

Edit each file's user-facing strings to the English above. Do NOT touch logic, only the message text. Keep Vietnamese code comments.

- [ ] **Step 2: Update the tests to the English text**

In each `.test.ts`, change the assertions that matched Vietnamese to match the new English. Examples:
- `time.test.ts`: `'vừa xong'` → `'just now'`, `'2 phút trước'` → `'2 min ago'`, `'chưa kết nối lần nào'` → `'never connected'`, `'3 giờ trước'` → `'3 h ago'`, `'2 ngày trước'` → `'2 d ago'`.
- `history.test.ts`: `/không tìm thấy|không còn/i` → `/could not be found|deleted/i`.
- `tools.test.ts`: `/không|thất bại/i` → `/could not|try/i`.
- `devices.test.ts`: `'Mã không đúng hoặc đã hết hạn'` → `'wrong or expired'`; friendlyDeviceError cases → English substrings.
- `auth.test.ts`: `/không hợp lệ/` → `/invalid data/i`.

This is updating a test to match intentionally-changed copy — correct, not weakening.

- [ ] **Step 3: Test + build**

Run: `cd lugo-web-client && pnpm test && pnpm build`
Expected: all pass. If a Vietnamese assertion you missed fails, that is the grep-incompleteness — fix it to English, don't skip it.

- [ ] **Step 4: Commit**

```bash
cd lugo-web-client
git add src/lib/time.ts src/lib/time.test.ts src/api/
git commit -m "feat(i18n): English copy for time + error layers"
```

---

### Task 4: Migrate Login + Nav + Talk (English + primitives)

**Files:**
- Modify: `src/routes/Login.tsx`, `src/components/Nav.tsx`, `src/components/Nav.css`, `src/routes/Talk.tsx`, `src/routes/Talk.css`

**English strings:**

Login:
- heading `LUGO` (unchanged), inputs labelled `Username` / `Password`, placeholders same, button `Sign in` / `Signing in…`

Nav: `Talk`, `History`, `Devices`, `Tools`, `Sign out`

Talk:
- wordmark `LUGO`
- state labels (aria-live): `Idle` / `Connecting` / `Listening` / `Thinking` / `Speaking` / `Error`
- idle hint: `Tap to start. Talk like you would with a friend.`
- live hint: `Just talk. Cut in any time to interrupt.`
- buttons: `Start talking` / `Stop`
- capability error: `This browser is missing {list}. Open it in a recent Chrome or Edge, over HTTPS.`

**What to change structurally:**
- Login: use `<TextInput>` for the two fields (they already have `aria-label`; give them `id` + `label`). Use `<Button variant="primary" type="submit" fullWidth>` for submit. Keep the existing submit/error logic identical.
- Talk: replace `.talk__btn`/`.talk__btn--primary` usages with `<Button>` (primary for "Start talking", secondary for "Stop"). Remove the now-dead `.talk__btn*` rules from `Talk.css`. Keep the LugoMark, the `data-surface="talk"` dark inversion, the aria-live region, and all state logic untouched.
- Nav: labels to English. Nav keeps its bespoke tab styling (it is not a generic button) — do NOT force it onto `.btn`. Just translate labels. The tight-fit fix from phase 1f must survive (labels one line at 320px); English labels are shorter, so this should only get easier — but verify in Task 7.

**Do not** change any behavior, routing, or the audio layer. Keep `pnpm test` at its current count (no new tests here; screens are visual). `pnpm build` clean.

- [ ] **Step 1:** Migrate Login → primitives + English.
- [ ] **Step 2:** Translate Nav labels; confirm no `.btn` forced onto tabs.
- [ ] **Step 3:** Migrate Talk buttons → `<Button>`; delete dead `.talk__btn*` CSS; English strings.
- [ ] **Step 4:** `pnpm test && pnpm build` — all green.
- [ ] **Step 5:** Commit `feat(ui): migrate Login/Nav/Talk to primitives + English`.

---

### Task 5: Migrate Devices + History (English + primitives + ConfirmModal)

The two destructive inline-confirm flows become `ConfirmModal`.

**Files:**
- Modify: `src/routes/Devices.tsx`, `src/routes/Devices.css`, `src/routes/History.tsx`, `src/routes/History.css`

**English strings:**

Devices:
- title `Devices`, sub `Paired devices talk to Lugo using your account.`
- empty: `No devices yet. Turn on your Lugo device — it'll show a 6-digit code. Enter it below.`
- item meta: `Active` (when recently active) / `Last seen: {relativeTime}`
- remove button `Remove`
- pair form: input labels `6-digit code` / `Device name`, placeholders `000000` / `Name it, e.g. Kitchen speaker`, button `Pair device` / `Pairing…`
- **Remove confirm modal:** title `Remove device?`, message `{name} will lose access and have to be paired again.`, confirm `Remove`, destructive.

History:
- title `History`, sub `Everything you and Lugo have said.`
- empty: `No conversations yet. Head to Talk to start.`
- row meta: `{relativeTime} · {n} messages`, empty preview `No content`
- detail: back `Back`, delete `Delete`, turn labels `YOU` / `LUGO`, empty detail `This conversation has no content.`
- **Delete confirm modal:** title `Delete conversation?`, message `This can't be undone.`, confirm `Delete`, destructive.

**Structural changes:**
- Replace the `confirming` inline-swap logic in BOTH screens with `<ConfirmModal open={...} .../>`. The `confirming` state becomes "which item's modal is open" (Devices: the device id or null; History: boolean). `onConfirm` runs the existing revoke/delete call; `onCancel` closes.
- Use `<TextInput>` for the pair-code and name fields, `<Button variant="primary">` for "Pair device", `<Button variant="danger" size="sm">` for the "Remove"/"Delete" trigger, `<Card>` for the device rows / list containers where it fits.
- Keep `relativeTime` / `isRecentlyActive` usage, the no-green-dot rule, the cream surface, all API calls, and the server-error handling intact.
- Remove the now-dead `.dev__btn*`, `.dev__input`, `.his__btn*` rules superseded by the primitives. Keep screen-specific layout rules (`.dev__item`, `.his__row`, list grids).

**Wire the modal's busy state:** while the revoke/delete request is in flight, pass `busy` to `ConfirmModal` so its buttons disable — prevents double-submit.

- [ ] **Step 1:** Devices → English + primitives + ConfirmModal for remove (with busy).
- [ ] **Step 2:** History → English + primitives + ConfirmModal for delete (with busy).
- [ ] **Step 3:** Delete superseded CSS; `pnpm test && pnpm build` green.
- [ ] **Step 4:** Commit `feat(ui): migrate Devices/History to primitives + confirm modals + English`.

---

### Task 6: Migrate Tools (English + primitives)

**Files:**
- Modify: `src/routes/Tools.tsx`, `src/routes/Tools.css`

**English strings:**
- title `Tools`, sub `Two quick jobs, no need to open a conversation.`
- STT card: heading `Recording to text`, hint `Pick a wav or mp3 file. Lugo will listen and type it out.`, button `To text` / `Listening…`, empty result `(nothing heard)`, file input label `Choose a recording`
- TTS card: heading `Text to speech`, hint `Type something and Lugo will read it aloud.`, textarea label `Text to read`, placeholder `Lovely day today…`, button `Read aloud` / `Reading…`

**Structural:** two `<Card>`s; `<Button variant="primary">` for each action (the two primary actions — the only orange on this screen); `<TextArea>` for the TTS input. Keep the file input (native, styled), the absolute-URL audio playback, and all API calls intact. Remove superseded `.tool__btn`, `.tool__area`, `.tool__card` rules; keep the `<audio>` styling.

- [ ] **Step 1:** Migrate + English.
- [ ] **Step 2:** `pnpm test && pnpm build` green.
- [ ] **Step 3:** Commit `feat(ui): migrate Tools to primitives + English`.

---

### Task 7: Verify everything (screenshots + a11y + responsive)

Nothing here is provable by unit test — it must be seen and driven.

- [ ] **Step 1: Start gateway + client**

```bash
cd /Users/lugon/code/speech-text-transformer
.venv/bin/uvicorn app.main:app --app-dir apps/api_gateway --port 8000 &
cd lugo-web-client && pnpm dev
```

- [ ] **Step 2: Screenshot every screen at 390px and look**

Tạo `lugo-web-client/verify-ui.mjs` (Playwright) that logs in as `e2e-user`/`pw12345678` and screenshots: Login, Talk (idle), History (list), a History detail, Devices, Tools. For each, read the PNG and check:
- **All copy is English** — no Vietnamese anywhere on screen. Grep the page text too: `await page.content()` must not contain `ạ ả ấ ầ ế ề ộ ơ ư đ` beyond the brand line. (A cheap check: assert the page text has none of `['Đăng', 'Thiết bị', 'Lịch sử', 'Công cụ', 'Ghép', 'Xoá', 'phút trước', 'vừa xong']`.)
- Buttons/inputs/cards look consistent across screens (same radius, padding, focus ring).
- Orange only on primary actions and the active Talk mark.
- Cream surface everywhere except Talk.
- Nothing clipped by the fixed nav; nav one row at 320px.

- [ ] **Step 3: Drive a confirm modal and test its keyboard behavior**

On Devices (pair a device first via `curl .../pair/init` if the list is empty), click Remove → the modal opens. Verify with Playwright:
- `role="dialog"` present, focus is inside it.
- `Escape` closes it (device NOT removed).
- Clicking the backdrop closes it.
- Tab stays trapped inside.
- Confirming actually removes the device (list refreshes).
Screenshot the open modal and look: cream card on a dimmed backdrop, danger confirm button, ghost cancel.

- [ ] **Step 4: Fix what you see, re-shoot, re-look.** Do not report done until the screenshots are clean.

- [ ] **Step 5: Commit** `test(ui): verify English + component system across all screens`.

## Ngoài phạm vi

- i18n framework / language switch (straight English replacement only)
- Đổi font (giữ Be Vietnam Pro)
- Dịch code comment (giữ tiếng Việt — nội bộ)
- Modal cho luồng pairing (pairing là form, không phải xác nhận phá hoại — để inline)
