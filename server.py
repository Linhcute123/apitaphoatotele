import os, json, time, threading, html, hashlib, requests, re, shlex
from typing import Any, Dict, List
from collections import defaultdict
import datetime 
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

# ----- Cấu hình môi trường (Env) -----
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# =================== CẤU HÌNH HỆ THỐNG ===================
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "12"))
VERIFY_TLS    = bool(int(os.getenv("VERIFY_TLS", "1")))
DISABLE_POLLER = os.getenv("DISABLE_POLLER", "0") == "1"

# =================== TRẠNG THÁI TOÀN CỤC ===================
GLOBAL_STATE = {
    "global_chat_id": "", 
    "pinger": {
        "enabled": False,
        "url": "",
        "interval": 300
    },
    "accounts": {}
}

ERROR_COOLDOWN_SECONDS = 3600 

# =================== APP FASTAPI ===================
app = FastAPI(title="TapHoaMMO Bot v12.1")

# =================== HÀM HỖ TRỢ ===================

def tg_send(text: str, bot_token: str, chat_id: str):
    if not bot_token or not chat_id: return
    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    MAX = 3900  
    chunks = [text[i:i+MAX] for i in range(0, len(text), MAX)] or [""]
    for part in chunks[:3]:
        try:
            requests.post(api_url, json={
                "chat_id": chat_id, "text": part, 
                "parse_mode": "HTML", "disable_web_page_preview": True
            }, timeout=20)
        except: pass

def can_send_error(error_key: str, account_data: dict) -> bool:
    global ERROR_COOLDOWN_SECONDS
    current_time = time.time()
    last_sent_time = account_data["state_last_error_times"][error_key]
    if (current_time - last_sent_time) > ERROR_COOLDOWN_SECONDS:
        account_data["state_last_error_times"][error_key] = current_time
        return True
    return False

# =================== XỬ LÝ DỮ LIỆU ===================

def _get_icon_for_label(label: str) -> str:
    low = label.lower()
    if "sản phẩm" in low: return "📦"
    if "khiếu nại" in low: return "⚠️"
    if "đánh giá" in low: return "⭐"
    if "tin nhắn" in low: return "✉️"
    return "•"

def _labels_for_notify(parts_len: int) -> List[str]:
    labels = [f"Mục {i+1}" for i in range(parts_len)]
    mapping = { 0: "Đơn hàng sản phẩm", 1: "Đánh giá", 7: "Khiếu nại", 8: "Tin nhắn" }
    for idx, name in mapping.items():
        if idx < parts_len: labels[idx] = name
    return labels

COLUMN_BASELINES = defaultdict(int)
COLUMN_BASELINES["Khiếu nại"] = 0 

def parse_notify_text(text: str) -> Dict[str, Any]:
    s = (text or "").strip()
    parts = s.split("|") if s else []
    if len(parts) > 0 and all(re.fullmatch(r"\d+", p or "") for p in parts):
        return {"raw": s, "numbers": [int(p) for p in parts]}
    return {"raw": s}

def parse_curl_command(curl_text: str) -> Dict[str, Any]:
    try: args = shlex.split(curl_text)
    except: return {"url": "", "method": "GET", "headers": {}}

    method = "GET"; headers = {}; data = None; url = ""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "curl": 
            i += 1; url = args[i] if i < len(args) else ""
        elif a in ("-X", "--request"): 
            i += 1; method = args[i].upper() if i < len(args) else "GET"
        elif a in ("-H", "--header"):
            i += 1
            if i < len(args) and ":" in args[i]:
                k, v = args[i].split(":", 1)
                headers[k.strip()] = v.strip()
        elif a in ("-b", "--cookie"): 
            i += 1; headers['cookie'] = args[i] if i < len(args) else ""
        elif a in ("--data", "--data-raw", "-d"): 
            i += 1; data = args[i] if i < len(args) else None
        i += 1

    if method == "GET" and data: method = "POST"
    final_headers = {k: v for k, v in headers.items() if not k.lower().startswith(('content-length', 'host'))}
    body_json = None
    if data:
        try: body_json = json.loads(data)
        except: pass
    return {"url": url, "method": method, "headers": final_headers, "body_json": body_json, "body_data": data if not body_json else None}

def _make_api_request(config: Dict[str, Any]) -> requests.Response:
    kwargs = {"headers": config.get("headers", {}), "verify": VERIFY_TLS, "timeout": 25}
    if config.get("method") == "POST":
        if config.get("body_json"): kwargs["json"] = config["body_json"]
        elif config.get("body_data"): kwargs["data"] = config["body_data"].encode('utf-8')
    return requests.request(config.get("method", "GET"), config.get("url", ""), **kwargs)

# =================== LOGIC CHÍNH ===================

def fetch_chats(account_data: dict, is_baseline: bool = False) -> List[Dict[str, str]]:
    if not account_data["chat_api"].get("url"): return []
    try:
        r = _make_api_request(account_data["chat_api"])
        try: data = r.json()
        except: return []
        if not isinstance(data, list): return []
        new_msgs = []
        curr_ids = set()
        SEEN = account_data["state_seen_chat_dates"]
        for chat in data:
            if not isinstance(chat, dict): continue
            uid = chat.get("guest_user", "Khách")
            msg = chat.get("last_chat", "")
            mid = chat.get("date") or hashlib.sha256(f"{uid}:{msg}".encode()).hexdigest()
            curr_ids.add(mid)
            if mid not in SEEN:
                SEEN.add(mid)
                if not is_baseline: new_msgs.append({"user": uid, "chat": msg})
        SEEN.intersection_update(curr_ids)
        return new_msgs
    except: return []

def poll_once(acc_id: str, acc_data: dict, chat_id: str, is_baseline: bool = False):
    acc_name = acc_data.get('account_name', 'N/A')
    token = acc_data.get('bot_token', '')
    if not acc_data["notify_api"].get("url"): return

    try:
        r = _make_api_request(acc_data["notify_api"])
        text = (r.text or "").strip()
        if not text: return
        if "<html" in text.lower() or "<!doctype" in text.lower():
            if not is_baseline and can_send_error("NOTIFY_HTML", acc_data):
                tg_send(f"⚠️ <b>[{html.escape(acc_name)}] Cookie hết hạn (HTML).</b> Vui lòng cập nhật cURL.", token, chat_id)
            return
        
        parsed = parse_notify_text(text)
        if "numbers" in parsed:
            nums = parsed["numbers"]
            last_nums = acc_data["state_last_notify_nums"]
            today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7))).strftime("%Y-%m-%d")
            if today != acc_data["state_daily_counter_date"]:
                acc_data["state_daily_counter_date"] = today
                acc_data["state_daily_order_count"].clear()

            if len(nums) != len(last_nums): last_nums = [0] * len(nums)
            labels = _labels_for_notify(len(nums))
            alerts = {}
            has_inc = False
            check_chat = False

            for i, val in enumerate(nums):
                old = last_nums[i]
                lbl = labels[i]
                if val > old:
                    has_inc = True
                    if "đơn hàng" in lbl.lower(): acc_data["state_daily_order_count"][lbl] += (val - old)
                    if "tin nhắn" in lbl.lower(): check_chat = True
                if val > COLUMN_BASELINES[lbl]:
                    alerts[lbl] = f"  {_get_icon_for_label(lbl)} <b>{lbl}:</b> {val}"

            chat_msgs = []
            if check_chat:
                for c in fetch_chats(acc_data, is_baseline):
                    chat_msgs.append(f"<b>✉️ {html.escape(c['user'])}:</b> <i>{html.escape(c['chat'])}</i>")

            if has_inc and not is_baseline:
                lines = [f"<b>🔔 BÁO CÁO - [{html.escape(acc_name)}]</b>"]
                if chat_msgs: 
                    lines.append("➖➖➖➖➖➖➖")
                    lines.extend(chat_msgs)
                
                ordered_keys = ["Đơn hàng sản phẩm", "Tin nhắn", "Khiếu nại", "Đánh giá"]
                alert_vals = []
                for k in ordered_keys:
                    if k in alerts: alert_vals.append(alerts.pop(k))
                alert_vals.extend(alerts.values())
                
                if alert_vals: 
                    lines.append("➖➖➖➖➖➖➖")
                    lines.extend(alert_vals)
                
                if chat_msgs or alert_vals: tg_send("\n".join(lines), token, chat_id)

            acc_data["state_last_notify_nums"] = nums
        else:
            if text != str(acc_data["state_last_notify_nums"]) and not is_baseline and can_send_error("NOTIFY_BAD", acc_data):
                tg_send(f"⚠️ <b>[{html.escape(acc_name)}] Lỗi định dạng:</b> {html.escape(text)}", token, chat_id)
    except Exception as e: print(f"Poll Error {acc_name}: {e}")

# =================== LOOPS ===================
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
        except: time.sleep(60)

def poller_loop():
    print("▶ Poller started (Multi-Account)")
    time.sleep(3)
    chat_id = GLOBAL_STATE["global_chat_id"]
    for aid, adata in GLOBAL_STATE["accounts"].items():
        fetch_chats(adata, True)
        poll_once(aid, adata, chat_id, True)
        if not adata["state_daily_counter_date"]:
            adata["state_daily_counter_date"] = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7))).strftime("%Y-%m-%d")
    while True:
        try:
            time.sleep(POLL_INTERVAL)
            chat_id = GLOBAL_STATE["global_chat_id"]
            if not chat_id: continue
            for aid, adata in list(GLOBAL_STATE["accounts"].items()):
                if "state_last_notify_nums" in adata: poll_once(aid, adata, chat_id, False)
        except: time.sleep(60)

# =================== WEB UI & CONFIG ===================
def _create_state():
    return {
        "notify_api": {}, "chat_api": {}, "state_last_notify_nums": [],
        "state_daily_order_count": defaultdict(int), "state_daily_counter_date": "",
        "state_seen_chat_dates": set(), "state_last_error_times": defaultdict(float)
    }

def _restore(data: dict):
    GLOBAL_STATE["global_chat_id"] = data.get("global_chat_id", "")
    GLOBAL_STATE["pinger"] = data.get("pinger", {"enabled": False, "url": "", "interval": 300})
    new_accs = {}
    for aid, cfg in data.get("accounts", {}).items():
        if not cfg.get("notify_curl"): continue
        adata = {
            "account_name": cfg.get("account_name", f"Shop {aid}"),
            "bot_token": cfg.get("bot_token", ""),
            "notify_curl": cfg.get("notify_curl"),
            "chat_curl": cfg.get("chat_curl"),
            **_create_state()
        }
        adata["notify_api"] = parse_curl_command(adata["notify_curl"])
        adata["chat_api"] = parse_curl_command(adata["chat_curl"])
        new_accs[aid] = adata
    GLOBAL_STATE["accounts"] = new_accs

# [FIX] Thêm endpoint healthz để Render không báo lỗi 404
@app.get("/healthz")
def health_check():
    return {"status": "ok"}

@app.get("/", response_class=HTMLResponse)
async def ui():
    return HTMLResponse(content=f"""
    <!DOCTYPE html>
    <html lang="vi">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>TapHoaMMO Bot v12.1</title>
        <style>
            :root {{ --bg: #f8f9fa; --card: #ffffff; --primary: #0d6efd; --danger: #dc3545; }}
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); padding: 20px; margin: 0; color: #333; }}
            .container {{ max-width: 800px; margin: 0 auto; }}
            .card {{ background: var(--card); padding: 25px; border-radius: 10px; margin-bottom: 20px; border: 1px solid #eee; }}
            h2 {{ margin-top: 0; font-size: 18px; border-bottom: 2px solid #f0f0f0; padding-bottom: 15px; margin-bottom: 20px; color: #0d6efd; }}
            label {{ font-weight: 600; font-size: 13px; display: block; margin-bottom: 6px; color: #555; }}
            input, textarea, select {{ width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 6px; box-sizing: border-box; margin-bottom: 15px; font-size: 14px; }}
            .row {{ display: flex; gap: 15px; }}
            .col {{ flex: 1; }}
            button {{ padding: 12px 20px; border: none; border-radius: 6px; cursor: pointer; font-weight: 600; transition: 0.2s; }}
            .btn-primary {{ background: var(--primary); color: white; width: 100%; }}
            .btn-sec {{ background: #e9ecef; color: #333; }}
            .btn-danger {{ background: #fff5f5; color: var(--danger); padding: 6px 12px; border: 1px solid #ffdcdc; position: absolute; top: 15px; right: 15px; }}
            .account-item {{ border: 1px solid #e0e0e0; padding: 20px; border-radius: 8px; position: relative; margin-bottom: 20px; background: #fff; }}
            .ping-box {{ background: #f0f7ff; padding: 15px; border-radius: 8px; border: 1px solid #cce5ff; margin-top: 15px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <form id="frm">
                <div class="card">
                    <h2>⚙️ Cấu hình Hệ thống</h2>
                    <label>Telegram Chat ID:</label>
                    <input type="text" id="gid" placeholder="-100xxxxx" required>
                    <div class="ping-box">
                        <label style="color: #004085;">📡 Cấu hình Ping (Giữ Server Sống)</label>
                        <div class="row">
                            <div style="width: 100px;">
                                <label>Bật/Tắt:</label>
                                <select id="p_enable"><option value="0">Tắt</option><option value="1">Bật</option></select>
                            </div>
                            <div class="col">
                                <label>Chu kỳ (Giây):</label>
                                <input type="number" id="p_interval" value="300">
                            </div>
                        </div>
                        <label>URL trang web này:</label>
                        <input type="text" id="p_url" placeholder="https://ten-app.onrender.com" style="margin-bottom:0;">
                    </div>
                </div>

                <div class="card">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:15px;">
                        <h2 style="border:none; margin:0; padding:0;">🛒 Danh sách Shop</h2>
                        <button type="button" class="btn-sec" onclick="addAcc()">+ Thêm Shop</button>
                    </div>
                    <div id="list"></div>
                </div>

                <div style="position:sticky; bottom:20px; z-index: 10;">
                    <button type="submit" class="btn-primary">💾 Lưu Cấu Hình</button>
                </div>
            </form>
            
            <div class="card" style="margin-top: 40px;">
                <h2>📦 Backup / Restore</h2>
                <textarea id="bkp" rows="3" placeholder="Dữ liệu JSON..."></textarea>
                <div class="row">
                    <button type="button" class="btn-sec col" onclick="getBackup()">⬇️ Lấy Backup</button>
                    <button type="button" class="btn-sec col" onclick="restBackup()">⬆️ Restore</button>
                </div>
            </div>
        </div>

        <script>
            function renAcc(id, d={{}}) {{
                const div = document.createElement('div');
                div.className = 'account-item';
                div.dataset.id = id;
                div.innerHTML = `
                    <button type="button" class="btn-danger" onclick="this.parentElement.remove()">🗑️ Xóa</button>
                    <div class="row">
                        <div class="col"><label>Tên Shop:</label><input type="text" class="n" value="${{d.account_name||''}}" required></div>
                        <div class="col"><label>Bot Token:</label><input type="password" class="t" value="${{d.bot_token||''}}" required></div>
                    </div>
                    <label>cURL Notify:</label><textarea class="cn" rows="2">${{d.notify_curl||''}}</textarea>
                    <label>cURL Chat:</label><textarea class="cc" rows="2">${{d.chat_curl||''}}</textarea>
                `;
                document.getElementById('list').appendChild(div);
            }}
            function addAcc() {{ renAcc(crypto.randomUUID()); }}
            async function load() {{
                try {{
                    const res = await fetch('/debug/get-backup');
                    const d = await res.json();
                    document.getElementById('gid').value = d.global_chat_id || '';
                    if(d.pinger) {{
                        document.getElementById('p_enable').value = d.pinger.enabled ? "1" : "0";
                        document.getElementById('p_url').value = d.pinger.url || "";
                        document.getElementById('p_interval').value = d.pinger.interval || "300";
                    }}
                    document.getElementById('list').innerHTML = '';
                    if(d.accounts) Object.entries(d.accounts).forEach(([k,v])=>renAcc(k,v));
                }} catch(e) {{}}
            }}
            document.getElementById('frm').onsubmit = async (e) => {{
                e.preventDefault();
                const accs = {{}};
                document.querySelectorAll('.account-item').forEach(el => {{
                    accs[el.dataset.id] = {{
                        account_name: el.querySelector('.n').value,
                        bot_token: el.querySelector('.t').value,
                        notify_curl: el.querySelector('.cn').value,
                        chat_curl: el.querySelector('.cc').value
                    }};
                }});
                const payload = {{
                    global_chat_id: document.getElementById('gid').value,
                    pinger: {{
                        enabled: document.getElementById('p_enable').value === "1",
                        url: document.getElementById('p_url').value,
                        interval: parseInt(document.getElementById('p_interval').value) || 300
                    }},
                    accounts: accs
                }};
                await fetch('/debug/set-config', {{
                    method: 'POST', headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify(payload)
                }});
                alert('✅ Đã lưu thành công!'); load();
            }};
            async function getBackup() {{
                const d = await (await fetch('/debug/get-backup')).json();
                document.getElementById('bkp').value = JSON.stringify(d, null, 2);
            }}
            async function restBackup() {{
                try {{
                    const d = JSON.parse(document.getElementById('bkp').value);
                    await fetch('/debug/set-config', {{
                        method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify(d)
                    }});
                    alert('✅ Restore thành công!'); location.reload();
                }} catch {{ alert('❌ JSON lỗi'); }}
            }}
            load();
        </script>
    </body>
    </html>
    """)

@app.get("/debug/get-backup")
def get_backup():
    return {
        "global_chat_id": GLOBAL_STATE["global_chat_id"],
        "pinger": GLOBAL_STATE.get("pinger", {"enabled": False, "url": "", "interval": 300}),
        "accounts": {k: {x: v.get(x) for x in ["account_name","bot_token","notify_curl","chat_curl"]} 
                     for k,v in GLOBAL_STATE["accounts"].items()}
    }

@app.post("/debug/set-config")
@app.post("/debug/restore-from-text")
async def set_config(req: Request):
    try: _restore(await req.json()); return {"ok": True}
    except Exception as e: raise HTTPException(400, str(e))

if not DISABLE_POLLER:
    threading.Thread(target=poller_loop, daemon=True).start()
    threading.Thread(target=pinger_loop, daemon=True).start()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
