import os, json, time, threading, html, hashlib, requests, re, shlex, random, copy
from typing import Any, Dict, List, Optional
from collections import defaultdict
import datetime # Để lấy ngày/giờ
from fastapi import FastAPI, Request, HTTPException, File, UploadFile
from fastapi.responses import JSONResponse, HTMLResponse, Response

# ----- .env (local); trên Render sẽ dùng Environment Variables -----
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

# =================== ENV ===================
# Các biến môi trường này VẪN CÓ TÁC DỤNG
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "12"))
VERIFY_TLS    = bool(int(os.getenv("VERIFY_TLS", "1")))
DISABLE_POLLER = os.getenv("DISABLE_POLLER", "0") == "1"

# [THAY ĐỔI v7.0] Các biến TELEGRAM_* và SECRET đã bị xóa,
# Chúng sẽ được quản lý trong GLOBAL_STATE thông qua UI.

# =================== CẤU HÌNH MẶC ĐỊNH ===================
DEFAULT_IMAGE_LINKS = [
    "Nhập đường link ảnh vào đây"
]
DEFAULT_GREETING_MESSAGES = [
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

# [THAY ĐỔI v7.0] Cấu trúc trạng thái toàn cục
# Đây là biến duy nhất chứa TOÀN BỘ cấu hình và trạng thái
GLOBAL_STATE = {
    "global_chat_id": "", # ID Telegram chung
    "accounts": {
        # "uuid-123-abc": {
        #     "account_name": "Tạp Hóa A",
        #     "bot_token": "...",
        #     "notify_curl": "...",
        #     "chat_curl": "...",
        #     "greeting_enabled": True,
        #     "greeting_images": [...],
        #     
        #     # Cấu hình đã parse (runtime)
        #     "notify_api": {"url": "", ...},
        #     "chat_api": {"url": "", ...},
        #
        #     # Trạng thái (runtime)
        #     "state_last_notify_nums": [],
        #     "state_daily_order_count": defaultdict(int),
        #     "state_daily_counter_date": "",
        #     "state_seen_chat_dates": set(),
        #     "state_last_error_times": defaultdict(float)
        # },
    }
}

# Thời gian cooldown lỗi (giữ nguyên)
ERROR_COOLDOWN_SECONDS = 3600 # 1 giờ

# =================== APP ===================
app = FastAPI(title="TapHoaMMO → Telegram (Multi-Account Poller)")

# =================== Telegram ===================
# [THAY ĐỔI v7.0] tg_send giờ nhận bot_token và chat_id
def tg_send(text: str, bot_token: str, chat_id: str, photo_url: Optional[str] = None):
    if not bot_token or not chat_id:
        print("[WARN] Missing bot_token or chat_id for tg_send")
        return

    api_url = ""
    payload = {}
    
    if photo_url:
        cache_buster = f"_t={int(time.time())}"
        if "?" in photo_url:
            final_photo_url = f"{photo_url}&{cache_buster}"
        else:
            final_photo_url = f"{photo_url}?{cache_buster}"
            
        api_url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
        caption = text
        if len(caption) > 1024:
            caption = text[:1021] + "..."
        
        payload = {
            "chat_id": chat_id,
            "photo": final_photo_url,
            "caption": caption,
            "parse_mode": "HTML"
        }
        
        try:
            r = requests.post(api_url, json=payload, timeout=30)
            if r.status_code >= 400:
                print(f"Telegram photo error: {r.status_code} {r.text}")
                # Nếu gửi ảnh lỗi, thử gửi chữ (không đệ quy vô hạn)
                tg_send(text, bot_token, chat_id, photo_url=None)
            return
        except Exception as e:
            print(f"Error sending photo: {e}")
            # Nếu gửi ảnh lỗi, thử gửi chữ
            tg_send(text, bot_token, chat_id, photo_url=None)
            return

    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    MAX = 3900  
    chunks = [text[i:i+MAX] for i in range(0, len(text), MAX)] or [""]
    
    for idx, part in enumerate(chunks[:3]):
        payload = {
            "chat_id": chat_id,
            "text": part,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        r_text = requests.post(api_url, json=payload, timeout=20)
        if r_text.status_code >= 400:
            print(f"Telegram text error: {r_text.status_code} {r_text.text}")
            break

# [THAY ĐỔI v7.0] can_send_error nhận state của tài khoản
def can_send_error(error_key: str, account_data: dict) -> bool:
    """Kiểm tra xem có nên gửi thông báo lỗi hay không, dựa trên thời gian cooldown."""
    global ERROR_COOLDOWN_SECONDS
    current_time = time.time()
    last_sent_time = account_data["state_last_error_times"][error_key]
    
    if (current_time - last_sent_time) > ERROR_COOLDOWN_SECONDS:
        account_data["state_last_error_times"][error_key] = current_time
        return True
    return False

# [THAY ĐỔI v7.0] Hàm gửi lời chúc nhận account_data
def send_good_morning_message(account_data: dict, global_chat_id: str):
    account_name = account_data.get('account_name', 'N/A')
    old_date = account_data.get('state_daily_counter_date', '')
    counts = account_data.get('state_daily_order_count', defaultdict(int))
    bot_token = account_data.get('bot_token', '')
    
    print(f"[{account_name}] Sending Good Morning message for end of day {old_date}...")

    try:
        date_obj = datetime.datetime.strptime(old_date, "%Y-%m-%d")
        formatted_date = date_obj.strftime("%d-%m-%Y")
    except ValueError:
        formatted_date = old_date

    product_total = counts.get("Đơn hàng sản phẩm", 0)
    service_total = counts.get("Đơn hàng dịch vụ", 0)
    total_orders = product_total + service_total

    msg_template = random.choice(DEFAULT_GREETING_MESSAGES)
    
    # [THÊM MỚI v7.0] Thêm tên tài khoản vào lời chúc
    prefix = f"<b>☀️ [{html.escape(account_name)}] ☀️</b>\n"
    msg = prefix + msg_template.format(date=formatted_date, orders=total_orders)

    photo = None
    links_to_use = account_data.get("greeting_images") if account_data.get("greeting_images") else DEFAULT_IMAGE_LINKS
    if links_to_use:
        photo = random.choice(links_to_use)
    
    tg_send(text=msg, bot_token=bot_token, chat_id=global_chat_id, photo_url=photo)


# =================== Helpers (Không đổi) ===================
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

def parse_curl_command(curl_text: str) -> Dict[str, Any]:
    # ... (Hàm này giữ nguyên, không cần thay đổi) ...
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

def _make_api_request(config: Dict[str, Any]) -> requests.Response:
    # ... (Hàm này giữ nguyên, không cần thay đổi) ...
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


# [THAY ĐỔI v7.0] Hàm gọi API Tin nhắn (nhận account_data)
def fetch_chats(account_data: dict, global_chat_id: str, is_baseline_run: bool = False) -> List[Dict[str, str]]:
    account_name = account_data.get('account_name', 'N/A')
    bot_token = account_data.get('bot_token', '')
    
    if not account_data["chat_api"].get("url"):
        if not is_baseline_run: print(f"[{account_name}] CHAT_API_URL is not set. Skipping chat fetch.")
        return []
    
    try:
        r = _make_api_request(account_data["chat_api"])

        try:
            data = r.json()
        except requests.exceptions.JSONDecodeError:
            error_msg = f"[{account_name}] [ERROR] Chat API (getNewConversion) did not return valid JSON. Status: {r.status_code}, Response: {r.text[:200]}..."
            print(error_msg)
            
            if not is_baseline_run and can_send_error("CHAT_JSON_DECODE", account_data):
                tg_send(f"⚠️ <b>[{html.escape(account_name)}] Lỗi API Chat:</b> Phản hồi không phải JSON (có thể do cookie/token sai). Lỗi sẽ chỉ báo lại sau 1 giờ.",
                        bot_token, global_chat_id)
            return []

        if not isinstance(data, list):
            print(f"[{account_name}] [ERROR] Chat API did not return a list. Response: {r.text[:200]}")
            return []

        new_messages = []
        current_chat_dates = set()
        
        # Trạng thái của tài khoản
        SEEN_CHAT_DATES = account_data["state_seen_chat_dates"]
        
        for chat in data:
            if not isinstance(chat, dict): continue
            
            user_id = chat.get("guest_user", "N/A")
            current_msg = chat.get("last_chat", "[không có nội dung]")

            chat_id = chat.get("date")
            if not chat_id:
                chat_id = hashlib.sha256(f"{user_id}:{current_msg}".encode()).hexdigest() 
            
            current_chat_dates.add(chat_id)
            
            is_new = chat_id not in SEEN_CHAT_DATES
            
            if is_new:
                SEEN_CHAT_DATES.add(chat_id)
                if not is_baseline_run:
                    new_messages.append({
                        "user": user_id,
                        "chat": current_msg,
                    })
        
        SEEN_CHAT_DATES.intersection_update(current_chat_dates)
        
        if new_messages:
            print(f"[{account_name}] Fetched {len(new_messages)} new message(s).")
        return new_messages

    except requests.exceptions.RequestException as e:
        if not is_baseline_run:
             print(f"[{account_name}] fetch_chats network error: {e}")
             if can_send_error("CHAT_NETWORK_ERROR", account_data):
                tg_send(f"⚠️ <b>[{html.escape(account_name)}] Lỗi Mạng API Chat:</b> Không thể kết nối. Lỗi sẽ chỉ báo lại sau 1 giờ.",
                        bot_token, global_chat_id)
        return []
    except Exception as e:
        if not is_baseline_run:
            print(f"[{account_name}] fetch_chats unexpected error: {e}")
            if can_send_error("CHAT_UNEXPECTED_ERROR", account_data):
                tg_send(f"⚠️ <b>[{html.escape(account_name)}] Lỗi không mong muốn API Chat:</b> Đã có lỗi xảy ra. Lỗi sẽ chỉ báo lại sau 1 giờ.",
                        bot_token, global_chat_id)
        return []

# [THAY ĐỔI v7.0] Hàm Poller (nhận account_data)
def poll_once(account_id: str, account_data: dict, global_chat_id: str, is_baseline_run: bool = False):
    account_name = account_data.get('account_name', 'N/A')
    bot_token = account_data.get('bot_token', '')

    # Lấy trạng thái từ account_data
    LAST_NOTIFY_NUMS = account_data["state_last_notify_nums"]
    DAILY_ORDER_COUNT = account_data["state_daily_order_count"]
    DAILY_COUNTER_DATE = account_data["state_daily_counter_date"]
    
    if not account_data["notify_api"].get("url"):
        if not is_baseline_run: print(f"[{account_name}] No NOTIFY_API_URL set")
        return

    try:
        r = _make_api_request(account_data["notify_api"])
        text = (r.text or "").strip()
        if not text:
            if not is_baseline_run: print(f"[{account_name}] getNotify: empty response")
            return

        low = text[:200].lower()
        if low.startswith("<!doctype") or "<html" in low:
            if text != str(LAST_NOTIFY_NUMS) and not is_baseline_run and can_send_error("NOTIFY_HTML_ERROR", account_data):
                tg_send(f"⚠️ <b>[{html.escape(account_name)}] getNotify trả về HTML</b> (Cookie/Header hết hạn?). Lỗi sẽ chỉ báo lại sau 1 giờ.",
                        bot_token, global_chat_id)
            if not is_baseline_run: print(f"[{account_name}] HTML detected, probably headers/cookie expired.")
            return
        
        parsed = parse_notify_text(text)
        
        if "numbers" in parsed:
            now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7)))
            today_str = now.strftime("%Y-%m-%d")

            if today_str != DAILY_COUNTER_DATE:
                if DAILY_COUNTER_DATE and account_data["greeting_enabled"]:
                    print(f"[{account_name}] New day detected ({today_str}). Sending good morning message for {DAILY_COUNTER_DATE}...")
                    # Cập nhật state trước khi gửi
                    account_data["state_daily_counter_date"] = DAILY_COUNTER_DATE
                    send_good_morning_message(account_data, global_chat_id)
                
                DAILY_COUNTER_DATE = today_str
                DAILY_ORDER_COUNT.clear()
            
            current_nums = parsed["numbers"]
            if len(current_nums) != len(LAST_NOTIFY_NUMS):
                LAST_NOTIFY_NUMS = [0] * len(current_nums)

            labels = _labels_for_notify(len(current_nums)) 
            instant_alerts_map = {}
            has_new_notification = False
            has_new_chat = False

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

            new_chat_messages = []
            if has_new_chat:
                fetched_messages = fetch_chats(account_data, global_chat_id, is_baseline_run=is_baseline_run) 
                for chat in fetched_messages:
                    user = html.escape(chat.get("user", "N/A"))
                    msg = html.escape(chat.get("chat", "..."))

                    new_chat_messages.append(f"<b>--- Tin nhắn từ: {user} ---</b>")
                    new_chat_messages.append(f"  <b>Nội dung: {msg}</b>")

            if has_new_notification and not is_baseline_run:
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
                    # [THÊM MỚI v7.0] Thêm tên tài khoản
                    f"<b>⭐ BÁO CÁO NHANH - [{html.escape(account_name)}]</b>"
                ]

                if new_chat_messages:
                    msg_lines.append("➖➖➖➖➖➖➖➖➖➖➖")
                    msg_lines.append("<b>💬 BẠN CÓ TIN NHẮN MỚI:</b>")
                    msg_lines.extend(new_chat_messages)
                
                if instant_alert_lines:
                    msg_lines.append("➖➖➖➖➖➖➖➖➖➖➖")
                    msg_lines.append("<b>🔔 CẬP NHẬT TRẠNG THÁI:</b>")
                    msg_lines.extend(instant_alert_lines)
                
                if new_chat_messages or instant_alert_lines:
                    msg = "\n".join(msg_lines)
                    tg_send(msg, bot_token, global_chat_id)
                    print(f"[{account_name}] getNotify changes (INCREASE) -> Professional Telegram sent.")
                else:
                    print(f"[{account_name}] getNotify changes (INCREASE) -> No new unread chats or alerts to show.")

            elif not is_baseline_run:
                print(f"[{account_name}] getNotify unchanged or DECREASED -> Skipping.")

            # Cập nhật lại state trong GLOBAL_STATE
            account_data["state_last_notify_nums"] = current_nums
            account_data["state_daily_counter_date"] = DAILY_COUNTER_DATE
            # DAILY_ORDER_COUNT được cập nhật qua tham chiếu
        
        else:
            if text != str(LAST_NOTIFY_NUMS) and not is_baseline_run and can_send_error("NOTIFY_NON_NUMERIC", account_data):
                msg = f"🔔 <b>[{html.escape(account_name)}] getNotify (lỗi)</b>\n<code>{html.escape(text)}</code>"
                tg_send(msg, bot_token, global_chat_id)
                print(f"[{account_name}] getNotify (non-numeric) changed -> Telegram sent.")

    except requests.exceptions.RequestException as e:
        if not is_baseline_run:
            print(f"[{account_name}] poll_once network error: {e}")
            if can_send_error("NOTIFY_NETWORK_ERROR", account_data):
                tg_send(f"⚠️ <b>[{html.escape(account_name)}] Lỗi Mạng API Notify:</b> Không thể kết nối. Lỗi sẽ chỉ báo lại sau 1 giờ.",
                        bot_token, global_chat_id)
    except Exception as e:
        if not is_baseline_run:
            print(f"[{account_name}] poll_once unexpected error: {e}")
            if can_send_error("NOTIFY_UNEXPECTED_ERROR", account_data):
                tg_send(f"⚠️ <b>[{html.escape(account_name)}] Lỗi không mong muốn API Notify:</b> Đã có lỗi xảy ra. Lỗi sẽ chỉ báo lại sau 1 giờ.",
                        bot_token, global_chat_id)

# [THAY ĐỔI v7.0] Vòng lặp Poller cho đa tài khoản
def poller_loop():
    print("▶ Poller started (Multi-Account Mode)")
    
    # Gửi tin nhắn khởi động 1 lần (nếu có thể)
    try:
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7)))
        time_str = now.strftime("%H:%M:%S")
        date_str = now.strftime("%Y-%m-%d")
        
        # Thử gửi bằng tài khoản đầu tiên nếu có
        global_chat_id = GLOBAL_STATE["global_chat_id"]
        first_account_token = ""
        if GLOBAL_STATE["accounts"]:
            first_account_token = list(GLOBAL_STATE["accounts"].values())[0].get("bot_token", "")
        
        if first_account_token and global_chat_id:
            tg_send(
                f"✅ <b>Bot đã khởi động! (Multi-Account)</b>\n"
                f"<i>(Lúc {time_str} - Ngày {date_str})</i>\n"
                f"Bắt đầu theo dõi...",
                first_account_token, global_chat_id
            )
        else:
            print("Startup message skipped (no chat_id or no accounts configured)")
            
    except Exception as e:
        print(f"Failed to send startup message: {e}")
        
    print("Running initial baseline fetch for all accounts...")
    
    # [THAY ĐỔI v7.0] Chạy baseline cho từng tài khoản
    current_accounts = list(GLOBAL_STATE["accounts"].items())
    global_chat_id = GLOBAL_STATE["global_chat_id"]
    
    for account_id, account_data in current_accounts:
        account_name = account_data.get('account_name', account_id)
        print(f"--- Running baseline for [{account_name}] ---")
        
        # 1. Baseline Chat
        print(f"Running initial chat fetch for [{account_name}]...")
        fetch_chats(account_data, global_chat_id, is_baseline_run=True)
        
        # 2. Baseline Notify
        print(f"Running initial notify poll for [{account_name}]...")
        poll_once(account_id, account_data, global_chat_id, is_baseline_run=True)
        
        # 3. Set Date
        if not account_data["state_daily_counter_date"]:
            account_data["state_daily_counter_date"] = datetime.datetime.now(
                datetime.timezone(datetime.timedelta(hours=7))
            ).strftime("%Y-%m-%d")
            print(f"[{account_name}] Baseline date set to: {account_data['state_daily_counter_date']}")

    print("--- Baseline complete. Starting main loop. ---")
    
    while True:
        try:
            time.sleep(POLL_INTERVAL)
            
            # [THAY ĐỔI v7.0] Lấy danh sách tài khoản MỖI LẦN lặp
            # Điều này cho phép thêm/xóa tài khoản mà không cần restart
            current_accounts_loop = list(GLOBAL_STATE["accounts"].items())
            global_chat_id_loop = GLOBAL_STATE["global_chat_id"]
            
            if not global_chat_id_loop:
                print("Poller loop skipped: global_chat_id is not set.")
                continue
                
            if not current_accounts_loop:
                print("Poller loop skipped: no accounts are configured.")
                continue

            for account_id, account_data in current_accounts_loop:
                # Đảm bảo các trường state tồn tại
                if "state_last_notify_nums" not in account_data:
                     print(f"Account {account_id} seems new, skipping first poll.")
                     continue
                
                poll_once(account_id, account_data, global_chat_id_loop, is_baseline_run=False)
        
        except Exception as e:
            print(f"[FATAL] Error in main poller_loop: {e}")
            time.sleep(60) # Chờ 1 phút nếu vòng lặp chính bị lỗi


# =================== [CẬP NHẬT v7.0] LÕI BACKUP/RESTORE (Đa tài khoản) ===================

def _create_account_state() -> dict:
    """Tạo một bộ state runtime rỗng cho tài khoản mới."""
    return {
        "notify_api": {"url": "", "method": "GET", "headers": {}, "body_json": None, "body_data": None},
        "chat_api": {"url": "", "method": "GET", "headers": {}, "body_json": None, "body_data": None},
        "state_last_notify_nums": [],
        "state_daily_order_count": defaultdict(int),
        "state_daily_counter_date": "",
        "state_seen_chat_dates": set(),
        "state_last_error_times": defaultdict(float)
    }

# [THAY ĐỔI v7.0] Hàm logic khôi phục (đa tài khoản)
def _apply_restore(new_config_data: Dict[str, Any]) -> bool:
    global GLOBAL_STATE
    
    # --- 1. Kiểm tra cấu trúc file backup ---
    if "global_chat_id" not in new_config_data or "accounts" not in new_config_data:
        print("Restore failed: Invalid structure (missing global_chat_id or accounts)")
        raise HTTPException(status_code=400, detail="Dữ liệu JSON không đúng cấu trúc (thiếu global_chat_id hoặc accounts).")
    
    if not isinstance(new_config_data["accounts"], dict):
        print("Restore failed: 'accounts' is not a dictionary")
        raise HTTPException(status_code=400, detail="Dữ liệu JSON không đúng cấu trúc ('accounts' phải là một đối tượng).")

    # --- 2. Tạo GLOBAL_STATE mới ---
    new_global_chat_id = new_config_data.get("global_chat_id", "")
    new_accounts_dict = {}

    for account_id, account_config in new_config_data["accounts"].items():
        try:
            # Lấy các trường config
            account_name = account_config.get("account_name", f"Account {account_id}")
            bot_token = account_config.get("bot_token", "")
            notify_curl = account_config.get("notify_curl", "")
            chat_curl = account_config.get("chat_curl", "")
            
            if not notify_curl or not chat_curl or not bot_token:
                print(f"Skipping account {account_id} (missing curl or bot_token)")
                continue

            # Parse cURL
            parsed_notify = parse_curl_command(notify_curl)
            parsed_chat = parse_curl_command(chat_curl)
            
            if not parsed_notify.get("url") or not parsed_chat.get("url"):
                 print(f"Skipping account {account_id} (invalid cURL parse)")
                 continue

            # Tạo account data hoàn chỉnh (config + state)
            new_account_data = {
                "account_name": account_name,
                "bot_token": bot_token,
                "notify_curl": notify_curl,
                "chat_curl": chat_curl,
                "greeting_enabled": account_config.get("greeting_enabled", True),
                "greeting_images": account_config.get("greeting_images", list(DEFAULT_IMAGE_LINKS)),
                
                **_create_account_state() # Thêm state rỗng
            }
            
            # Ghi đè state đã parse
            new_account_data["notify_api"] = parsed_notify
            new_account_data["chat_api"] = parsed_chat
            
            new_accounts_dict[account_id] = new_account_data
            
        except Exception as e:
            print(f"Failed to parse account {account_id}: {e}")
            # Bỏ qua tài khoản lỗi và tiếp tục
    
    # --- 3. Áp dụng trạng thái mới ---
    GLOBAL_STATE["global_chat_id"] = new_global_chat_id
    GLOBAL_STATE["accounts"] = new_accounts_dict
    
    print("--- CONFIG RESTORED BY UI (Multi-Account) ---")
    print(f"Global Chat ID set to: {GLOBAL_STATE['global_chat_id']}")
    print(f"Restored {len(GLOBAL_STATE['accounts'])} accounts.")
    
    # Gửi thông báo (thử dùng bot đầu tiên)
    try:
        first_account_token = ""
        if GLOBAL_STATE["accounts"]:
            first_account_token = list(GLOBAL_STATE["accounts"].values())[0].get("bot_token", "")
        
        if first_account_token and new_global_chat_id:
             tg_send("✅ <b>KHÔI PHỤC THÀNH CÔNG (Multi-Account)</b>\nToàn bộ cấu hình đã được khôi phục. Bot sẽ chạy lại từ đầu.",
                     first_account_token, new_global_chat_id)
    except Exception as e:
        print(f"Failed to send restore confirmation: {e}")
        
    return True

# =================== API endpoints ===================

# [CẬP NHẬT v7.0] Giao diện web (Đa tài khoản)
@app.get("/", response_class=HTMLResponse)
async def get_curl_ui():
    # HTML này giờ đây là 1 cái "khung"
    # Dữ liệu sẽ được load bằng JavaScript qua API
    html_content = f"""
    <!DOCTYPE html>
    <html lang="vi">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Bảng điều khiển Poller (Đa tài khoản)</title>
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;700&display=swap');
            
            :root {{
                --bg-color: #0F0F1A;
                --card-bg: #1A1A2B;
                --card-bg-light: #2A2A3B;
                --text-color: #E0E0FF;
                --text-muted: #8F8FA8;
                --border-color: #3A3A5A;
                --primary-glow: #00AFFF;
                --secondary-glow: #6A00FF;
                --success-color: #00FFC2;
                --error-color: #FF4D80;
                --warn-color: #FFB800;
                --shadow: 0 0 15px rgba(0, 175, 255, 0.2);
            }}

            /* ... (Giữ nguyên hiệu ứng sao băng) ... */
            @keyframes shooting-star {{
                0% {{ transform: translateX(100vw) translateY(-100vh); opacity: 1; }}
                100% {{ transform: translateX(-100vw) translateY(100vh); opacity: 0; }}
            }}
            .star {{
                position: fixed; top: 0; left: 0; width: 2px; height: 2px;
                background: linear-gradient(to bottom, rgba(255,255,255,0.8), rgba(255,255,255,0));
                border-radius: 50%; box-shadow: 0 0 10px 2px #FFF; opacity: 0;
                animation: shooting-star 10s linear infinite; z-index: -1;
            }}
            .star:nth-child(1) {{ animation-delay: 0s; left: 20%; top: -50%; animation-duration: 5s; }}
            .star:nth-child(2) {{ animation-delay: 1.5s; left: 50%; top: -30%; animation-duration: 7s; }}
            .star:nth-child(3) {{ animation-delay: 3s; left: 80%; top: -60%; animation-duration: 6s; }}
            .star:nth-child(4) {{ animation-delay: 5s; left: 10%; top: -40%; animation-duration: 8s; }}

            body {{
                font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                margin: 0; padding: 2.5rem; background: var(--bg-color);
                color: var(--text-color); line-height: 1.6; min-height: 100vh;
                box-sizing: border-box; overflow-x: hidden;
            }}
            .container {{ max-width: 900px; margin: 1rem auto; position: relative; z-index: 1; }}
            .card {{
                background: rgba(26, 26, 43, 0.85); backdrop-filter: blur(10px);
                padding: 2.5rem 3rem; border-radius: 16px;
                border: 1px solid transparent;
                border-image: linear-gradient(135deg, var(--primary-glow) 0%, var(--secondary-glow) 100%) 1;
                box-shadow: 0 10px 30px rgba(0, 0, 0, 0.3), 0 0 25px rgba(106, 0, 255, 0.2);
                margin-bottom: 2.5rem;
            }}
            h1, h2 {{
                font-weight: 700; margin-top: 0; display: flex; align-items: center;
                letter-spacing: -0.5px;
            }}
            h1 {{ 
                font-size: 2.25rem; 
                background: linear-gradient(90deg, var(--primary-glow), var(--success-color));
                -webkit-background-clip: text; -webkit-text-fill-color: transparent;
                text-shadow: 0 0 10px rgba(0, 175, 255, 0.3);
            }}
            h2 {{ 
                font-size: 1.75rem; color: var(--text-color);
                border-bottom: 1px solid var(--border-color); padding-bottom: 0.75rem;
            }}
            h1 span, h2 span {{ 
                font-size: 2.25rem; margin-right: 0.75rem; line-height: 1; 
                color: var(--primary-glow);
            }}
            
            p.description {{ font-size: 1.1rem; color: var(--text-muted); margin-bottom: 2rem; }}
            label {{
                display: block; margin-top: 1.5rem; margin-bottom: 0.5rem;
                font-weight: 500; font-size: 0.9rem; color: var(--text-muted);
                text-transform: uppercase; letter-spacing: 0.5px;
            }}
            textarea, input[type="text"], input[type="password"], select {{
                width: 100%; padding: 14px; border: 1px solid var(--border-color);
                border-radius: 8px; font-family: "SF Mono", "Fira Code", "Consolas", monospace;
                font-size: 14px; background-color: var(--bg-color); color: var(--text-color);
                box-sizing: border-box; transition: border-color 0.3s, box-shadow 0.3s;
            }}
            select {{
                font-family: 'Inter', sans-serif; appearance: none;
                background-image: url("data:image/svg+xml,%3csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3e%3cpath fill='none' stroke='%238F8FA8' stroke-linecap='round' stroke-linejoin='round' stroke-width='2' d='M2 5l6 6 6-6'/%3e%3c/svg%3e");
                background-repeat: no-repeat; background-position: right 0.75rem center;
                background-size: 16px 12px;
            }}
            textarea {{ height: 120px; resize: vertical; }}
            textarea#backup_data {{ height: 100px; }}
            textarea:focus, input[type="text"]:focus, input[type="password"]:focus, select:focus {{
                outline: none; border-color: var(--primary-glow);
                box-shadow: 0 0 15px rgba(0, 175, 255, 0.3);
            }}
            
            input[type="file"] {{ display: none; }}
            .file-upload-btn {{
                display: block; padding: 14px; background: var(--secondary-glow); color: white;
                border-radius: 8px; text-align: center; cursor: pointer;
                font-weight: 500; transition: background-color 0.3s; margin-top: 1rem;
            }}
            .file-upload-btn:hover {{ background: #5a00d1; }}
            #file-name {{ color: var(--text-muted); font-style: italic; margin-top: 0.5rem; }}

            button {{
                background: linear-gradient(90deg, var(--primary-glow) 0%, var(--secondary-glow) 100%);
                color: white; padding: 16px 24px;
                border: none; border-radius: 8px; cursor: pointer;
                font-size: 1rem; font-weight: 700; letter-spacing: 0.5px;
                margin-top: 2rem; transition: all 0.3s; width: 100%;
                box-shadow: 0 4px 15px rgba(0, 175, 255, 0.3);
            }}
            button.secondary {{
                background: var(--card-bg-light);
                border: 1px solid var(--border-color);
                box-shadow: none;
            }}
            button.danger {{
                background: #4d1a2b; /* Màu đỏ sẫm */
                border: 1px solid var(--error-color);
                color: var(--error-color);
                box-shadow: none;
            }}
            button:disabled {{ 
                background: var(--border-color); cursor: not-allowed; opacity: 0.7; box-shadow: none;
            }}
            button:not(:disabled):hover {{ 
                transform: translateY(-2px);
                box-shadow: 0 8px 20px rgba(0, 175, 255, 0.5);
            }}
            button.secondary:not(:disabled):hover {{ 
                background: var(--border-color);
                box-shadow: none; transform: translateY(-2px);
            }}
            button.danger:not(:disabled):hover {{ 
                background: rgba(255, 77, 128, 0.2);
                box-shadow: none; transform: translateY(-2px);
            }}
            
            .status-message {{
                margin-top: 2rem; padding: 1.25rem; border-radius: 8px; font-weight: 500;
                display: none; border: 1px solid transparent; opacity: 0;
                transform: translateY(10px); transition: opacity 0.3s ease-out, transform 0.3s ease-out;
            }}
            .status-message.show {{ display: block; opacity: 1; transform: translateY(0); }}
            .status-message strong {{ font-weight: 700; display: block; margin-bottom: 0.25rem; }}
            .status-message.loading {{ background-color: #333; border-color: var(--border-color); color: var(--text-muted); }}
            .status-message.loading strong::before {{ content: '⏳  ĐANG XỬ LÝ...'; }}
            .status-message.success {{ background-color: rgba(0, 255, 194, 0.1); border-color: var(--success-color); color: var(--success-color); }}
            .status-message.success strong::before {{ content: '✅  THÀNH CÔNG!'; }}
            .status-message.error {{ background-color: rgba(255, 77, 128, 0.1); border-color: var(--error-color); color: var(--error-color); }}
            .status-message.error strong::before {{ content: '❌  THẤT BẠI!'; }}
            .status-message.warn {{ background-color: rgba(255, 184, 0, 0.1); border-color: var(--warn-color); color: var(--warn-color); }}
            
            .footer-text {{
                text-align: center; margin-top: 2.5rem; font-size: 0.9rem; color: var(--text-muted); opacity: 0.8;
                display: flex; align-items: center; justify-content: center;
            }}
            .blue-check {{ width: 18px; height: 18px; margin-left: 8px; }}

            /* [THÊM MỚI v7.0] Kiểu cho thẻ tài khoản */
            .account-card {{
                background: var(--card-bg-light);
                padding: 1.5rem 2rem;
                border-radius: 12px;
                border: 1px solid var(--border-color);
                margin-top: 1.5rem;
                position: relative;
            }}
            .account-card-header {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                border-bottom: 1px solid var(--border-color);
                padding-bottom: 1rem;
                margin-bottom: 1rem;
            }}
            .account-card-header h3 {{
                margin: 0;
                font-size: 1.25rem;
                color: var(--primary-glow);
            }}
            .account-card .grid {{
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 1.5rem;
            }}
            .account-card .col-span-2 {{
                grid-column: span 2 / span 2;
            }}
            /* Nút test nhỏ */
            .account-card button.test-btn {{
                width: auto;
                font-size: 0.9rem;
                padding: 10px 16px;
                margin-top: 1rem;
                margin-right: 0.5rem;
            }}
            /* Nút Xóa */
            .account-delete-btn {{
                background: none; border: none; font-size: 1.5rem;
                color: var(--text-muted); cursor: pointer;
                padding: 0.5rem; line-height: 1;
            }}
            .account-delete-btn:hover {{ color: var(--error-color); }}
            
            #global-save-btn-container {{
                position: sticky;
                bottom: 0;
                padding: 1.5rem;
                background: linear-gradient(180deg, rgba(15, 15, 26, 0) 0%, var(--bg-color) 70%);
                z-index: 10;
                margin: 2rem -1.5rem -1.5rem -1.5rem;
            }}
            
            @media (max-width: 768px) {{
                body {{ padding: 1.5rem; }}
                .card {{ padding: 2rem 1.5rem; }}
                .account-card .grid {{ grid-template-columns: 1fr; }}
                .account-card .col-span-2 {{ grid-column: span 1 / span 1; }}
            }}
        </style>
    </head>
    <body>
        <div class="star"></div><div class="star"></div><div class="star"></div><div class="star"></div>

        <div class="container">
            <div class="card">
                <h1><span>🌌</span>Bảng Điều Khiển (v7.0 - Multi)</h1>
                <p class="description">Quản lý API và Lời chúc 0h cho nhiều tài khoản.</p>
                
                <form id="config-form">
                    <h2><span>🌍</span> Cấu hình chung</h2>
                    <label for="global_chat_id">1. ID Telegram (Chat ID chung)</label>
                    <input type="text" id="global_chat_id" placeholder="Nhập ID kênh/nhóm chat Telegram (ví dụ: -100123...)" required>

                    <h2 style="margin-top: 2.5rem;"><span>📦</span> Danh sách Tài khoản Tạp Hóa</h2>
                    <div id="account-list">
                        </div>
                    
                    <button type="button" id="add-account-btn" class="secondary" style="margin-top: 1.5rem;">
                        + Thêm Tài Khoản Tạp Hóa Mới
                    </button>
                    
                    <div id="global-save-btn-container">
                        <div id="status" class="status-message">
                            <strong></strong> <span id="status-body"></span>
                        </div>
                        <button type="submit" id="submit-btn">Lưu Toàn Bộ Cấu Hình</button>
                    </div>
                </form>
            </div>

            <div class="card">
                <h2><span>📦</span> Backup & Restore (Toàn bộ)</h2>
                <p class="description">Tạo hoặc khôi phục TOÀN BỘ cấu hình (Chat ID và tất cả tài khoản).</p>
                
                <label for="backup_data" style="margin-top: 1.5rem;">Dữ liệu Backup (Copy/Paste):</label>
                <textarea id="backup_data" placeholder="Ấn '1. Tạo Backup' để lấy dữ liệu. Hoặc dán dữ liệu restore vào đây..."></textarea>
                
                <div style="display: flex; gap: 1rem; margin-top: 2rem; flex-wrap: wrap;">
                    <button type="button" id="backup-btn" class="secondary" style="flex-grow: 1; margin: 0;">1. Tạo Backup (Hiển thị)</button>
                    <button type="button" id="restore-text-btn" style="flex-grow: 1; margin: 0;">2. Khôi phục từ Text</button>
                </div>

                <label for="restore-file" class="file-upload-btn" style="width: 100%; margin: 1rem 0 0 0; background: var(--secondary-glow);">
                    ... Hoặc 3. Khôi phục từ File (.json) ...
                </label>
                <input type="file" id="restore-file" accept=".json">
                <div id="file-name" style="text-align: center; margin-top: 1rem;">Chưa chọn file nào.</div>

                <div id="backup-status" class="status-message">
                    <strong></strong> <span id="backup-status-body"></span>
                </div>
            </div>
            
            <footer class="footer-text">
                Bản quyền thuộc về Admin Văn Linh
                <svg class="blue-check" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                    <path fill-rule="evenodd" clip-rule="evenodd" d="M12 2C6.47715 2 2 6.47715 2 12C2 17.5228 6.47715 22 12 22C17.5228 22 22 17.5228 22 12C22 6.47715 17.5228 2 12 2ZM16.7071 9.29289C17.0976 9.68342 17.0976 10.3166 16.7071 10.7071L11.7071 15.7071C11.3166 16.0976 10.6834 16.0976 10.2929 15.7071L7.29289 12.7071C6.90237 12.3166 6.90237 11.6834 7.29289 11.2929C7.68342 10.9024 8.31658 10.9024 8.70711 11.2929L11 13.5858L15.2929 9.29289C15.6834 8.90237 16.3166 8.90237 16.7071 9.29289Z" fill="url(#paint0_linear_v6)"/>
                    <defs>
                        <linearGradient id="paint0_linear_v6" x1="2" y1="2" x2="22" y2="22" gradientUnits="userSpaceOnUse">
                            <stop stop-color="#00AFFF"/><stop offset="1" stop-color="#6A00FF"/>
                        </linearGradient>
                    </defs>
                </svg>
            </footer>
        </div>

        <template id="account-template">
            <div class="account-card" data-account-id="">
                <div class="account-card-header">
                    <h3 class="account-title">Tài khoản mới</h3>
                    <button type="button" class="account-delete-btn" title="Xóa tài khoản này">×</button>
                </div>
                
                <div class="grid">
                    <div>
                        <label>Tên Tạp Hóa (Để nhận diện)</label>
                        <input type="text" class="account-name" placeholder="Ví dụ: Tạp Hóa A" required>
                    </div>
                    <div>
                        <label>Telegram Bot Token</label>
                        <input type="password" class="account-bot-token" placeholder="123456:ABC-..." required>
                    </div>
                </div>

                <label>cURL Thông Báo (getNotify):</label>
                <textarea class="account-curl-notify" placeholder="curl '.../api/getNotify' ..." required></textarea>
                
                <label>cURL Tin Nhắn (getNewConversion):</label>
                <textarea class="account-curl-chat" placeholder="curl '.../api/getNewConversion' ..." required></textarea>

                <div class="grid">
                    <div>
                        <label>Trạng thái Lời chúc 0h:</label>
                        <select class="account-greeting-toggle">
                            <option value="1">Bật</option>
                            <option value="0">Tắt</option>
                        </select>
                    </div>
                    <div>
                        <label>&nbsp;</label>
                        <button type="button" class="test-greeting-btn secondary test-btn">Gửi Thử Lời chúc 0h</button>
                    </div>
                </div>
                
                <label>Danh sách Link ảnh (mỗi link 1 dòng):</label>
                <textarea class="account-image-links" placeholder="https://i.imgur.com/...jpeg"></textarea>

                <div class="status-message warn test-status" style="margin-top: 1rem;">
                    <strong></strong> <span></span>
                </div>
            </div>
        </template>
        
        <script>
            // [CẬP NHẬT v7.0] Toàn bộ JS quản lý UI
            
            const accountList = document.getElementById('account-list');
            const template = document.getElementById('account-template');
            const mainStatusEl = document.getElementById('status');
            const mainStatusBody = document.getElementById('status-body');
            const mainSubmitBtn = document.getElementById('submit-btn');

            /**
             * Hiển thị thông báo chính (dưới nút Lưu)
             */
            function showMainStatus(type, message) {{
                mainStatusBody.textContent = message;
                mainStatusEl.className = `status-message ${{type}} show`;
            }}

            /**
             * Hiển thị thông báo test (trong card)
             */
            function showTestStatus(cardEl, type, message) {{
                const statusEl = cardEl.querySelector('.test-status');
                statusEl.querySelector('span').textContent = message;
                statusEl.className = `status-message ${{type}} show test-status`;
            }}

            /**
             * Thêm một card tài khoản vào UI
             */
            function addAccountCard(accountId, accountData) {{
                const card = template.content.cloneNode(true).firstElementChild;
                const newAccountId = accountId || crypto.randomUUID();
                card.dataset.accountId = newAccountId;

                const nameInput = card.querySelector('.account-name');
                nameInput.value = accountData.account_name || '';
                
                const title = card.querySelector('.account-title');
                title.textContent = accountData.account_name || 'Tài khoản mới';
                
                // Cập nhật tiêu đề khi gõ tên
                nameInput.addEventListener('input', () => {{
                    title.textContent = nameInput.value || 'Tài khoản mới';
                }});
                
                card.querySelector('.account-bot-token').value = accountData.bot_token || '';
                card.querySelector('.account-curl-notify').value = accountData.notify_curl || '';
                card.querySelector('.account-curl-chat').value = accountData.chat_curl || '';
                card.querySelector('.account-greeting-toggle').value = (accountData.greeting_enabled === false) ? '0' : '1';
                card.querySelector('.account-image-links').value = (accountData.greeting_images || []).join('\\n');

                // Nút Xóa
                card.querySelector('.account-delete-btn').addEventListener('click', () => {{
                    if (confirm(`Bạn có chắc chắn muốn xóa tài khoản "${{nameInput.value}}"?`)) {{
                        card.remove();
                        showMainStatus('warn', 'Đã xóa tài khoản. (Chưa lưu)');
                    }}
                }});

                // Nút Test Greeting
                card.querySelector('.test-greeting-btn').addEventListener('click', async (e) => {{
                    const btn = e.currentTarget;
                    const accountId = card.dataset.accountId;
                    
                    if (!accountId) {{
                        showTestStatus(card, 'error', 'Không thể test, tài khoản chưa được lưu.');
                        return;
                    }}
                    
                    showTestStatus(card, 'loading', 'Đang gửi tin nhắn test...');
                    btn.disabled = true;
                    
                    try {{
                        // Lưu ý: Endpoint này yêu cầu account_id
                        const response = await fetch(`/debug/test-greeting?account_id=${{encodeURIComponent(accountId)}}`, {{ 
                            method: "POST" 
                        }});
                        const result = await response.json();
                        
                        if (response.ok) {{
                            showTestStatus(card, 'success', 'Đã gửi tin nhắn test thành công! (Kiểm tra Telegram)');
                        }} else {{
                            showTestStatus(card, 'error', `Lỗi: ${{result.detail || 'Lỗi không xác định.'}}`);
                        }}
                    }} catch (err) {{
                        showTestStatus(card, 'error', `Lỗi kết nối: ${{err.message}}.`);
                    }} finally {{
                        btn.disabled = false;
                        // Tự ẩn sau 5s
                        setTimeout(() => {{
                            showTestStatus(card, '', '');
                            card.querySelector('.test-status').classList.remove('show');
                        }}, 5000);
                    }}
                }});
                
                accountList.appendChild(card);
            }}
            
            /**
             * Tải cấu hình hiện tại từ server
             */
            async function loadConfig() {{
                showMainStatus('loading', 'Đang tải cấu hình hiện tại...');
                try {{
                    const response = await fetch('/debug/get-backup');
                    if (!response.ok) throw new Error('Không thể tải backup');
                    
                    const config = await response.json();
                    
                    document.getElementById('global_chat_id').value = config.global_chat_id || '';
                    
                    accountList.innerHTML = ''; // Xóa card cũ
                    if (config.accounts) {{
                        for (const [accountId, accountData] of Object.entries(config.accounts)) {{
                            addAccountCard(accountId, accountData);
                        }}
                    }}
                    
                    showMainStatus('success', `Đã tải thành công ${{{Object.keys(config.accounts || {}).length}}} tài khoản.`);
                    setTimeout(() => mainStatusEl.classList.remove('show'), 3000);
                    
                }} catch (err) {{
                    showMainStatus('error', `Lỗi tải cấu hình: ${{err.message}}`);
                }}
            }}
            
            /**
             * Thu thập dữ liệu từ UI và Lưu
             */
            document.getElementById('config-form').addEventListener('submit', async function(e) {{
                e.preventDefault();
                showMainStatus('loading', 'Đang thu thập và lưu dữ liệu...');
                mainSubmitBtn.disabled = true;

                try {{
                    const globalChatId = document.getElementById('global_chat_id').value;
                    if (!globalChatId) {{
                        throw new Error('Vui lòng nhập ID Telegram (Chat ID chung).');
                    }}
                    
                    const newState = {{
                        global_chat_id: globalChatId,
                        accounts: {{}}
                    }};
                    
                    const accountCards = document.querySelectorAll('.account-card');
                    let validAccounts = 0;
                    
                    for (const card of accountCards) {{
                        const accountId = card.dataset.accountId;
                        const accountName = card.querySelector('.account-name').value;
                        const botToken = card.querySelector('.account-bot-token').value;
                        const notifyCurl = card.querySelector('.account-curl-notify').value;
                        const chatCurl = card.querySelector('.account-curl-chat').value;
                        
                        if (!accountName || !botToken || !notifyCurl || !chatCurl) {{
                            showMainStatus('error', `Tài khoản "${{accountName || 'Không tên'}}" bị thiếu thông tin. Vui lòng điền đủ Tên, Token và 2 cURL.`);
                            mainSubmitBtn.disabled = false;
                            card.style.borderColor = 'var(--error-color)'; // Highlight card lỗi
                            return;
                        }}
                        card.style.borderColor = 'var(--border-color)'; // Reset highlight

                        newState.accounts[accountId] = {{
                            account_name: accountName,
                            bot_token: botToken,
                            notify_curl: notifyCurl,
                            chat_curl: chatCurl,
                            greeting_enabled: card.querySelector('.account-greeting-toggle').value === '1',
                            greeting_images: card.querySelector('.account-image-links').value
                                                .split('\\n')
                                                .map(line => line.trim())
                                                .filter(line => line.startsWith('http'))
                        }};
                        validAccounts++;
                    }}
                    
                    // Gửi dữ liệu mới
                    // [THAY ĐỔI v7.0] Endpoint set-config giờ = restore-from-text
                    const response = await fetch(`/debug/set-config`, {{
                        method: "POST",
                        headers: {{"Content-Type": "application/json"}},
                        body: JSON.stringify(newState)
                    }});
                    
                    const result = await response.json();
                    
                    if (response.ok) {{
                        showMainStatus('success', result.detail || `Đã lưu thành công ${{validAccounts}} tài khoản. Bot sẽ áp dụng ngay.`);
                        // Tải lại config để đồng bộ (lấy UUID mới nếu có)
                        await loadConfig();
                    }} else {{
                        throw new Error(result.detail || 'Lỗi không xác định từ server.');
                    }}
                }} catch (err) {{
                    showMainStatus('error', `Lỗi khi lưu: ${{err.message}}.`);
                }} finally {{
                    mainSubmitBtn.disabled = false;
                }}
            }});
            
            // Nút Thêm Tài Khoản
            document.getElementById('add-account-btn').addEventListener('click', () => {{
                addAccountCard(null, {{ greeting_enabled: true, greeting_images: [] }});
                showMainStatus('warn', 'Đã thêm tài khoản mới. Vui lòng điền thông tin và Lưu.');
            }});
            
            // Tải config khi trang mở
            document.addEventListener('DOMContentLoaded', loadConfig);
            
            // --- [CẬP NHẬT v7.0] Xử lý Backup/Restore ---
            
            const backupStatusEl = document.getElementById('backup-status');
            const backupStatusBody = document.getElementById('backup-status-body');
            const backupDataEl = document.getElementById('backup_data');

            function showBackupStatus(type, message) {{
                backupStatusBody.textContent = message;
                backupStatusEl.className = `status-message ${{type}} show`;
            }}

            // 1. Tạo Backup
            document.getElementById('backup-btn').addEventListener('click', async function() {{
                showBackupStatus('loading', 'Đang lấy dữ liệu backup...');
                try {{
                    const response = await fetch(`/debug/get-backup`);
                    const result = await response.json();
                    if (response.ok) {{
                        backupDataEl.value = JSON.stringify(result, null, 2);
                        showBackupStatus('success', 'Đã lấy dữ liệu backup thành công. Hãy copy text bên trên.');
                    }} else {{
                        throw new Error(result.detail || 'Lỗi không xác định.');
                    }}
                }} catch (err) {{
                    showBackupStatus('error', `Lỗi kết nối: ${{err.message}}.`);
                }}
            }});
            
            // Hàm logic chung để Restore
            async function triggerRestore(data) {{
                showBackupStatus('loading', 'Đang khôi phục...');
                try {{
                    // Đảm bảo data là JSON
                    try {{ JSON.parse(data); }} catch (e) {{ throw new Error('Dữ liệu không phải là JSON hợp lệ.'); }}
                    
                    // [THAY ĐỔI v7.0] Xóa secret
                    const response = await fetch(`/debug/restore-from-text`, {{
                        method: "POST",
                        headers: {{"Content-Type": "application/json"}},
                        body: data
                    }});
                    const result = await response.json();
                    
                    if (response.ok) {{
                        showBackupStatus('success', 'Khôi phục thành công! Cấu hình đã được áp dụng. Trang sẽ tự tải lại...');
                        // Tải lại config ở trang chính
                        await loadConfig();
                        // Xóa dữ liệu backup
                        backupDataEl.value = '';
                        document.getElementById('file-name').textContent = 'Chưa chọn file nào.';
                        document.getElementById('restore-file').value = '';
                    }} else {{
                        throw new Error(result.detail || 'Lỗi không xác định.');
                    }}
                }} catch (err) {{
                    showBackupStatus('error', `Lỗi khôi phục: ${{err.message}}.`);
                }}
            }}

            // 2. Khôi phục từ Text
            document.getElementById('restore-text-btn').addEventListener('click', async function() {{
                const backupData = backupDataEl.value;
                if (!backupData) {{
                    showBackupStatus('error', 'Vui lòng dán dữ liệu Backup vào ô.');
                    return;
                }}
                if (confirm("Bạn có chắc chắn muốn khôi phục? TOÀN BỘ dữ liệu cũ (tất cả tài khoản) sẽ bị ghi đè.")) {{
                    triggerRestore(backupData);
                }}
            }});

            // 3. Khôi phục từ File
            const fileInput = document.getElementById('restore-file');
            const fileNameEl = document.getElementById('file-name');
            fileInput.addEventListener('change', function(e) {{
                const file = e.target.files[0];
                if (file) {{
                    fileNameEl.textContent = `Đã chọn: ${{file.name}}`;
                    if (confirm("Bạn có chắc chắn muốn khôi phục? TOÀN BỘ dữ liệu cũ (tất cả tài khoản) sẽ bị ghi đè.")) {{
                        const reader = new FileReader();
                        reader.onload = function(evt) {{
                            triggerRestore(evt.target.result);
                        }};
                        reader.readAsText(file);
                    }} else {{
                        fileInput.value = "";
                        fileNameEl.textContent = "Chưa chọn file nào.";
                    }}
                }}
            }});
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


@app.get("/healthz")
def health():
    # Trả về thông tin tóm tắt
    account_details = {}
    for acc_id, data in GLOBAL_STATE["accounts"].items():
        account_details[acc_id] = {
            "name": data.get("account_name"),
            "notify_url": data.get("notify_api", {}).get("url"),
            "chat_url": data.get("chat_api", {}).get("url"),
            "last_nums": data.get("state_last_notify_nums"),
            "daily_date": data.get("state_daily_counter_date"),
            "daily_counts": data.get("state_daily_order_count"),
            "seen_chats": len(data.get("state_seen_chat_dates", set()))
        }
        
    return {
        "ok": True, 
        "poller": not DISABLE_POLLER,
        "global_chat_id_set": bool(GLOBAL_STATE["global_chat_id"]),
        "account_count": len(GLOBAL_STATE["accounts"]),
        "accounts": account_details
    }

# [THAY ĐỔI v7.0] Cần account_id
@app.get("/debug/notify-now")
def debug_notify(account_id: str):
    # [THAY ĐỔI v7.0] Xóa secret
    account_data = GLOBAL_STATE["accounts"].get(account_id)
    if not account_data:
        raise HTTPException(status_code=404, detail="Account ID not found.")
        
    global_chat_id = GLOBAL_STATE["global_chat_id"]
    if not global_chat_id:
        raise HTTPException(status_code=400, detail="Global Chat ID is not set.")

    before = str(account_data["state_last_notify_nums"])
    poll_once(account_id, account_data, global_chat_id, is_baseline_run=False)
    after = str(account_data["state_last_notify_nums"])
    
    return {
        "ok": True,
        "account_id": account_id,
        "account_name": account_data.get("account_name"),
        "last_before": before, 
        "last_after": after,
        "daily_stats": account_data["state_daily_order_count"]
    }

# [THAY ĐỔI v7.0] Cần account_id
@app.post("/debug/test-greeting")
async def debug_test_greeting(account_id: str):
    # [THAY ĐỔI v7.0] Xóa secret
    account_data = GLOBAL_STATE["accounts"].get(account_id)
    if not account_data:
        raise HTTPException(status_code=404, detail="Account ID not found.")
        
    global_chat_id = GLOBAL_STATE["global_chat_id"]
    if not global_chat_id:
        raise HTTPException(status_code=400, detail="Global Chat ID is not set.")

    try:
        # Đảm bảo ngày
        if not account_data["state_daily_counter_date"]:
             account_data["state_daily_counter_date"] = datetime.datetime.now(
                 datetime.timezone(datetime.timedelta(hours=7))
             ).strftime("%Y-%m-%d")
             
        send_good_morning_message(account_data, global_chat_id)
        return {"ok": True, "detail": "Đã gửi tin nhắn test."}
    except Exception as e:
        print(f"Test greeting error: {e}")
        raise HTTPException(status_code=500, detail=f"Lỗi khi gửi test: {e}")

# [THAY ĐỔI v7.0] Backup (trả về JSON cấu hình)
@app.get("/debug/get-backup")
async def debug_get_backup():
    # [THAY ĐỔI v7.0] Xóa secret
    global GLOBAL_STATE
    
    # Tạo bản backup "sạch" (chỉ chứa config, không chứa state runtime)
    backup_data = {
        "global_chat_id": GLOBAL_STATE["global_chat_id"],
        "accounts": {}
    }
    
    for acc_id, data in GLOBAL_STATE["accounts"].items():
        backup_data["accounts"][acc_id] = {
            "account_name": data.get("account_name"),
            "bot_token": data.get("bot_token"),
            "notify_curl": data.get("notify_curl"),
            "chat_curl": data.get("chat_curl"),
            "greeting_enabled": data.get("greeting_enabled"),
            "greeting_images": data.get("greeting_images")
        }
        
    return JSONResponse(content=backup_data)

# [THAY ĐỔI v7.0] Endpoint Restore (từ File Upload)
@app.post("/debug/restore-from-file")
async def debug_restore_from_file(file: UploadFile = File(...)):
    # [THAY ĐỔI v7.0] Xóa secret
    try:
        contents = await file.read()
        new_config_data = json.loads(contents)
        _apply_restore(new_config_data) # Gọi hàm logic chung
    except Exception as e:
        print(f"Restore from file failed: {e}")
        if not isinstance(e, HTTPException):
             raise HTTPException(status_code=400, detail=f"Invalid file or JSON data: {e}")
        else:
             raise e
    
    return {"ok": True, "detail": "Khôi phục từ file thành công!"}

# [THAY ĐỔI v7.0] Endpoint Restore (từ Text)
@app.post("/debug/restore-from-text")
async def debug_restore_from_text(req: Request):
    # [THAY ĐỔI v7.0] Xóa secret
    try:
        new_config_data = await req.json()
        _apply_restore(new_config_data) # Gọi hàm logic chung
    except Exception as e:
        print(f"Restore from text failed: {e}")
        if not isinstance(e, HTTPException):
             raise HTTPException(status_code=400, detail=f"Invalid JSON data: {e}")
        else:
             raise e
    
    return {"ok": True, "detail": "Khôi phục từ text thành công!"}


# [THAY ĐỔI v7.0] Endpoint set-config (lưu toàn bộ state)
@app.post("/debug/set-config")
async def debug_set_config(req: Request):
    # [THAY ĐỔI v7.0] Xóa secret
    
    try:
        new_config_data = await req.json()
        
        # Hàm _apply_restore sẽ parse cURL, tạo state, và gán vào GLOBAL_STATE
        _apply_restore(new_config_data) 
        
    except Exception as e:
        print(f"Set config failed: {e}")
        if isinstance(e, HTTPException):
            raise e
        else:
            raise HTTPException(status_code=400, detail=f"Invalid config data: {e}")
    
    return {
        "ok": True,
        "detail": "Đã lưu cấu hình đa tài khoản thành công."
    }

# =================== START ===================
def _maybe_start():
    if DISABLE_POLLER:
        print("Poller disabled by env.")
        return
    # Khởi động poller trong một thread riêng
    t = threading.Thread(target=poller_loop, daemon=True)
    t.start()

# Chạy thread poller
_maybe_start()

if __name__ == "__main__":
    import uvicorn
    # Chạy FastAPI server
    uvicorn.run("server:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
