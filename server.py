import os, json, time, threading, html, hashlib, requests, re, shlex, random
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

# Cấu hình runtime (thêm body_data)
try:
    NOTIFY_CONFIG = {
        "url": NOTIFY_API_URL, "method": NOTIFY_API_METHOD,
        "headers": json.loads(NOTIFY_HEADERS_ENV),
        "body_json": json.loads(NOTIFY_BODY_JSON_ENV) if NOTIFY_BODY_JSON_ENV else None,
        "body_data": None
    }
except Exception:
    NOTIFY_CONFIG = {"url": "", "method": "GET", "headers": {}, "body_json": None, "body_data": None}

try:
    CHAT_CONFIG = {
        "url": CHAT_API_URL, "method": CHAT_API_METHOD,
        "headers": json.loads(CHAT_HEADERS_ENV),
        "body_json": json.loads(CHAT_BODY_JSON_ENV) if CHAT_BODY_JSON_ENV else None,
        "body_data": None
    }
except Exception:
    CHAT_CONFIG = {"url": "", "method": "GET", "headers": {}, "body_json": None, "body_data": None}


# =================== APP ===================
app = FastAPI(title="TapHoaMMO → Telegram (Dual-API Poller)")

LAST_NOTIFY_NUMS: List[int] = []     
DAILY_ORDER_COUNT = defaultdict(int) 
DAILY_COUNTER_DATE = ""              
SEEN_CHAT_DATES: set[str] = set()
LAST_SEEN_CHATS: Dict[str, str] = {}

# [THÊM MỚI] Cấu hình lời chúc 0h
GREETING_ENABLED = True
DEFAULT_IMAGE_LINKS = [
    "https://i.imgur.com/g6m3l08.jpeg",
    "https://i.imgur.com/L1b6iQZ.jpeg",
    "https://i.imgur.com/Uf7bS3T.jpeg",
    "https://i.imgur.com/0PViC3S.jpeg",
    "https://i.imgur.com/7gK10sL.jpeg"
]
GREETING_IMAGE_LINKS = list(DEFAULT_IMAGE_LINKS) # Danh sách này sẽ được UI ghi đè
GREETING_MESSAGES = [
    (
        "🥂 <b>BÁO CÁO TỔNG KẾT NGÀY {date}</b> 🥂\n\n"
        "Thưa Ông Chủ,\n"
        "Ngày làm việc đã kết thúc với <b>{orders} đơn hàng</b> được ghi nhận. 📈\n\n"
        "Chúc Ông Chủ một ngày mới tràn đầy năng lượng và bùng nổ doanh thu! 🚀💰"
    ),
    (
        "💎 <b>KẾT THÚC NGÀY GIAO DỊCH {date}</b> 💎\n\n"
        "Tổng kết nhanh, thưa Sếp:\n"
        "Hệ thống đã ghi nhận <b>{orders} đơn hàng</b> thành công. 🔥\n\n"
        "Chúc Sếp ngày mới giao dịch x2, x3. Tiền về như nước! 🌊"
    ),
    (
        "🌙 <b>BÁO CÁO CUỐI NGÀY {date}</b> 🌙\n\n"
        "Một ngày tuyệt vời đã qua, Ông Chủ.\n"
        "Số đơn hàng hôm nay: <b>{orders} đơn</b>. 📊\n\n"
        "Chúc Ông Chủ ngủ ngon và thức dậy với một ngày mới rực rỡ! ☀️"
    ),
    (
        "👑 <b>BÁO CÁO HOÀNG GIA NGÀY {date}</b> 👑\n\n"
        "Thần xin báo cáo, thưa Bệ hạ:\n"
        "Lãnh thổ của ngài hôm nay đã mở rộng thêm <b>{orders} đơn hàng</b>. 🏰\n\n"
        "Chúc Bệ hạ một ngày mới uy quyền và chinh phục thêm nhiều thành công! ⚔️"
    ),
    (
        "✈️ <b>THÔNG BÁO TỪ TRUNG TÂM ĐIỀU HÀNH NGÀY {date}</b> ✈️\n\n"
        "Phi công,\n"
        "Chuyến bay hôm nay đã hạ cánh an toàn với <b>{orders} hành khách</b> (đơn hàng). 🛫\n\n"
        "Chuẩn bị nhiên liệu cho ngày mai. Chúc sếp một hành trình mới rực rỡ! ✨"
    ),
    (
        "🍾 <b>TIN NHẮN TỪ HẦM RƯỢU NGÀY {date}</b> 🍾\n\n"
        "Thưa Quý ngài,\n"
        "Chúng ta vừa khui <b>{orders} chai</b> (đơn hàng) để ăn mừng ngày hôm nay. 🥂\n\n"
        "Chúc Quý ngài một ngày mới thật 'chill' và tiếp tục gặt hái thành công! 💸"
    )
]

# =================== Telegram ===================
def tg_send(text: str, photo_url: Optional[str] = None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Missing TELEGRAM_* env")
        return

    api_url = ""
    payload = {}
    
    if photo_url:
        # Gửi ảnh kèm caption (chú thích)
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        caption = text
        if len(caption) > 1024: # Giới hạn caption của Telegram
            caption = text[:1021] + "..."
        
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "photo": photo_url,
            "caption": caption,
            "parse_mode": "HTML"
        }
        
        try:
            r = requests.post(api_url, json=payload, timeout=30)
            if r.status_code >= 400:
                print(f"Telegram photo error: {r.status_code} {r.text}")
                # [Dự phòng] Nếu gửi ảnh lỗi (ví dụ link ảnh hỏng), gửi text
                tg_send(text, photo_url=None) # Chỉ gửi text
            return # Đã gửi (hoặc đã thử gửi)
        except Exception as e:
            print(f"Error sending photo: {e}")
            tg_send(text, photo_url=None) # Gửi text nếu có lỗi
            return

    # Gửi text (chia nhỏ nếu quá dài)
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    MAX = 3900  
    chunks = [text[i:i+MAX] for i in range(0, len(text), MAX)] or [""]
    
    for idx, part in enumerate(chunks[:3]):
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": part,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        r_text = requests.post(api_url, json=payload, timeout=20)
        if r_text.status_code >= 400:
            print(f"Telegram text error: {r_text.status_code} {r_text.text}")
            break

# Hàm gửi lời chúc 0h (lấy link và lời chúc ngẫu nhiên)
def send_good_morning_message(old_date: str, counts: defaultdict):
    """
    Gửi lời chúc mừng ngày mới kèm tổng kết ngày cũ và ảnh.
    """
    print(f"Sending Good Morning message for end of day {old_date}...")
    
    # Tính tổng đơn ngày cũ
    product_total = counts.get("Đơn hàng sản phẩm", 0)
    service_total = counts.get("Đơn hàng dịch vụ", 0)
    total_orders = product_total + service_total

    # Lấy lời chúc ngẫu nhiên
    msg_template = random.choice(GREETING_MESSAGES)
    msg = msg_template.format(date=old_date, orders=total_orders)
    
    # Lấy ảnh ngẫu nhiên từ danh sách (ưu tiên danh sách tùy chỉnh)
    photo = None
    links_to_use = GREETING_IMAGE_LINKS if GREETING_IMAGE_LINKS else DEFAULT_IMAGE_LINKS
    if links_to_use:
        photo = random.choice(links_to_use)
    
    tg_send(text=msg, photo_url=photo)


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
COLUMN_BASELINES["Khiếu nại"] = 1

def parse_notify_text(text: str) -> Dict[str, Any]:
    s = (text or "").strip()
    parts = s.split("|") if s else []
    if all(re.fullmatch(r"\d+", p or "") for p in parts):
        nums = [int(p) for p in parts]
        labels = _labels_for_notify(len(nums))
        table = {labels[i]: nums[i] for i in range(len(nums))}
        return {"raw": s, "numbers": nums, "table": table}
    return {"raw": s}

# Hàm Parse cURL (hỗ trợ data-raw)
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
    raw_data = None 
    
    if data:
        try: 
            body_json = json.loads(data)
        except Exception: 
            print(f"cURL body is not valid JSON, storing as raw text.")
            raw_data = data
    
    return {
        "url": url, "method": method, "headers": final_headers, 
        "body_json": body_json, 
        "body_data": raw_data
    }

# Hàm gửi request chung
def _make_api_request(config: Dict[str, Any]) -> requests.Response:
    """Gửi request dựa trên config, tự động chọn json hoặc data."""
    method = config.get("method", "GET")
    url = config.get("url", "")
    headers = config.get("headers", {})
    body_json = config.get("body_json")
    body_data = config.get("body_data")
    
    kwargs = {
        "headers": headers,
        "verify": VERIFY_TLS,
        "timeout": 25
    }
    
    if method == "POST":
        if body_json is not None:
            kwargs["json"] = body_json
        elif body_data is not None:
            kwargs["data"] = body_data.encode('utf-8')
    
    return requests.request(method, url, **kwargs)


# Hàm gọi API Tin nhắn (chỉ lấy tin CÓ CỜ UNREAD)
def fetch_chats(is_baseline_run: bool = False) -> List[Dict[str, str]]:
    if not CHAT_CONFIG.get("url"):
        print("[WARN] CHAT_API_URL is not set. Skipping chat fetch.")
        return []

    global SEEN_CHAT_DATES, LAST_SEEN_CHATS
    
    try:
        r = _make_api_request(CHAT_CONFIG)

        try:
            data = r.json()
        except requests.exceptions.JSONDecodeError:
            error_msg = f"[ERROR] Chat API (getNewConversion) did not return valid JSON. Status: {r.status_code}, Response: {r.text[:200]}..."
            print(error_msg)
            if not is_baseline_run:
                tg_send(f"⚠️ <b>Lỗi API Chat:</b> Phản hồi không phải JSON (có thể do cookie/token sai).\n<code>{html.escape(r.text[:200])}</code>")
            return []

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
                chat_id = hashlib.sha256(f"{user_id}:{current_msg}".encode()).hexdigest() 
            
            new_mes_val = chat.get("newMes")
            is_unread = str(new_mes_val) == "1" or new_mes_val is True

            if not is_baseline_run:
                current_chat_dates.add(chat_id)

                if is_unread and chat_id not in SEEN_CHAT_DATES:
                    SEEN_CHAT_DATES.add(chat_id)
                    
                    new_messages.append({
                        "user": user_id,
                        "chat": current_msg,
                    })
            
            LAST_SEEN_CHATS[user_id] = current_msg
        
        if not is_baseline_run:
            SEEN_CHAT_DATES.intersection_update(current_chat_dates)
        
        for user in list(LAST_SEEN_CHATS.keys()):
            if user not in all_users_in_response:
                del LAST_SEEN_CHATS[user]
        
        if new_messages:
            print(f"Fetched {len(new_messages)} new unread message(s).")
        return new_messages

    except requests.exceptions.RequestException as e:
        print(f"fetch_chats network error: {e}")
        if not is_baseline_run:
             tg_send(f"⚠️ <b>Lỗi Mạng API Chat:</b> Không thể kết nối hoặc phản hồi.\n<code>{html.escape(str(e))}</code>")
        return []
    except Exception as e:
        print(f"fetch_chats unexpected error: {e}")
        if not is_baseline_run:
            tg_send(f"⚠️ <b>Lỗi không mong muốn API Chat:</b>\n<code>{html.escape(str(e))}</code>")
        return []

# Hàm Poller (thêm logic chúc 0h)
def poll_once():
    global LAST_NOTIFY_NUMS, DAILY_ORDER_COUNT, DAILY_COUNTER_DATE

    if not NOTIFY_CONFIG.get("url"):
        print("No NOTIFY_API_URL set")
        return

    try:
        # 1. GỌI API THÔNG BÁO (getNotify)
        r = _make_api_request(NOTIFY_CONFIG)

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

            # GỬI LỜI CHÚC NẾU SANG NGÀY MỚI
            if today_str != DAILY_COUNTER_DATE:
                # Chỉ gửi nếu ĐÃ BẬT và không phải lần chạy đầu tiên
                if DAILY_COUNTER_DATE and GREETING_ENABLED:
                    print(f"New day detected ({today_str}). Sending good morning message for {DAILY_COUNTER_DATE}...")
                    send_good_morning_message(DAILY_COUNTER_DATE, DAILY_ORDER_COUNT)
                
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
                fetched_messages = fetch_chats(is_baseline_run=False) 
                for chat in fetched_messages:
                    user = html.escape(chat.get("user", "N/A"))
                    msg = html.escape(chat.get("chat", "..."))

                    new_chat_messages.append(f"<b>--- Tin nhắn từ: {user} ---</b>")
                    new_chat_messages.append(f"  <b>Nội dung: {msg}</b>")


            # 5. GỬI THÔNG BÁO TỨC THỜI
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
                
                msg_lines = [
                    f"<b>⭐ BÁO CÁO NHANH - TAPHOAMMO</b>"
                ]

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

    except requests.exceptions.RequestException as e:
        print(f"poll_once network error: {e}")
        tg_send(f"⚠️ <b>Lỗi Mạng API Notify:</b> Không thể kết nối hoặc phản hồi.\n<code>{html.escape(str(e))}</code>")
    except Exception as e:
        print(f"poll_once unexpected error: {e}")
        tg_send(f"⚠️ <b>Lỗi không mong muốn API Notify:</b>\n<code>{html.escape(str(e))}</code>")

# Vòng lặp Poller (thêm logic set DAILY_COUNTER_DATE)
def poller_loop():
    print("▶ Poller started (Dual-API Mode)")
    
    try:
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7)))
        time_str = now.strftime("%H:%M:%S")
        date_str = now.strftime("%Y-%m-%d")
        tg_send(
            f"✅ <b>Bot đã khởi động!</b>\n"
            f"<i>(Lúc {time_str} - Ngày {date_str})</i>\n"
            f"Bắt đầu theo dõi TapHoaMMO..."
        )
    except Exception as e:
        print(f"Failed to send startup message: {e}")
        
    print("Running initial chat fetch to set baseline (LAST_SEEN_CHATS)...")
    fetch_chats(is_baseline_run=True)
    
    print("Running initial notify poll...")
    poll_once()
    
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

# [CẬP NHẬT] Giao diện web
@app.get("/", response_class=HTMLResponse)
async def get_curl_ui():
    """
    Trả về giao diện HTML "siêu chuyên nghiệp" để dán 2 cURL và cấu hình lời chúc.
    """
    global GREETING_ENABLED, GREETING_IMAGE_LINKS, DEFAULT_IMAGE_LINKS
    
    links_to_show = GREETING_IMAGE_LINKS if GREETING_IMAGE_LINKS else DEFAULT_IMAGE_LINKS
    image_links_text = "\n".join(links_to_show)
    
    toggle_on_selected = "selected" if GREETING_ENABLED else ""
    toggle_off_selected = "" if GREETING_ENABLED else "selected"

    html_content = f"""
    <!DOCTYPE html>
    <html lang="vi">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Cập nhật cURL Poller - TapHoaMMO</title>
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700&display=swap');
            :root {{
                --bg-gradient: linear-gradient(135deg, #f4f7f9 0%, #e1e7ed 100%);
                --text-color: #333;
                --card-bg: #ffffff;
                --border-color: #dee2e6;
                --primary-color: #0061ff;
                --primary-hover: #004ecc;
                --primary-rgb: 0, 97, 255;
                --secondary-color: #6c757d;
                --secondary-hover: #5a6268;
                --success-bg: #d1f7e0; --success-border: #a3e9be; --success-text: #0a6847;
                --error-bg: #f8d7da; --error-border: #f5c6cb; --error-text: #721c24;
                --loading-bg: #e9ecef; --loading-border: #ced4da; --loading-text: #495057;
                --shadow: 0 8px 25px rgba(0,0,0,0.08);
            }}
            @media (prefers-color-scheme: dark) {{
                :root {{
                    --bg-gradient: linear-gradient(135deg, #2b3035 0%, #1a1e23 100%);
                    --text-color: #f0f0f0;
                    --card-bg: #22272e;
                    --border-color: #444951;
                    --primary-color: #1a88ff; --primary-hover: #006fff;
                    --primary-rgb: 26, 136, 255;
                    --secondary-color: #adb5bd; --secondary-hover: #9fa8b3;
                    --success-bg: #162a22; --success-border: #2a5a3a; --success-text: #a7d0b0;
                    --error-bg: #3a1a24; --error-border: #5a2a3a; --error-text: #f0a7b0;
                    --loading-bg: #343a40; --loading-border: #495057; --loading-text: #f8f9fa;
                }}
            }}
            body {{
                font-family: 'Roboto', -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                margin: 0; padding: 2rem; background: var(--bg-gradient);
                color: var(--text-color); line-height: 1.6; min-height: 100vh;
                box-sizing: border-box;
            }}
            .container {{
                max-width: 800px; margin: 2rem auto; 
            }}
            .card {{
                background: var(--card-bg);
                padding: 2.5rem 3rem; border-radius: 16px;
                box-shadow: var(--shadow); border: 1px solid var(--border-color);
                margin-bottom: 2rem;
            }}
            h1, h2 {{
                color: var(--primary-color); font-weight: 700;
                margin-top: 0; display: flex; align-items: center;
            }}
            h1 {{ font-size: 2.25rem; }}
            h2 {{ font-size: 1.75rem; border-bottom: 2px solid var(--border-color); padding-bottom: 0.5rem; }}
            h1 span, h2 span {{ font-size: 2.25rem; margin-right: 0.75rem; line-height: 1; filter: grayscale(30%); }}
            
            p.description {{
                font-size: 1.1rem; color: var(--text-color); opacity: 0.8; margin-bottom: 2rem;
            }}
            label {{
                display: block; margin-top: 1.5rem; margin-bottom: 0.5rem;
                font-weight: 500; font-size: 0.9rem; color: var(--text-color); opacity: 0.9;
            }}
            textarea, input[type="password"], select {{
                width: 100%; padding: 14px; border: 1px solid var(--border-color);
                border-radius: 8px; font-family: "SF Mono", "Fira Code", "Consolas", monospace;
                font-size: 14px; background-color: var(--card-bg); color: var(--text-color);
                box-sizing: border-box; transition: border-color 0.2s, box-shadow 0.2s;
            }}
            select {{
                font-family: 'Roboto', sans-serif;
                appearance: none;
                background-image: url("data:image/svg+xml,%3csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3e%3cpath fill='none' stroke='%23343a40' stroke-linecap='round' stroke-linejoin='round' stroke-width='2' d='M2 5l6 6 6-6'/%3e%3c/svg%3e");
                background-repeat: no-repeat;
                background-position: right 0.75rem center;
                background-size: 16px 12px;
            }}
            textarea {{ height: 150px; resize: vertical; }}
            textarea:focus, input[type="password"]:focus, select:focus {{
                outline: none; border-color: var(--primary-color);
                box-shadow: 0 0 0 3px rgba(var(--primary-rgb), 0.25);
            }}
            button {{
                background: var(--primary-color); color: white; padding: 16px 24px;
                border: none; border-radius: 8px; cursor: pointer;
                font-size: 1rem; font-weight: 700; letter-spacing: 0.5px;
                margin-top: 2rem; transition: background-color 0.2s, transform 0.1s;
                width: 100%;
            }}
            button.secondary {{
                background: var(--secondary-color);
            }}
            button:disabled {{ background-color: var(--border-color); cursor: not-allowed; opacity: 0.7; }}
            button:not(:disabled):hover {{ background: var(--primary-hover); transform: translateY(-2px); }}
            button.secondary:not(:disabled):hover {{ background: var(--secondary-hover); }}
            
            .status-message {{
                margin-top: 2rem; padding: 1.25rem; border-radius: 8px; font-weight: 500;
                display: none; border: 1px solid transparent; opacity: 0;
                transform: translateY(10px); transition: opacity 0.3s ease-out, transform 0.3s ease-out;
            }}
            .status-message.show {{ display: block; opacity: 1; transform: translateY(0); }}
            .status-message strong {{ font-weight: 700; display: block; margin-bottom: 0.25rem; }}
            .status-message.loading {{ background-color: var(--loading-bg); border-color: var(--loading-border); color: var(--loading-text); }}
            .status-message.loading strong::before {{ content: '⏳  ĐANG XỬ LÝ...'; }}
            .status-message.success {{ background-color: var(--success-bg); border-color: var(--success-border); color: var(--success-text); }}
            .status-message.success strong::before {{ content: '✅  THÀNH CÔNG!'; }}
            .status-message.error {{ background-color: var(--error-bg); border-color: var(--error-border); color: var(--error-text); }}
            .status-message.error strong::before {{ content: '❌  THẤT BẠI!'; }}
            .footer-text {{ text-align: center; margin-top: 2.5rem; font-size: 0.85rem; color: var(--text-color); opacity: 0.6; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="card">
                <h1><span>⚙️</span>Trình Cập Nhật Poller (v5.0)</h1>
                <p class="description">Dán cURL và cấu hình lời chúc 0h tại đây.</p>
                
                <form id="config-form">
                    <h2><span>📡</span> Cấu hình API (cURL)</h2>
                    <label for="curl_notify_text">1. cURL Thông Báo (getNotify):</label>
                    <textarea id="curl_notify_text" name="curl_notify" placeholder="curl '.../api/getNotify' ..." required></textarea>
                    
                    <label for="curl_chat_text">2. cURL Tin Nhắn (getNewConversion):</label>
                    <textarea id="curl_chat_text" name="curl_chat" placeholder="curl '.../api/getNewConversion' ..." required></textarea>

                    <h2 style="margin-top: 2.5rem;"><span>🌅</span> Cấu hình Lời chúc 0h</h2>
                    <label for="greeting_toggle">Trạng thái Lời chúc 0h:</label>
                    <select id="greeting_toggle" name="greeting_toggle">
                        <option value="1" {toggle_on_selected}>Bật</option>
                        <option value="0" {toggle_off_selected}>Tắt</option>
                    </select>

                    <label for="image_links">Danh sách Link ảnh (mỗi link 1 dòng):</label>
                    <textarea id="image_links" name="image_links" placeholder="https://i.imgur.com/...jpeg">{image_links_text}</textarea>
                    
                    <label for="secret_key">Secret Key (Dùng để Lưu):</label>
                    <input type="password" id="secret_key" name="secret" placeholder="Nhập WEBHOOK_SECRET của bạn" required>
                    
                    <button type="submit" id="submit-btn">Lưu Toàn Bộ Cấu Hình</button>
                </form>
                
                <div id="status" class="status-message">
                    <strong></strong>
                    <span id="status-body"></span>
                </div>
            </div>

            <div class="card">
                <h2><span>🧪</span> Khu vực Thử nghiệm</h2>
                <label for="test_secret_key">Secret Key (Dùng để Test):</label>
                <input type="password" id="test_secret_key" name="test_secret" placeholder="Nhập WEBHOOK_SECRET của bạn">
                
                <button type="button" id="test-greeting-btn" class="secondary">Gửi Thử Lời chúc 0h Ngay</button>
                
                <div id="test-status" class="status-message">
                    <strong></strong>
                    <span id="test-status-body"></span>
                </div>
            </div>
            
            <p class="footer-text">TapHoaMMO Poller Service 5.0 (Full Config)</p>
        </div>
        
        <script>
            // Xử lý Lưu Cấu hình
            document.getElementById("config-form").addEventListener("submit", async function(e) {{
                e.preventDefault();
                
                const curlNotifyText = document.getElementById("curl_notify_text").value;
                const curlChatText = document.getElementById("curl_chat_text").value;
                const imageLinks = document.getElementById("image_links").value;
                const greetingToggle = document.getElementById("greeting_toggle").value;
                const secret = document.getElementById("secret_key").value;
                
                const statusEl = document.getElementById("status");
                const statusBody = document.getElementById("status-body");
                const button = document.getElementById("submit-btn");
                
                statusBody.textContent = "Vui lòng chờ trong giây lát...";
                statusEl.className = "status-message loading show";
                button.disabled = true;
                
                if (!curlNotifyText || !curlChatText || !secret) {{
                    statusBody.textContent = "Vui lòng nhập ĐẦY ĐỦ 2 cURL và Secret Key.";
                    statusEl.className = "status-message error show";
                    button.disabled = false;
                    return;
                }}
                
                try {{
                    const response = await fetch(`/debug/set-config?secret=${{encodeURIComponent(secret)}}`, {{
                        method: "POST",
                        headers: {{"Content-Type": "application/json"}},
                        body: JSON.stringify({{ 
                            curl_notify: curlNotifyText,
                            curl_chat: curlChatText,
                            image_links_raw: imageLinks,
                            greeting_enabled_raw: greetingToggle
                        }})
                    }});
                    
                    const result = await response.json();
                    
                    if (response.ok) {{
                        statusBody.textContent = result.detail || "Đã lưu toàn bộ cấu hình. Bot sẽ áp dụng ngay.";
                        statusEl.className = "status-message success show";
                    }} else {{
                        statusBody.textContent = `Lỗi: ${{result.detail || 'Lỗi không xác định.'}}`;
                        statusEl.className = "status-message error show";
                    }}
                }} catch (err) {{
                    statusBody.textContent = `Lỗi kết nối: ${{err.message}}.`;
                    statusEl.className = "status-message error show";
                }} finally {{
                    button.disabled = false;
                }}
            }});

            // Xử lý Nút Test
            document.getElementById("test-greeting-btn").addEventListener("click", async function(e) {{
                e.preventDefault();
                
                const secret = document.getElementById("test_secret_key").value;
                const statusEl = document.getElementById("test-status");
                const statusBody = document.getElementById("test-status-body");
                const button = document.getElementById("test-greeting-btn");

                if (!secret) {{
                    statusBody.textContent = "Vui lòng nhập Secret Key (Dùng để Test).";
                    statusEl.className = "status-message error show";
                    return;
                }}

                statusBody.textContent = "Đang gửi tin nhắn test...";
                statusEl.className = "status-message loading show";
                button.disabled = true;

                try {{
                    const response = await fetch(`/debug/test-greeting?secret=${{encodeURIComponent(secret)}}`, {{
                        method: "POST"
                    }});
                    
                    const result = await response.json();
                    
                    if (response.ok) {{
                        statusBody.textContent = "Đã gửi tin nhắn test thành công! (Kiểm tra Telegram)";
                        statusEl.className = "status-message success show";
                    }} else {{
                        statusBody.textContent = `Lỗi: ${{result.detail || 'Lỗi không xác định.'}}`;
                        statusEl.className = "status-message error show";
                    }}
                }} catch (err) {{
                    statusBody.textContent = `Lỗi kết nối: ${{err.message}}.`;
                    statusEl.className = "status-message error show";
                }} finally {{
                    button.disabled = false;
                }}
            }});
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
        "greeting_enabled": GREETING_ENABLED,
        "greeting_image_count": len(GREETING_IMAGE_LINKS),
        "api_notify": {"url": NOTIFY_CONFIG.get("url"), "data": NOTIFY_CONFIG.get("body_data") is not None},
        "api_chat": {"url": CHAT_CONFIG.get("url"), "data": CHAT_CONFIG.get("body_data") is not None}
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

# [THÊM MỚI] Endpoint Test lời chúc
@app.post("/debug/test-greeting")
async def debug_test_greeting(secret: str):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    
    try:
        # Lấy ngày giờ hiện tại (hoặc ngày cuối cùng nếu có)
        date_to_show = DAILY_COUNTER_DATE or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7))).strftime("%Y-%m-%d")
        
        # Gửi test với số đơn hiện tại
        send_good_morning_message(date_to_show, DAILY_ORDER_COUNT)
        
        return {"ok": True, "detail": "Đã gửi tin nhắn test."}
    except Exception as e:
        print(f"Test greeting error: {e}")
        raise HTTPException(status_code=500, detail=f"Lỗi khi gửi test: {e}")


# [CẬP NHẬT] Endpoint set-config (thay thế set-curl)
@app.post("/debug/set-config")
async def debug_set_config(req: Request, secret: str):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    
    body = await req.json()
    curl_notify_txt = str(body.get("curl_notify") or "")
    curl_chat_txt = str(body.get("curl_chat") or "")
    image_links_raw = str(body.get("image_links_raw") or "")
    greeting_enabled_raw = str(body.get("greeting_enabled_raw") or "1")

    # --- 1. Xử lý cURL ---
    if not curl_notify_txt or not curl_chat_txt:
        msg = ("❌ <b>CẬP NHẬT CẤU HÌNH THẤT BẠI</b>\n"
               "Lý do: Một trong hai ô cURL bị bỏ trống.")
        tg_send(msg)
        raise HTTPException(status_code=400, detail="curl_notify and curl_chat are required.")

    parsed_notify = None
    parsed_chat = None
    
    try:
        parsed_notify = parse_curl_command(curl_notify_txt)
        parsed_chat = parse_curl_command(curl_chat_txt)
    except Exception as e:
        msg = ("❌ <b>CẬP NHẬT CẤU HÌNH THẤT BẠI</b>\n"
               f"Lý do: Lỗi nghiêm trọng khi phân tích cURL.\n"
               f"<code>{html.escape(str(e))}</code>")
        tg_send(msg)
        raise HTTPException(status_code=500, detail=f"Parsing error: {e}")

    if not (parsed_notify and parsed_notify.get("url")):
        tg_send("❌ <b>CẬP NHẬT CẤU HÌNH THẤT BẠI</b>\nLý do: Không thể phân tích URL từ cURL 1 (Notify).")
        raise HTTPException(status_code=400, detail="Invalid Notify cURL.")
        
    if not (parsed_chat and parsed_chat.get("url")):
        tg_send("❌ <b>CẬP NHẬT CẤU HÌNH THẤT BẠI</b>\nLý do: Không thể phân tích URL từ cURL 2 (Chat).")
        raise HTTPException(status_code=400, detail="Invalid Chat cURL.")

    if "getNewConversion" in parsed_notify.get("url", ""):
        tg_send("❌ <b>CẬP NHẬT CẤU HÌNH THẤT BẠI</b>\nLý do: <b>Bạn đã dán nhầm URL!</b>\nÔ <b>Notify</b> đang chứa link <b>getNewConversion</b>.")
        raise HTTPException(status_code=400, detail="URL Mismatch: Notify cURL contains getNewConversion.")
        
    if "getNotify" in parsed_chat.get("url", ""):
        tg_send("❌ <b>CẬP NHẬT CẤU HÌNH THẤT BẠI</b>\nLý do: <b>Bạn đã dán nhầm URL!</b>\nÔ <b>Chat</b> đang chứa link <b>getNotify</b>.")
        raise HTTPException(status_code=400, detail="URL Mismatch: Chat cURL contains getNotify.")

    # --- 2. Xử lý Cấu hình Lời chúc ---
    global NOTIFY_CONFIG, CHAT_CONFIG
    global LAST_NOTIFY_NUMS, DAILY_ORDER_COUNT, DAILY_COUNTER_DATE, SEEN_CHAT_DATES
    global LAST_SEEN_CHATS, GREETING_ENABLED, GREETING_IMAGE_LINKS
    
    # Áp dụng cURL
    NOTIFY_CONFIG = parsed_notify
    CHAT_CONFIG = parsed_chat

    # Áp dụng Cấu hình Lời chúc
    GREETING_ENABLED = bool(int(greeting_enabled_raw))
    GREETING_IMAGE_LINKS = [line.strip() for line in image_links_raw.splitlines() if line.strip().startswith('http')]
    
    # Reset lại toàn bộ
    LAST_NOTIFY_NUMS = []
    DAILY_ORDER_COUNT.clear()
    DAILY_COUNTER_DATE = "" 
    SEEN_CHAT_DATES.clear()
    LAST_SEEN_CHATS.clear()
    
    print("--- CONFIG UPDATED BY UI ---")
    print(f"Notify API set to: {NOTIFY_CONFIG.get('url')}")
    print(f"Chat API set to: {CHAT_CONFIG.get('url')}")
    print(f"Greeting Enabled: {GREETING_ENABLED}")
    print(f"Greeting Images: {len(GREETING_IMAGE_LINKS)} links")
    
    # Gửi thông báo thành công
    msg_success = (
        "✅ <b>CẬP NHẬT CẤU HÌNH THÀNH CÔNG (TAPHOAMMO)</b>\n"
        "Đã áp dụng cài đặt mới cho cả 2 API."
    )
    tg_send(msg_success)
    
    print("Config set. Poller loop will pick it up.")
    
    return {
        "ok": True,
        "detail": "Đã lưu cấu hình API và cấu hình Lời chúc 0h."
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
