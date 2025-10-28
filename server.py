import os, json, time, threading, html, hashlib, requests, re, shlex
from typing import Any, Dict, List, Optional
# [THÊM MỚI] Import defaultdict cho baseline
from collections import defaultdict
from fastapi import FastAPI, Request, HTTPException
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
# [ĐÃ SỬA] Lưu lại các SỐ lần cuối
LAST_NOTIFY_NUMS: List[int] = []     

# =================== Telegram ===================
def tg_send(text: str):
    """Gửi an toàn (chặn lỗi 400: text is too long)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Missing TELEGRAM_* env")
        return

    MAX = 3900  # chừa biên cho parse_mode=HTML
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
        # c7 là "Khiếu nại"
        return [
            "Đơn hàng sản phẩm",  # c1
            "Đánh giá",          # c2
            "Chưa rõ 3",         # c3
            "Chưa rõ 4",         # c4
            "Đặt trước",          # c5
            "Đơn hàng dịch vụ",   # c6
            "Khiếu nại",         # c7
            "Tin nhắn"            # c8
        ]
    
    return [f"c{i+1}" for i in range(parts_len)]

# ----- [THÊM MỚI] MỐC CƠ BẢN (BASELINE) -----
# Chỉ hiển thị các mục nếu giá trị của chúng LỚN HƠN mốc cơ bản.
# Mặc định là 0, "Khiếu nại" là 1 (hoặc 4, tùy bạn).
COLUMN_BASELINES = defaultdict(int)
COLUMN_BASELINES["Khiếu nại"] = 1
# ----------------------------------------------


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
        elif a in ("-b", "--cookie"):
            i += 1
            if i < len(args):
                headers['cookie'] = args[i]
        elif a in ("--data", "--data-raw", "--data-binary", "-d"):
            i += 1
            if i < len(args):
                data = args[i]
        i += 1

    if method == "GET" and data is not None:
        method = "POST"
    
    final_headers: Dict[str, str] = {}
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

    if not final_headers and headers:
         return {"url": url, "method": method, "headers": headers, "body": data}

    return {"url": url, "method": method, "headers": final_headers, "body": data}
# ----- [HẾT TOOL CHỈNH SỬA TỰ ĐỘNG] -----


# =================== Poller ===================
def poll_once():
    """
    [LOGIC ĐÃ CẬP NHẬT HOÀN TOÀN]
    - Chỉ check getNotify (text) và xử lý JSON (nếu có).
    - Logic chỉ thông báo khi SỐ TĂNG LÊN (0->1, 1->2).
    - Bỏ qua thông báo khi SỐ GIẢM (1->0).
    - Chỉ hiển thị các mục > BASELINE (fix lỗi Khiếu nại: 1).
    - Gộp icon, sắp xếp thứ tự ưu tiên.
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

        # Nhận diện HTML (Cloudflare/login…)
        low = text[:200].lower()
        if low.startswith("<!doctype") or "<html" in low:
            preview = html.escape(text[:800])
            msg = (
                "⚠️ <b>getNotify trả về HTML</b> (có thể cookie/CF token hết hạn hoặc header thiếu).\n"
                f"Độ dài: {len(text)} ký tự. Preview:\n<code>{preview}</code>\n"
                "→ Cập nhật HEADERS_JSON bằng 'Copy as cURL (bash)'."
            )
            if text != str(LAST_NOTIFY_NUMS):
                tg_send(msg)
            print("HTML detected, preview sent. Probably headers/cookie expired.")
            return

        # Text quá dài
        if len(text) > 1200:
            preview = html.escape(text[:1200])
            msg = (
                f"ℹ️ <b>getNotify (rút gọn)</b>\n"
                f"Độ dài: {len(text)} ký tự. Preview:\n<code>{preview}</code>"
            )
            tg_send(msg)
            return
        
        # ----- [BẮT ĐẦU LOGIC MỚI CỦA BẠN] -----
        parsed = parse_notify_text(text)
        
        if "numbers" in parsed:
            current_nums = parsed["numbers"]
            
            if len(current_nums) != len(LAST_NOTIFY_NUMS):
                LAST_NOTIFY_NUMS = [0] * len(current_nums)

            # Hàm tạo icon
            def get_icon_for_label(label: str) -> str:
                low_label = label.lower()
                if "sản phẩm" in low_label: return "📦"
                if "dịch vụ" in low_label: return "🛎️"
                if "khiếu nại" in low_label: return "⚠️"
                if "đặt trước" in low_label: return "⏰"
                if "reseller" in low_label: return "👥"
                if "đánh giá" in low_label: return "💬"
                if "tin nhắn" in low_label: return "✉️"
                return "•"

            labels = _labels_for_notify(len(current_nums))
            results = {} # Dùng dict để lưu kết quả
            has_new_notification = False

            # 1. So sánh giá trị MỚI và CŨ
            for i in range(len(current_nums)):
                current_val = current_nums[i]
                last_val = LAST_NOTIFY_NUMS[i]
                label = labels[i] # Lấy label
                
                # [YÊU CẦU CHÍNH] Chỉ kích hoạt khi SỐ TĂNG LÊN
                if current_val > last_val:
                    has_new_notification = True
                
                # [FIX KHIẾU NẠI] Lấy mốc cơ bản (baseline)
                baseline = COLUMN_BASELINES[label]

                # [FIX KHIẾU NẠI] Chỉ hiển thị nếu giá trị LỚN HƠN mốc cơ bản
                if current_val > baseline:
                    icon = get_icon_for_label(label)
                    results[label] = f"{icon} <b>{label}</b>: <b>{current_val}</b>"

            # 2. Gửi thông báo NẾU CÓ ÍT NHẤT 1 MỤC TĂNG
            if has_new_notification:
                # [SẮP XẾP THỨ TỰ] (C1, C6, C5, C7, C8, C2)
                ordered_labels = [
                    "Đơn hàng sản phẩm",  # c1
                    "Đơn hàng dịch vụ",   # c6
                    "Đặt trước",          # c5
                    "Khiếu nại",         # c7
                    "Tin nhắn",            # c8
                    "Đánh giá"            # c2
                ]
                
                lines = []
                # Thêm các mục theo thứ tự ưu tiên
                for label in ordered_labels:
                    if label in results:
                        lines.append(results.pop(label))
                
                # Thêm các mục còn lại (vd: c3, c4)
                for remaining_line in results.values():
                    lines.append(remaining_line)
                
                if lines:
                    detail = "\n".join(lines)
                    msg = f"🔔 <b>TapHoa có thông báo mới</b>\n{detail}"
                    tg_send(msg)
                    print("getNotify changes (INCREASE) -> Telegram sent.")
                else:
                    print("getNotify changes (INCREASE but all <= baseline) -> Skipping.")
            else:
                print("getNotify unchanged or DECREASED -> Skipping.")

            # 3. Cập nhật trạng thái CŨ = MỚI để check lần sau
            LAST_NOTIFY_NUMS = current_nums
        
        else:
            # Xử lý trường hợp getNotify trả về text lạ
            if text != str(LAST_NOTIFY_NUMS):
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

# [GIAO DIỆN MỚI] Đã viết lại toàn bộ HTML/CSS/JS cho "siêu đẹp"
@app.get("/", response_class=HTMLResponse)
async def get_curl_ui():
    """
    Trả về giao diện HTML "siêu đẹp" để dán cURL.
    """
    html_content = """
    <!DOCTYPE html>
    <html lang="vi">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Cập nhật cURL Poller</title>
        <style>
            :root {
                --bg-color: #f8f9fa;
                --text-color: #212529;
                --card-bg: #ffffff;
                --border-color: #dee2e6;
                --primary-color: #007bff;
                --primary-hover: #0056b3;
                --success-bg: #d4edda;
                --success-border: #c3e6cb;
                --success-text: #155724;
                --error-bg: #f8d7da;
                --error-border: #f5c6cb;
                --error-text: #721c24;
                --loading-bg: #e2e3e5;
                --loading-border: #d6d8db;
                --loading-text: #383d41;
                --shadow: 0 4px 12px rgba(0,0,0,0.05);
            }
            
            @media (prefers-color-scheme: dark) {
                :root {
                    --bg-color: #121212;
                    --text-color: #e0e0e0;
                    --card-bg: #1e1e1e;
                    --border-color: #444;
                    --primary-color: #0d6efd;
                    --primary-hover: #0a58ca;
                    --success-bg: #1a3a24;
                    --success-border: #2a5a3a;
                    --success-text: #a7d0b0;
                    --error-bg: #3a1a24;
                    --error-border: #5a2a3a;
                    --error-text: #f0a7b0;
                    --loading-bg: #343a40;
                    --loading-border: #495057;
                    --loading-text: #f8f9fa;
                }
            }
            
            body {
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
                margin: 0;
                padding: 2rem;
                background-color: var(--bg-color);
                color: var(--text-color);
                transition: background-color 0.2s, color 0.2s;
                line-height: 1.6;
            }
            .container {
                max-width: 800px;
                margin: 2rem auto;
                background: var(--card-bg);
                padding: 2.5rem;
                border-radius: 12px;
                box-shadow: var(--shadow);
                border: 1px solid var(--border-color);
            }
            h1 {
                color: var(--text-color);
                border-bottom: 2px solid var(--primary-color);
                padding-bottom: 0.5rem;
                margin-top: 0;
            }
            label {
                display: block;
                margin-top: 1.5rem;
                margin-bottom: 0.5rem;
                font-weight: 600;
                font-size: 0.9rem;
                text-transform: uppercase;
                letter-spacing: 0.5px;
            }
            textarea, input[type="password"] {
                width: 100%;
                padding: 12px;
                border: 1px solid var(--border-color);
                border-radius: 8px;
                font-family: "SF Mono", "Fira Code", "Consolas", monospace;
                font-size: 14px;
                background-color: var(--bg-color);
                color: var(--text-color);
                box-sizing: border-box; /* Fix 100% width */
            }
            textarea { height: 250px; resize: vertical; }
            button {
                background-color: var(--primary-color);
                color: white;
                padding: 14px 22px;
                border: none;
                border-radius: 8px;
                cursor: pointer;
                font-size: 16px;
                font-weight: 600;
                margin-top: 2rem;
                transition: background-color 0.2s, transform 0.1s;
                width: 100%;
            }
            button:disabled {
                background-color: var(--border-color);
                cursor: not-allowed;
            }
            button:not(:disabled):hover {
                background-color: var(--primary-hover);
                transform: translateY(-2px);
            }
            
            /* [GIAO DIỆN MỚI] Trạng thái "Màu mè" */
            .status-message {
                margin-top: 2rem;
                padding: 1.25rem;
                border-radius: 8px;
                font-weight: 600;
                display: none; /* Ẩn mặc định */
                border: 1px solid transparent;
                opacity: 0;
                transform: translateY(10px);
                transition: opacity 0.3s ease-out, transform 0.3s ease-out;
            }
            .status-message.show {
                display: block;
                opacity: 1;
                transform: translateY(0);
            }
            
            .status-message.loading {
                background-color: var(--loading-bg);
                border-color: var(--loading-border);
                color: var(--loading-text);
            }
            .status-message.loading::before {
                content: '⏳  ';
            }

            .status-message.success {
                background-color: var(--success-bg);
                border-color: var(--success-border);
                color: var(--success-text);
                box-shadow: 0 4px 10px rgba(21, 87, 36, 0.1);
            }
            .status-message.success::before {
                content: '✅  CẤU HÌNH THÀNH CÔNG! ';
                font-weight: 700;
            }

            .status-message.error {
                background-color: var(--error-bg);
                border-color: var(--error-border);
                color: var(--error-text);
                box-shadow: 0 4px 10px rgba(114, 28, 36, 0.1);
            }
            .status-message.error::before {
                content: '❌  CẤU HÌNH THẤT BẠI! ';
                font-weight: 700;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Cập nhật Poller bằng cURL</h1>
            <p>Dán nội dung 'Copy as cURL (bash)' từ DevTools (F12) vào đây. Cấu hình sẽ được áp dụng ngay lập tức cho poller.</p>
            
            <form id="curl-form">
                <label for="curl_text">Lệnh cURL (bash):</label>
                <textarea id="curl_text" name="curl" placeholder="curl 'https://taphoammo.net/api/getNotify' ..." required></textarea>
                
                <label for="secret_key">Secret Key:</label>
                <input type="password" id="secret_key" name="secret" placeholder="Nhập WEBHOOK_SECRET của bạn" required>
                
                <button type="submit" id="submit-btn">Cập nhật và Chạy Thử</button>
            </form>
            
            <div id="status" class="status-message"></div>
        </div>
        
        <script>
            document.getElementById("curl-form").addEventListener("submit", async function(e) {
                e.preventDefault();
                
                const curlText = document.getElementById("curl_text").value;
                const secret = document.getElementById("secret_key").value;
                const statusEl = document.getElementById("status");
                const button = document.getElementById("submit-btn");
                
                statusEl.textContent = "Đang xử lý, vui lòng chờ...";
                statusEl.className = "status-message loading show"; // Hiện trạng thái loading
                button.disabled = true;
                
                if (!curlText || !secret) {
                    statusEl.textContent = "Vui lòng nhập cả cURL và Secret Key.";
                    statusEl.className = "status-message error show";
                    button.disabled = false;
                    return;
                }
                
                try {
                    const response = await fetch(`/debug/set-curl?secret=${encodeURIComponent(secret)}`, {
                        method: "POST",
                        headers: {"Content-Type": "application/json"},
                        body: JSON.stringify({ curl: curlText })
                    });
                    
                    const result = await response.json();
                    
                    if (response.ok) {
                        statusEl.textContent = "Đã áp dụng cấu hình mới. Poller sẽ sử dụng thông tin này cho lần chạy tiếp theo.";
                        statusEl.className = "status-message success show";
                    } else {
                        statusEl.textContent = `Lỗi: ${result.detail || 'Lỗi không xác định.'}`;
                        statusEl.className = "status-message error show";
                    }
                } catch (err) {
                    statusEl.textContent = `Lỗi kết nối: ${err.message}. Kiểm tra lại mạng hoặc URL service.`;
                    statusEl.className = "status-message error show";
                } finally {
                    button.disabled = false;
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

    # [SỬA] Reset lại bộ đếm khi set cURL mới
    global LAST_NOTIFY_NUMS
    LAST_NOTIFY_NUMS = [] 
    
    poll_once() # Chạy thử 1 lần
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

# [ĐÃ XÓA] Endpoint /taphoammo (webhook) đã bị xóa

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
