
# TapHoa → Telegram (Render 24/7)

Một web duy nhất (FastAPI) vừa có `/taphoammo` để nhận webhook, vừa có **poller chạy nền** tự kéo danh sách đơn và gửi Telegram.

## Cấu trúc
```
server.py
requirements.txt
render.yaml
.env.example
```

## Deploy trên Render
1. Push 4 file này lên GitHub.
2. Trên Render → **New → Blueprint** → chọn repo này (Render sẽ đọc `render.yaml`).
3. Thêm **Environment Variables** cho service:
   - `TELEGRAM_BOT_TOKEN` – token bot
   - `TELEGRAM_CHAT_ID` – ID chat nhận tin
   - `WEBHOOK_SECRET` – chuỗi bí mật cho `/taphoammo`
   - `TAPHOA_API_ORDERS_URL` – URL API **danh sách đơn** (KHÔNG phải `/api/getNotify`)
   - `TAPHOA_METHOD` – `GET` hoặc `POST`
   - `HEADERS_JSON` – JSON 1 dòng (cookie, x-csrf-token, user-agent, referer, x-requested-with, ...)
   - `TAPHOA_BODY_JSON` – nếu `POST` body là JSON thì dán nguyên JSON; nếu `GET` để trống
   - (tuỳ chọn) `POLL_INTERVAL` – giây; `VERIFY_TLS` – 1/0; `DISABLE_POLLER` – 1 để tắt poller
4. Build & deploy xong, kiểm tra `GET /healthz`.
5. Test webhook:
   ```bash
   curl -X POST "https://<service>.onrender.com/taphoammo" \
     -H "X-Auth-Secret: change-me-please" \
     -H "Content-Type: application/json" \
     -d '{"order_id":"TEST123","buyer_name":"demo","shop":"Demo","product_name":"Mở khóa < 100","quantity":3,"price":1500,"total":4500,"status":"Tạm giữ","created_at":"2025-10-27 14:08"}'
   ```

## Lấy đúng API “danh sách đơn”
Chrome DevTools → Network → **Fetch/XHR** → bấm **Tìm đơn hàng** → chọn request có **Preview/Response là JSON** (mảng `[...]` hoặc `{"data":[...]}`…), **không phải** `0|0|0|...`. Chuột phải → **Copy as cURL (bash)** rồi map:
- URL → `TAPHOA_API_ORDERS_URL`
- Method → `TAPHOA_METHOD`
- Headers quan trọng → `HEADERS_JSON`
- Body JSON (nếu có) → `TAPHOA_BODY_JSON`
