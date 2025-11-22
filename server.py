import os, json, time, threading, html, hashlib, requests, re, shlex
from typing import Any, Dict, List, Optional
from collections import defaultdict
import datetime 
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse

# ----- Cấu hình môi trường (Env) -----
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# =================== CẤU HÌNH HỆ THỐNG ===================
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "12")) # Số giây check 1 lần
VERIFY_TLS    = bool(int(os.getenv("VERIFY_TLS", "1")))
DISABLE_POLLER = os.getenv("DISABLE_POLLER", "0") == "1"

# =================== TRẠNG THÁI TOÀN CỤC (GLOBAL STATE) ===================
# Nơi lưu trữ toàn bộ cấu hình và dữ liệu chạy
GLOBAL_STATE = {
    "global_chat_id": "", 
    "pinger": {
        "enabled": False,
        "url": "", # URL của chính server này để tự ping
        "interval": 300 
    },
    "accounts": {
        # Cấu trúc: "uuid": { config... }
    }
}

# Thời gian cooldown báo lỗi (tránh spam lỗi liên tục): 1 giờ
ERROR_COOLDOWN_SECONDS = 3600 

# =================== APP FASTAPI ===================
app = FastAPI(title="TapHoaMMO Bot v9.0 (Clean UI)")

# =================== HÀM HỖ TRỢ (HELPERS) ===================

def tg_send(text: str, bot_token: str, chat_id: str):
    """Gửi tin nhắn text đơn giản qua Telegram"""
    if not bot_token or not chat_id: return

    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    
    # Cắt tin nhắn nếu quá dài (Telegram giới hạn 4096 ký tự)
    MAX = 3900  
    chunks = [text[i:i+MAX] for i in range(0, len(text), MAX)] or [""]
    
    for part in chunks[:3]: # Gửi tối đa 3 phần để tránh spam
        payload = {
            "chat_id": chat_id,
            "text": part,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        try:
            requests.post(api_url, json=payload, timeout=20)
        except Exception as e:
            print(f"[Telegram Error] {e}")

def can_send_error(error_key: str, account_data: dict) -> bool:
    """Kiểm tra xem có được phép gửi báo lỗi hay không (dựa trên cooldown)"""
    global ERROR_COOLDOWN_SECONDS
    current_time = time.time()
    last_sent_time = account_data["state_last_error_times"][error_key]
    
    if (current_time - last_sent_time) > ERROR_COOLDOWN_SECONDS:
        account_data["state_last_error_times"][error_key] = current_time
        return True
    return False

# =================== XỬ LÝ DỮ LIỆU (PARSING) ===================

def _get_icon_for_label(label: str) -> str:
    low = label.lower()
    if "sản phẩm" in low: return "📦" # Đơn hàng
    if "khiếu nại" in low: return "⚠️" # Khiếu nại
    if "đánh giá" in low: return "⭐" # Đánh giá
    if "tin nhắn" in low: return "✉️" # Tin nhắn
    return "•"

def _labels_for_notify(parts_len: int) -> List[str]:
    """
    Mapping cột dữ liệu từ TapHoaMMO sang tên gọi.
    Cấu trúc chuỗi trả về: num|num|...|num
    Index bắt đầu từ 0.
    """
    labels = [f"Mục {i+1}" for i in range(parts_len)]
    
    # Mapping theo yêu cầu:
    # Cột 1 (index 0) = Đơn hàng
    # Cột 2 (index 1) = Đánh giá
    # Cột 8 (index 7) = Khiếu nại
    # Cột 9 (index 8) = Tin nhắn
    mapping = {
        0: "Đơn hàng sản phẩm",
        1: "Đánh giá",
        7: "Khiếu nại",
        8: "Tin nhắn"
    }
    
    for idx, name in mapping.items():
        if idx < parts_len:
            labels[idx] = name
            
    return labels

# Ngưỡng báo động (lớn hơn số này mới báo kèm icon trong danh sách)
COLUMN_BASELINES = defaultdict(int)
COLUMN_BASELINES["Khiếu nại"] = 0 

def parse_notify_text(text: str) -> Dict[str, Any]:
    """Phân tích chuỗi số 0|0|... trả về từ API getNotify"""
    s = (text or "").strip()
    parts = s.split("|") if s else []
    
    # Kiểm tra xem có phải toàn là số không
    if len(parts) > 0 and all(re.fullmatch(r"\d+", p or "") for p in parts):
        nums = [int(p) for p in parts]
        return {"raw": s, "numbers": nums}
    return {"raw": s}

def parse_curl_command(curl_text: str) -> Dict[str, Any]:
    """Chuyển đổi lệnh cURL copy từ trình duyệt thành cấu hình request Python"""
    try:
        args = shlex.split(curl_text)
    except:
        return {"url": "", "method": "GET", "headers": {}}

    method = "GET"; headers = {}; data = None; url = ""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "curl": 
            i += 1
            if i < len(args): url = args[i]
        elif a in ("-X", "--request"): 
            i += 1; method = args[i].upper() if i < len(args) else "GET"
        elif a in ("-H", "--header"):
            i += 1
            if i < len(args): 
                val = args[i]
                if ":" in val:
                    k, v = val.split(":", 1)
                    headers[k.strip()] = v.strip()
        elif a in ("-b", "--cookie"): 
            i += 1; headers['cookie'] = args[i] if i < len(args) else ""
        elif a in ("--data", "--data-raw", "--data-binary", "-d"): 
            i += 1; data = args[i] if i < len(args) else None
        i += 1

    if method == "GET" and data is not None: method = "POST"
    
    # Lọc header rác
    final_headers: Dict[str, str] = {}
    junk_prefixes = ('content-length', 'host', 'connection')
    for key, value in headers.items():
        if not any(key.lower().startswith(p) for p in junk_prefixes):
            final_headers[key] = value

    body_json = None
    raw_data = None 
    if data:
        try: body_json = json.loads(data)
        except: raw_data = data
    
    return {
        "url": url, "method": method, "headers": final_headers, 
        "body_json": body_json, "body_data": raw_data
    }

def _make_api_request(config: Dict[str, Any]) -> requests.Response:
    """Thực hiện request HTTP dựa trên cấu hình đã parse"""
    method = config.get("method", "GET")
    url = config.get("url", "")
    headers = config.get("headers", {})
    body_json = config.get("body_json")
    body_data = config.get("body_data")
    
    kwargs = {"headers": headers, "verify": VERIFY_TLS, "timeout": 25}
    if method == "POST":
        if body_json is not None: kwargs["json"] = body_json
        elif body_data is not None: kwargs["data"] = body_data.encode('utf-8')
    
    return requests.request(method, url, **kwargs)

# =================== LOGIC CHÍNH (POLLING) ===================

def fetch_chats(account_data: dict, global_chat_id: str, is_baseline_run: bool = False) -> List[Dict[str, str]]:
    """Lấy tin nhắn mới"""
    if not account_data["chat_api"].get("url"): return []
    
    try:
        r = _make_api_request(account_data["chat_api"])
        try: data = r.json()
        except: return []

        if not isinstance(data, list): return []

        new_messages = []
        current_chat_ids = set()
        SEEN_CHAT_IDS = account_data["state_seen_chat_dates"] # Dùng biến này lưu ID tin nhắn đã xem
        
        for chat in data:
            if not isinstance(chat, dict): continue
            
            user_id = chat.get("guest_user", "Khách")
            current_msg = chat.get("last_chat", "")
            
            # Tạo ID duy nhất cho tin nhắn: ưu tiên dùng 'date', nếu không có thì hash nội dung
            msg_id = chat.get("date") or hashlib.sha256(f"{user_id}:{current_msg}".encode()).hexdigest()
            
            current_chat_ids.add(msg_id)
            
            if msg_id not in SEEN_CHAT_IDS:
                SEEN_CHAT_IDS.add(msg_id)
                if not is_baseline_run:
                    new_messages.append({"user": user_id, "chat": current_msg})
        
        # Giữ bộ nhớ không bị phình to: chỉ nhớ những tin nhắn đang còn trong list API trả về
        SEEN_CHAT_IDS.intersection_update(current_chat_ids)
        
        return new_messages
    except Exception as e: 
        print(f"Chat Error: {e}")
        return []

def poll_once(account_id: str, account_data: dict, global_chat_id: str, is_baseline_run: bool = False):
    """Hàm kiểm tra 1 lần cho 1 tài khoản"""
    account_name = account_data.get('account_name', 'N/A')
    bot_token = account_data.get('bot_token', '')

    LAST_NOTIFY_NUMS = account_data["state_last_notify_nums"]
    DAILY_ORDER_COUNT = account_data["state_daily_order_count"]
    DAILY_COUNTER_DATE = account_data["state_daily_counter_date"]
    
    if not account_data["notify_api"].get("url"): return

    try:
        r = _make_api_request(account_data["notify_api"])
        text = (r.text or "").strip()
        if not text: return

        # Kiểm tra lỗi HTML (Cookie chết)
        low = text[:200].lower()
        if "<!doctype" in low or "<html" in low:
            if not is_baseline_run and can_send_error("NOTIFY_HTML", account_data):
                tg_send(f"⚠️ <b>[{html.escape(account_name)}] Cookie hết hạn</b> (API trả về HTML). Vui lòng cập nhật cURL mới.", bot_token, global_chat_id)
            return
        
        parsed = parse_notify_text(text)
        
        if "numbers" in parsed:
            now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7)))
            today_str = now.strftime("%Y-%m-%d")

            # [QUAN TRỌNG] Reset ngày mới âm thầm (KHÔNG GỬI LỜI CHÚC)
            if today_str != DAILY_COUNTER_DATE:
                DAILY_COUNTER_DATE = today_str
                DAILY_ORDER_COUNT.clear()
                # print(f"[{account_name}] New day reset: {today_str}")
            
            current_nums = parsed["numbers"]
            
            # Nếu số lượng cột thay đổi đột ngột, reset baseline
            if len(current_nums) != len(LAST_NOTIFY_NUMS):
                LAST_NOTIFY_NUMS = [0] * len(current_nums)

            labels = _labels_for_notify(len(current_nums)) 
            instant_alerts_map = {} # Các dòng thông báo hiển thị ngay
            has_change_increase = False
            has_new_chat = False 

            for i in range(len(current_nums)):
                current_val = current_nums[i]
                last_val = LAST_NOTIFY_NUMS[i]
                label = labels[i]
                
                # Nếu có tăng số lượng
                if current_val > last_val:
                    has_change_increase = True
                    
                    if "đơn hàng" in label.lower():
                        DAILY_ORDER_COUNT[label] += (current_val - last_val)
                    if "tin nhắn" in label.lower():
                        has_new_chat = True
                
                # Luôn hiển thị các mục quan trọng nếu số lượng > 0 (hoặc > baseline)
                if current_val > COLUMN_BASELINES[label]:
                    icon = _get_icon_for_label(label)
                    instant_alerts_map[label] = f"  {icon} <b>{label}:</b> {current_val}"

            # Nếu phát hiện tin nhắn mới tăng -> gọi API Chat để lấy nội dung
            new_chat_messages = []
            if has_new_chat:
                fetched = fetch_chats(account_data, global_chat_id, is_baseline_run=is_baseline_run) 
                for chat in fetched:
                    user = html.escape(chat.get('user','Unknown'))
                    content = html.escape(chat.get('chat','...'))
                    new_chat_messages.append(f"<b>✉️ Tin nhắn từ {user}:</b>\n  <i>{content}</i>")

            # Gửi thông báo nếu có thay đổi tăng (và không phải lần chạy đầu tiên)
            if has_change_increase and not is_baseline_run:
                # Sắp xếp thứ tự hiển thị ưu tiên
                ordered_keys = ["Đơn hàng sản phẩm", "Tin nhắn", "Khiếu nại", "Đánh giá"]
                alert_lines = []
                
                # Lấy theo thứ tự ưu tiên trước
                for label in ordered_keys:
                    if label in instant_alerts_map: 
                        alert_lines.append(instant_alerts_map.pop(label))
                # Lấy các mục còn lại
                for v in instant_alerts_map.values(): 
                    alert_lines.append(v)
                
                msg_lines = [f"<b>🔔 THÔNG BÁO - [{html.escape(account_name)}]</b>"]
                
                if new_chat_messages:
                    msg_lines.append("➖➖➖➖➖➖➖")
                    msg_lines.extend(new_chat_messages)
                
                if alert_lines:
                    msg_lines.append("➖➖➖➖➖➖➖")
                    msg_lines.extend(alert_lines)
                
                # Chỉ gửi nếu có nội dung
                if new_chat_messages or alert_lines:
                    tg_send("\n".join(msg_lines), bot_token, global_chat_id)

            # Cập nhật trạng thái mới
            account_data["state_last_notify_nums"] = current_nums
            account_data["state_daily_counter_date"] = DAILY_COUNTER_DATE
        
        else:
            # Lỗi định dạng trả về không phải số
            if text != str(LAST_NOTIFY_NUMS) and not is_baseline_run and can_send_error("NOTIFY_BAD_FMT", account_data):
                tg_send(f"🔔 <b>[{html.escape(account_name)}] Lỗi định dạng dữ liệu:</b>\n<code>{html.escape(text)}</code>", bot_token, global_chat_id)

    except Exception as e:
        print(f"Poll Exception [{account_name}]: {e}")

# =================== TIẾN TRÌNH NỀN (BACKGROUND THREADS) ===================

def pinger_loop():
    """Giữ cho server không bị ngủ đông bằng cách tự request chính mình"""
    print("▶ Pinger started...")
    while True:
        try:
            pinger_conf = GLOBAL_STATE.get("pinger", {})
            is_enabled = pinger_conf.get("enabled", False)
            url = pinger_conf.get("url", "")
            interval = int(pinger_conf.get("interval", 300))
            
            if is_enabled and url:
                try: 
                    requests.get(url, timeout=10)
                    # print(f"Pinged {url}")
                except: 
                    pass
            
            time.sleep(max(10, interval)) # Tối thiểu 10s
        except Exception:
            time.sleep(60)

def poller_loop():
    """Vòng lặp chính kiểm tra dữ liệu định kỳ"""
    print("▶ Poller started (v9.0 Clean)")
    time.sleep(3) # Chờ server khởi động xong
    
    # Chạy Baseline (lần đầu tiên) để lấy mốc số liệu hiện tại
    print("--- Running Baseline Fetch ---")
    for account_id, account_data in GLOBAL_STATE["accounts"].items():
        fetch_chats(account_data, GLOBAL_STATE["global_chat_id"], True)
        poll_once(account_id, account_data, GLOBAL_STATE["global_chat_id"], True)
        
        # Set ngày hiện tại nếu chưa có
        if not account_data["state_daily_counter_date"]:
            account_data["state_daily_counter_date"] = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7))).strftime("%Y-%m-%d")

    # Vòng lặp vô tận
    while True:
        try:
            time.sleep(POLL_INTERVAL)
            
            chat_id = GLOBAL_STATE["global_chat_id"]
            # Nếu chưa cấu hình chat_id thì bỏ qua
            if not chat_id: continue

            # Copy danh sách accounts để tránh lỗi khi đang loop mà có thay đổi
            current_accounts = list(GLOBAL_STATE["accounts"].items())
            
            for account_id, account_data in current_accounts:
                # Nếu thiếu state thì bỏ qua (tài khoản lỗi/chưa init)
                if "state_last_notify_nums" not in account_data: continue
                
                poll_once(account_id, account_data, chat_id, False)
                
        except Exception as e:
            print(f"Main Loop Error: {e}")
            time.sleep(60)

# =================== QUẢN LÝ CẤU HÌNH & RESTORE ===================

def _create_account_state() -> dict:
    """Tạo bộ nhớ tạm cho 1 tài khoản mới"""
    return {
        "notify_api": {}, "chat_api": {},
        "state_last_notify_nums": [],
        "state_daily_order_count": defaultdict(int),
        "state_daily_counter_date": "",
        "state_seen_chat_dates": set(),
        "state_last_error_times": defaultdict(float)
    }

def _apply_restore(new_config_data: Dict[str, Any]) -> bool:
    """Áp dụng cấu hình từ JSON Backup hoặc Form UI"""
    global GLOBAL_STATE
    
    if "global_chat_id" not in new_config_data or "accounts" not in new_config_data:
        raise HTTPException(status_code=400, detail="Dữ liệu cấu hình không hợp lệ.")

    new_chat_id = new_config_data.get("global_chat_id", "")
    new_pinger = new_config_data.get("pinger", {"enabled": False, "url": "", "interval": 300})
    
    new_accounts_dict = {}
    
    for account_id, config in new_config_data["accounts"].items():
        try:
            notify_curl = config.get("notify_curl", "")
            chat_curl = config.get("chat_curl", "")
            
            # Bắt buộc phải có cURL
            if not notify_curl or not chat_curl: continue

            # Tạo dữ liệu tài khoản
            acc_data = {
                "account_name": config.get("account_name", f"Acc {account_id}"),
                "bot_token": config.get("bot_token", ""),
                "notify_curl": notify_curl,
                "chat_curl": chat_curl,
                **_create_account_state() # Gắn thêm vùng nhớ runtime
            }
            
            # Parse cURL ngay lập tức
            acc_data["notify_api"] = parse_curl_command(notify_curl)
            acc_data["chat_api"] = parse_curl_command(chat_curl)
            
            new_accounts_dict[account_id] = acc_data
        except Exception as e:
            print(f"Skip account {account_id} due to error: {e}")
    
    # Cập nhật Global State
    GLOBAL_STATE["global_chat_id"] = new_chat_id
    GLOBAL_STATE["accounts"] = new_accounts_dict
    GLOBAL_STATE["pinger"] = new_pinger
    
    print(f"--- CONFIG RESTORED: {len(new_accounts_dict)} accounts loaded ---")
    return True

# =================== WEB UI & API ROUTES ===================

@app.get("/", response_class=HTMLResponse)
async def ui():
    return HTMLResponse(content=f"""
    <!DOCTYPE html>
    <html lang="vi">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>TapHoaMMO Bot Manager (v9.0)</title>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
        <style>
            :root {{
                --bg: #f3f4f6; --card: #ffffff; --text: #1f2937; 
                --border: #e5e7eb; --primary: #2563eb; --danger: #ef4444;
                --success: #10b981;
            }}
            body {{ font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 20px; }}
            .container {{ max-width: 900px; margin: 0 auto; }}
            
            /* Card Styles */
            .card {{ 
                background: var(--card); border-radius: 12px; padding: 24px; 
                box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1), 0 2px 4px -1px rgba(0,0,0,0.06); 
                margin-bottom: 24px; 
            }}
            h2 {{ margin-top: 0; font-size: 1.25rem; color: #111827; border-bottom: 1px solid var(--border); padding-bottom: 15px; margin-bottom: 20px; }}
            
            /* Form Elements */
            label {{ display: block; font-size: 0.875rem; font-weight: 600; margin-bottom: 6px; color: #374151; }}
            input, textarea, select {{
                width: 100%; padding: 10px 12px; border: 1px solid var(--border); border-radius: 8px;
                font-size: 14px; box-sizing: border-box; margin-bottom: 16px; transition: all 0.2s;
                background: #f9fafb;
            }}
            input:focus, textarea:focus {{ border-color: var(--primary); outline: none; background: #fff; box-shadow: 0 0 0 3px rgba(37,99,235,0.1); }}
            textarea {{ font-family: monospace; font-size: 12px; color: #4b5563; }}

            /* Grid Layout */
            .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
            @media (max-width: 600px) {{ .grid {{ grid-template-columns: 1fr; }} }}
            
            /* Account Item */
            .account-item {{ 
                background: #fff; border: 1px solid var(--border); border-radius: 8px; 
                padding: 20px; margin-bottom: 20px; position: relative;
                transition: transform 0.2s;
            }}
            .account-item:hover {{ border-color: #d1d5db; }}
            .remove-btn {{
                position: absolute; top: 15px; right: 15px;
                background: #fee2e2; color: var(--danger); border: none; 
                padding: 6px 12px; border-radius: 6px; cursor: pointer; font-weight: 600; font-size: 12px;
            }}
            .remove-btn:hover {{ background: #fecaca; }}

            /* Buttons */
            .btn {{ 
                display: inline-block; padding: 12px 24px; border-radius: 8px; border: none; 
                font-weight: 600; cursor: pointer; width: 100%; text-align: center; font-size: 14px;
                transition: background 0.2s;
            }}
            .btn-primary {{ background: var(--primary); color: white; }}
            .btn-primary:hover {{ background: #1d4ed8; }}
            .btn-secondary {{ background: #e5e7eb; color: #374151; }}
            .btn-secondary:hover {{ background: #d1d5db; }}
            
            /* Save Bar */
            .save-bar {{
                position: sticky; bottom: 20px; z-index: 100;
                background: rgba(255,255,255,0.9); backdrop-filter: blur(10px);
                padding: 15px; border-radius: 12px; border: 1px solid var(--border);
                box-shadow: 0 10px 15px -3px rgba(0,0,0,0.1);
            }}

            .badge {{
                display: inline-block; background: #dbeafe; color: #1e40af; 
                padding: 2px 8px; border-radius: 4px; font-size: 12px; margin-bottom: 10px;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <form id="configForm">
                <div class="card">
                    <h2>⚙️ Cấu hình Hệ thống</h2>
                    <div class="grid">
                        <div>
                            <label>ID Telegram (Kênh/Nhóm nhận tin):</label>
                            <input type="text" id="global_chat_id" placeholder="Ví dụ: -100123456789" required>
                        </div>
                        <div>
                            <label>Pinger (Chống ngủ đông):</label>
                            <div style="display: flex; gap: 10px;">
                                <select id="pinger_enabled" style="width: 100px;">
                                    <option value="0">Tắt</option>
                                    <option value="1">Bật</option>
                                </select>
                                <input type="text" id="pinger_url" placeholder="URL của trang web này (VD: https://abc.onrender.com)" style="margin-bottom: 0;">
                            </div>
                            <div style="font-size: 12px; color: #6b7280; margin-top: 6px;">Tự động truy cập trang web mỗi 5 phút để giữ server online.</div>
                        </div>
                    </div>
                </div>

                <div class="card">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; border-bottom: 1px solid var(--border); padding-bottom: 15px;">
                        <h2 style="margin: 0; border: none; padding: 0;">🛒 Danh sách Shop</h2>
                        <button type="button" class="btn btn-secondary" style="width: auto; padding: 8px 16px;" onclick="addAccount()">+ Thêm Shop</button>
                    </div>
                    
                    <div id="accountList"></div>
                    
                    <div id="emptyState" style="text-align: center; padding: 40px; color: #9ca3af; display: none;">
                        Chưa có tài khoản nào. Bấm nút "Thêm Shop" để bắt đầu.
                    </div>
                </div>

                <div class="save-bar">
                    <button type="submit" class="btn btn-primary">💾 Lưu Cấu Hình & Áp Dụng Ngay</button>
                </div>
            </form>

            <div class="card" style="margin-top: 40px;">
                <h2>📦 Sao lưu & Khôi phục</h2>
                <p style="font-size: 13px; color: #6b7280; margin-bottom: 10px;">Copy nội dung bên dưới để lưu trữ hoặc dán dữ liệu cũ vào để khôi phục.</p>
                <textarea id="backupData" rows="4" placeholder="Dữ liệu JSON..."></textarea>
                <div class="grid">
                    <button type="button" class="btn btn-secondary" onclick="getBackup()">⬇️ Lấy dữ liệu Backup hiện tại</button>
                    <button type="button" class="btn btn-secondary" onclick="restoreBackup()">⬆️ Khôi phục từ Text ở trên</button>
                </div>
            </div>
        </div>

        <script>
            // Hàm tạo giao diện cho 1 tài khoản
            function renderAccount(id, data = {{}}) {{
                const div = document.createElement('div');
                div.className = 'account-item';
                div.dataset.id = id;
                div.innerHTML = `
                    <span class="badge">ID: ${{id.substring(0,8)}}...</span>
                    <button type="button" class="remove-btn" onclick="removeAccount(this)">Xóa Shop</button>
                    
                    <div class="grid">
                        <div>
                            <label>Tên Shop (Gợi nhớ):</label>
                            <input type="text" class="acc-name" value="${{data.account_name || ''}}" placeholder="Ví dụ: Tạp Hóa A" required>
                        </div>
                        <div>
                            <label>Bot Token (Telegram):</label>
                            <input type="password" class="acc-token" value="${{data.bot_token || ''}}" placeholder="123456:ABC-DEF..." required>
                        </div>
                    </div>
                    
                    <div style="margin-top: 10px;">
                        <label>Lệnh cURL Thông báo (getNotify):</label>
                        <textarea class="acc-notify" rows="3" placeholder="Copy từ F12 -> Network -> getNotify -> Copy as cURL (bash)">${{data.notify_curl || ''}}</textarea>
                    </div>
                    
                    <div>
                        <label>Lệnh cURL Tin nhắn (getNewConversion):</label>
                        <textarea class="acc-chat" rows="3" placeholder="Copy từ F12 -> Network -> getNewConversion -> Copy as cURL (bash)">${{data.chat_curl || ''}}</textarea>
                    </div>
                `;
                document.getElementById('accountList').appendChild(div);
                checkEmpty();
            }}

            function addAccount() {{
                renderAccount(crypto.randomUUID());
            }}

            function removeAccount(btn) {{
                if(confirm('Bạn chắc chắn muốn xóa shop này?')) {{
                    btn.parentElement.remove();
                    checkEmpty();
                }}
            }}

            function checkEmpty() {{
                const list = document.getElementById('accountList');
                const emptyState = document.getElementById('emptyState');
                if(list.children.length === 0) {{
                    emptyState.style.display = 'block';
                }} else {{
                    emptyState.style.display = 'none';
                }}
            }}

            // Load Data từ Server khi mở web
            async function loadConfig() {{
                try {{
                    const res = await fetch('/debug/get-backup');
                    const data = await res.json();
                    
                    document.getElementById('global_chat_id').value = data.global_chat_id || '';
                    if (data.pinger) {{
                        document.getElementById('pinger_enabled').value = data.pinger.enabled ? "1" : "0";
                        document.getElementById('pinger_url').value = data.pinger.url || "";
                    }}
                    
                    document.getElementById('accountList').innerHTML = '';
                    if (data.accounts && Object.keys(data.accounts).length > 0) {{
                        Object.entries(data.accounts).forEach(([id, acc]) => renderAccount(id, acc));
                    }} else {{
                        checkEmpty();
                    }}
                }} catch (e) {{ 
                    console.error(e);
                    checkEmpty();
                }}
            }}

            // Save Data lên Server
            document.getElementById('configForm').onsubmit = async (e) => {{
                e.preventDefault();
                const accounts = {{}};
                
                // Thu thập dữ liệu từ các card
                document.querySelectorAll('.account-item').forEach(el => {{
                    const notify = el.querySelector('.acc-notify').value.trim();
                    const chat = el.querySelector('.acc-chat').value.trim();
                    
                    if(notify && chat) {{
                        accounts[el.dataset.id] = {{
                            account_name: el.querySelector('.acc-name').value,
                            bot_token: el.querySelector('.acc-token').value,
                            notify_curl: notify,
                            chat_curl: chat
                        }};
                    }}
                }});

                const payload = {{
                    global_chat_id: document.getElementById('global_chat_id').value,
                    pinger: {{
                        enabled: document.getElementById('pinger_enabled').value === "1",
                        url: document.getElementById('pinger_url').value,
                        interval: 300
                    }},
                    accounts: accounts
                }};

                try {{
                    const btn = e.target.querySelector('button[type="submit"]');
                    const originalText = btn.innerText;
                    btn.innerText = "⏳ Đang lưu...";
                    btn.disabled = true;

                    const res = await fetch('/debug/set-config', {{
                        method: 'POST', headers: {{'Content-Type': 'application/json'}},
                        body: JSON.stringify(payload)
                    }});
                    
                    if(res.ok) {{
                        alert('✅ Đã lưu cấu hình thành công!');
                    }} else {{
                        const err = await res.json();
                        alert('❌ Lỗi: ' + (err.detail || 'Unknown error'));
                    }}
                    
                    btn.innerText = originalText;
                    btn.disabled = false;

                }} catch (e) {{ alert('❌ Lỗi kết nối: ' + e); }}
            }};

            // Chức năng Backup
            async function getBackup() {{
                const res = await fetch('/debug/get-backup');
                const data = await res.json();
                document.getElementById('backupData').value = JSON.stringify(data, null, 2);
            }}

            // Chức năng Restore
            async function restoreBackup() {{
                try {{
                    const raw = document.getElementById('backupData').value;
                    if(!raw) return alert('Vui lòng dán dữ liệu vào ô trống trước.');
                    
                    const data = JSON.parse(raw);
                    await fetch('/debug/restore-from-text', {{
                        method: 'POST', headers: {{'Content-Type': 'application/json'}},
                        body: JSON.stringify(data)
                    }});
                    alert('✅ Khôi phục thành công! Trang sẽ tự tải lại.');
                    window.location.reload();
                }} catch (e) {{ alert('❌ Dữ liệu JSON không hợp lệ hoặc lỗi kết nối.'); }}
            }}

            // Khởi chạy
            loadConfig();
        </script>
    </body>
    </html>
    """)

@app.get("/debug/get-backup")
def get_backup():
    """API lấy dữ liệu backup hiện tại (ẩn thông tin nhạy cảm nếu cần)"""
    data = {
        "global_chat_id": GLOBAL_STATE["global_chat_id"],
        "pinger": GLOBAL_STATE.get("pinger", {"enabled": False, "url": "", "interval": 300}),
        "accounts": {}
    }
    for k, v in GLOBAL_STATE["accounts"].items():
        data["accounts"][k] = {
            "account_name": v.get("account_name"),
            "bot_token": v.get("bot_token"),
            "notify_curl": v.get("notify_curl"),
            "chat_curl": v.get("chat_curl")
        }
    return data

@app.post("/debug/set-config")
@app.post("/debug/restore-from-text")
async def set_config(req: Request):
    """API nhận cấu hình mới và áp dụng ngay lập tức"""
    try:
        js = await req.json()
        _apply_restore(js)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(400, detail=str(e))

# =================== KHỞI ĐỘNG SERVER ===================
if not DISABLE_POLLER:
    # Chạy 2 luồng riêng biệt: 1 cho Poller (nghiệp vụ), 1 cho Pinger (duy trì)
    threading.Thread(target=poller_loop, daemon=True).start()
    threading.Thread(target=pinger_loop, daemon=True).start()

if __name__ == "__main__":
    import uvicorn
    # Chạy server trên port 8080
    uvicorn.run("server:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
