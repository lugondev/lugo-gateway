# Lugo Web Client — Design

**Date:** 2026-07-16
**Status:** Approved (design), pending implementation plan

## Mục tiêu

Tách giao diện end-user ra khỏi webui admin hiện tại. Webui hiện tại
(`apps/api_gateway/app/static`, 29 ES modules) đã phình thành công cụ vận hành:
model registry, system config, engines, devices, users, MCP servers. Nhồi giao
diện người dùng cuối vào đó ép hai đối tượng rất khác nhau dùng chung một cây
điều hướng và một bundle.

Client mới — **Lugo** ("The AI Companion Platform") — tối ưu cho một luồng duy
nhất: nói chuyện với trợ lý.

## Nguyên tắc nền

**Bỏ role ở UI, không bỏ ở backend.** Web client không render gì liên quan tới
quản trị, nhưng cùng một API gateway phục vụ cả hai app. Việc client không có
màn hình admin không ngăn được ai gọi thẳng `/v1/system_config`.
`AuthGuardMiddleware` vẫn là ranh giới thật và không được nới lỏng.

## Ranh giới hệ thống

Ba thành phần tách bạch:

| Thành phần | Auth | Thay đổi |
|---|---|---|
| API gateway (`apps/api_gateway`) | session cookie **và** bearer | Thêm đường bearer |
| Admin webui (`app/static`) | session cookie, same-origin | Không đụng tới |
| Lugo web client (submodule mới) | bearer token, cross-origin | Mới |

Admin webui **không** migrate sang bearer. Hai cơ chế auth cùng tồn tại — đây là
chủ ý: migrate admin sẽ nhân đôi rủi ro mà không đem lại lợi ích nào.

## Auth

### Một phương thức, không fallback

Nếu request chìa ra `Authorization: Bearer`, thì bearer **là** danh tính của nó.
Token hỏng/hết hạn/user bị vô hiệu hoá → **401 dứt khoát**, không bao giờ âm
thầm rơi về danh tính của cookie.

Ban đầu HTTP làm ngược lại (bỏ qua bearer hỏng rồi dùng cookie) trong khi WS đã
fail-closed sẵn — hai lối không thống nhất chính sách. Đã sửa cho khớp nhau.
Ngoài tính nhất quán, điều này còn cần thiết để SPA biết đường gọi refresh: một
token hết hạn phải trả 401 rõ ràng, chứ không phải biến client thành một người
dùng khác mà nó không hề hay biết.

Header không phải bearer (ví dụ `Basic`) **không** kích hoạt lối này — nó đơn
giản không phải một lần thử bearer, nên vẫn rơi về cookie như cũ.

401 của lối bearer luôn là JSON, không bao giờ redirect về trang login kể cả khi
`Accept: text/html` — client mang token là API client, không phải trình duyệt
đang điều hướng.

### Bearer luôn là user, không ngoại lệ

Đường bearer hardcode `role = "user"`. Backend **không đọc role claim từ token**.
Không có đường nào trong code dẫn từ bearer tới `"admin"`.

Hệ quả: token giả mạo claim, thiếu claim, hay bị lẫn lộn đều không thể leo thang
đặc quyền — vì không có gì để leo. Admin đăng nhập vào Lugo cũng chỉ là user
thường; muốn quản trị thì vào admin webui bằng session cookie. Nghĩa là phải
đăng nhập hai chỗ — chấp nhận có ý thức.

### Ràng buộc bắt buộc với `actor.py`

`current_role()` hiện trả `"admin"` khi session có `user_id` mà thiếu `role`.
Docstring của nó ghi rõ invariant: không được viết `user_id` mà không viết
`role` cùng chỗ.

**Đường bearer phải set `role = "user"` tường minh ở cùng nơi nó set `user_id`.**
Nếu chỉ set `user_id`, request sẽ resolve thành admin. Đây là landmine cụ thể,
không phải rủi ro lý thuyết.

### Cấu trúc thay đổi

Tách phân giải danh tính khỏi kiểm tra quyền: một chỗ duy nhất trả về actor từ
**hoặc** session cookie **hoặc** `Authorization: Bearer`. Logic gate quyền
(`_USER_PREFIXES` / `_ADMIN_PREFIXES`) giữ nguyên không đổi — mọi route đang
được bảo vệ vẫn được bảo vệ y hệt, chỉ nguồn danh tính rộng hơn.

### Token

- Access token 1 giờ, cộng refresh token.
- **Hạn chế đã biết:** bearer không tra cứu khi validate → thu hồi không có hiệu
  lực tức thì. Ban user hoặc đổi mật khẩu thì token cũ vẫn sống tới hết 1 giờ.
  Cửa sổ này chỉ áp cho quyền user, không bao giờ chạm quyền admin. Nếu sau này
  cần thu hồi tức thì: thêm denylist nhỏ, không phải làm lại.
- **XSS:** SPA giữ token trong JS nên XSS đọc được. Đây là cái giá đã chấp nhận
  khi chọn bearer thay vì BFF. TTL 1h giới hạn thiệt hại.
- ⚠️ **`SESSION_SECRET` PHẢI được set ở env prod trước khi phase 1 ship.** Mặc
  định nó rỗng, và khi rỗng thì secret ký được sinh ngẫu nhiên **mỗi process**.
  Hệ quả: mọi refresh token 30 ngày chết theo mỗi lần restart/redeploy, nên
  `REFRESH_TTL_SECONDS = 30 ngày` chỉ trung thực khi biến này được set thật.
  Hành vi này khớp đúng cookie session trước đây (bảo toàn có chủ ý, không phải
  regression), nhưng main tự động deploy prod — nên nếu quên, SPA sẽ trông như
  ngẫu nhiên đăng xuất người dùng sau mỗi lần deploy.
- **WebSocket:** token qua subprotocol, **không** qua query string (query string
  bị ghi vào access log và lịch sử proxy).

### CORS

Domain riêng → cần CORS. Chỉ mở cho origin của web client, không wildcard.

**Trạng thái sau giai đoạn 0: làm một nửa, phần còn lại hoãn có chủ đích.**

- ✅ Đã làm: `allow_credentials=False`. Trước đó `allow_origins=["*"]` đi cùng
  `allow_credentials=True` khiến Starlette echo lại **mọi** origin kèm
  `Allow-Credentials: true` — mọi website đọc được response xác thực bằng
  cookie. Chưa khai thác được nhờ `SameSite=lax`, tức một lỗ hổng tiềm ẩn chỉ
  còn một lớp phòng thủ. Đã đóng.
- ✅ Đã làm: `CORSMiddleware` chuyển thành ngoài cùng. Trước đó nó nằm trong
  cùng nên mọi response 401/403 do `AuthGuardMiddleware` short-circuit đều
  không mang header CORS — SPA cross-origin sẽ thấy `TypeError: Failed to
  fetch` mờ đục thay vì 401, và luồng refresh không bao giờ chạy.
- ⏸️ **Hoãn sang phase 1: allowlist origin.** `cors_allow_origins` vẫn mặc định
  `"*"`. Lý do hoãn: domain của web client chưa chốt, và đoán sai rồi sửa lại
  còn tệ hơn hoãn có chủ đích. Chấp nhận được tạm thời vì credentials đã tắt —
  không response nào xác thực bằng cookie đọc được từ origin lạ, và bearer token
  thì origin lạ không có. **Việc phải làm khi domain có thật:** đặt
  `CORS_ALLOW_ORIGINS=https://<domain>` ở env prod (đây là cấu hình env, không
  phải sửa code).

## Web client

### Điều hướng — 4 mục, một trục chính

Đăng nhập xong vào **thẳng Talk**, không qua dashboard. Đúng với "companion":
không bắt người dùng điều hướng để làm việc chính.

- **Talk** — màn hình chính. Hội thoại giọng nói realtime (WS, VAD, barge-in,
  TTS phát về). Trạng thái nghe/nghĩ/nói thể hiện qua chính vòng tròn hở của
  logo: vòng xoay khi nghĩ, chấm cam pulse khi nghe.
- **History** — danh sách session, transcript, xoá phiên. **Audio phát lại: hoãn
  sang phase sau** — xem bên dưới.
- **Devices** — pair ESP32, **"lần cuối thấy"** (không phải trạng thái online —
  xem bên dưới), đặt tên. Dùng `/v1/devices/mine` và `/v1/devices/pair/claim`
  (đã nằm trong `_USER_PREFIXES`, được match trước `_ADMIN_PREFIXES` nên không
  cần nới quyền).
- **Tools** — STT/TTS thủ công gộp một chỗ, dạng đơn giản hoá của batch hiện có.

### Kiến trúc module

- **Lớp API client** — giữ token + refresh. Mọi màn hình gọi qua nó; không màn
  nào tự fetch. Một chỗ duy nhất biết về token.
- **Lớp audio/WS** — realtime capture + playback. Phần khó nhất, phải test được
  độc lập với React.
- **UI** — thuần, không biết gì về token hay WS.

### Branding

Palette (từ bộ nhận diện Lugo):

| Vai trò | Màu |
|---|---|
| Nền tối | `#111111`, `#2A2A2A` |
| Nền sáng | `#F7F4EE`, `#E8E1D6` |
| Accent | `#FF8A00` → `#FFC857` (gradient logo) |

- Nền tối mặc định cho **Talk**: màn hình dùng lâu, thường buổi tối, và logo
  trắng trên nền tối là bản mạnh nhất trong bộ nhận diện.
- Nền kem cho các màn đọc-nhiều như **History**.
- Cam **chỉ** dùng cho trạng thái hoạt động và hành động chính. Cam dùng cho mọi
  thứ thì không còn báo hiệu gì.

## Quyết định kỹ thuật

### React SPA + Vite (không Next.js)

Toàn bộ app nằm sau đăng nhập → SSR vô dụng (server không có token của user),
không có nhu cầu SEO. Vite build ra static, không cần Node runtime ở prod, và
audio/WS thuần client không vướng hydration.

### Submodule đúng chuẩn

Repo riêng + gitlink + **`.gitmodules` đăng ký đàng hoàng**.

**Tiền lệ đang hỏng:** `esp32-assistant` được commit ở mode `160000` (gitlink)
nhưng repo **không có `.gitmodules`**. Ai clone mới sẽ nhận thư mục rỗng và
không có cách nào biết phải clone gì vào đó, vì không URL nào được ghi lại.
Không nhân bản mô hình này. (Sửa `esp32-assistant` là việc riêng, ngoài phạm vi.)

## Kiểm thử

- **Backend auth (giai đoạn 0): TDD.** Phần bắt buộc phải chắc. Test tối thiểu
  phải phủ: bearer không bao giờ resolve thành admin; bearer thiếu/sai/hết hạn bị
  từ chối; session cookie vẫn hoạt động y như cũ cho admin webui;
  `_ADMIN_PREFIXES` từ chối bearer.
- **Client:** test lớp API client và lớp audio. UI thuần không test tự động ở
  giai đoạn đầu.

## Thứ tự triển khai

0. **Bearer auth ở backend** — có test, không UI. Làm xong và verify trước khi
   động tới client. Đây là phần rủi ro nhất.
1. Tạo submodule + khung SPA + login
2. Talk
3. History
4. Devices
5. Tools

## Hai lời hứa ban đầu đã phải chỉnh lại sau khi đo thực tế

### Audio phát lại trong History — hoãn có chủ đích

Spec bản đầu ghi History có "audio phát lại". **Không giao được ở phase này**, vì
ba lý do độc lập nhau, mỗi lý do đủ để chặn:

1. **History không lưu audio.** `get_messages()` trả đúng `{turn, role, content}`
   — thuần văn bản. Không có cột nào tham chiếu tới file audio.
2. **Artifacts bị dọn sau 24 giờ** (`artifacts_ttl_hours = 24.0`, janitor chạy
   mỗi giờ). Kể cả có link thì link cũng chết sau một ngày.
3. **Luồng của web client không sinh file audio nào cả.** Ta chọn
   `audio_out=opus`, nên audio đi thẳng qua WebSocket dưới dạng packet và không
   bao giờ được ghi ra đĩa. Không có file để mà phát lại.

Điểm 3 là hệ quả không tránh khỏi của chính quyết định `audio_out=opus` — quyết
định đúng (vì `/artifacts` không có auth, xem phần CORS), nhưng cái giá là mất
khả năng phát lại. Đây là đánh đổi có ý thức, không phải sơ suất.

**Muốn có audio phát lại thì cần, ở backend:** server vừa stream Opus vừa lưu
wav; thêm cột tham chiếu artifact vào bảng message; ngừng dọn (hoặc tăng TTL cho)
những file đó; và bảo vệ `/artifacts` bằng auth. Đó là thay đổi chính sách lưu
trữ, không phải một tính năng UI.

### "Trạng thái online" của Devices — thay bằng "lần cuối thấy"

Backend chỉ có `last_seen_at`, và nó chỉ được cập nhật khi thiết bị mở
WebSocket. Đó là **dấu vết quá khứ, không phải hiện diện thật**. Một chấm xanh
"Online" dựa trên nó thành lời nói dối ngay khi thiết bị rớt mạng 30 giây trước
— và người dùng sẽ tin nó rồi đi tìm lỗi nhầm chỗ.

UI hiển thị "lần cuối thấy" dạng tương đối. Ngoại lệ duy nhất: trong vòng 90
giây thì nói "Đang hoạt động" — khoảng đó đủ hẹp để không thành lời nói dối.
Muốn hiện diện thật thì backend cần một kênh presence, hiện chưa có.

## Nợ kỹ thuật đã biết (ghi nhận, không chặn merge)

- **`lugo.py` không echo subprotocol** trong khi `conversation.py`/`livehost.py`/
  `stt.py` đều có. Vì nó dùng chung `resolve_ws_identity`, một access token user
  giờ authenticate được `/v1/lugo/stream` — nhưng **không cấp gì thật**:
  capability nhận được là một phiên voice conversation, thứ mà chính token đó đã
  có qua `/v1/conversation/stream`; `device_id=None` nên không chạm device row.
  Trình duyệt cũng không dùng được vì subprotocol không được echo. Không sửa ở
  giai đoạn 0 vì plan đã chốt không đụng `lugo.py` (hệ device riêng). Khi phase 1
  chạm tới: hoặc echo cho nhất quán, hoặc thêm comment nói việc bỏ qua là cố ý —
  hiện đọc như sơ suất.

## Ngoài phạm vi

- Migrate admin webui sang bearer
- Sửa gitlink hỏng của `esp32-assistant`
- Denylist thu hồi token tức thì
- Test tự động cho UI
