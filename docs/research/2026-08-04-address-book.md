# Address Book — connecting clients to each other

**Status:** research only. Nothing designed, nothing built. Written 2026-08-04 so the
decision can be picked up later without re-doing the groundwork.

**Question asked:** what would an address book — letting clients (devices, web) reach each
other — actually be worth building here, and what is it worth?

**Short answer:** the address book is worth building as an *infrastructure layer*, not as a
feature. What should run on top of it is **asynchronous AI-mediated voice messaging**, not
real-time intercom. Market evidence and implementation cost both point the same way.

---

## 1. Where the product stands today

The gateway knows exactly one topology: **device ↔ AI**. There is no notion of a peer, no
registry of live connections, and no audio path from one connection to another. A grep for
`peer|intercom|broadcast|address.?book` across `apps/`, `docs/`, and `lugo-web-client/`
returns nothing but unrelated hits (socket peers, `TypeError` handlers).

What exists and would be reused:

| Piece | Where | Note |
|---|---|---|
| `users` → owns `devices` | `app/services/db/models.py:81-111` | `name`, `serial`, `profile_id`, `token_hash`, `last_seen_at`, `revoked` |
| Device pairing | `app/api/routes/devices.py`, `app/services/auth/pairing.py` | init / status / claim, per-device token |
| Unified voice WS | `app/api/routes/lugo.py` (654 LOC) | `/v1/lugo/stream`, full-duplex |
| Binary framing | `app/services/conversation/lugo_frame.py` | 4-byte header, `OPUS=0` / `JSON=1` |
| Spoken announcements | `app/services/conversation/announce.py` (130 LOC) | LLM writes a line, session speaks it |
| Speaker identification | `servers/voiceprint-api` | the way to know *who* is standing in front of a shared device |
| Memory scoping | keyed `(user_id, profile_id)` | already multi-user aware |
| Usage + quota | `app/services/usage`, `app/services/quota` | per-message cost is measurable |

What does **not** exist and would have to be built — see §5.

---

## 2. Market evidence

This is the part most worth keeping, because it is the part that is expensive to re-derive.

### Google killed device-to-device calling on Nest (Feb 2024)

Google discontinued calling between home devices, leaving only one-way Broadcast. Users
objected loudly — "broadcast is not two-way", and many had bought Nest devices specifically
for calling. Google cut it anyway.

A company with the best calling infrastructure on earth, removing the feature over vocal
user objection, is a strong signal that **measured usage did not justify the maintenance
cost**.

### Amazon keeps Drop In, but the retention picture is poor

Drop In was among the most-requested Echo features and is still shipping. No usage numbers
have ever been published for it. The nearest available datum: **~62% of people who tried one
of Alexa's top six functions did not keep using it regularly**. That is a general figure, not
Drop In specifically — treat it as weak evidence, not proof.

### Relay pivoted away from families

Relay (Republic Wireless) — a screenless, cloud-connected walkie-talkie, the closest possible
analogue to "device ↔ device over a gateway" — launched for kids and families and **moved to
enterprise**: hotels, theme parks, stadiums, push-to-talk plus a panic button. The durable
demand for push-to-talk turned out to be *at work*, not *at home*.

### Meanwhile, the adjacent market sells

Devices whose entire purpose is letting family reach someone who cannot use a smartphone are
real products with real buyers:

- **Komp** — one-button screen; relatives push photos, messages, and video calls from an app.
- **Jubilee TV** — calls, photos, and voice control delivered through the television.
- **RAZ Memory Cell Phone** — voice and video calls only, remotely administered by a family caregiver.

### Vietnam context

Only three smart-speaker lines support Vietnamese: OLLI Maika, FPT Play Box S, and Google
Home — and Google Assistant's Vietnamese recognition is widely described as weak. The niche
of "a Vietnamese-speaking voice device that lets grandparents send and receive messages" is
close to unoccupied.

### What the evidence adds up to

> In-home real-time intercom is a *demo feature*: compelling at purchase, unused by week
> two. A normal house is small enough to shout across. Asynchronous messaging to someone who
> cannot use a phone is a *product*.

---

## 3. The four variants, priced

"Address book" is a single name over four quite different products.

| Variant | User value | Run cost | Build cost | Market evidence |
|---|---|---|---|---|
| **AI-mediated voice messaging** (async) | High — native to what an AI device is for; no app needed on the receiving end | LLM + TTS per message | **Lowest** | ✅ Komp / Jubilee / RAZ sell |
| **Broadcast / group announcement** | Medium — useful, low engagement risk | TTS only | Low | ✅ the piece Google kept |
| **Real-time intercom** | Low at home, high in B2B | ~0 (bandwidth only) | **Highest** | ❌ Google cut it, Relay pivoted |
| **AI ↔ AI across profiles** | No demonstrated demand | ×2 model spend | High + prompt-injection exposure | ❌ no precedent |

The inversion is the thing to remember: **intercom is the cheapest to operate and the most
expensive to build, with the weakest demand.** Messaging is the exact opposite.

---

## 4. Why it is worth anything — three separate arguments

**User value.** Not "we can call each other." It is: *someone who cannot use a smartphone can
still send and receive voice messages.* Lugo already has good Vietnamese STT (PhoWhisper /
Qwen3-ASR, benchmarked), Vietnamese TTS, an LLM, and per-user memory. The address book is the
last missing piece that turns it from a question-answering speaker into a family
communication channel. None of the three Vietnamese competitors covers this.

**Business value — the strongest argument, and the least obvious.** The address book moves
the unit of value from *per-device* to *per-household*. Today a second device in the same
house adds almost nothing: each one is an independent assistant. With a directory, the second
device finally has a reason to exist. That is a revenue lever, not a feature lever.

**Architectural value.** The layer is `contact → target (device | user | group) + permission`.
Messaging, broadcast, and intercom are three payload types on one routing pipe. Build it once
correctly and all three become cheap.

---

## 5. What would have to be built

### The directory itself

A `contacts` table plus routes and UI. `app/api/routes/devices.py` (186 LOC) is the pattern to
copy — same ownership checks, same shape.

**The authz landmine:** `devices.user_id` today hard-binds a device to exactly one owner. An
address book is the first thing that breaks that assumption. It lands directly on
`app/core/auth_guard.py` (503 LOC), which is **default-deny**: any new router prefix that is
not classified into `_NO_AUTH_PREFIXES` / `_USER_PREFIXES` / `_USER_EXACT` / `_ADMIN_PREFIXES`
fails a test by design. It also lands on precisely the layer where IDOR bugs were found and
fixed before. Cross-user reach must be designed, not bolted on.

*Mitigation:* scope v1 to **within-household** contacts (targets already owned by the same
user). That keeps the whole thing inside existing ownership rules and defers the hard part.

### Presence

The gateway has no registry of live connections. `devices.last_seen_at` is a heartbeat, not an
online state. Async messaging does **not** need this; intercom does. Another reason messaging
comes first.

### A mailbox

Somewhere for a message to wait until the recipient is present, plus a way to know they *are*
present. `servers/voiceprint-api` is the honest answer for shared devices — this is the use
case where speaker identification earns its keep. `announce.py` already covers the "speak a
generated line into a session" half.

### Real-time relay (only if intercom is ever pursued)

This cannot go through `ConversationSession` — that class is built around the STT → LLM → TTS
turn loop. It needs a parallel path.

**Known hazard:** `app/services/conversation/turn_stream.py:38-83` documents a real bug already
hit here — when synthesis runs slower than real time the pacer falls into *arrears* and
settles them by bursting, overrunning the device's 32-frame queue and distorting audio. The
fix caps arrears. A continuous device→device relay pushes on that exact mechanism from a new
direction and will need its own pacing story. This is the most expensive work in the whole
space, serving the variant with the weakest demand.

---

## 6. Risks

- **Privacy.** Drop In draws sustained criticism for letting a remote party open a
  microphone. The design must default to *receive-only* — deliver a message, never open a
  live mic one-way. Worth stating loudly as a differentiator rather than quietly avoiding.
- **Per-message model cost.** Measurable through the existing usage/quota services, but
  someone has to decide who pays: sender, or household owner.
- **Spam and abuse** once contacts extend past one household. Scoping v1 to
  within-household sidesteps nearly all of it.
- **Feature-drift into a messenger.** The value is reaching people who cannot use apps. Every
  step toward general-purpose chat competes with Zalo and loses.

---

## 7. Recommended sequence, if this is ever picked up

1. **Within-household directory.** Contacts resolve only to devices/users the same owner
   already controls. Small; no cross-user authz work.
2. **Async AI voice messaging.** Reuse `announce.py`; no presence needed. Cheapest possible
   test of whether the directory is worth anything.
3. **Measure.** If people actually send messages, then extend to cross-user and groups —
   and only then pay the `auth_guard` / IDOR design cost.
4. **Do not build real-time intercom** unless a clear B2B signal appears (the Relay pattern).
   In-home, it is not worth the parallel audio path.

---

## 8. Open questions

- Who is the paying customer — the adult child who buys the device, or the household?
- Does a message play automatically when the recipient is detected, or wait to be asked for?
  (Auto-play is the whole point for a non-smartphone user; it is also the privacy risk.)
- Is the message delivered as the sender's own recorded voice, as TTS of a transcript, or as
  an LLM-rewritten summary? Each has a different cost and a very different feel.
- Household model: does a "household" become a first-class entity, or is it just
  "everything one `user_id` owns"? v1 can assume the latter; v2 probably cannot.

## 9. Confidence

Medium-high on the direction, with one stated gap: **there are no public usage numbers for
Alexa Drop In.** The claim that in-home intercom sees little use rests on revealed behaviour
(Google removing it, Relay pivoting) rather than direct measurement. Real usage data from
Lugo devices would override this.

---

## Sources

- [What Is Alexa Drop In and How Does It Work? — Amazon](https://www.amazon.com/gp/help/customer/display.html?nodeId=GS3WRTSRKD2U6MCK)
- [The Amazon Echo now doubles as a home intercom system — TechCrunch](https://techcrunch.com/2017/06/26/the-amazon-echo-now-doubles-as-a-home-intercom-system)
- [Amazon Alexa: Getting Smart About Smart Homes — Ivey Business Review](https://www.iveybusinessreview.ca/magazine/articles/amazon-alexa-smart-homes)
- [Discontinuing calling between home devices as of Feb 2024 — Google Nest Community](https://www.googlenestcommunity.com/t5/Home-Automation/Discontinuing-calling-between-home-devices-as-of-feb-2024/m-p/600613)
- [This $50 device is trying to finally kill off the walkie-talkie — Fast Company](https://www.fastcompany.com/90435429/this-50-device-is-trying-to-finally-kill-off-the-walkie-talkie)
- [2025's Best Video Calling Devices for Seniors — ONSCREEN](https://onscreeninc.com/pages/best-video-calling-devices-for-seniors)
- [4 Best Cell Phones for Seniors — RAZ Mobility](https://www.razmobility.com/assistive-technology-blog/4-simple-cell-phones-for-seniors-a-review/)
- [Loa thông minh là gì? — OLLI](https://olli.vn/blogs/alls/loa-thong-minh-la-gi)
- [Loa thông minh OLLI Maika — OLLI](https://olli.vn/products/loa-thong-minh-maika)
