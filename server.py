import os, json, time, threading, html, hashlib, requests, re, shlex
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, Request, HTTPException
# Thêm HTMLResponse để trả về giao diện web
from fastapi.responses import JSONResponse, HTMLResponse

# ----- .env (local); trên Render sẽ dùng Environment Variables -----
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

# =================== ENV ===================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
WEBHOOK_SECRET     = os.getenv("WEBHOOK_SECRET", "change-me-please")

# API có thể là getNotify (text) hoặc list-orders (JSON)
API_URL       = os.getenv("TAPHOA_API_ORDERS_URL", "")
API_METHOD    = os.getenv("TAPHOA_METHOD", "POST").upper()
HEADERS_ENV   = os.getenv("HEADERS_JSON") or "{}"
BODY_JSON_ENV = os.getenv("TAPHOA_BODY_JSON", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "12"))
VERIFY_TLS    = bool(int(os.getenv("VERIFY_TLS", "1")))
DISABLE_POLLER = os.getenv("DISABLE_POLLER", "0") == "1"

try:
    HEADERS: Dict[str, str] = json.loads(HEADERS_ENV)
except Exception:
    HEADERS = {}

# =================== APP ===================
app = FastAPI(title="TapHoa → Telegram (Poller only)")

SEEN_JSON_IDS: set[str] = set()      # nếu sau này xài JSON list-orders
# [ĐÃ SỬA] Lưu lại các SỐ lần cuối (thay cho LAST_NOTIFY)
LAST_NOTIFY_NUMS: List[int] = []     

# =================== Telegram ===================
def tg_send(text: str):
    """Gửi an toàn (chặn lỗi 400: text is too long)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Missing TELEGRAM_* env")
        return

    MAX = 3900  # chừa biên cho parse_mode=HTML (HTML entities nở ra)
    chunks = [text[i:i+MAX] for i in range(0, len(text), MAX)] or [""]

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for idx, part in enumerate(chunks[:3]):  # tối đa 3 message/1 lần
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": part,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }, timeout=20)
        if r.status_code >= 400:
            print("Telegram error:", r.status_code, r.text)
            break

# =================== Helpers ===================
def _labels_for_notify(parts_len: int) -> List[str]:
    # [ĐÃ SỬA THEO YÊU CẦU CỦA BẠN]
    # Gán tên cho 8 cột
    if parts_len == 8:
        # c1 là đơn hàng sản phẩm
        # c2 là đánh giá
        # c5 là đặt trước
        # c6 là đơn hàng dịch vụ
        # c8 là tin nhắn
        # c7 mình đoán là "Khiếu nại" từ ảnh cũ
        return [
            "Đơn hàng sản phẩm",  # c1
            "Đánh giá",          # c2
            "Chưa rõ 3",         # c3 (Bạn tự đổi tên nếu biết)
            "Chưa rõ 4",         # c4 (Bạn tự đổi tên nếu biết)
            "Đặt trước",          # c5
            "Đơn hàng dịch vụ",   # c6
            "Khiếu nại",         # c7 (Bạn tự đổi tên nếu biết)
            "Tin nhắn"            # c8
        ]
    
    # Fallback cho trường hợp API trả về số lượng cột khác
    return [f"c{i+1}" for i in range(parts_len)]

def parse_notify_text(text: str) -> Dict[str, Any]:
    s = (text or "").strip()
    parts = s.split("|") if s else []
    if all(re.fullmatch(r"\d+", p or "") for p in parts):
        nums = [int(p) for p in parts]
        labels = _labels_for_notify(len(nums))
        table = {labels[i]: nums[i] for i in range(len(nums))}
        return {"raw": s, "numbers": nums, "table": table}
    return {"raw": s}

# ----- [TOOL CHỈNH SỬA TỰ ĐỘNG] -----
# Hàm này đã được nâng cấp để tự động xử lý cURL thô
def parse_curl_command(curl_text: str) -> Dict[str, Any]:
    """
    [ĐÃ CẬP NHẬT] Nhận 'Copy as cURL (bash)' từ DevTools.
    Tự động xử lý -b (cookie) và -H (header), và lọc bỏ header rác.
    Trả về: {"url","method","headers","body"}
    """
    args = shlex.split(curl_text)
    method = "GET"
    headers: Dict[str, str] = {}
    data = None
    url = ""

    i = 0
    while i < len(args):
        a = args[i]
        if a == "curl":
            i += 1
            if i < len(args):
                url = args[i]
        elif a in ("-X", "--request"):
            i += 1
            if i < len(args):
                method = args[i].upper()
        elif a in ("-H", "--header"):
            i += 1
            if i < len(args):
                h = args[i]
                if ":" in h:
                    k, v = h.split(":", 1)
                    headers[k.strip()] = v.strip()
        # ----- TỰ ĐỘNG XỬ LÝ -b -----
        elif a in ("-b", "--cookie"):
            i += 1
            if i < len(args):
                # Tự động đổi -b 'value' thành -H 'cookie: value'
                headers['cookie'] = args[i]
        # ----- KẾT THÚC PHẦN THÊM -----
        elif a in ("--data", "--data-raw", "--data-binary", "-d"):
            i += 1
            if i < len(args):
                data = args[i]
        i += 1

    if method == "GET" and data is not None:
        method = "POST"
    
    # ----- TỰ ĐỘNG DỌN DẸP HEADER RÁC -----
    final_headers: Dict[str, str] = {}
    # Các header rác thường thấy
    junk_prefixes = ('sec-ch-ua', 'sec-fetch-', 'priority', 'accept', 'content-length')
    for key, value in headers.items():
        low_key = key.lower()
        is_junk = False
        for prefix in junk_prefixes:
            if low_key.startswith(prefix):
                is_junk = True
                break
        if not is_junk:
            final_headers[key] = value
    # ----- KẾT THÚC PHẦN DỌN DẸP -----

    # Nếu không lọc được gì (ví dụ: cURL quá lạ), trả về bản gốc
    if not final_headers and headers:
         return {"url": url, "method": method, "headers": headers, "body": data}

    return {"url": url, "method": method, "headers": final_headers, "body": data}
# ----- [HẾT TOOL CHỈNH SỬA TỰ ĐỘNG] -----


# =================== Poller ===================

# [HÀM poll_once ĐÃ ĐƯỢC VIẾT LẠI HOÀN TOÀN]
def poll_once():
    """
    - [ĐÃ SỬA] Chỉ check getNotify (text) và xử lý JSON (nếu có).
    - [ĐÃ SỬA] Logic chỉ thông báo khi SỐ TĂNG LÊN (0->1, 1->2).
    - [ĐÃ SỬA] Bỏ qua thông báo khi SỐ GIẢM (1->0).
    - [ĐÃ SỬA] Gộp icon, sắp xếp thứ tự ưu tiên.
    """
    global LAST_NOTIFY_NUMS, API_URL, API_METHOD, HEADERS, BODY_JSON_ENV

    if not API_URL:
        print("No API_URL set")
        return

    try:
        body_json = None
        if API_METHOD == "POST" and BODY_JSON_ENV:
            try:
                body_json = json.loads(BODY_JSON_ENV)
            except Exception:
                body_json = None

        # call
        if API_METHOD == "POST":
            r = requests.post(API_URL, headers=HEADERS, json=body_json, verify=VERIFY_TLS, timeout=25)
        else:
            r = requests.get(API_URL, headers=HEADERS, verify=VERIFY_TLS, timeout=25)

        # 1) thử JSON trước (API list-orders)
        try:
            data = r.json()
        except Exception:
            data = None

        if data is not None:
            # (Phần xử lý JSON API này giữ nguyên, nó dành cho API list-orders)
            rows: List[Dict[str, Any]] = []
            if isinstance(data, list):
                rows = [x for x in data if isinstance(x, dict)]
            elif isinstance(data, dict):
                for key in ("data","items","rows","list","orders","result","content"):
                    v = data.get(key)
                    if isinstance(v, list):
                        rows = [x for x in v if isinstance(x, dict)]
                        break
                if not rows:  # lồng 1 lớp
                    for v in data.values():
                        if isinstance(v, dict):
                            for key in ("data","items","rows","list","orders","result","content"):
                                vv = v.get(key)
                                if isinstance(vv, list):
                                    rows = [x for x in vv if isinstance(x, dict)]
                                    break
                        if rows:
                            break
            if rows:
                sent = 0
                for o in rows:
                    uid = str(o.get("order_id") or o.get("id") or hashlib.md5(
                        json.dumps(o, sort_keys=True, ensure_ascii=False).encode("utf-8")
                    ).hexdigest())
                    if uid in SEEN_JSON_IDS:
                        continue
                    SEEN_JSON_IDS.add(uid)
                    buyer = html.escape(str(o.get("buyer_name") or o.get("buyer") or o.get("customer") or "N/A"))
                    total = o.get("total") or o.get("grand_total") or o.get("price_total")
                    msg = (
                        f"🛒 <b>ĐƠN MỚI</b>\n"
                        f"• Mã: <b>{html.escape(uid)}</b>\n"
                        f"• Người mua: <b>{buyer}</b>\n"
                        f"• Tổng: <b>{total}</b>"
                    )
                    tg_send(msg)
                    sent += 1
                if sent:
                    print(f"Sent {sent} order(s) from JSON API.")
                return  # kết thúc nếu là JSON

        # 2) không phải JSON → text (getNotify)
        text = (r.text or "").strip()
        if not text:
            print("getNotify: empty response")
            return

        # Nhận diện HTML (Cloudflare/login…) và gửi preview ngắn
        low = text[:200].lower()
        if low.startswith("<!doctype") or "<html" in low:
            preview = html.escape(text[:800])
            msg = (
                "⚠️ <b>getNotify trả về HTML</b> (có thể cookie/CF token hết hạn hoặc header thiếu).\n"
                f"Độ dài: {len(text)} ký tự. Preview:\n<code>{preview}</code>\n"
                "→ Cập nhật HEADERS_JSON bằng 'Copy as cURL (bash)': cookie, x-csrf-token, user-agent, referer, x-requested-with…"
            )
            # Chỉ gửi cảnh báo HTML nếu nó khác với lần trước
            if text != str(LAST_NOTIFY_NUMS): # check tạm
                tg_send(msg)
            print("HTML detected, preview sent. Probably headers/cookie expired.")
            return

        # Text quá dài → rút gọn để tránh 400
        if len(text) > 1200:
            preview = html.escape(text[:1200])
            msg = (
                "ℹ️ <b>getNotify (rút gọn)</b>\n"
                f"Độ dài: {len(text)} ký tự. Preview:\n<code>{preview}</code>"
            )
            tg_send(msg)
            return
        
        # ----- [BẮT ĐẦU LOGIC MỚI CỦA BẠN] -----
        parsed = parse_notify_text(text)
        
        if "numbers" in parsed:
            current_nums = parsed["numbers"]
            
            # Nếu số cột thay đổi, reset và coi như lần đầu
            if len(current_nums) != len(LAST_NOTIFY_NUMS):
                LAST_NOTIFY_NUMS = [0] * len(current_nums)

            # Hàm tạo icon (đã thêm Tin nhắn)
            def get_icon_for_label(label: str) -> str:
                low_label = label.lower()
                if "sản phẩm" in low_label: return "📦"
                if "dịch vụ" in low_label: return "🛎️"
                if "khiếu nại" in low_label: return "⚠️"
                if "đặt trước" in low_label: return "⏰"
                if "reseller" in low_label: return "👥"
                if "đánh giá" in low_label: return "💬"
                if "tin nhắn" in low_label: return "✉️"
                return "•" # Mặc định

            labels = _labels_for_notify(len(current_nums))
            results = {} # Dùng dict để lưu kết quả
            has_new_notification = False

            # 1. So sánh giá trị MỚI và CŨ
            for i in range(len(current_nums)):
                current_val = current_nums[i]
                last_val = LAST_NOTIFY_NUMS[i]
                
                # [YÊU CẦU CHÍNH] Chỉ kích hoạt khi SỐ TĂNG LÊN
                if current_val > last_val:
                    has_new_notification = True
                
                # Chỉ lưu lại các mục có số > 0 để hiển thị
                if current_val > 0:
                    label = labels[i]
                    icon = get_icon_for_label(label)
                    results[label] = f"{icon} <b>{label}</b>: <b>{current_val}</b>"

            # 2. Gửi thông báo NẾU CÓ ÍT NHẤT 1 MỤC TĂNG
            if has_new_notification:
                # [SẮP XẾP THỨ TỰ]
                # Đây là thứ tự ưu tiên bạn yêu cầu (C1, C6, C5, C7, C8)
                # Mình thêm C2 (Đánh giá) vào cuối
                ordered_labels = [
                    "Đơn hàng sản phẩm",  # c1
                    "Đơn hàng dịch vụ",   # c6
                    "Đặt trước",          # c5
                    "Khiếu nại",         # c7 (mình đoán)
                    "Tin nhắn",            # c8
                    "Đánh giá"            # c2
                ]
                
                lines = []
                # Thêm các mục theo thứ tự ưu tiên
                for label in ordered_labels:
                    if label in results:
                        lines.append(results.pop(label)) # Thêm và xóa khỏi dict
                
                # Thêm các mục còn lại (không có trong thứ tự ưu tiên, vd: c3, c4)
                for remaining_line in results.values():
                    lines.append(remaining_line)
                
                # Chỉ gửi nếu có nội dung
                if lines:
                    detail = "\n".join(lines)
                    # [SỬA] Tiêu đề thông báo rõ ràng hơn
                    msg = f"🔔 <b>TapHoa có thông báo mới</b>\n{detail}"
                    tg_send(msg)
                    print("getNotify changes (INCREASE) -> Telegram sent.")
                else:
                    # Trường hợp này GẦN NHƯ không xảy ra
                    # (vd: 0|0 -> 0|1 nhưng 1|0 -> 0|0)
                    print("getNotify changes (INCREASE but all 0) -> Skipping.")
            else:
                print("getNotify unchanged or DECREASED -> Skipping.")

            # 3. Cập nhật trạng thái CŨ = MỚI để check lần sau
            LAST_NOTIFY_NUMS = current_nums
        
        else:
            # Xử lý trường hợp getNotify trả về text lạ (không phải số)
            if text != str(LAST_NOTIFY_NUMS): # So sánh tạm
                msg = f"🔔 <b>TapHoa getNotify thay đổi</b>\n<code>{html.escape(text)}</code>"
                tg_send(msg)
                print("getNotify (non-numeric) changed -> Telegram sent.")
        # ----- [KẾT THÚC LOGIC MỚI] -----

    except Exception as e:
        print("poll_once error:", e)

def poller_loop():
    print("▶ poller started (getNotify compatible)")
    poll_once()
    while True:
        time.sleep(POLL_INTERVAL)
        poll_once()

# =================== API endpoints ===================

@app.get("/", response_class=HTMLResponse)
async def get_curl_ui():
    """
    Trả về giao diện HTML đơn giản để dán cURL.
    Form này sẽ gọi API /debug/set-curl
    """
    html_content = """
    <!DOCTYPE html>
    <html lang="vi">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Cập nhật cURL Poller</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 2rem; background-color: #f4f7f6; }
            .container { max-width: 800px; margin: 0 auto; background: #fff; padding: 2rem; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.05); }
            h1 { color: #333; }
            textarea { width: 98%; height: 250px; padding: 10px; border: 1px solid #ccc; border-radius: 4px; font-family: monospace; font-size: 14px; margin-top: 0.5rem; }
            label { display: block; margin-top: 1rem; margin-bottom: 0.5rem; font-weight: 600; }
            input[type="password"] { width: 98%; padding: 10px; border: 1px solid #ccc; border-radius: 4px; margin-top: 0.5rem;}
            button { background-color: #007bff; color: white; padding: 12px 20px; border: none; border-radius: 4px; cursor: pointer; font-size: 16px; margin-top: 1.5rem; }
            button:hover { background-color: #0056b3; }
            #status { margin-top: 1.5rem; padding: 10px; border-radius: 4px; font-weight: 600; display: none; }
            .success { display: block; background-color: #e0ffe0; border: 1px solid #00c000; color: #006000; }
            .error { display: block; background-color: #ffe0e0; border: 1px solid #c00000; color: #600000; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Cập nhật Poller bằng cURL (bash)</h1>
            <p>Dán nội dung 'Copy as cURL (bash)' từ DevTools (F12) vào đây.</p>
            
            <form id="curl-form">
                <label for="curl_text">Lệnh cURL (bash):</label>
                <textarea id="curl_text" name="curl" placeholder="curl 'https://taphoammo.net/api/getNotify' ..." required></textarea>
                
                <label for="secret_key">Secret Key:</label>
                <input type="password" id="secret_key" name="secret" placeholder="Nhập WEBHOOK_SECRET của bạn" required>
                
                <button type="submit">Cập nhật và Chạy Thử</button>
            </form>
            
            <p id="status"></p>
        </div>
        
        <script>
            document.getElementById("curl-form").addEventListener("submit", async function(e) {
                e.preventDefault();
                
                const curlText = document.getElementById("curl_text").value;
                const secret = document.getElementById("secret_key").value;
                const statusEl = document.getElementById("status");
                
                statusEl.textContent = "Đang xử lý...";
                statusEl.className = "";
                
                if (!curlText || !secret) {
                    statusEl.textContent = "Vui lòng nhập cả cURL và Secret Key.";
                    statusEl.className = "error";
                    return;
                }
                
                try {
                    const response = await fetch(`/debug/set-curl?secret=${encodeURIComponent(secret)}`, {
                        method: "POST",
                        headers: {
                            "Content-Type": "application/json"
                        },
                        body: JSON.stringify({
                            curl: curlText
                        })
                    });
                    
                    const result = await response.json();
                    
                    if (response.ok) {
                        statusEl.textContent = "Cập nhật thành công! Đã chạy thử 1 lần. Poller sẽ dùng cấu hình mới này.";
                        statusEl.className = "success";
                        console.log("Applied:", result.using);
                    } else {
                        statusEl.textContent = `Lỗi: ${result.detail || 'Lỗi không xác định.'}`;
                        statusEl.className = "error";
                    }
                } catch (err) {
                    statusEl.textContent = `Lỗi kết nối: ${err.message}`;
                    statusEl.className = "error";
                }
            });
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


@app.get("/healthz")
def health():
    return {
        "ok": True,
        "poller": not DISABLE_POLLER,
        "seen_json": len(SEEN_JSON_IDS),
        "last_notify_nums": LAST_NOTIFY_NUMS, # [ĐÃ SỬA]
        "api": {"url": API_URL, "method": API_METHOD}
    }

@app.get("/debug/notify-now")
def debug_notify(secret: str):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    before = str(LAST_NOTIFY_NUMS) # [ĐÃ SỬA]
    poll_once()
    after = str(LAST_NOTIFY_NUMS) # [ĐÃ SỬA]
    return {"ok": True, "last_before": before, "last_after": after}

@app.post("/debug/parse-curl")
async def debug_parse_curl(req: Request, secret: str):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    body = await req.json()
    curl_txt = str(body.get("curl") or "")
    parsed = parse_curl_command(curl_txt)
    return {
        "ok": True,
        "parsed": parsed,
        "env_suggestion": {
            "TAPHOA_API_ORDERS_URL": parsed["url"],
            "TAPHOA_METHOD": parsed["method"],
            "HEADERS_JSON": parsed["headers"],
            "TAPHOA_BODY_JSON": parsed["body"] or ""
        }
    }

@app.post("/debug/set-curl")
async def debug_set_curl(req: Request, secret: str):
    """
    Áp cURL tạm thời trong process (không ghi ENV). Dùng để test nhanh trên Render.
    """
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    body = await req.json()
    curl_txt = str(body.get("curl") or "")
    parsed = parse_curl_command(curl_txt)

    global API_URL, API_METHOD, HEADERS, BODY_JSON_ENV
    API_URL = parsed["url"]
    API_METHOD = parsed["method"]
    HEADERS = parsed["headers"]
    BODY_JSON_ENV = parsed["body"] or ""

    poll_once()
    return {
        "ok": True,
        "using": {
            "url": API_URL,
            "method": API_METHOD,
            "headers": HEADERS,
            "body": BODY_JSON_ENV
        },
        "note": "Applied for current process only. Update Render Environment to persist."
    }

# [ĐÃ XÓA] Đã xóa endpoint /taphoammo (webhook) theo yêu cầu

# =================== START ===================
def _maybe_start():
    if DISABLE_POLLER:
        print("Poller disabled by env.")
        return
    t = threading.Thread(target=poller_loop, daemon=True)
    t.start()

_maybe_start()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
