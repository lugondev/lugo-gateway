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
- **WebSocket:** token qua subprotocol, **không** qua query string (query string
  bị ghi vào access log và lịch sử proxy).

### CORS

Domain riêng → cần CORS. Chỉ mở cho origin của web client, không wildcard.

## Web client

### Điều hướng — 4 mục, một trục chính

Đăng nhập xong vào **thẳng Talk**, không qua dashboard. Đúng với "companion":
không bắt người dùng điều hướng để làm việc chính.

- **Talk** — màn hình chính. Hội thoại giọng nói realtime (WS, VAD, barge-in,
  TTS phát về). Trạng thái nghe/nghĩ/nói thể hiện qua chính vòng tròn hở của
  logo: vòng xoay khi nghĩ, chấm cam pulse khi nghe.
- **History** — danh sách session, transcript, audio phát lại.
- **Devices** — pair ESP32, trạng thái online, đặt tên. Dùng
  `/v1/devices/mine` và `/v1/devices/pair/claim` (đã nằm trong `_USER_PREFIXES`,
  được match trước `_ADMIN_PREFIXES` nên không cần nới quyền).
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

## Ngoài phạm vi

- Migrate admin webui sang bearer
- Sửa gitlink hỏng của `esp32-assistant`
- Denylist thu hồi token tức thì
- Test tự động cho UI
