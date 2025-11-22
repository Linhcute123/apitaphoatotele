import os, json, time, threading, html, hashlib, requests, re, shlex, random
from typing import Any, Dict, List, Optional
from collections import defaultdict
import datetime 
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse

# ----- .env (local) -----
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# =================== ENV ===================
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "12"))
VERIFY_TLS    = bool(int(os.getenv("VERIFY_TLS", "1")))
DISABLE_POLLER = os.getenv("DISABLE_POLLER", "0") == "1"

# =================== GLOBAL STATE ===================
# [THAY ĐỔI v9.0] Xóa cấu hình greeting khỏi state
GLOBAL_STATE = {
    "global_chat_id": "", 
    "pinger": {
        "enabled": False,
        "url": "https://google.com",
        "interval": 300 
    },
    "accounts": {}
}

# Thời gian cooldown lỗi (1 giờ)
ERROR_COOLDOWN_SECONDS = 3600 

# =================== APP ===================
app = FastAPI(title="TapHoaMMO → Telegram (v9.0 Clean)")

# =================== Telegram Helper ===================
def tg_send(text: str, bot_token: str, chat_id: str):
    if not bot_token or not chat_id: return

    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    # Cắt tin nhắn nếu quá dài
    MAX = 3900  
    chunks = [text[i:i+MAX] for i in range(0, len(text), MAX)] or [""]
    
    for part in chunks[:3]:
        payload = {
            "chat_id": chat_id,
            "text": part,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        try:
            requests.post(api_url, json=payload, timeout=20)
        except: pass

def can_send_error(error_key: str, account_data: dict) -> bool:
    global ERROR_COOLDOWN_SECONDS
    current_time = time.time()
    last_sent_time = account_data["state_last_error_times"][error_key]
    
    if (current_time - last_sent_time) > ERROR_COOLDOWN_SECONDS:
        account_data["state_last_error_times"][error_key] = current_time
        return True
    return False

# =================== Logic Phân Tích Dữ Liệu ===================
def _get_icon_for_label(label: str) -> str:
    low = label.lower()
    if "sản phẩm" in low: return "📦" # Đơn hàng
    if "khiếu nại" in low: return "⚠️" # Khiếu nại
    if "đánh giá" in low: return "⭐" # Đánh giá
    if "tin nhắn" in low: return "✉️" # Tin nhắn
    return "•"

# Mapping cột: 1=Đơn hàng, 2=Đánh giá, 8=Khiếu nại, 9=Tin nhắn
def _labels_for_notify(parts_len: int) -> List[str]:
    labels = [f"Mục {i+1}" for i in range(parts_len)]
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

COLUMN_BASELINES = defaultdict(int)
COLUMN_BASELINES["Khiếu nại"] = 0 

def parse_notify_text(text: str) -> Dict[str, Any]:
    s = (text or "").strip()
    parts = s.split("|") if s else []
    
    if len(parts) > 0 and all(re.fullmatch(r"\d+", p or "") for p in parts):
        nums = [int(p) for p in parts]
        labels = _labels_for_notify(len(nums))
        return {"raw": s, "numbers": nums}
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
    junk_prefixes = ('content-length',)
    for key, value in headers.items():
        low_key = key.lower()
        if not any(low_key.startswith(p) for p in junk_prefixes):
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

# =================== Core Logic ===================

def fetch_chats(account_data: dict, global_chat_id: str, is_baseline_run: bool = False) -> List[Dict[str, str]]:
    account_name = account_data.get('account_name', 'N/A')
    
    if not account_data["chat_api"].get("url"): return []
    
    try:
        r = _make_api_request(account_data["chat_api"])
        try: data = r.json()
        except: return []

        if not isinstance(data, list): return []

        new_messages = []
        current_chat_dates = set()
        SEEN_CHAT_DATES = account_data["state_seen_chat_dates"]
        
        for chat in data:
            if not isinstance(chat, dict): continue
            user_id = chat.get("guest_user", "N/A")
            current_msg = chat.get("last_chat", "")
            chat_id = chat.get("date") or hashlib.sha256(f"{user_id}:{current_msg}".encode()).hexdigest()
            
            current_chat_dates.add(chat_id)
            if chat_id not in SEEN_CHAT_DATES:
                SEEN_CHAT_DATES.add(chat_id)
                if not is_baseline_run:
                    new_messages.append({"user": user_id, "chat": current_msg})
        
        SEEN_CHAT_DATES.intersection_update(current_chat_dates)
        return new_messages
    except Exception: return []

def poll_once(account_id: str, account_data: dict, global_chat_id: str, is_baseline_run: bool = False):
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

        low = text[:200].lower()
        if "<!doctype" in low or "<html" in low:
            if not is_baseline_run and can_send_error("NOTIFY_HTML", account_data):
                tg_send(f"⚠️ <b>[{html.escape(account_name)}] Cookie hết hạn</b> (API trả về HTML).", bot_token, global_chat_id)
            return
        
        parsed = parse_notify_text(text)
        
        if "numbers" in parsed:
            now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7)))
            today_str = now.strftime("%Y-%m-%d")

            # [THAY ĐỔI v9.0] Chỉ reset bộ đếm ngày mới, KHÔNG gửi tin nhắn chúc mừng
            if today_str != DAILY_COUNTER_DATE:
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
                    if "đơn hàng" in label.lower():
                        DAILY_ORDER_COUNT[label] += (current_val - last_val)
                    if "tin nhắn" in label.lower():
                        has_new_chat = True
                
                if current_val > COLUMN_BASELINES[label]:
                    icon = _get_icon_for_label(label)
                    instant_alerts_map[label] = f"  {icon} <b>{label}:</b> {current_val}"

            new_chat_messages = []
            if has_new_chat:
                fetched = fetch_chats(account_data, global_chat_id, is_baseline_run=is_baseline_run) 
                for chat in fetched:
                    new_chat_messages.append(f"<b>--- ✉️ {html.escape(chat.get('user',''))} ---</b>\n  {html.escape(chat.get('chat',''))}")

            if has_new_notification and not is_baseline_run:
                ordered = ["Đơn hàng sản phẩm", "Tin nhắn", "Khiếu nại", "Đánh giá"]
                alert_lines = []
                
                for label in ordered:
                    if label in instant_alerts_map: alert_lines.append(instant_alerts_map.pop(label))
                for v in instant_alerts_map.values(): alert_lines.append(v)
                
                msg_lines = [f"<b>⭐ BÁO CÁO - [{html.escape(account_name)}]</b>"]
                
                if new_chat_messages:
                    msg_lines.append("➖➖➖➖➖➖➖")
                    msg_lines.extend(new_chat_messages)
                
                if alert_lines:
                    msg_lines.append("➖➖➖➖➖➖➖")
                    msg_lines.extend(alert_lines)
                
                if new_chat_messages or alert_lines:
                    tg_send("\n".join(msg_lines), bot_token, global_chat_id)

            account_data["state_last_notify_nums"] = current_nums
            account_data["state_daily_counter_date"] = DAILY_COUNTER_DATE
        
        else:
            if text != str(LAST_NOTIFY_NUMS) and not is_baseline_run and can_send_error("NOTIFY_BAD_FMT", account_data):
                tg_send(f"🔔 <b>[{html.escape(account_name)}] Lỗi định dạng:</b> {html.escape(text)}", bot_token, global_chat_id)

    except Exception as e:
        print(f"Poll error {account_name}: {e}")

# =================== Background Loops ===================

def pinger_loop():
    while True:
        try:
            pinger_conf = GLOBAL_STATE.get("pinger", {})
            is_enabled = pinger_conf.get("enabled", False)
            url = pinger_conf.get("url", "")
            interval = int(pinger_conf.get("interval", 300))
            
            if is_enabled and url:
                try: requests.get(url, timeout=10)
                except: pass
            
            time.sleep(max(10, interval))
        except Exception:
            time.sleep(60)

def poller_loop():
    print("▶ Poller started")
    time.sleep(2)
    
    # Baseline
    for account_id, account_data in GLOBAL_STATE["accounts"].items():
        fetch_chats(account_data, GLOBAL_STATE["global_chat_id"], True)
        poll_once(account_id, account_data, GLOBAL_STATE["global_chat_id"], True)
        if not account_data["state_daily_counter_date"]:
            account_data["state_daily_counter_date"] = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7))).strftime("%Y-%m-%d")

    while True:
        try:
            time.sleep(POLL_INTERVAL)
            chat_id = GLOBAL_STATE["global_chat_id"]
            if not chat_id: continue

            for account_id, account_data in list(GLOBAL_STATE["accounts"].items()):
                if "state_last_notify_nums" not in account_data: continue
                poll_once(account_id, account_data, chat_id, False)
        except Exception:
            time.sleep(60)

# =================== Config Logic ===================

def _create_account_state() -> dict:
    return {
        "notify_api": {}, "chat_api": {},
        "state_last_notify_nums": [],
        "state_daily_order_count": defaultdict(int),
        "state_daily_counter_date": "",
        "state_seen_chat_dates": set(),
        "state_last_error_times": defaultdict(float)
    }

def _apply_restore(new_config_data: Dict[str, Any]) -> bool:
    global GLOBAL_STATE
    
    if "global_chat_id" not in new_config_data or "accounts" not in new_config_data:
        raise HTTPException(status_code=400, detail="JSON lỗi.")

    new_chat_id = new_config_data.get("global_chat_id", "")
    new_pinger = new_config_data.get("pinger", {"enabled": False, "url": "", "interval": 300})
    
    new_accounts_dict = {}
    for account_id, config in new_config_data["accounts"].items():
        try:
            notify_curl = config.get("notify_curl", "")
            chat_curl = config.get("chat_curl", "")
            if not notify_curl or not chat_curl: continue

            acc_data = {
                "account_name": config.get("account_name", f"Acc {account_id}"),
                "bot_token": config.get("bot_token", ""),
                "notify_curl": notify_curl,
                "chat_curl": chat_curl,
                **_create_account_state()
            }
            acc_data["notify_api"] = parse_curl_command(notify_curl)
            acc_data["chat_api"] = parse_curl_command(chat_curl)
            new_accounts_dict[account_id] = acc_data
        except: pass
    
    GLOBAL_STATE["global_chat_id"] = new_chat_id
    GLOBAL_STATE["accounts"] = new_accounts_dict
    GLOBAL_STATE["pinger"] = new_pinger
    return True

# =================== ROUTES ===================

@app.get("/", response_class=HTMLResponse)
async def ui():
    return HTMLResponse(content=f"""
    <!DOCTYPE html>
    <html lang="vi">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>TapHoaMMO Bot Manager</title>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600&display=swap" rel="stylesheet">
        <style>
            :root {{
                --bg: #f3f4f6; --card: #ffffff; --text: #1f2937; 
                --border: #e5e7eb; --primary: #3b82f6; --danger: #ef4444;
            }}
            body {{ font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 20px; }}
            .container {{ max-width: 900px; margin: 0 auto; }}
            
            /* Card Styles */
            .card {{ background: var(--card); border-radius: 12px; padding: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 24px; }}
            h2 {{ margin-top: 0; font-size: 1.25rem; color: #111827; border-bottom: 2px solid var(--bg); padding-bottom: 10px; margin-bottom: 20px; }}
            h3 {{ font-size: 1rem; color: #4b5563; margin: 10px 0 5px; }}
            
            /* Form Elements */
            label {{ display: block; font-size: 0.875rem; font-weight: 600; margin-bottom: 4px; color: #374151; }}
            input, textarea, select {{
                width: 100%; padding: 10px; border: 1px solid var(--border); border-radius: 6px;
                font-size: 14px; box-sizing: border-box; margin-bottom: 12px; transition: border 0.2s;
            }}
            input:focus, textarea:focus {{ border-color: var(--primary); outline: none; }}
            textarea {{ font-family: monospace; font-size: 12px; color: #4b5563; }}

            /* Grid Layout */
            .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 15px; }}
            
            /* Account Card Specifics */
            .account-item {{ 
                background: #f9fafb; border: 1px solid var(--border); border-radius: 8px; 
                padding: 20px; margin-bottom: 20px; position: relative;
            }}
            .remove-btn {{
                position: absolute; top: 15px; right: 15px;
                background: transparent; color: var(--danger); border: none; cursor: pointer; font-weight: bold;
            }}

            /* Buttons */
            .btn {{ 
                display: inline-block; padding: 10px 20px; border-radius: 6px; border: none; 
                font-weight: 600; cursor: pointer; width: 100%; text-align: center;
            }}
            .btn-primary {{ background: var(--primary); color: white; }}
            .btn-primary:hover {{ background: #2563eb; }}
            .btn-secondary {{ background: #e5e7eb; color: #374151; margin-top: 10px; }}
            .btn-secondary:hover {{ background: #d1d5db; }}
            
            /* Backup Section */
            .backup-area {{ display: flex; gap: 10px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <form id="configForm">
                <div class="card">
                    <h2>⚙️ Cấu hình Hệ thống</h2>
                    <div class="grid">
                        <div>
                            <label>ID Telegram Nhận tin:</label>
                            <input type="text" id="global_chat_id" placeholder="-100..." required>
                        </div>
                        <div>
                            <label>Pinger (Giữ Bot Online):</label>
                            <div style="display: flex; gap: 10px;">
                                <select id="pinger_enabled" style="width: 80px;">
                                    <option value="0">Tắt</option>
                                    <option value="1">Bật</option>
                                </select>
                                <input type="text" id="pinger_url" placeholder="URL Web này (ví dụ: https://myapp.onrender.com)">
                            </div>
                        </div>
                    </div>
                </div>

                <div class="card">
                    <h2>🛒 Danh sách Tài khoản</h2>
                    <div id="accountList"></div>
                    <button type="button" class="btn btn-secondary" onclick="addAccount()">+ Thêm Tài khoản Mới</button>
                </div>

                <div style="position: sticky; bottom: 20px; z-index: 10;">
                    <button type="submit" class="btn btn-primary" style="box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
                        💾 Lưu Cấu Hình & Khởi Động Lại
                    </button>
                </div>
            </form>

            <div class="card" style="margin-top: 40px;">
                <h2>📦 Sao lưu & Khôi phục</h2>
                <textarea id="backupData" rows="3" placeholder="Dữ liệu backup sẽ hiện ở đây hoặc dán vào đây để restore..."></textarea>
                <div class="backup-area">
                    <button type="button" class="btn btn-secondary" onclick="getBackup()">Lấy dữ liệu Backup</button>
                    <button type="button" class="btn btn-secondary" onclick="restoreBackup()">Khôi phục từ Text</button>
                </div>
            </div>
        </div>

        <script>
            // Render Logic
            function renderAccount(id, data = {{}}) {{
                const div = document.createElement('div');
                div.className = 'account-item';
                div.dataset.id = id;
                div.innerHTML = `
                    <button type="button" class="remove-btn" onclick="this.parentElement.remove()">🗑️ Xóa</button>
                    <div class="grid">
                        <div>
                            <label>Tên Shop / Tài khoản:</label>
                            <input type="text" class="acc-name" value="${{data.account_name || ''}}" placeholder="VD: Shop A" required>
                        </div>
                        <div>
                            <label>Bot Token (Telegram):</label>
                            <input type="password" class="acc-token" value="${{data.bot_token || ''}}" placeholder="1234:ABC..." required>
                        </div>
                    </div>
                    <div style="margin-top: 10px;">
                        <label>Lệnh cURL Thông báo (getNotify):</label>
                        <textarea class="acc-notify" rows="3" placeholder="Dán toàn bộ lệnh cURL vào đây...">${{data.notify_curl || ''}}</textarea>
                    </div>
                    <div>
                        <label>Lệnh cURL Tin nhắn (getNewConversion):</label>
                        <textarea class="acc-chat" rows="3" placeholder="Dán toàn bộ lệnh cURL vào đây...">${{data.chat_curl || ''}}</textarea>
                    </div>
                `;
                document.getElementById('accountList').appendChild(div);
            }}

            function addAccount() {{
                renderAccount(crypto.randomUUID());
            }}

            // Load Data
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
                    if (data.accounts) {{
                        Object.entries(data.accounts).forEach(([id, acc]) => renderAccount(id, acc));
                    }}
                }} catch (e) {{ console.error(e); }}
            }}

            // Save Data
            document.getElementById('configForm').onsubmit = async (e) => {{
                e.preventDefault();
                const accounts = {{}};
                document.querySelectorAll('.account-item').forEach(el => {{
                    accounts[el.dataset.id] = {{
                        account_name: el.querySelector('.acc-name').value,
                        bot_token: el.querySelector('.acc-token').value,
                        notify_curl: el.querySelector('.acc-notify').value,
                        chat_curl: el.querySelector('.acc-chat').value
                    }};
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
                    await fetch('/debug/set-config', {{
                        method: 'POST', headers: {{'Content-Type': 'application/json'}},
                        body: JSON.stringify(payload)
                    }});
                    alert('✅ Đã lưu thành công!');
                }} catch (e) {{ alert('❌ Lỗi: ' + e); }}
            }};

            // Backup/Restore
            async function getBackup() {{
                const res = await fetch('/debug/get-backup');
                const data = await res.json();
                document.getElementById('backupData').value = JSON.stringify(data, null, 2);
            }}

            async function restoreBackup() {{
                try {{
                    const data = JSON.parse(document.getElementById('backupData').value);
                    await fetch('/debug/restore-from-text', {{
                        method: 'POST', headers: {{'Content-Type': 'application/json'}},
                        body: JSON.stringify(data)
                    }});
                    alert('✅ Khôi phục thành công, đang tải lại...');
                    loadConfig();
                }} catch (e) {{ alert('❌ JSON không hợp lệ'); }}
            }}

            loadConfig();
        </script>
    </body>
    </html>
    """)

@app.get("/debug/get-backup")
def get_backup():
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
    try:
        js = await req.json()
        _apply_restore(js)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(400, detail=str(e))

# =================== STARTUP ===================
if not DISABLE_POLLER:
    threading.Thread(target=poller_loop, daemon=True).start()
    threading.Thread(target=pinger_loop, daemon=True).start()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
