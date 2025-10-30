import os, json, time, threading, html, hashlib, requests, re, shlex, random
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
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
WEBHOOK_SECRET     = os.getenv("WEBHOOK_SECRET", "change-me-please")

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "12"))
VERIFY_TLS    = bool(int(os.getenv("VERIFY_TLS", "1")))
DISABLE_POLLER = os.getenv("DISABLE_POLLER", "0") == "1"

# [CẬP NHẬT v5.3] Cấu hình runtime được gom vào 1 chỗ
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

# Biến toàn cục chứa TOÀN BỘ cấu hình (sẽ được backup/restore)
GLOBAL_CONFIG = {
    "notify_curl": "",
    "chat_curl": "",
    "notify_api": {"url": "", "method": "GET", "headers": {}, "body_json": None, "body_data": None},
    "chat_api": {"url": "", "method": "GET", "headers": {}, "body_json": None, "body_data": None},
    "greeting_enabled": True,
    "greeting_images": list(DEFAULT_IMAGE_LINKS)
}

# =================== APP ===================
app = FastAPI(title="TapHoaMMO → Telegram (Dual-API Poller)")

# Biến trạng thái (sẽ được reset khi restore)
LAST_NOTIFY_NUMS: List[int] = []     
DAILY_ORDER_COUNT = defaultdict(int) 
DAILY_COUNTER_DATE = ""              
SEEN_CHAT_DATES: set[str] = set()

# =================== Telegram ===================
def tg_send(text: str, photo_url: Optional[str] = None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Missing TELEGRAM_* env")
        return

    api_url = ""
    payload = {}
    
    if photo_url:
        cache_buster = f"_t={int(time.time())}"
        if "?" in photo_url:
            final_photo_url = f"{photo_url}&{cache_buster}"
        else:
            final_photo_url = f"{photo_url}?{cache_buster}"
            
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        caption = text
        if len(caption) > 1024:
            caption = text[:1021] + "..."
        
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "photo": final_photo_url,
            "caption": caption,
            "parse_mode": "HTML"
        }
        
        try:
            r = requests.post(api_url, json=payload, timeout=30)
            if r.status_code >= 400:
                print(f"Telegram photo error: {r.status_code} {r.text}")
                tg_send(text, photo_url=None)
            return
        except Exception as e:
            print(f"Error sending photo: {e}")
            tg_send(text, photo_url=None)
            return

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

# Hàm gửi lời chúc 0h
def send_good_morning_message(old_date: str, counts: defaultdict):
    global GLOBAL_CONFIG
    print(f"Sending Good Morning message for end of day {old_date}...")

    try:
        date_obj = datetime.datetime.strptime(old_date, "%Y-%m-%d")
        formatted_date = date_obj.strftime("%d-%m-%Y")
    except ValueError:
        formatted_date = old_date

    product_total = counts.get("Đơn hàng sản phẩm", 0)
    service_total = counts.get("Đơn hàng dịch vụ", 0)
    total_orders = product_total + service_total

    msg_template = random.choice(DEFAULT_GREETING_MESSAGES)
    msg = msg_template.format(date=formatted_date, orders=total_orders)

    photo = None
    links_to_use = GLOBAL_CONFIG["greeting_images"] if GLOBAL_CONFIG["greeting_images"] else DEFAULT_IMAGE_LINKS
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


# Hàm gọi API Tin nhắn (logic v5.2 - bỏ lọc `newMes`)
def fetch_chats(is_baseline_run: bool = False) -> List[Dict[str, str]]:
    global GLOBAL_CONFIG, SEEN_CHAT_DATES
    
    if not GLOBAL_CONFIG["chat_api"].get("url"):
        if not is_baseline_run: print("[WARN] CHAT_API_URL is not set. Skipping chat fetch.")
        return []
    
    try:
        r = _make_api_request(GLOBAL_CONFIG["chat_api"])

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
            print(f"Fetched {len(new_messages)} new message(s) (regardless of read status).")
        return new_messages

    except requests.exceptions.RequestException as e:
        if not is_baseline_run:
             print(f"fetch_chats network error: {e}")
             tg_send(f"⚠️ <b>Lỗi Mạng API Chat:</b> Không thể kết nối hoặc phản hồi.\n<code>{html.escape(str(e))}</code>")
        return []
    except Exception as e:
        if not is_baseline_run:
            print(f"fetch_chats unexpected error: {e}")
            tg_send(f"⚠️ <b>Lỗi không mong muốn API Chat:</b>\n<code>{html.escape(str(e))}</code>")
        return []

# [CẬP NHẬT v6.1] Hàm Poller (Fix lỗi 'labels' not defined)
def poll_once(is_baseline_run: bool = False):
    global LAST_NOTIFY_NUMS, DAILY_ORDER_COUNT, DAILY_COUNTER_DATE, GLOBAL_CONFIG

    if not GLOBAL_CONFIG["notify_api"].get("url"):
        if not is_baseline_run: print("No NOTIFY_API_URL set")
        return

    try:
        r = _make_api_request(GLOBAL_CONFIG["notify_api"])
        text = (r.text or "").strip()
        if not text:
            if not is_baseline_run: print("getNotify: empty response")
            return

        low = text[:200].lower()
        if low.startswith("<!doctype") or "<html" in low:
            if text != str(LAST_NOTIFY_NUMS) and not is_baseline_run:
                tg_send("⚠️ <b>getNotify trả về HTML</b> (Cookie/Header hết hạn?).")
            if not is_baseline_run: print("HTML detected, probably headers/cookie expired.")
            return
        
        parsed = parse_notify_text(text)
        
        if "numbers" in parsed:
            now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7)))
            today_str = now.strftime("%Y-%m-%d")

            if today_str != DAILY_COUNTER_DATE:
                if DAILY_COUNTER_DATE and GLOBAL_CONFIG["greeting_enabled"]:
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
                fetched_messages = fetch_chats(is_baseline_run=is_baseline_run) 
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
                
                if new_chat_messages or instant_alert_lines:
                    msg = "\n".join(msg_lines)
                    tg_send(msg)
                    print("getNotify changes (INCREASE) -> Professional Telegram sent.")
                else:
                    print("getNotify changes (INCREASE) -> No new unread chats or alerts to show.")

            elif not is_baseline_run:
                print("getNotify unchanged or DECREASED -> Skipping.")

            LAST_NOTIFY_NUMS = current_nums
        
        else:
            if text != str(LAST_NOTIFY_NUMS) and not is_baseline_run:
                msg = f"🔔 <b>TapHoaMMO getNotify (lỗi)</b>\n<code>{html.escape(text)}</code>"
                tg_send(msg)
                print("getNotify (non-numeric) changed -> Telegram sent.")

    except requests.exceptions.RequestException as e:
        if not is_baseline_run:
            print(f"poll_once network error: {e}")
            tg_send(f"⚠️ <b>Lỗi Mạng API Notify:</b> Không thể kết nối hoặc phản hồi.\n<code>{html.escape(str(e))}</code>")
    except Exception as e:
        if not is_baseline_run:
            print(f"poll_once unexpected error: {e}")
            tg_send(f"⚠️ <b>Lỗi không mong muốn API Notify:</b>\n<code>{html.escape(str(e))}</code>")

# Vòng lặp Poller
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
        
    print("Running initial chat fetch to set baseline (SEEN_CHAT_DATES)...")
    fetch_chats(is_baseline_run=True)
    
    print("Running initial notify poll to set baseline (LAST_NOTIFY_NUMS)...")
    poll_once(is_baseline_run=True)
    
    global DAILY_COUNTER_DATE
    if not DAILY_COUNTER_DATE:
        DAILY_COUNTER_DATE = datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=7))
        ).strftime("%Y-%m-%d")
        print(f"Baseline date set to: {DAILY_COUNTER_DATE}")
    
    print("--- Baseline complete. Starting main loop. ---")
    while True:
        time.sleep(POLL_INTERVAL)
        poll_once(is_baseline_run=False)

# =================== [CẬP NHẬT] LÕI BACKUP/RESTORE ===================

# [THÊM MỚI v6.0] Hàm logic khôi phục
def _apply_restore(new_config_data: Dict[str, Any]) -> bool:
    global GLOBAL_CONFIG, LAST_NOTIFY_NUMS, DAILY_ORDER_COUNT
    global DAILY_COUNTER_DATE, SEEN_CHAT_DATES
    
    # --- Kiểm tra dữ liệu backup ---
    if "notify_curl" not in new_config_data or "chat_curl" not in new_config_data:
        tg_send(f"❌ <b>KHÔI PHỤC THẤT BẠI</b>\nDữ liệu JSON không đúng cấu trúc (thiếu cURL).")
        raise HTTPException(status_code=400, detail="Invalid config structure.")
    
    try:
        parsed_notify = parse_curl_command(new_config_data["notify_curl"])
        parsed_chat = parse_curl_command(new_config_data["chat_curl"])
    except Exception as e:
        tg_send(f"❌ <b>KHÔI PHỤC THẤT BẠI</b>\nLỗi khi phân tích cURL từ file backup.\n<code>{e}</code>")
        raise HTTPException(status_code=400, detail=f"Failed to parse cURL from backup: {e}")

    # --- Áp dụng cấu hình mới ---
    GLOBAL_CONFIG["notify_curl"] = new_config_data["notify_curl"]
    GLOBAL_CONFIG["chat_curl"] = new_config_data["chat_curl"]
    GLOBAL_CONFIG["notify_api"] = parsed_notify
    GLOBAL_CONFIG["chat_api"] = parsed_chat
    GLOBAL_CONFIG["greeting_enabled"] = new_config_data.get("greeting_enabled", True)
    GLOBAL_CONFIG["greeting_images"] = new_config_data.get("greeting_images", list(DEFAULT_IMAGE_LINKS))
    
    # Reset lại toàn bộ trạng thái
    LAST_NOTIFY_NUMS = []
    DAILY_ORDER_COUNT.clear()
    DAILY_COUNTER_DATE = "" 
    SEEN_CHAT_DATES.clear()
    
    print("--- CONFIG RESTORED BY UI ---")
    print(f"Notify API set to: {GLOBAL_CONFIG['notify_api'].get('url')}")
    print(f"Chat API set to: {GLOBAL_CONFIG['chat_api'].get('url')}")
    print(f"Greeting Enabled: {GLOBAL_CONFIG['greeting_enabled']}")
    
    tg_send("✅ <b>KHÔI PHỤC THÀNH CÔNG</b>\nToàn bộ cấu hình đã được khôi phục. Bot sẽ chạy lại từ đầu.")
    return True

# =================== API endpoints ===================

# [CẬP NHẬT] Giao diện web v6.0 (Giao diện VŨ TRỤ)
@app.get("/", response_class=HTMLResponse)
async def get_curl_ui():
    global GLOBAL_CONFIG
    
    links_to_show = GLOBAL_CONFIG["greeting_images"] if GLOBAL_CONFIG["greeting_images"] else DEFAULT_IMAGE_LINKS
    image_links_text = "\n".join(links_to_show)
    
    toggle_on_selected = "selected" if GLOBAL_CONFIG["greeting_enabled"] else ""
    toggle_off_selected = "" if GLOBAL_CONFIG["greeting_enabled"] else "selected"

    notify_curl_text = GLOBAL_CONFIG["notify_curl"]
    chat_curl_text = GLOBAL_CONFIG["chat_curl"]

    html_content = f"""
    <!DOCTYPE html>
    <html lang="vi">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Bảng điều khiển Poller - TapHoaMMO</title>
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;700&display=swap');
            
            :root {{
                --bg-color: #0F0F1A;
                --card-bg: #1A1A2B;
                --text-color: #E0E0FF;
                --text-muted: #8F8FA8;
                --border-color: #3A3A5A;
                --primary-glow: #00AFFF;
                --secondary-glow: #6A00FF;
                --success-color: #00FFC2;
                --error-color: #FF4D80;
                --shadow: 0 0 15px rgba(0, 175, 255, 0.2);
            }}

            /* [THÊM MỚI v6.0] Hiệu ứng sao băng */
            @keyframes shooting-star {{
                0% {{ transform: translateX(100vw) translateY(-100vh); opacity: 1; }}
                100% {{ transform: translateX(-100vw) translateY(100vh); opacity: 0; }}
            }}
            .star {{
                position: fixed;
                top: 0;
                left: 0;
                width: 2px;
                height: 2px;
                background: linear-gradient(to bottom, rgba(255,255,255,0.8), rgba(255,255,255,0));
                border-radius: 50%;
                box-shadow: 0 0 10px 2px #FFF;
                opacity: 0;
                animation: shooting-star 10s linear infinite;
                z-index: -1;
            }}
            .star:nth-child(1) {{ animation-delay: 0s; left: 20%; top: -50%; animation-duration: 5s; }}
            .star:nth-child(2) {{ animation-delay: 1.5s; left: 50%; top: -30%; animation-duration: 7s; }}
            .star:nth-child(3) {{ animation-delay: 3s; left: 80%; top: -60%; animation-duration: 6s; }}
            .star:nth-child(4) {{ animation-delay: 5s; left: 10%; top: -40%; animation-duration: 8s; }}

            body {{
                font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                margin: 0; padding: 2.5rem; background: var(--bg-color);
                color: var(--text-color); line-height: 1.6; min-height: 100vh;
                box-sizing: border-box;
                overflow-x: hidden; /* Ẩn thanh cuộn ngang do sao băng */
            }}
            .container {{
                max-width: 800px; margin: 1rem auto; 
                position: relative; /* Để UI nổi lên trên sao băng */
                z-index: 1;
            }}
            .card {{
                background: rgba(26, 26, 43, 0.85); /* Hơi trong suốt */
                backdrop-filter: blur(10px); /* Hiệu ứng mờ */
                padding: 2.5rem 3rem; border-radius: 16px;
                border: 1px solid transparent;
                border-image: linear-gradient(135deg, var(--primary-glow) 0%, var(--secondary-glow) 100%) 1;
                box-shadow: 0 10px 30px rgba(0, 0, 0, 0.3), 0 0 25px rgba(106, 0, 255, 0.2);
                margin-bottom: 2.5rem;
            }}
            h1, h2 {{
                font-weight: 700;
                margin-top: 0; display: flex; align-items: center;
                letter-spacing: -0.5px;
            }}
            h1 {{ 
                font-size: 2.25rem; 
                background: linear-gradient(90deg, var(--primary-glow), var(--success-color));
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                text-shadow: 0 0 10px rgba(0, 175, 255, 0.3);
            }}
            h2 {{ 
                font-size: 1.75rem; 
                color: var(--text-color);
                border-bottom: 1px solid var(--border-color); 
                padding-bottom: 0.75rem;
            }}
            h1 span, h2 span {{ 
                font-size: 2.25rem; margin-right: 0.75rem; line-height: 1; 
                color: var(--primary-glow);
            }}
            
            p.description {{
                font-size: 1.1rem; color: var(--text-muted); margin-bottom: 2rem;
            }}
            label {{
                display: block; margin-top: 1.5rem; margin-bottom: 0.5rem;
                font-weight: 500; font-size: 0.9rem; color: var(--text-muted);
                text-transform: uppercase; letter-spacing: 0.5px;
            }}
            textarea, input[type="password"], select {{
                width: 100%; padding: 14px; border: 1px solid var(--border-color);
                border-radius: 8px; font-family: "SF Mono", "Fira Code", "Consolas", monospace;
                font-size: 14px; background-color: var(--bg-color); color: var(--text-color);
                box-sizing: border-box; transition: border-color 0.3s, box-shadow 0.3s;
            }}
            select {{
                font-family: 'Inter', sans-serif;
                appearance: none;
                background-image: url("data:image/svg+xml,%3csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3e%3cpath fill='none' stroke='%238F8FA8' stroke-linecap='round' stroke-linejoin='round' stroke-width='2' d='M2 5l6 6 6-6'/%3e%3c/svg%3e");
                background-repeat: no-repeat;
                background-position: right 0.75rem center;
                background-size: 16px 12px;
            }}
            textarea {{ height: 150px; resize: vertical; }}
            textarea#backup_data {{ height: 100px; }}
            textarea:focus, input[type="password"]:focus, select:focus {{
                outline: none; border-color: var(--primary-glow);
                box-shadow: 0 0 15px rgba(0, 175, 255, 0.3);
            }}
            
            input[type="file"] {{ display: none; }}
            .file-upload-btn {{
                display: block;
                padding: 14px;
                background: var(--secondary-color);
                color: white;
                border-radius: 8px;
                text-align: center;
                cursor: pointer;
                font-weight: 500;
                transition: background-color 0.3s;
                margin-top: 1rem;
            }}
            .file-upload-btn:hover {{ background: var(--secondary-hover); }}
            #file-name {{ color: var(--text-muted); font-style: italic; margin-top: 0.5rem; }}

            button {{
                background: linear-gradient(90deg, var(--primary-glow) 0%, var(--secondary-glow) 100%);
                color: white; padding: 16px 24px;
                border: none; border-radius: 8px; cursor: pointer;
                font-size: 1rem; font-weight: 700; letter-spacing: 0.5px;
                margin-top: 2rem; transition: all 0.3s;
                width: 100%;
                box-shadow: 0 4px 15px rgba(0, 175, 255, 0.3);
            }}
            button.secondary {{
                background: var(--secondary-color);
                box-shadow: 0 4px 15px rgba(0, 0, 0, 0.2);
            }}
            button:disabled {{ 
                background: var(--border-color); 
                cursor: not-allowed; opacity: 0.7; 
                box-shadow: none;
            }}
            button:not(:disabled):hover {{ 
                transform: translateY(-2px);
                box-shadow: 0 8px 20px rgba(0, 175, 255, 0.5);
            }}
            button.secondary:not(:disabled):hover {{ 
                background: var(--secondary-hover);
                box-shadow: 0 8px 20px rgba(0, 0, 0, 0.3);
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
            
            .footer-text {{
                text-align: center; margin-top: 2.5rem; font-size: 0.9rem; color: var(--text-muted); opacity: 0.8;
                display: flex; align-items: center; justify-content: center;
            }}
            .blue-check {{
                width: 18px; height: 18px; margin-left: 8px;
            }}
        </style>
    </head>
    <body>
        <div class="star"></div>
        <div class="star"></div>
        <div class="star"></div>
        <div class="star"></div>

        <div class="container">
            <div class="card">
                <h1><span>🌌</span>Bảng Điều Khiển Poller (v6.0)</h1>
                <p class="description">Quản lý API và Lời chúc 0h tại trung tâm điều khiển.</p>
                
                <form id="config-form">
                    <h2><span>🛰️</span> Cấu hình API (cURL)</h2>
                    <label for="curl_notify_text">1. cURL Thông Báo (getNotify):</label>
                    <textarea id="curl_notify_text" name="curl_notify" placeholder="curl '.../api/getNotify' ...">{notify_curl_text}</textarea>
                    
                    <label for="curl_chat_text">2. cURL Tin Nhắn (getNewConversion):</label>
                    <textarea id="curl_chat_text" name="curl_chat" placeholder="curl '.../api/getNewConversion' ...">{chat_curl_text}</textarea>

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
                <h2><span>📦</span> Backup & Restore</h2>
                <p class="description">Tạo hoặc khôi phục cấu hình của sếp (bao gồm cURL và link ảnh).</p>
                
                <label for="backup_secret_key">Secret Key (Dùng cho các nút dưới):</label>
                <input type="password" id="backup_secret_key" placeholder="Nhập WEBHOOK_SECRET của bạn">

                <label for="backup_data" style="margin-top: 1.5rem;">Dữ liệu Backup (Copy/Paste):</label>
                <textarea id="backup_data" placeholder="Ấn '1. Tạo Backup' để lấy dữ liệu. Hoặc dán dữ liệu restore vào đây..."></textarea>
                
                <div style="display: flex; gap: 1rem; margin-top: 2rem;">
                    <button type="button" id="backup-btn" class="secondary" style="width: 50%; margin: 0;">1. Tạo Backup (Hiển thị)</button>
                    <button type="button" id="restore-text-btn" style="width: 50%; margin: 0;">2. Khôi phục từ Text</button>
                </div>

                <label for="restore-file" class="file-upload-btn" style="width: 100%; margin: 1rem 0 0 0; background: var(--secondary-color);">
                    ... Hoặc 3. Khôi phục từ File ...
                </label>
                <input type="file" id="restore-file" accept=".json">
                <div id="file-name" style="text-align: center; margin-top: 1rem;">Chưa chọn file nào.</div>

                <div id="backup-status" class="status-message">
                    <strong></strong>
                    <span id="backup-status-body"></span>
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
            
            <footer class="footer-text">
                Bản quyền thuộc về Admin Văn Linh
                <svg class="blue-check" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                    <path fill-rule="evenodd" clip-rule="evenodd" d="M12 2C6.47715 2 2 6.47715 2 12C2 17.5228 6.47715 22 12 22C17.5228 22 22 17.5228 22 12C22 6.47715 17.5228 2 12 2ZM16.7071 9.29289C17.0976 9.68342 17.0976 10.3166 16.7071 10.7071L11.7071 15.7071C11.3166 16.0976 10.6834 16.0976 10.2929 15.7071L7.29289 12.7071C6.90237 12.3166 6.90237 11.6834 7.29289 11.2929C7.68342 10.9024 8.31658 10.9024 8.70711 11.2929L11 13.5858L15.2929 9.29289C15.6834 8.90237 16.3166 8.90237 16.7071 9.29289Z" fill="url(#paint0_linear_v6)"/>
                    <defs>
                        <linearGradient id="paint0_linear_v6" x1="2" y1="2" x2="22" y2="22" gradientUnits="userSpaceOnUse">
                            <stop stop-color="#00AFFF"/>
                            <stop offset="1" stop-color="#6A00FF"/>
                        </linearGradient>
                    </defs>
                </svg>
            </footer>
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
                            curl_notify_curl: curlNotifyText,
                            curl_chat_curl: curlChatText,
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
                const secret = document.getElementById("test_secret_key").value || document.getElementById("backup_secret_key").value || document.getElementById("secret_key").value;
                const statusEl = document.getElementById("test-status");
                const statusBody = document.getElementById("test-status-body");
                const button = document.getElementById("test-greeting-btn");

                if (!secret) {{
                    statusBody.textContent = "Vui lòng nhập Secret Key ở bất kỳ ô nào.";
                    statusEl.className = "status-message error show";
                    return;
                }}

                statusBody.textContent = "Đang gửi tin nhắn test...";
                statusEl.className = "status-message loading show";
                button.disabled = true;

                try {{
                    const response = await fetch(`/debug/test-greeting?secret=${{encodeURIComponent(secret)}}`, {{ method: "POST" }});
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

            // [CẬP NHẬT v6.0] Xử lý Backup (Hiển thị text)
            document.getElementById("backup-btn").addEventListener("click", async function(e) {{
                e.preventDefault();
                const secret = document.getElementById("backup_secret_key").value;
                const statusEl = document.getElementById("backup-status");
                const statusBody = document.getElementById("backup-status-body");
                const backupDataEl = document.getElementById("backup_data");
                
                if (!secret) {{
                    statusBody.textContent = "Vui lòng nhập Secret Key (Dùng để Backup/Restore).";
                    statusEl.className = "status-message error show";
                    return;
                }}
                statusBody.textContent = "Đang lấy dữ liệu backup...";
                statusEl.className = "status-message loading show";

                try {{
                    const response = await fetch(`/debug/get-backup?secret=${{encodeURIComponent(secret)}}`);
                    const result = await response.json();
                    if (response.ok) {{
                        backupDataEl.value = JSON.stringify(result, null, 2); // Format JSON cho đẹp
                        statusBody.textContent = "Đã lấy dữ liệu backup thành công. Hãy copy text bên trên.";
                        statusEl.className = "status-message success show";
                    }} else {{
                        statusBody.textContent = `Lỗi: ${{result.detail || 'Lỗi không xác định.'}}`;
                        statusEl.className = "status-message error show";
                    }}
                }} catch (err) {{
                    statusBody.textContent = `Lỗi kết nối: ${{err.message}}.`;
                    statusEl.className = "status-message error show";
                }}
            }});
            
            // [CẬP NHẬT v6.0] Hàm logic chung để Restore
            async function triggerRestore(data, secret) {{
                const statusEl = document.getElementById("backup-status");
                const statusBody = document.getElementById("backup-status-body");
                const fileInput = document.getElementById("restore-file");
                const fileNameEl = document.getElementById("file-name");

                statusBody.textContent = "Đang khôi phục...";
                statusEl.className = "status-message loading show";

                try {{
                    const response = await fetch(`/debug/restore-from-text?secret=${{encodeURIComponent(secret)}}`, {{
                        method: "POST",
                        headers: {{"Content-Type": "application/json"}},
                        body: data // Gửi text JSON thô
                    }});
                    const result = await response.json();
                    if (response.ok) {{
                        statusBody.textContent = "Khôi phục thành công! Cấu hình đã được áp dụng. Trang sẽ tự tải lại...";
                        statusEl.className = "status-message success show";
                        setTimeout(() => window.location.reload(), 2000);
                    }} else {{
                        statusBody.textContent = `Lỗi: ${{result.detail || 'Lỗi không xác định.'}}`;
                        statusEl.className = "status-message error show";
                    }}
                }} catch (err) {{
                    statusBody.textContent = `Lỗi kết nối: ${{err.message}}.`;
                    statusEl.className = "status-message error show";
                }} finally {{
                    fileInput.value = "";
                    fileNameEl.textContent = "Chưa chọn file nào.";
                }}
            }}

            // [THÊM MỚI v6.0] Xử lý Restore từ Text
            document.getElementById("restore-text-btn").addEventListener("click", async function(e) {{
                e.preventDefault();
                const secret = document.getElementById("backup_secret_key").value;
                const backupData = document.getElementById("backup_data").value;
                const statusEl = document.getElementById("backup-status");
                const statusBody = document.getElementById("backup-status-body");

                if (!secret || !backupData) {{
                    statusBody.textContent = "Vui lòng nhập Secret Key và dán dữ liệu Backup vào ô.";
                    statusEl.className = "status-message error show";
                    return;
                }}
                
                try {{ JSON.parse(backupData); }} catch (jsonErr) {{
                    statusBody.textContent = "Lỗi: Dữ liệu dán vào không phải là JSON hợp lệ.";
                    statusEl.className = "status-message error show";
                    return;
                }}

                if (confirm("Bạn có chắc chắn muốn khôi phục? Dữ liệu cũ sẽ bị ghi đè.")) {{
                    triggerRestore(backupData, secret);
                }}
            }});

            // [CẬP NHẬT v6.0] Xử lý Restore từ File
            const fileInput = document.getElementById("restore-file");
            const fileNameEl = document.getElementById("file-name");

            fileInput.addEventListener("change", function(e) {{
                const file = e.target.files[0];
                if (file) {{
                    fileNameEl.textContent = `Đã chọn: ${{file.name}}`;
                    
                    const secret = document.getElementById("backup_secret_key").value;
                    const statusEl = document.getElementById("backup-status");
                    const statusBody = document.getElementById("backup-status-body");

                    if (!secret) {{
                        statusBody.textContent = "Vui lòng nhập Secret Key (Dùng để Backup/Restore) trước khi chọn file.";
                        statusEl.className = "status-message error show";
                        fileInput.value = "";
                        fileNameEl.textContent = "Chưa chọn file nào.";
                        return;
                    }}

                    if (confirm("Bạn có chắc chắn muốn khôi phục? Dữ liệu cũ sẽ bị ghi đè.")) {{
                        const reader = new FileReader();
                        reader.onload = function(evt) {{
                            triggerRestore(evt.target.result, secret);
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
    global GLOBAL_CONFIG
    return {
        "ok": True, "poller": not DISABLE_POLLER,
        "last_notify_nums": LAST_NOTIFY_NUMS,
        "daily_stats": {"date": DAILY_COUNTER_DATE, "counts": DAILY_ORDER_COUNT},
        "seen_chats": len(SEEN_CHAT_DATES),
        "greeting_enabled": GLOBAL_CONFIG["greeting_enabled"],
        "greeting_image_count": len(GLOBAL_CONFIG["greeting_images"]),
        "api_notify": {"url": GLOBAL_CONFIG["notify_api"].get("url"), "data": GLOBAL_CONFIG["notify_api"].get("body_data") is not None},
        "api_chat": {"url": GLOBAL_CONFIG["chat_api"].get("url"), "data": GLOBAL_CONFIG["chat_api"].get("body_data") is not None}
    }

@app.get("/debug/notify-now")
def debug_notify(secret: str):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    before = str(LAST_NOTIFY_NUMS) 
    poll_once(is_baseline_run=False) # Chạy test ở chế độ "không phải baseline"
    after = str(LAST_NOTIFY_NUMS)
    return {
        "ok": True, "last_before": before, "last_after": after,
        "daily_stats": DAILY_ORDER_COUNT
    }

# Endpoint Test lời chúc
@app.post("/debug/test-greeting")
async def debug_test_greeting(secret: str):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    
    try:
        date_to_show = DAILY_COUNTER_DATE or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7))).strftime("%Y-%m-%d")
        send_good_morning_message(date_to_show, DAILY_ORDER_COUNT)
        return {"ok": True, "detail": "Đã gửi tin nhắn test."}
    except Exception as e:
        print(f"Test greeting error: {e}")
        raise HTTPException(status_code=500, detail=f"Lỗi khi gửi test: {e}")

# Endpoint Backup (trả về JSON)
@app.get("/debug/get-backup")
async def debug_get_backup(secret: str):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    
    global GLOBAL_CONFIG
    backup_data = {
        "notify_curl": GLOBAL_CONFIG["notify_curl"],
        "chat_curl": GLOBAL_CONFIG["chat_curl"],
        "greeting_enabled": GLOBAL_CONFIG["greeting_enabled"],
        "greeting_images": GLOBAL_CONFIG["greeting_images"]
    }
    return JSONResponse(content=backup_data)

# [CẬP NHẬT v6.0] Endpoint Restore (từ File Upload)
@app.post("/debug/restore-from-file")
async def debug_restore_from_file(secret: str, file: UploadFile = File(...)):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    
    try:
        contents = await file.read()
        new_config_data = json.loads(contents)
        _apply_restore(new_config_data) # Gọi hàm logic chung
    except Exception as e:
        # Lỗi đã được xử lý/gửi bởi _apply_restore hoặc do đọc file
        print(f"Restore from file failed: {e}")
        if not isinstance(e, HTTPException):
             raise HTTPException(status_code=400, detail=f"Invalid file or JSON data: {e}")
        else:
             raise e
    
    return {"ok": True, "detail": "Khôi phục từ file thành công!"}

# [THÊM MỚI v6.0] Endpoint Restore (từ Text)
@app.post("/debug/restore-from-text")
async def debug_restore_from_text(req: Request, secret: str):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    
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


# [CẬP NHẬT] Endpoint set-config (lưu cả cURL thô)
@app.post("/debug/set-config")
async def debug_set_config(req: Request, secret: str):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    
    body = await req.json()
    curl_notify_txt = str(body.get("curl_notify_curl") or "")
    curl_chat_txt = str(body.get("curl_chat_curl") or "")
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

    # --- 2. Áp dụng Cấu hình ---
    global GLOBAL_CONFIG, LAST_NOTIFY_NUMS, DAILY_ORDER_COUNT
    global DAILY_COUNTER_DATE, SEEN_CHAT_DATES
    
    GLOBAL_CONFIG["notify_curl"] = curl_notify_txt
    GLOBAL_CONFIG["chat_curl"] = curl_chat_txt
    GLOBAL_CONFIG["notify_api"] = parsed_notify
    GLOBAL_CONFIG["chat_api"] = parsed_chat

    GLOBAL_CONFIG["greeting_enabled"] = bool(int(greeting_enabled_raw))
    GLOBAL_CONFIG["greeting_images"] = [line.strip() for line in image_links_raw.splitlines() if line.strip().startswith('http')]
    
    # Reset lại toàn bộ
    LAST_NOTIFY_NUMS = []
    DAILY_ORDER_COUNT.clear()
    DAILY_COUNTER_DATE = "" 
    SEEN_CHAT_DATES.clear()
    
    print("--- CONFIG UPDATED BY UI ---")
    print(f"Notify API set to: {GLOBAL_CONFIG['notify_api'].get('url')}")
    print(f"Chat API set to: {GLOBAL_CONFIG['chat_api'].get('url')}")
    print(f"Greeting Enabled: {GLOBAL_CONFIG['greeting_enabled']}")
    print(f"Greeting Images: {len(GLOBAL_CONFIG['greeting_images'])} links")
    
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
