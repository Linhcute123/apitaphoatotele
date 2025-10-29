import os, json, time, threading, html, hashlib, requests, re, shlex
from typing import Any, Dict, List, Optional
from collections import defaultdict
import datetime # Để lấy ngày/giờ
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

# 1. API Thông báo (getNotify)
NOTIFY_API_URL       = os.getenv("NOTIFY_API_URL", "")
NOTIFY_API_METHOD    = os.getenv("NOTIFY_API_METHOD", "POST").upper()
NOTIFY_HEADERS_ENV   = os.getenv("NOTIFY_HEADERS_JSON") or "{}"
NOTIFY_BODY_JSON_ENV = os.getenv("NOTIFY_BODY_JSON", "")

# 2. API Tin nhắn (getNewConversion)
CHAT_API_URL       = os.getenv("CHAT_API_URL", "")
CHAT_API_METHOD    = os.getenv("CHAT_API_METHOD", "POST").upper()
CHAT_HEADERS_ENV   = os.getenv("CHAT_HEADERS_JSON") or "{}"
CHAT_BODY_JSON_ENV = os.getenv("CHAT_BODY_JSON", "")

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "12"))
VERIFY_TLS    = bool(int(os.getenv("VERIFY_TLS", "1")))
DISABLE_POLLER = os.getenv("DISABLE_POLLER", "0") == "1"

# Cấu hình runtime (sẽ bị ghi đè bởi UI)
try:
    NOTIFY_CONFIG = {
        "url": NOTIFY_API_URL, "method": NOTIFY_API_METHOD,
        "headers": json.loads(NOTIFY_HEADERS_ENV),
        "body_json": json.loads(NOTIFY_BODY_JSON_ENV) if NOTIFY_BODY_JSON_ENV else None
    }
except Exception:
    NOTIFY_CONFIG = {"url": "", "method": "GET", "headers": {}, "body_json": None}

try:
    CHAT_CONFIG = {
        "url": CHAT_API_URL, "method": CHAT_API_METHOD,
        "headers": json.loads(CHAT_HEADERS_ENV),
        "body_json": json.loads(CHAT_BODY_JSON_ENV) if CHAT_BODY_JSON_ENV else None
    }
except Exception:
    CHAT_CONFIG = {"url": "", "method": "GET", "headers": {}, "body_json": None}


# =================== APP ===================
app = FastAPI(title="TapHoaMMO → Telegram (Dual-API Poller)")

LAST_NOTIFY_NUMS: List[int] = []     
DAILY_ORDER_COUNT = defaultdict(int) 
DAILY_COUNTER_DATE = "" 
SEEN_CHAT_DATES: set[str] = set()
LAST_SEEN_CHATS: Dict[str, str] = {} # Key: user_id, Value: last_chat

# =================== Telegram ===================
def tg_send(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Missing TELEGRAM_* env")
        return
    MAX = 3900  
    chunks = [text[i:i+MAX] for i in range(0, len(text), MAX)] or [""]
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for idx, part in enumerate(chunks[:3]):
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": part, "parse_mode": "HTML",
            "disable_web_page_preview": True
        }, timeout=20)
        if r.status_code >= 400:
            print("Telegram error:", r.status_code, r.text)
            break

# [THÊM MỚI] Hàm gửi tổng kết cuối ngày
def send_daily_summary(date_str: str, counts: defaultdict):
    """
    Gửi báo cáo tổng kết của ngày đã qua.
    """
    if not counts:
        print(f"Skipping summary for {date_str}, no data.")
        return

    msg_lines = [
        f"<b>🗓️ TỔNG KẾT NGÀY {date_str}</b>", # [THAY ĐỔI ICON]
        "===================="
    ]
    
    total_today = 0
    product_total = counts.get("Đơn hàng sản phẩm", 0)
    service_total = counts.get("Đơn hàng dịch vụ", 0)
    
    if product_total > 0:
        msg_lines.append(f"  📦 Đơn hàng sản phẩm: <b>{product_total}</b>")
        total_today += product_total
    if service_total > 0:
        msg_lines.append(f"  🛎️ Đơn hàng dịch vụ: <b>{service_total}</b>")
        total_today += service_total
    
    if total_today > 0:
        msg_lines.append("--------------------")
        msg_lines.append(f"🎉 <b>Tổng cộng: {total_today} đơn hàng.</b>")
    else:
        msg_lines.append("<i>Không có đơn hàng nào được ghi nhận.</i>")

    tg_send("\n".join(msg_lines))
    print(f"Sent daily summary for {date_str}.")


# =================== Helpers ===================
def _get_icon_for_label(label: str) -> str:
    low = label.lower()
    if "sản phẩm" in low: return "📦"
    if "dịch vụ" in low: return "🛎️"
    if "khiếu nại" in low: return "⚠️"
    if "đặt trước" in low: return "⏰"
    if "đánh giá" in low: return "💬"
    if "tin nhắn" in low: return "✉️"
    return "•"

def _labels_for_notify(parts_len: int) -> List[str]:
    if parts_len == 8:
        return [
            "Đơn hàng sản phẩm", "Đánh giá", "Chưa rõ 3", "Chưa rõ 4",
            "Đặt trước", "Đơn hàng dịch vụ", "Khiếu nại", "Tin nhắn"
        ]
    return [f"c{i+1}" for i in range(parts_len)]

COLUMN_BASELINES = defaultdict(int)
COLUMN_BASELINES["Khiếu nại"] = 1 # Báo cáo ngay cả khi chỉ có 1

def parse_notify_text(text: str) -> Dict[str, Any]:
    s = (text or "").strip()
    parts = s.split("|") if s else []
    if all(re.fullmatch(r"\d+", p or "") for p in parts):
        nums = [int(p) for p in parts]
        labels = _labels_for_notify(len(nums))
        table = {labels[i]: nums[i] for i in range(len(nums))}
        return {"raw": s, "numbers": nums, "table": table}
    return {"raw": s}

def parse_curl_command(curl_text: str) -> Dict[str, Any]:
    args = shlex.split(curl_text)
    method = "GET"; headers = {}; data = None; url = ""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "curl": i += 1; url = args[i] if i < len(args) else ""
        elif a in ("-X", "--request"): i += 1; method = args[i].upper() if i < len(args) else "GET"
        elif a in ("-H", "--header"):
            i += 1
            if i < len(args): h = args[i]; k, v = h.split(":", 1); headers[k.strip()] = v.strip()
        elif a in ("-b", "--cookie"): i += 1; headers['cookie'] = args[i] if i < len(args) else ""
        elif a in ("--data", "--data-raw", "--data-binary", "-d"): i += 1; data = args[i] if i < len(args) else None
        i += 1

    if method == "GET" and data is not None: method = "POST"
    
    final_headers: Dict[str, str] = {}
    junk_prefixes = ('sec-ch-ua', 'sec-fetch-', 'priority', 'accept', 'content-length')
    for key, value in headers.items():
        low_key = key.lower()
        if not any(low_key.startswith(p) for p in junk_prefixes):
            final_headers[key] = value

    if not final_headers and headers: final_headers = headers
    
    body_json = None
    if data:
        try: body_json = json.loads(data)
        except Exception: print(f"cURL body is not valid JSON, storing as raw text: {data[:50]}...")
    
    return {"url": url, "method": method, "headers": final_headers, "body_json": body_json}

# =================== [CẬP NHẬT] Hàm gọi API Tin nhắn ===================
def fetch_chats(is_baseline_run: bool = False) -> List[Dict[str, str]]:
    """
    [CẬP NHẬT]
    Gọi API getNewConversion.
    - Nếu is_baseline_run = True: Chỉ cập nhật LAST_SEEN_CHATS (bộ nhớ tin cũ).
    - Nếu is_baseline_run = False: Cập nhật SEEN_CHAT_DATES và trả về tin nhắn mới.
    """
    if not CHAT_CONFIG.get("url"):
        print("[WARN] CHAT_API_URL is not set. Skipping chat fetch.")
        return []

    global SEEN_CHAT_DATES, LAST_SEEN_CHATS
    
    try:
        if CHAT_CONFIG["method"] == "POST":
            r = requests.post(CHAT_CONFIG["url"], headers=CHAT_CONFIG["headers"], 
                                json=CHAT_CONFIG["body_json"], verify=VERIFY_TLS, timeout=25)
        else:
            r = requests.get(CHAT_CONFIG["url"], headers=CHAT_CONFIG["headers"], 
                               verify=VERIFY_TLS, timeout=25)

        data = r.json()
        if not isinstance(data, list):
            print(f"[ERROR] Chat API did not return a list. Response: {r.text[:200]}")
            return []

        new_messages = []
        current_chat_dates = set()
        all_users_in_response = set()
        
        for chat in data:
            if not isinstance(chat, dict): continue
            
            user_id = chat.get("guest_user", "N/A")
            current_msg = chat.get("last_chat", "[không có nội dung]")
            all_users_in_response.add(user_id)

            chat_id = chat.get("date")
            if not chat_id:
                chat_id = f"{user_id}:{current_msg}" # Fallback
            
            if not is_baseline_run:
                # [LOGIC SỬA LỖI] Chỉ chạy logic "tin nhắn mới" nếu đây KHÔNG PHẢI là
                # lần chạy baseline đầu tiên.
                current_chat_dates.add(chat_id)

                if chat_id not in SEEN_CHAT_DATES:
                    SEEN_CHAT_DATES.add(chat_id)
                    previous_msg = LAST_SEEN_CHATS.get(user_id)
                    
                    new_messages.append({
                        "user": user_id,
                        "chat": current_msg,
                        "previous_chat": previous_msg
                    })
            
            # [LOGIC SỬA LỖI] LUÔN cập nhật tin nhắn cuối cùng vào bộ nhớ,
            # kể cả khi chạy baseline.
            LAST_SEEN_CHATS[user_id] = current_msg
        
        if not is_baseline_run:
            # [LOGIC SỬA LỖI] Chỉ dọn dẹp SEEN_CHAT_DATES khi không phải baseline
            SEEN_CHAT_DATES.intersection_update(current_chat_dates)
        
        for user in list(LAST_SEEN_CHATS.keys()):
            if user not in all_users_in_response:
                del LAST_SEEN_CHATS[user]
        
        if new_messages:
            print(f"Fetched {len(new_messages)} new chat message(s).")
        return new_messages # Sẽ là [] nếu is_baseline_run = True

    except Exception as e:
        print(f"fetch_chats error: {e}")
        return []

# =================== [VIẾT LẠI] Hàm Poller Chính ===================
def poll_once():
    """
    [LOGIC ĐÃ CẬP NHẬT]
    1. Gọi API getNotify.
    2. Nếu 'c8: Tin nhắn' tăng, gọi API getNewConversion (is_baseline_run=False).
    3. Gửi thông báo tức thời (không kèm tổng kết).
    4. [MỚI] Kiểm tra nếu sang ngày mới, gửi báo cáo tổng kết của ngày cũ.
    """
    global LAST_NOTIFY_NUMS, DAILY_ORDER_COUNT, DAILY_COUNTER_DATE 

    if not NOTIFY_CONFIG.get("url"):
        print("No NOTIFY_API_URL set")
        return

    try:
        # 1. GỌI API THÔNG BÁO (getNotify)
        if NOTIFY_CONFIG["method"] == "POST":
            r = requests.post(NOTIFY_CONFIG["url"], headers=NOTIFY_CONFIG["headers"], 
                                json=NOTIFY_CONFIG["body_json"], verify=VERIFY_TLS, timeout=25)
        else:
            r = requests.get(NOTIFY_CONFIG["url"], headers=NOTIFY_CONFIG["headers"], 
                               verify=VERIFY_TLS, timeout=25)

        text = (r.text or "").strip()
        if not text:
            print("getNotify: empty response")
            return

        low = text[:200].lower()
        if low.startswith("<!doctype") or "<html" in low:
            if text != str(LAST_NOTIFY_NUMS):
                tg_send("⚠️ <b>getNotify trả về HTML</b> (Cookie/Header hết hạn?).")
            print("HTML detected, preview sent. Probably headers/cookie expired.")
            return
        
        # 2. XỬ LÝ KẾT QUẢ getNotify
        parsed = parse_notify_text(text)
        
        if "numbers" in parsed:
            now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7)))
            today_str = now.strftime("%Y-%m-%d")
            time_str = now.strftime("%H:%M:%S")

            # [LOGIC MỚI] GỬI TỔNG KẾT NẾU SANG NGÀY MỚI
            if today_str != DAILY_COUNTER_DATE:
                if DAILY_COUNTER_DATE:
                    print(f"New day detected ({today_str}). Sending summary for {DAILY_COUNTER_DATE}...")
                    send_daily_summary(DAILY_COUNTER_DATE, DAILY_ORDER_COUNT)
                
                DAILY_COUNTER_DATE = today_str
                DAILY_ORDER_COUNT.clear()
            
            current_nums = parsed["numbers"]
            if len(current_nums) != len(LAST_NOTIFY_NUMS):
                LAST_NOTIFY_NUMS = [0] * len(current_nums)

            labels = _labels_for_notify(len(current_nums))
            instant_alerts_map = {}
            has_new_notification = False
            has_new_chat = False

            # 3. SO SÁNH GIÁ TRỊ MỚI VÀ CŨ
            for i in range(len(current_nums)):
                current_val = current_nums[i]
                last_val = LAST_NOTIFY_NUMS[i]
                label = labels[i]
                
                if current_val > last_val:
                    has_new_notification = True
                    
                    if "đơn hàng sản phẩm" in label.lower():
                        DAILY_ORDER_COUNT[label] += (current_val - last_val)
                    elif "đơn hàng dịch vụ" in label.lower():
                        DAILY_ORDER_COUNT[label] += (current_val - last_val)
                    
                    if "tin nhắn" in label.lower():
                        has_new_chat = True
                
                baseline = COLUMN_BASELINES[label]
                if current_val > baseline:
                    icon = _get_icon_for_label(label)
                    instant_alerts_map[label] = f"  {icon} <b>{label}:</b> {current_val}"

            # 4. GỌI API TIN NHẮN (nếu cần)
            new_chat_messages = []
            if has_new_chat:
                # [LOGIC SỬA LỖI] Gọi với is_baseline_run=False (mặc định)
                fetched_messages = fetch_chats() 
                for chat in fetched_messages:
                    user = html.escape(chat.get("user", "N/A"))
                    msg = html.escape(chat.get("chat", "..."))
                    prev_msg = html.escape(chat.get("previous_chat") or "")

                    new_chat_messages.append(f"<b>--- Tin nhắn từ: {user} ---</b>")
                    if prev_msg and prev_msg != msg:
                        new_chat_messages.append(f"  <i>Lần trước: {prev_msg}</i>")
                        new_chat_messages.append(f"  <b>Bây giờ: {msg}</b>")
                    else:
                        new_chat_messages.append(f"  <b>Nội dung: {msg}</b>")


            # 5. GỬI THÔNG BÁO TỨC THỜI (ĐÃ BỎ TỔNG KẾT)
            if has_new_notification:
                ordered_labels = [
                    "Đơn hàng sản phẩm", "Đơn hàng dịch vụ", "Đặt trước",
                    "Khiếu nại", "Tin nhắn", "Đánh giá"
                ]
                
                instant_alert_lines = []
                for label in ordered_labels:
                    if label in instant_alerts_map:
                        instant_alert_lines.append(instant_alerts_map.pop(label))
                for remaining_line in instant_alerts_map.values():
                    instant_alert_lines.append(remaining_line)
                

                # Lắp ráp thông báo "siêu chuyên nghiệp"
                msg_lines = [
                    f"<b>🏪 BÁO CÁO NHANH - TAPHOAMMO</b>", # [THAY ĐỔI ICON]
                    f"<i>(Lúc {time_str} - Ngày {today_str})</i>"
                ]

                # Đặt tin nhắn lên đầu
                if new_chat_messages:
                    msg_lines.append("➖➖➖➖➖➖➖➖➖➖➖")
                    msg_lines.append("<b>💬 BẠN CÓ TIN NHẮN MỚI:</b>")
                    msg_lines.extend(new_chat_messages)
                
                if instant_alert_lines:
                    msg_lines.append("➖➖➖➖➖➖➖➖➖➖➖")
                    msg_lines.append("<b>🔔 CẬP NHẬT TRẠNG THÁI:</b>")
                    msg_lines.extend(instant_alert_lines)
                
                msg = "\n".join(msg_lines)
                tg_send(msg)
                print("getNotify changes (INCREASE) -> Professional Telegram sent.")
                
            else:
                print("getNotify unchanged or DECREASED -> Skipping.")

            LAST_NOTIFY_NUMS = current_nums
        
        else:
            if text != str(LAST_NOTIFY_NUMS):
                msg = f"🔔 <b>TapHoaMMO getNotify (lỗi)</b>\n<code>{html.escape(text)}</code>"
                tg_send(msg)
                print("getNotify (non-numeric) changed -> Telegram sent.")

    except Exception as e:
        print(f"poll_once error: {e}")

# [CẬP NHẬT] Vòng lặp Poller
def poller_loop():
    print("▶ Poller started (Dual-API Mode)")
    # [LOGIC SỬA LỖI] Chạy fetch_chats ở chế độ baseline (chỉ lấy tin cũ)
    print("Running initial chat fetch to set baseline (LAST_SEEN_CHATS)...")
    fetch_chats(is_baseline_run=True)
    
    print("Running initial notify poll...")
    poll_once()
    
    # Đảm bảo DAILY_COUNTER_DATE được set ngay sau lần chạy đầu tiên
    global DAILY_COUNTER_DATE
    if not DAILY_COUNTER_DATE:
        DAILY_COUNTER_DATE = datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=7))
        ).strftime("%Y-%m-%d")
        print(f"Baseline date set to: {DAILY_COUNTER_DATE}")
    
    while True:
        time.sleep(POLL_INTERVAL)
        poll_once()

# =================== API endpoints ===================

@app.get("/", response_class=HTMLResponse)
async def get_curl_ui():
    html_content = """
    <!DOCTYPE html>
    <html lang="vi">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale-1.0">
        <title>Cập nhật cURL Poller - TapHoaMMO</title>
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700&display=swap');
            :root {
                --bg-gradient: linear-gradient(135deg, #f4f7f9 0%, #e1e7ed 100%);
                --text-color: #333;
                --card-bg: #ffffff;
                --border-color: #dee2e6;
                --primary-color: #0061ff;
                --primary-hover: #004ecc;
                --primary-rgb: 0, 97, 255;
                --success-bg: #d1f7e0; --success-border: #a3e9be; --success-text: #0a6847;
                --error-bg: #f8d7da; --error-border: #f5c6cb; --error-text: #721c24;
                --loading-bg: #e9ecef; --loading-border: #ced4da; --loading-text: #495057;
                --shadow: 0 8px 25px rgba(0,0,0,0.08);
                --shadow-hover: 0 12px 30px rgba(0, 97, 255, 0.15);
            }
            @media (prefers-color-scheme: dark) {
                :root {
                    --bg-gradient: linear-gradient(135deg, #2b3035 0%, #1a1e23 100%);
                    --text-color: #f0f0f0;
                    --card-bg: #22272e;
                    --border-color: #444951;
                    --primary-color: #1a88ff; --primary-hover: #006fff;
                    --primary-rgb: 26, 136, 255;
                    --success-bg: #162a22; --success-border: #2a5a3a; --success-text: #a7d0b0;
                    --error-bg: #3a1a24; --error-border: #5a2a3a; --error-text: #f0a7b0;
                    --loading-bg: #343a40; --loading-border: #495057; --loading-text: #f8f9fa;
                }
            }
            body {
                font-family: 'Roboto', -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                margin: 0; padding: 2rem; background: var(--bg-gradient);
                color: var(--text-color); line-height: 1.6; min-height: 100vh;
                box-sizing: border-box;
            }
            .container {
                max-width: 800px; margin: 2rem auto; background: var(--card-bg);
                padding: 2.5rem 3rem; border-radius: 16px;
                box-shadow: var(--shadow); border: 1px solid var(--border-color);
                transition: transform 0.3s ease, box-shadow 0.3s ease;
            }
            .container:hover {
                transform: translateY(-5px); box-shadow: var(--shadow-hover);
            }
            h1 {
                color: var(--primary-color); font-size: 2.25rem; font-weight: 700;
                margin-top: 0; margin-bottom: 1rem; display: flex; align-items: center;
            }
            h1 span { font-size: 2.5rem; margin-right: 0.75rem; line-height: 1; filter: grayscale(30%); }
            p.description {
                font-size: 1.1rem; color: var(--text-color); opacity: 0.8; margin-bottom: 2rem;
            }
            label {
                display: block; margin-top: 1.5rem; margin-bottom: 0.5rem;
                font-weight: 500; font-size: 0.9rem; color: var(--text-color); opacity: 0.9;
            }
            textarea, input[type="password"] {
                width: 100%; padding: 14px; border: 1px solid var(--border-color);
                border-radius: 8px; font-family: "SF Mono", "Fira Code", "Consolas", monospace;
                font-size: 14px; background-color: var(--bg-color); color: var(--text-color);
                box-sizing: border-box; transition: border-color 0.2s, box-shadow 0.2s;
            }
            textarea { height: 200px; resize: vertical; }
            textarea:focus, input[type="password"]:focus {
                outline: none; border-color: var(--primary-color);
                box-shadow: 0 0 0 3px rgba(var(--primary-rgb), 0.25);
            }
            button {
                background: var(--primary-color); color: white; padding: 16px 24px;
                border: none; border-radius: 8px; cursor: pointer;
                font-size: 1rem; font-weight: 700; letter-spacing: 0.5px;
                margin-top: 2rem; transition: background-color 0.2s, transform 0.1s;
                width: 100%;
            }
            button:disabled { background-color: var(--border-color); cursor: not-allowed; opacity: 0.7; }
            button:not(:disabled):hover { background: var(--primary-hover); transform: translateY(-2px); }
            
            .status-message {
                margin-top: 2rem; padding: 1.25rem; border-radius: 8px; font-weight: 500;
                display: none; border: 1px solid transparent; opacity: 0;
                transform: translateY(10px); transition: opacity 0.3s ease-out, transform 0.3s ease-out;
            }
            .status-message.show { display: block; opacity: 1; transform: translateY(0); }
            .status-message strong { font-weight: 700; display: block; margin-bottom: 0.25rem; }
            .status-message.loading { background-color: var(--loading-bg); border-color: var(--loading-border); color: var(--loading-text); }
            .status-message.loading strong::before { content: '⏳  ĐANG XỬ LÝ...'; }
            .status-message.loading span { font-style: italic; }
            .status-message.success { background-color: var(--success-bg); border-color: var(--success-border); color: var(--success-text); }
            .status-message.success strong::before { content: '✅  CẤU HÌNH THÀNH CÔNG!'; }
            .status-message.error { background-color: var(--error-bg); border-color: var(--error-border); color: var(--error-text); }
            .status-message.error strong::before { content: '❌  CẤU HÌNH THẤT BẠI!'; }
            .footer-text { text-align: center; margin-top: 2.5rem; font-size: 0.85rem; color: var(--text-color); opacity: 0.6; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1><span>⚙️</span>Trình Cập Nhật Poller (Dual-API)</h1>
            <p class="description">Dán 2 cURL từ DevTools. Cấu hình sẽ được áp dụng ngay lập tức.</p>
            
            <form id="curl-form">
                <label for="curl_notify_text">1. cURL Thông Báo (getNotify):</label>
                <textarea id="curl_notify_text" name="curl_notify" placeholder="curl '.../api/getNotify' ..." required></textarea>
                
                <label for="curl_chat_text">2. cURL Tin Nhắn (getNewConversion):</label>
                <textarea id="curl_chat_text" name="curl_chat" placeholder="curl '.../api/getNewConversion' ..." required></textarea>

                <label for="secret_key">Secret Key:</label>
                <input type="password" id="secret_key" name="secret" placeholder="Nhập WEBHOOK_SECRET của bạn" required>
                
                <button type="submit" id="submit-btn">Cập nhật và Chạy Thử</button>
            </form>
            
            <div id="status" class="status-message">
                <strong></strong>
                <span id="status-body"></span>
            </div>
            
            <p class="footer-text">TapHoaMMO Poller Service 3.2 (Bugfix + EOD)</p>
        </div>
        
        <script>
            document.getElementById("curl-form").addEventListener("submit", async function(e) {
                e.preventDefault();
                
                const curlNotifyText = document.getElementById("curl_notify_text").value;
                const curlChatText = document.getElementById("curl_chat_text").value;
                const secret = document.getElementById("secret_key").value;
                
                const statusEl = document.getElementById("status");
                const statusBody = document.getElementById("status-body");
                const statusHeader = statusEl.querySelector("strong");
                const button = document.getElementById("submit-btn");
                
                statusHeader.textContent = ""; 
                statusBody.textContent = "Vui lòng chờ trong giây lát...";
                statusEl.className = "status-message loading show";
                button.disabled = true;
                
                if (!curlNotifyText || !curlChatText || !secret) {
                    statusHeader.textContent = "";
                    statusBody.textContent = "Vui lòng nhập ĐẦY ĐỦ cả 2 cURL và Secret Key.";
                    statusEl.className = "status-message error show";
                    button.disabled = false;
                    return;
                }
                
                try {
                    const response = await fetch(`/debug/set-curl?secret=${encodeURIComponent(secret)}`, {
                        method: "POST",
                        headers: {"Content-Type": "application/json"},
                        body: JSON.stringify({ 
                            curl_notify: curlNotifyText,
                            curl_chat: curlChatText
                        })
                    });
                    
                    const result = await response.json();
                    
                    if (response.ok) {
                        statusHeader.textContent = "";
                        statusBody.textContent = "Đã áp dụng cấu hình cho cả 2 API. Poller sẽ sử dụng thông tin này ngay bây giờ.";
                        statusEl.className = "status-message success show";
                    } else {
                        statusHeader.textContent = "";
                        statusBody.textContent = `Lỗi: ${result.detail || 'Lỗi không xác định.'}`;
                        statusEl.className = "status-message error show";
                    }
                } catch (err) {
                    statusHeader.textContent = "";
                    statusBody.textContent = `Lỗi kết nối: ${err.message}. Kiểm tra lại mạng hoặc URL service.`;
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
        "ok": True, "poller": not DISABLE_POLLER,
        "last_notify_nums": LAST_NOTIFY_NUMS,
        "daily_stats": {"date": DAILY_COUNTER_DATE, "counts": DAILY_ORDER_COUNT},
        "seen_chats": len(SEEN_CHAT_DATES),
        "api_notify": {"url": NOTIFY_CONFIG.get("url")},
        "api_chat": {"url": CHAT_CONFIG.get("url")}
    }

@app.get("/debug/notify-now")
def debug_notify(secret: str):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    before = str(LAST_NOTIFY_NUMS) 
    poll_once()
    after = str(LAST_NOTIFY_NUMS)
    return {
        "ok": True, "last_before": before, "last_after": after,
        "daily_stats": DAILY_ORDER_COUNT
    }

# [CẬP NHẬT] Endpoint set-curl (có thông báo Telegram)
@app.post("/debug/set-curl")
async def debug_set_curl(req: Request, secret: str):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    
    body = await req.json()
    curl_notify_txt = str(body.get("curl_notify") or "")
    curl_chat_txt = str(body.get("curl_chat") or "")

    # --- Kiểm tra thất bại 1: Thiếu input ---
    if not curl_notify_txt or not curl_chat_txt:
        msg = (
            "❌ <b>CẬP NHẬT CẤU HÌNH THẤT BẠI</b>\n"
            "Lý do: Một trong hai ô cURL bị bỏ trống."
        )
        tg_send(msg)
        raise HTTPException(status_code=400, detail="curl_notify and curl_chat are required.")

    parsed_notify = None
    parsed_chat = None
    
    # --- Kiểm tra thất bại 2: Lỗi parsing ---
    try:
        parsed_notify = parse_curl_command(curl_notify_txt)
        parsed_chat = parse_curl_command(curl_chat_txt)
    except Exception as e:
        msg = (
            "❌ <b>CẬP NHẬT CẤU HÌNH THẤT BẠI</b>\n"
            f"Lý do: Lỗi nghiêm trọng khi phân tích cURL.\n"
            f"<code>{html.escape(str(e))}</code>"
        )
        tg_send(msg)
        raise HTTPException(status_code=500, detail=f"Parsing error: {e}")

    # --- Kiểm tra thất bại 3: Không tìm thấy URL ---
    notify_url_ok = bool(parsed_notify and parsed_notify.get("url"))
    chat_url_ok = bool(parsed_chat and parsed_chat.get("url"))

    if not notify_url_ok or not chat_url_ok:
        error_lines = ["❌ <b>CẬP NHẬT CẤU HÌNH THẤT BẠI</b>\nLý do: Không thể phân tích URL từ cURL."]
        if not notify_url_ok:
            error_lines.append("<b>- API Notify:</b> Thất bại (Kiểm tra lại cURL 1)")
        if not chat_url_ok:
            error_lines.append("<b>- API Chat:</b> Thất bại (Kiểm tra lại cURL 2)")
        
        msg_fail = "\n".join(error_lines)
        tg_send(msg_fail)
        
        raise HTTPException(status_code=400, detail="Một hoặc cả hai cURL không hợp lệ. Không tìm thấy URL.")

    # --- Trường hợp thành công ---
    global NOTIFY_CONFIG, CHAT_CONFIG
    global LAST_NOTIFY_NUMS, DAILY_ORDER_COUNT, DAILY_COUNTER_DATE, SEEN_CHAT_DATES
    global LAST_SEEN_CHATS
    
    NOTIFY_CONFIG = parsed_notify
    CHAT_CONFIG = parsed_chat

    # Reset lại toàn bộ
    LAST_NOTIFY_NUMS = []
    DAILY_ORDER_COUNT.clear()
    DAILY_COUNTER_DATE = "" # Sẽ được set ở lần poll_once() tiếp theo
    SEEN_CHAT_DATES.clear()
    LAST_SEEN_CHATS.clear()
    
    print("--- CONFIG UPDATED BY UI ---")
    print(f"Notify API set to: {NOTIFY_CONFIG.get('url')}")
    print(f"Chat API set to: {CHAT_CONFIG.get('url')}")
    
    # Gửi thông báo thành công
    msg_success = (
        "✅ <b>CẬP NHẬT CẤU HÌNH THÀNH CÔNG (TAPHOAMMO)</b>\n"
        "Đã áp dụng cài đặt mới cho cả 2 API.\n\n"
        f"<b>1. API Notify:</b> <code>{html.escape(NOTIFY_CONFIG.get('url'))}</code>\n"
        f"<b>2. API Chat:</b> <code>{html.escape(CHAT_CONFIG.get('url'))}</code>"
    )
    tg_send(msg_success)
    
    # Chạy thử 1 lần (sẽ chạy cả 2 API nếu cần và set ngày mới)
    # [LOGIC SỬA LỖI] Chạy poll_once ngay sau khi set config SẼ GÂY LỖI
    # vì baseline chưa được chạy. Chúng ta sẽ để poller_loop tự chạy.
    # poll_once() # <-- Xóa dòng này
    print("Config set. Poller loop will pick it up (hoặc chạy lần đầu nếu mới khởi động).")
    
    return {
        "ok": True,
        "using_notify": {
            "url": NOTIFY_CONFIG.get("url"),
            "method": NOTIFY_CONFIG.get("method"),
            "headers": NOTIFY_CONFIG.get("headers"),
        },
        "using_chat": {
            "url": CHAT_CONFIG.get("url"),
            "method": CHAT_CONFIG.get("method"),
            "headers": CHAT_CONFIG.get("headers"),
        },
        "note": "Applied for current process. Update Render Environment to persist."
    }

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
