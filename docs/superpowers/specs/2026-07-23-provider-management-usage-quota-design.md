# Provider Management + Usage Attribution + Quota — Design

**Ngày:** 2026-07-23
**Trạng thái:** Design đã được duyệt (brainstorming), chờ review spec → writing-plans.

## 1. Mục tiêu

- Quản lý **Provider** (OpenAI, Gemini, OpenRouter, QwenCloud, …): cấu hình `base_url` + `api_key` **một lần**, tái dùng cho nhiều model.
- Model Registry chọn model từ provider → user dùng, không lặp lại credential mỗi dòng.
- **Usage attribution**: ghi nhận usage theo `user_id`/`profile_id` cho cả 3 kind (LLM/STT/TTS).
- **Cost**: quy mọi usage ra `cost_usd` từ bảng giá theo model (tùy chọn, gắn per-model).
- **Quota/rate-limit**: đặt hạn mức `$` theo 3 chiều — per-user/profile, per-provider, global — và **chặn** request khi vượt.

### Non-goals (YAGNI, để lại sau)
- Auto-discovery model từ `/models` API của provider.
- Billing/thanh toán thực; chỉ theo dõi + enforce nội bộ.
- Cắt request giữa chừng khi vượt quota (xem đánh đổi ở §7).
- Mã hóa api_key ở tầng ứng dụng vượt mức hiện có (giữ nguyên cách lưu credential hiện tại của registry, xem §9).

## 2. Hiện trạng (điểm liên quan)

- `ModelRegistryEntry` (`apps/api_gateway/app/services/db/models.py`) lưu `api_key` + `base_url` **theo từng dòng model** → thêm N model cùng provider = lặp credential N lần.
- Có sẵn "config sentinel row" (`model_id == ""`) làm engine-config, không phải model chọn được.
- LLM gọi qua `OpenAICompatResponder` (`conversation/responder.py`) → POST `{base_url}/chat/completions`. **Chưa bắt `usage`**; stream chưa gửi `stream_options.include_usage` → token stats hiện = 0.
- STT/TTS có provider cloud sẵn: `stt/providers/openrouter_provider.py`, `qwen3_asr_provider.py`; `tts/providers/qwen3_tts_provider.py`.
- DB migration = `Base.metadata.create_all` (`db/engine.py:141`) → **chỉ tạo bảng thiếu, KHÔNG ALTER cột**. Không có Alembic.
- `responder.py` **chưa cầm** `user_id`/`profile_id`. Memory/chat đã keyed theo `(user_id, profile_id)` ở tầng khác → identity tồn tại nhưng chưa thread xuống điểm gọi model.

## 3. Kiến trúc — Provider (Hướng A đã chọn)

Bảng `providers` mới, model tham chiếu provider **qua `config.provider_id`** (không thêm cột vào bảng cũ → tránh ALTER không được hỗ trợ).

```
providers
  id            str PK (uuid)
  name          str    # "openai" | "gemini" | "openrouter" | "qwencloud" | custom
  label         str
  base_url      str
  api_key       str    # lưu như credential registry hiện tại
  enabled       bool
  config        JSON   # header phụ, org id, timeout mặc định…
```

`model_registry_entries`: **không đổi schema**. Liên kết provider + giá đặt trong `config` JSON sẵn có:

```jsonc
{
  "provider_id": "<uuid providers.id>",   // rỗng/absent = engine local, dùng api_key/base_url cũ của entry
  "price": { "unit": "1M_tokens", "in": 0.15, "out": 0.60 }  // tùy chọn
}
```

### Resolve credential (thay đổi ở `model_registry/resolve.py`)
Khi build responder/provider cho một entry:
1. `config.provider_id` có giá trị → nạp `providers` tương ứng, dùng `provider.base_url` + `provider.api_key`.
2. Không có → fallback về `entry.base_url` + `entry.api_key` (giữ nguyên hành vi hiện tại cho ollama & engine local).

→ **Zero-migration cho engine local**; 1 key OpenAI dùng chung cho LLM + STT + TTS qua nhiều entry.

## 4. Pricing (per-model, tùy chọn, trong `config.price`)

- Áp dụng cho **mọi entry**, không chỉ cloud provider.
- Thiếu `price` → usage vẫn ghi (native units + attribution), `cost_usd = 0`, không ăn vào quota `$`.
- Có `price` (kể cả service tự deploy — imputed cost) → quy ra `$`.
- `unit` phân biệt kind:
  - LLM: `{ "unit": "1M_tokens", "in": <giá input>, "out": <giá output> }`
  - STT: `{ "unit": "minute", "rate": <giá/phút audio> }`
  - TTS: `{ "unit": "1k_chars", "rate": <giá/1k ký tự> }`
- Hàm `compute_cost(entry_config, usage) -> float` tập trung ở một module (`usage/pricing.py`), là nơi duy nhất biết công thức mỗi unit.

## 5. Usage schema

```
usage_events            # append-only, 1 dòng / request thành công (hoặc lỗi có billing)
  id            str PK
  ts            datetime (UTC, index)
  user_id       str  (index, "" = shared-device)
  profile_id    str  (index)
  provider_id   str  ("" nếu local)
  kind          str  # stt|tts|llm
  engine        str
  model_id      str
  unit          str  # tokens|seconds|chars
  native_amount float
  prompt_tokens int  nullable  # LLM
  completion_tokens int nullable
  cost_usd      float
  request_id    str  nullable
  status        str  # ok|error|blocked

usage_counters          # rollup cho quota check nhanh, không phải quét usage_events
  id            str PK
  scope         str  # user|provider|global
  scope_id      str  # user_id | provider_id | "" (global)
  period_key    str  # "2026-07" (monthly) | "total"
  cost_usd      float
  updated_at    datetime

quotas
  id            str PK
  scope         str  # user|provider|global
  scope_id      str  # user_id | provider_id | "" (global)
  limit_usd     float
  period        str  # monthly | total
  enabled       bool
```

Tất cả là **bảng mới** → `create_all` tự tạo, không migration thủ công.

## 6. Điểm bắt usage (3 chỗ)

| Kind | File | Cách lấy native_amount |
|------|------|------------------------|
| LLM  | `conversation/responder.py` | Non-stream: đọc `data["usage"]`. **Stream: thêm `stream_options:{include_usage:true}` vào JSON body**, đọc usage chunk cuối (chunk có `choices: []` + `usage`). |
| STT  | `stt/providers/*` (điểm chung sau khi transcribe) | Thời lượng audio (giây) — đã tính được từ WAV/decoded input. |
| TTS  | `tts/providers/*` (điểm chung trước synth) | `len(text)` ký tự input. |

Ghi usage đi qua **một hàm chung** `record_usage(ctx, kind, engine, model_id, provider_id, native)`:
1. Tính `cost_usd` qua `compute_cost`.
2. Insert `usage_events`.
3. Cộng dồn `usage_counters` cho cả 3 scope (user/provider/global) của `period_key` hiện tại.

`ctx` mang `user_id`/`profile_id`/`request_id` — **cần thread identity** từ tầng request/WS xuống các điểm gọi model (chi phí tích hợp chính của feature này).

## 7. Quota enforcement

**Pre-flight** (trước khi gọi provider, ở resolve/hot path):
- `quota_gate(ctx, provider_id)`: với mỗi scope áp dụng (user của ctx, provider_id, global), lấy `usage_counters` kỳ hiện tại, so với `quotas.limit_usd` (chỉ dòng `enabled`).
- Vượt bất kỳ scope nào → raise `QuotaExceededError` → request bị chặn, ghi `usage_events(status="blocked", cost_usd=0)` để audit.

**Post-call**: `record_usage` (§6).

**Đánh đổi (đã thống nhất):** không biết token/cost trước khi gọi LLM → enforcement là *"chặn nếu đã vượt"* (best-effort), cho phép lố nhẹ đúng 1 request cuối. Chuẩn công nghiệp (OpenAI dùng mô hình này). Không cắt giữa stream.

**Quota chỉ tính trên usage có `cost_usd > 0`** → engine local giá 0 không bao giờ bị chặn bởi quota `$` (đúng kỳ vọng).

## 8. API / Routes (mới, admin-gated)

- `providers`: CRUD (`GET/POST/PATCH/DELETE /api/providers`). `api_key` **mask** khi trả về (giống model_registry routes hiện tại).
- `model_registry` route hiện có: thêm chọn `provider_id` + `price` khi tạo/sửa entry (đi vào `config`).
- `quotas`: CRUD.
- `usage`: `GET /api/usage/summary?group_by=user|provider|model&period=…` cho dashboard; `GET /api/usage/me` cho user tự xem.

## 9. Bảo mật credential
- `providers.api_key` lưu **cùng cơ chế** như `ModelRegistryEntry.api_key` hiện tại (không thêm/bớt lớp mã hóa trong scope này — tránh mở rộng phạm vi). Mask ở API response. Nếu sau này cần mã hóa at-rest → issue riêng.

## 10. Migration & tương thích
- **Không ALTER** bảng cũ. Chỉ tạo bảng mới (`create_all`).
- Entry local hiện có: không có `config.provider_id` → resolve fallback về api_key/base_url cũ → **chạy y nguyên**.
- Có thể có bước seed nhẹ: tạo `providers` từ các entry cloud đang có (gom theo base_url) — **tùy chọn**, không bắt buộc cho lần đầu.

## 11. Testing
- Unit: `compute_cost` cho cả 3 unit (in/out tokens, minute, 1k_chars) + thiếu price = 0.
- Unit: resolve credential (có provider_id → provider; không có → fallback).
- Unit: `quota_gate` — dưới/bằng/vượt cho từng scope; local (cost 0) không bao giờ bị chặn.
- Unit: `record_usage` cộng dồn đúng `usage_counters` cho cả 3 scope + đúng `period_key`.
- Integration: LLM stream có `include_usage` → bắt đúng prompt/completion tokens.
- Integration: request bị chặn ghi `status="blocked"`.
- Chỉ chạy test của repo `apps/api_gateway` (theo quy ước scope-tests-to-changed-repo).

## 12. Câu hỏi mở / để sau
- Reset `usage_counters` đầu tháng: dùng `period_key="YYYY-MM"` nên "reset" là tự nhiên (kỳ mới = key mới, counter mới = 0). Dọn counter cũ = job phụ, chưa cần.
- Gemini native API khác OpenAI-compat: giả định đi qua endpoint OpenAI-compat (`base_url`). Nếu cần native Gemini → provider adapter riêng, ngoài scope.
- Dashboard UI chi tiết (biểu đồ) tách sang spec/plan riêng nếu cần.

## 13. Trạng thái triển khai (cập nhật 2026-07-26)

Đã bổ sung theo plan `plans/2026-07-26-usage-cost-p0.md`:
- `usage/attribution.py`: resolve `(engine, model_id)` khi ghi usage — hết
  `(none)`; quan trọng hơn: row có model rỗng KHÔNG khớp được registry row giữ
  giá nên trước đây luôn $0. Chỉ suy ra khi chắc chắn (engine có đúng 1 model
  non-sentinel), không đoán.
- `usage/backfill.py` (chạy lúc boot): backfill model_id cho row cũ khi suy được
  (270/307 row trên prod); không bao giờ tính lại `cost_usd` lịch sử.
- LLM usage lấy model từ `responder.model` (model thật đã gọi), không phải pin
  của profile.
- `/v1/usage/me` group thêm theo `engine`; UI hiện cột Engine và nhãn
  `(not recorded)` cho row không suy được.
- `usage/price_schema.py`: validate/normalize `config.price` khi ghi (unit suy
  từ kind, chặn field lạ / số âm / bool), áp cho cả POST/PATCH model_registry.
- `GET/PATCH /v1/model_registry/prices` + tab admin "Pricing".
- `kind="embed"` thành kind chính thức của Model Registry.
- Đo usage 4 call site của memory: extractor LLM, compactor LLM, embed facts,
  embed query mỗi lượt (`kind="embed"`).
- `quota_gate` cho memory hậu-session: vượt hạn mức thì bỏ qua + log.

Còn thiếu (xem audit 2026-07-26, nhóm P1/P2): audit row `status="blocked"`;
metering/gate cho `POST /v1/tts/stream` và `WS /v1/stt/stream`; `profile_id=""`
ở REST; hiển thị spend/limit trên tab Quotas và My Usage; rollup
`usage_counters`; validate quota (scope_id, trùng, limit<=0).

## 14. Quota enforcement gaps đã đóng (2026-07-26)

Theo plan `plans/2026-07-26-quota-enforcement-gaps.md`:
- **Quota theo provider giờ mới thực sự chạy.** Trước đó mọi gate tra
  `provider_id` bằng `find(kind, engine, "")` nên không khớp row nào và
  `_applies()` bỏ qua toàn bộ quota scope=provider (đo được: /transcribe,
  /synthesize, và turn hội thoại đều ra NONE). Nay mọi gate đi qua
  `resolve_usage_model` trước, giống `/chat`.
- **livehost đã có gate.** Trước đó `grep quota` trong `routes/livehost.py`
  không ra gì — cả hai đường turn (voice + social) chạy STT/LLM/TTS không kiểm.
- **Audit row `status="blocked"`** (spec §7) do chính `quota_gate` ghi, với
  `cost_usd = 0` và `native_amount = 0` để không bao giờ tự cộng vào spend đã
  gây ra block. Summary usage chỉ đếm `status="ok"`.
- **Validate quota:** `scope_id` bắt buộc với scope user/provider (rỗng sẽ khớp
  bucket thiết bị chung), `limit_usd > 0` (0 và số âm bị gate bỏ qua, tức là
  "không giới hạn" — ngược ý admin), và chặn trùng `(scope, scope_id, period)`.

Còn lại (xem audit 2026-07-26): metering + gate cho `POST /v1/tts/stream` và
`WS /v1/stt/stream`; `profile_id=""` ở REST; hiển thị spend/limit trên tab
Quotas và My Usage; client chưa xử lý 429 riêng; tab Pricing vẫn liệt kê row
sentinel; rollup `usage_counters` và prune `usage_events`.

## 15. Metering gaps đã đóng + cơ chế chống bỏ sót (2026-07-26)

Theo plan `plans/2026-07-26-close-metering-gaps.md`:
- **`WS /v1/stt/stream`**: gate lúc connect và mỗi lần `flush`/`end`; đo theo giây
  audio NHẬN được (trước VAD — provider tính tiền theo phần nó xử lý), một row mỗi
  lần finalize, cộng một row cuối cho phần chưa flush khi disconnect.
- **`POST /v1/tts/stream`**: thêm `Request` để có identity (trước đó không có),
  gate đồng bộ trả 429 trước khi spawn job, đo theo từng chunk đã synthesize.
- **Cơ chế chống bỏ sót** (`tests/unit/test_paid_call_site_inventory.py`): liệt kê
  mọi call site gọi provider từ source và bắt buộc mỗi cái phải có status + lý do +
  tên test bao phủ. Thêm call site mới, thêm call thứ hai vào file đã liệt kê, hay
  khai một test không tồn tại → CI đỏ. Kèm
  `tests/unit/test_every_paid_entry_point_meters.py` chạy thật từng entry point và
  assert có row trong DB — cái đầu bắt "quên", cái sau bắt "khai sai".
- **Add-time test call của Model Registry**: ĐO nhưng KHÔNG gate (`request_id =
  "registry-test-call"`) — admin hết quota vẫn phải test được provider để sửa
  đúng cái config làm họ hết quota.
- REST metering ghi `profile_id` khi có `?profile=`; `/chat` resolve trong guard;
  tab Pricing bỏ row sentinel (giá đặt ở đó không bao giờ khớp).
- **Hiển thị**: tab Quotas có cột Spent (%, đỏ khi vượt); `/v1/usage/me` trả thêm
  `limits` (chỉ quota user của chính caller + global, không lộ cross-tenant) và My
  Usage hiện chúng; 429 nay hiện đúng lý do thay vì lỗi chung chung (helper
  `quotaMessage` cho static UI, `QuotaExceededError` cho React client).

Còn lại: rollup `usage_counters` + prune `usage_events` (442 row, có index — chưa
cần); `status="error"` cho call lỗi sau khi provider đã tính tiền; `quota_store`
cache theo process (nhiều worker sẽ stale).
