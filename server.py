"""
PROJECT: TAPHOAMMO GALAXY ENTERPRISE
VERSION: 30.0 (VIP PRO MAX UI & LOGIC UPDATE)
AUTHOR: AI ASSISTANT & ADMIN VAN LINH
LICENSE: PROPRIETARY
"""

import os
import json
import time
import threading
import html
import hashlib
import requests
import re
import shlex
import sqlite3
import logging
from typing import Any, Dict, List
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

# Import Libraries
try:
    from fastapi import FastAPI, Request, HTTPException, Depends, status, Form, Cookie, File, UploadFile
    from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
    from fastapi.security import APIKeyCookie
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("CRITICAL ERROR: Thiếu thư viện. Chạy: pip install fastapi uvicorn requests python-dotenv python-multipart aiofiles")
    exit(1)

# ==============================================================================
# 1. CONFIGURATION
# ==============================================================================

class SystemConfig:
    APP_NAME = "TapHoaMMO Enterprise"
    VERSION = "30.0.0"
    DATABASE_FILE = "galaxy_data.db"
    LOG_FILE = "system_run.log"
    
    # --- BẢO MẬT ---
    ADMIN_SECRET = os.getenv("ADMIN_SECRET", "admin").strip()
    
    # --- BACKUP ---
    BACKUP_DIR = os.getenv("BACKUP_DIR", "") 

    DEFAULT_POLL_INTERVAL = 10
    VERIFY_TLS = bool(int(os.getenv("VERIFY_TLS", "1")))
    DISABLE_POLLER = os.getenv("DISABLE_POLLER", "0") == "1"

# ==============================================================================
# 2. DATABASE & LOGGING
# ==============================================================================

class LoggerManager:
    _instance = None
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(LoggerManager, cls).__new__(cls)
            cls._instance._setup()
        return cls._instance
    def _setup(self):
        self.logger = logging.getLogger("GalaxyBot")
        self.logger.setLevel(logging.INFO)
        formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
        file_handler = RotatingFileHandler(SystemConfig.LOG_FILE, maxBytes=5*1024*1024, backupCount=3)
        file_handler.setFormatter(formatter)
        self.logger.addHandler(file_handler)
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        self.logger.addHandler(console_handler)
    def info(self, msg): self.logger.info(msg)
    def error(self, msg): self.logger.error(msg)

SYS_LOG = LoggerManager()

class DatabaseManager:
    def __init__(self, db_file):
        self.db_file = db_file
        self.init_db()
    def get_connection(self):
        conn = sqlite3.connect(self.db_file, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn
    def init_db(self):
        conn = self.get_connection()
        conn.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
        conn.execute('CREATE TABLE IF NOT EXISTS accounts (id TEXT PRIMARY KEY, name TEXT, bot_token TEXT, notify_curl TEXT, chat_curl TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)')
        conn.execute('CREATE TABLE IF NOT EXISTS stats (id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT, date TEXT, category TEXT, count INTEGER DEFAULT 0, UNIQUE(account_id, date, category))')
        conn.commit()
        conn.close()

    def get_setting(self, key, default=None):
        with self.get_connection() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row['value'] if row else default
    def set_setting(self, key, value):
        with self.get_connection() as conn:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    def get_all_accounts(self):
        with self.get_connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM accounts").fetchall()]
    def save_account(self, acc_id, data):
        with self.get_connection() as conn:
            conn.execute('INSERT OR REPLACE INTO accounts (id, name, bot_token, notify_curl, chat_curl) VALUES (?, ?, ?, ?, ?)', 
                         (acc_id, data['account_name'], data['bot_token'], data['notify_curl'], data['chat_curl']))
    def delete_account(self, acc_id):
        with self.get_connection() as conn:
            conn.execute("DELETE FROM accounts WHERE id = ?", (acc_id,))
    def update_stat(self, acc_id, date, category, amount):
        with self.get_connection() as conn:
            row = conn.execute("SELECT count FROM stats WHERE account_id=? AND date=? AND category=?", (acc_id, date, category)).fetchone()
            if row: conn.execute("UPDATE stats SET count=? WHERE account_id=? AND date=? AND category=?", (row['count'] + amount, acc_id, date, category))
            else: conn.execute("INSERT INTO stats (account_id, date, category, count) VALUES (?, ?, ?, ?)", (acc_id, date, category, amount))

DB = DatabaseManager(SystemConfig.DATABASE_FILE)

# ==============================================================================
# 3. BACKUP MANAGER
# ==============================================================================

class BackupManager:
    @staticmethod
    def create_backup_data(clean_curl=True):
        data = {
            "meta": {"version": SystemConfig.VERSION, "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "type": "clean" if clean_curl else "full"},
            "global_chat_id": DB.get_setting("global_chat_id", ""),
            "poll_interval": int(DB.get_setting("poll_interval", "10")),
            "pinger": {"enabled": DB.get_setting("pinger_enabled") == "1", "url": DB.get_setting("pinger_url", ""), "interval": int(DB.get_setting("pinger_interval", "300"))},
            "accounts": {}
        }
        accounts = DB.get_all_accounts()
        for acc in accounts:
            data["accounts"][acc['id']] = {
                "account_name": acc['name'], "bot_token": acc['bot_token'],
                "notify_curl": "" if clean_curl else acc['notify_curl'], "chat_curl": "" if clean_curl else acc['chat_curl']
            }
        return data

    @staticmethod
    def auto_backup_to_disk(data):
        if not SystemConfig.BACKUP_DIR: return
        try:
            os.makedirs(SystemConfig.BACKUP_DIR, exist_ok=True)
            filename = f"auto_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            filepath = os.path.join(SystemConfig.BACKUP_DIR, filename)
            with open(filepath, 'w', encoding='utf-8') as f: json.dump(data, f, indent=4, ensure_ascii=False)
            files = sorted([os.path.join(SystemConfig.BACKUP_DIR, f) for f in os.listdir(SystemConfig.BACKUP_DIR)], key=os.path.getmtime)
            if len(files) > 10: [os.remove(f) for f in files[:-10]]
        except Exception as e: SYS_LOG.error(f"❌ Auto-backup failed: {e}")

# ==============================================================================
# 4. CORE LOGIC
# ==============================================================================

class Utils:
    @staticmethod
    def parse_curl(curl_text: str) -> Dict[str, Any]:
        try: args = shlex.split(curl_text)
        except: return {"url": "", "method": "GET", "headers": {}}
        method = "GET"; headers = {}; data = None; url = ""
        i = 0
        while i < len(args):
            a = args[i]
            if a == "curl": i += 1; url = args[i] if i < len(args) else ""
            elif a in ("-X", "--request"): i += 1; method = args[i].upper() if i < len(args) else "GET"
            elif a in ("-H", "--header"): i += 1; (k, v) = args[i].split(":", 1) if ":" in args[i] else (args[i], ""); headers[k.strip()] = v.strip()
            elif a in ("-b", "--cookie"): i += 1; headers['cookie'] = args[i] if i < len(args) else ""
            elif a in ("--data", "--data-raw", "-d"): i += 1; data = args[i] if i < len(args) else None
            i += 1
        if method == "GET" and data: method = "POST"
        final_headers = {k: v for k, v in headers.items() if not k.lower().startswith(('content-length', 'host'))}
        body_json = None
        if data:
            try: body_json = json.loads(data)
            except: pass
        return {"url": url, "method": method, "headers": final_headers, "body_json": body_json, "body_data": data if not body_json else None}

    @staticmethod
    def parse_notify_text(text: str) -> Dict[str, Any]:
        s = (text or "").strip()
        parts = s.split("|") if s else []
        if len(parts) > 0 and all(re.fullmatch(r"\d+", p or "") for p in parts): return {"raw": s, "numbers": [int(p) for p in parts]}
        return {"raw": s}
    
    @staticmethod
    def get_labels(length: int) -> List[str]:
        labels = [f"Mục {i+1}" for i in range(length)]
        mapping = { 
            0: "Đơn hàng sản phẩm", 
            1: "Đánh giá",
            5: "Đặt hàng trước",
            6: "Đơn hàng dịch vụ", 
            7: "Khiếu nại", 
            8: "Tin nhắn" 
        }
        for idx, name in mapping.items():
            if idx < length: labels[idx] = name
        return labels
    
    @staticmethod
    def get_icon(label: str) -> str:
        low = label.lower()
        if "sản phẩm" in low: return "📦"
        if "khiếu nại" in low: return "⚠️"
        if "đánh giá" in low: return "⭐"
        if "tin nhắn" in low: return "✉️"
        if "đặt hàng trước" in low: return "⏳"
        if "dịch vụ" in low: return "🛎️"
        return "🔹"

class AccountProcessor:
    def __init__(self, account_data: dict):
        self.id = account_data['id']
        self.name = account_data.get('name') or account_data.get('account_name') or 'Unknown'
        self.bot_token = account_data['bot_token']
        self.notify_config = Utils.parse_curl(account_data['notify_curl'])
        self.chat_config = Utils.parse_curl(account_data['chat_curl'])
        self.last_notify_nums = []
        self.seen_chat_dates = set()
        self.daily_date = ""
        # Flag để kiểm soát thông báo cookie
        self.cookie_alert_sent = False 

    def make_request(self, config):
        kwargs = {"headers": config.get("headers", {}), "verify": SystemConfig.VERIFY_TLS, "timeout": 25}
        if config.get("method") == "POST":
            if config.get("body_json"): kwargs["json"] = config["body_json"]
            elif config.get("body_data"): kwargs["data"] = config["body_data"].encode('utf-8')
        return requests.request(config.get("method", "GET"), config.get("url", ""), **kwargs)

    def fetch_chats(self, is_baseline=False) -> List[str]:
        if not self.chat_config.get("url"): return []
        try:
            r = self.make_request(self.chat_config)
            try: data = r.json()
            except: return []
            if not isinstance(data, list): return []
            new_msgs = []
            curr_ids = set()
            for chat in data:
                if not isinstance(chat, dict): continue
                uid = chat.get("guest_user", "Khách")
                msg = chat.get("last_chat", "")
                mid = chat.get("date") or hashlib.sha256(f"{uid}:{msg}".encode()).hexdigest()
                curr_ids.add(mid)
                
                # Chỉ thông báo tin nhắn nếu ID này chưa từng thấy (Tin nhắn mới/Chưa đọc với Bot)
                if mid not in self.seen_chat_dates:
                    self.seen_chat_dates.add(mid)
                    if not is_baseline: 
                        new_msgs.append(f"<b>✉️ {html.escape(uid)}:</b> <i>{html.escape(msg)}</i>")
            
            # Cập nhật lại set để tránh memory leak (giữ lại những cái đang có)
            self.seen_chat_dates.intersection_update(curr_ids)
            return new_msgs
        except: return []

    def check_notify(self, global_chat_id, is_baseline=False):
        if not self.notify_config.get("url"): return
        try:
            r = self.make_request(self.notify_config)
            text = (r.text or "").strip()
            
            # [LOGIC FIX] Kiểm tra Cookie hết hạn
            if "<html" in text.lower():
                # Chỉ thông báo 1 lần duy nhất cho đến khi có phản hồi hợp lệ lại
                if not self.cookie_alert_sent and not is_baseline:
                    self.send_tele(global_chat_id, f"⚠️ <b>[{html.escape(self.name)}] Cookie đã hết hạn!</b>\nVui lòng cập nhật ngay để bot tiếp tục hoạt động.")
                    self.cookie_alert_sent = True
                return
            
            # Nếu request thành công (không phải HTML lỗi), reset cờ báo lỗi
            self.cookie_alert_sent = False

            parsed = Utils.parse_notify_text(text)
            if "numbers" in parsed:
                nums = parsed["numbers"]
                today = datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d")
                if today != self.daily_date: self.daily_date = today
                if len(nums) != len(self.last_notify_nums): self.last_notify_nums = [0] * len(nums)
                labels = Utils.get_labels(len(nums))
                alerts = []
                has_change = False
                check_chat = False
                
                for i, val in enumerate(nums):
                    old = self.last_notify_nums[i]
                    lbl = labels[i]
                    
                    if "khiếu nại" in lbl.lower(): continue 

                    if val > old:
                        has_change = True
                        DB.update_stat(self.id, today, lbl, val - old)
                        if "tin nhắn" in lbl.lower(): check_chat = True
                    
                    if val > 0 and val > old:
                         alerts.append(f"{Utils.get_icon(lbl)} {lbl}: <b>{val}</b>")
                
                chat_msgs = self.fetch_chats(is_baseline) if check_chat else []
                
                if has_change and not is_baseline:
                    msg_lines = [f"⭐ <b>[{html.escape(self.name)}] - BIẾN ĐỘNG MỚI</b>"]
                    msg_lines.append("<code>---------------------------</code>")
                    
                    if alerts: msg_lines.extend(alerts)
                    if chat_msgs:
                        msg_lines.append("<b>💬 Tin nhắn chưa đọc:</b>")
                        msg_lines.extend(chat_msgs)
                    
                    self.send_tele(global_chat_id, "\n".join(msg_lines))
                
                self.last_notify_nums = nums
        except Exception as e: SYS_LOG.error(f"Err {self.name}: {e}")

    def send_tele(self, chat_id, text):
        if not self.bot_token or not chat_id: return
        api = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        chunks = [text[i:i+3900] for i in range(0, len(text), 3900)] or [""]
        for part in chunks[:3]:
            try: requests.post(api, json={"chat_id": chat_id, "text": part, "parse_mode": "HTML"}, timeout=15)
            except: pass

class BackgroundService:
    def __init__(self):
        self.processors = {}
        self.lock = threading.Lock()
    def reload_processors(self):
        with self.lock:
            db_accounts = DB.get_all_accounts()
            current_ids = set()
            for acc in db_accounts:
                aid = acc['id']
                current_ids.add(aid)
                if aid not in self.processors: self.processors[aid] = AccountProcessor(acc)
                else: 
                    old = self.processors[aid]
                    new = AccountProcessor(acc)
                    new.last_notify_nums = old.last_notify_nums
                    new.seen_chat_dates = old.seen_chat_dates
                    new.cookie_alert_sent = old.cookie_alert_sent # Giữ trạng thái alert
                    self.processors[aid] = new
            for aid in list(self.processors.keys()):
                if aid not in current_ids: del self.processors[aid]
    
    def broadcast_config_success(self, global_chat_id):
        """Gửi thông báo đến TỪNG bot đã cấu hình"""
        if not global_chat_id: return
        msg = (
            f"✅ <b>CẤU HÌNH THÀNH CÔNG!</b>\n"
            f"🤖 Bot đang hoạt động.\n"
            f"🕒 Time: {datetime.now().strftime('%H:%M:%S')}"
        )
        with self.lock:
            for proc in self.processors.values():
                # Gửi bằng chính token của shop đó
                proc.send_tele(global_chat_id, msg)

    def pinger_loop(self):
        while True:
            try:
                enabled = DB.get_setting("pinger_enabled") == "1"
                url = DB.get_setting("pinger_url")
                interval = int(DB.get_setting("pinger_interval", "300"))
                if enabled and url: requests.get(url, timeout=10)
                time.sleep(max(10, interval))
            except: time.sleep(60)
    def poller_loop(self):
        self.reload_processors()
        global_chat_id = DB.get_setting("global_chat_id")
        with self.lock:
            for proc in self.processors.values():
                proc.fetch_chats(is_baseline=True)
                proc.check_notify(global_chat_id, is_baseline=True)
        while True:
            try:
                interval = max(3, int(DB.get_setting("poll_interval", str(SystemConfig.DEFAULT_POLL_INTERVAL))))
                time.sleep(interval)
                global_chat_id = DB.get_setting("global_chat_id")
                if not global_chat_id: continue
                with self.lock: procs = list(self.processors.values())
                for proc in procs: proc.check_notify(global_chat_id)
            except Exception: time.sleep(60)

SERVICE = BackgroundService()

# ==============================================================================
# 5. API ROUTES
# ==============================================================================

app = FastAPI(docs_url=None, redoc_url=None)

def verify_session(session_id: str = Cookie(None)):
    if session_id != "admin_authorized":
        raise HTTPException(status_code=status.HTTP_307_TEMPORARY_REDIRECT, headers={"Location": "/login"})
    return True

@app.get("/login", response_class=HTMLResponse)
def login_page(): return HTML_LOGIN

@app.post("/login")
def login_action(secret: str = Form(...)):
    if secret.strip() == SystemConfig.ADMIN_SECRET:
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(key="session_id", value="admin_authorized", max_age=86400)
        return resp
    return HTMLResponse(content="<script>alert('❌ MẬT KHẨU SAI!'); window.location.href='/login';</script>")

@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("session_id")
    return resp

@app.get("/healthz")
def health(): return {"status": "ok"}

@app.get("/", response_class=HTMLResponse)
def root(authorized: bool = Depends(verify_session)): return HTML_DASHBOARD

@app.get("/api/config")
def get_config(authorized: bool = Depends(verify_session)):
    raw_accounts = DB.get_all_accounts()
    formatted_accounts = []
    for acc in raw_accounts:
        acc_dict = dict(acc)
        acc_dict['account_name'] = acc_dict['name']
        formatted_accounts.append(acc_dict)

    return {
        "global_chat_id": DB.get_setting("global_chat_id", ""),
        "poll_interval": int(DB.get_setting("poll_interval", "10")),
        "pinger": {
            "enabled": DB.get_setting("pinger_enabled") == "1",
            "url": DB.get_setting("pinger_url", ""),
            "interval": int(DB.get_setting("pinger_interval", "300"))
        },
        "accounts": formatted_accounts
    }

@app.post("/api/config")
async def save_config(req: Request, authorized: bool = Depends(verify_session)):
    data = await req.json()
    global_chat_id = data.get("global_chat_id", "")
    DB.set_setting("global_chat_id", global_chat_id)
    DB.set_setting("poll_interval", str(data.get("poll_interval", 10)))
    pinger = data.get("pinger", {})
    DB.set_setting("pinger_enabled", "1" if pinger.get("enabled") else "0")
    DB.set_setting("pinger_url", pinger.get("url", ""))
    DB.set_setting("pinger_interval", str(pinger.get("interval", 300)))
    
    incoming_accs = data.get("accounts", {})
    current_ids = {a['id'] for a in DB.get_all_accounts()}
    incoming_ids = set(incoming_accs.keys())
    for aid in current_ids:
        if aid not in incoming_ids: DB.delete_account(aid)
    for aid, adata in incoming_accs.items():
        DB.save_account(aid, adata)
    
    SERVICE.reload_processors()
    
    # Auto Backup
    full_data = BackupManager.create_backup_data(clean_curl=False) 
    BackupManager.auto_backup_to_disk(full_data)
    
    # Gửi thông báo đến TỪNG bot
    threading.Thread(target=SERVICE.broadcast_config_success, args=(global_chat_id,)).start()
    
    return {"status": "success"}

@app.get("/api/stats")
def get_stats(authorized: bool = Depends(verify_session)):
    conn = DB.get_connection()
    # Chỉ lấy đơn hàng
    rows = conn.execute("SELECT date, SUM(count) as total FROM stats WHERE category LIKE '%Đơn hàng%' GROUP BY date ORDER BY date DESC LIMIT 7").fetchall()
    conn.close()
    labels = []; data = []
    for r in reversed(rows):
        labels.append(r['date'])
        data.append(r['total'])
    return {"labels": labels, "data": data}

@app.get("/api/backup/download")
def download_backup(authorized: bool = Depends(verify_session)):
    data = BackupManager.create_backup_data(clean_curl=True)
    filename = f"galaxy_backup_clean_{datetime.now().strftime('%Y%m%d')}.json"
    temp_path = f"/tmp/{filename}"
    with open(temp_path, 'w', encoding='utf-8') as f: json.dump(data, f, indent=4)
    return FileResponse(path=temp_path, filename=filename, media_type='application/json')

@app.post("/api/backup/restore")
async def restore_backup(file: UploadFile = File(...), authorized: bool = Depends(verify_session)):
    try:
        content = await file.read()
        data = json.loads(content)
        DB.set_setting("global_chat_id", data.get("global_chat_id", ""))
        DB.set_setting("poll_interval", str(data.get("poll_interval", 10)))
        pinger = data.get("pinger", {})
        DB.set_setting("pinger_enabled", "1" if pinger.get("enabled") else "0")
        DB.set_setting("pinger_url", pinger.get("url", ""))
        DB.set_setting("pinger_interval", str(pinger.get("interval", 300)))
        accounts = data.get("accounts", {})
        all_old = DB.get_all_accounts()
        for old in all_old: DB.delete_account(old['id'])
        for aid, acc_data in accounts.items(): DB.save_account(aid, acc_data)
        SERVICE.reload_processors()
        return {"status": "success", "message": f"Đã khôi phục {len(accounts)} shop thành công!"}
    except Exception as e: return JSONResponse(status_code=400, content={"status": "error", "message": str(e)})

# ==============================================================================
# 6. FRONTEND (VIP PRO MAX UI)
# ==============================================================================

HTML_LOGIN = f"""
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>GALAXY ACCESS v30.0</title>
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700;900&family=Rajdhani:wght@400;600&display=swap" rel="stylesheet">
    <style>
        :root {{ --neon-blue: #00f3ff; --neon-purple: #bc13fe; --dark-bg: #050510; }}
        body {{ margin: 0; height: 100vh; background: var(--dark-bg); display: flex; justify-content: center; align-items: center; font-family: 'Rajdhani', sans-serif; overflow: hidden; }}
        .stars {{ position: fixed; inset: 0; z-index: -1; background: radial-gradient(circle at center, #1a1a3a 0%, #000 100%); }}
        .glass-panel {{ background: rgba(255, 255, 255, 0.03); backdrop-filter: blur(20px); border: 1px solid rgba(255, 255, 255, 0.1); padding: 50px; border-radius: 20px; box-shadow: 0 0 50px rgba(0, 243, 255, 0.1); width: 320px; text-align: center; animation: float 6s ease-in-out infinite; }}
        @keyframes float {{ 0%, 100% {{ transform: translateY(0); }} 50% {{ transform: translateY(-10px); }} }}
        h1 {{ font-family: 'Orbitron'; background: linear-gradient(90deg, var(--neon-blue), var(--neon-purple)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; font-size: 2rem; margin-bottom: 40px; text-transform: uppercase; letter-spacing: 2px; }}
        .input-group {{ position: relative; margin-bottom: 30px; }}
        input {{ width: 100%; padding: 15px; background: rgba(0,0,0,0.5); border: 1px solid rgba(255,255,255,0.1); color: #fff; border-radius: 8px; font-size: 1.1rem; text-align: center; outline: none; transition: 0.3s; box-sizing: border-box; font-family: 'Orbitron'; letter-spacing: 3px; }}
        input:focus {{ border-color: var(--neon-blue); box-shadow: 0 0 20px rgba(0, 243, 255, 0.3); }}
        button {{ width: 100%; padding: 15px; background: linear-gradient(90deg, var(--neon-blue), var(--neon-purple)); border: none; color: #fff; font-weight: 900; border-radius: 8px; cursor: pointer; font-size: 1.2rem; font-family: 'Orbitron'; transition: 0.3s; text-transform: uppercase; }}
        button:hover {{ transform: scale(1.05); box-shadow: 0 0 30px rgba(188, 19, 254, 0.6); letter-spacing: 1px; }}
        .footer {{ margin-top: 30px; color: rgba(255,255,255,0.3); font-size: 0.8rem; }}
    </style>
</head>
<body>
    <div class="stars"></div>
    <div class="glass-panel">
        <h1>Galaxy<br>Access</h1>
        <form action="/login" method="POST">
            <div class="input-group">
                <input type="password" name="secret" placeholder="••••••••" required autofocus>
            </div>
            <button type="submit">Unlock System</button>
        </form>
        <div class="footer">SECURE CONNECTION ESTABLISHED</div>
    </div>
</body>
</html>
"""

HTML_DASHBOARD = f"""
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>GALAXY ENTERPRISE - VIP Dashboard</title>
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;600;900&family=Rajdhani:wght@400;600;700&display=swap" rel="stylesheet">
    <style>
        :root {{ --neon-blue: #00f3ff; --neon-pink: #bc13fe; --glass: rgba(255, 255, 255, 0.05); --border: rgba(255, 255, 255, 0.1); --text: #ffffff; --bg: #050510; }}
        * {{ box-sizing: border-box; outline: none; }}
        body {{ margin: 0; background-color: var(--bg); color: var(--text); font-family: 'Rajdhani', sans-serif; min-height: 100vh; overflow-x: hidden; }}
        #particles {{ position: fixed; top: 0; left: 0; width: 100%; height: 100%; z-index: -1; pointer-events: none; }}
        
        .container {{ max-width: 1400px; margin: 0 auto; padding: 20px; }}
        
        /* HEADER */
        header {{ display: flex; justify-content: space-between; align-items: center; padding: 20px 0; border-bottom: 1px solid var(--border); margin-bottom: 30px; }}
        .brand {{ font-family: 'Orbitron'; font-size: 2.2rem; font-weight: 900; text-transform: uppercase; background: linear-gradient(90deg, var(--neon-blue), var(--neon-pink)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; letter-spacing: 2px; text-shadow: 0 0 20px rgba(0, 243, 255, 0.3); }}
        .user-panel {{ display: flex; gap: 20px; align-items: center; }}
        .badge {{ background: rgba(0, 243, 255, 0.1); border: 1px solid var(--neon-blue); color: var(--neon-blue); padding: 5px 15px; border-radius: 20px; font-weight: 600; font-size: 0.9rem; letter-spacing: 1px; }}
        .btn-logout {{ text-decoration: none; color: #fff; opacity: 0.7; font-weight: 600; transition: 0.3s; font-family: 'Orbitron'; }}
        .btn-logout:hover {{ opacity: 1; color: var(--neon-pink); }}

        /* SECTIONS */
        .section-title {{ font-family: 'Orbitron'; font-size: 1.4rem; color: var(--neon-blue); margin-bottom: 20px; display: flex; align-items: center; gap: 10px; }}
        .section-title::before {{ content: ''; display: block; width: 5px; height: 25px; background: var(--neon-pink); box-shadow: 0 0 10px var(--neon-pink); }}
        
        .grid-layout {{ display: grid; grid-template-columns: 2fr 1fr; gap: 30px; margin-bottom: 40px; }}
        @media (max-width: 1000px) {{ .grid-layout {{ grid-template-columns: 1fr; }} }}

        .card {{ background: var(--glass); backdrop-filter: blur(10px); border: 1px solid var(--border); border-radius: 15px; padding: 25px; transition: 0.3s; position: relative; overflow: hidden; }}
        .card::before {{ content: ''; position: absolute; top: 0; left: 0; width: 100%; height: 2px; background: linear-gradient(90deg, transparent, var(--neon-blue), transparent); opacity: 0.5; }}
        .card:hover {{ border-color: rgba(255,255,255,0.2); transform: translateY(-2px); box-shadow: 0 10px 30px rgba(0,0,0,0.5); }}

        /* FORMS */
        .form-group {{ margin-bottom: 20px; }}
        label {{ display: block; color: rgba(255,255,255,0.6); margin-bottom: 8px; font-weight: 600; letter-spacing: 0.5px; font-size: 0.9rem; }}
        input, select, textarea {{ width: 100%; background: rgba(0,0,0,0.4); border: 1px solid var(--border); color: #fff; padding: 12px 15px; border-radius: 8px; font-family: monospace; transition: 0.3s; font-size: 1rem; }}
        input:focus, textarea:focus {{ border-color: var(--neon-blue); box-shadow: 0 0 15px rgba(0, 243, 255, 0.2); }}

        /* BUTTONS */
        .btn {{ padding: 12px 25px; border: none; border-radius: 6px; cursor: pointer; font-family: 'Orbitron'; font-weight: bold; font-size: 0.9rem; transition: 0.3s; text-transform: uppercase; color: #fff; display: inline-flex; align-items: center; justify-content: center; gap: 8px; }}
        .btn-primary {{ background: linear-gradient(135deg, var(--neon-blue), #0066ff); box-shadow: 0 4px 15px rgba(0, 102, 255, 0.4); }}
        .btn-primary:hover {{ transform: scale(1.02); box-shadow: 0 0 25px var(--neon-blue); }}
        .btn-success {{ background: linear-gradient(135deg, #00ff99, #00cc66); color: #000; }}
        .btn-danger {{ background: rgba(255, 50, 50, 0.1); border: 1px solid #ff3333; color: #ff3333; padding: 8px 15px; font-size: 0.8rem; }}
        .btn-danger:hover {{ background: #ff3333; color: white; }}
        .btn-add {{ background: transparent; border: 1px dashed var(--neon-blue); color: var(--neon-blue); width: 100%; padding: 15px; margin-bottom: 20px; }}
        .btn-add:hover {{ background: rgba(0, 243, 255, 0.1); box-shadow: 0 0 20px rgba(0, 243, 255, 0.2); }}

        /* ACCOUNT CARDS */
        .accounts-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(350px, 1fr)); gap: 20px; }}
        .acc-card {{ background: rgba(0,0,0,0.4); border: 1px solid var(--border); padding: 20px; border-radius: 10px; position: relative; }}
        .acc-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; border-bottom: 1px solid var(--border); padding-bottom: 10px; }}
        .acc-title {{ font-family: 'Orbitron'; color: var(--neon-pink); font-size: 1.1rem; }}
        
        /* CHART */
        .chart-wrap {{ height: 250px; display: flex; align-items: flex-end; gap: 8px; padding-top: 20px; }}
        .chart-bar {{ flex: 1; background: linear-gradient(to top, rgba(0, 243, 255, 0.2), var(--neon-blue)); border-radius: 4px 4px 0 0; position: relative; min-height: 4px; transition: height 1s cubic-bezier(0.4, 0, 0.2, 1); box-shadow: 0 0 10px rgba(0, 243, 255, 0.2); }}
        .chart-val {{ position: absolute; top: -25px; width: 100%; text-align: center; font-weight: bold; color: #fff; font-size: 0.9rem; }}
        .chart-lbl {{ position: absolute; bottom: -30px; width: 100%; text-align: center; color: rgba(255,255,255,0.5); font-size: 0.75rem; transform: rotate(-45deg); }}

        /* TOAST */
        #toast-container {{ position: fixed; top: 20px; right: 20px; z-index: 9999; }}
        .toast {{ background: rgba(0, 0, 0, 0.9); border-left: 4px solid var(--neon-blue); color: #fff; padding: 15px 25px; margin-bottom: 10px; border-radius: 5px; box-shadow: 0 5px 15px rgba(0,0,0,0.5); display: flex; align-items: center; gap: 15px; transform: translateX(120%); transition: transform 0.4s cubic-bezier(0.68, -0.55, 0.27, 1.55); min-width: 300px; backdrop-filter: blur(5px); border: 1px solid rgba(255,255,255,0.1); }}
        .toast.show {{ transform: translateX(0); }}
        
        /* FOOTER */
        .footer {{ text-align: center; margin-top: 50px; padding: 20px; border-top: 1px solid var(--border); color: rgba(255,255,255,0.3); font-size: 0.9rem; }}
        
        /* LOADER */
        #loader {{ position: fixed; inset: 0; background: #000; z-index: 10000; display: flex; justify-content: center; align-items: center; transition: opacity 0.5s; }}
        .hex-spinner {{ width: 60px; height: 60px; border: 2px solid var(--neon-blue); border-radius: 50%; border-top-color: transparent; animation: spin 1s infinite linear; box-shadow: 0 0 20px var(--neon-blue); }}
        @keyframes spin {{ 100% {{ transform: rotate(360deg); }} }}
    </style>
</head>
<body>
    <div id="loader"><div class="hex-spinner"></div></div>
    <canvas id="particles"></canvas>
    
    <div id="toast-container"></div>

    <div class="container">
        <header>
            <div class="brand">Galaxy Enterprise</div>
            <div class="user-panel">
                <span class="badge">● ADMIN ACCESS</span>
                <a href="/logout" class="btn-logout">LOGOUT</a>
            </div>
        </header>

        <div class="grid-layout">
            <div class="card">
                <div class="section-title">THỐNG KÊ ĐƠN HÀNG (7 NGÀY)</div>
                <div id="chart-area" class="chart-wrap"></div>
            </div>
            
            <div class="card">
                <div class="section-title">CẤU HÌNH CHUNG</div>
                <div class="form-group">
                    <label>TELEGRAM MASTER ID</label>
                    <input type="text" id="gid" placeholder="Nhập ID Admin nhận tin tổng...">
                </div>
                <div style="display: flex; gap: 15px;">
                    <div style="flex: 1;">
                        <label>QUÉT (Giây)</label>
                        <input type="number" id="poll_int" value="10">
                    </div>
                </div>
                <div style="margin-top: 20px; border: 1px dashed var(--border); padding: 15px; border-radius: 8px;">
                    <label style="color: var(--neon-blue);">ANTI-SLEEP (PINGER)</label>
                    <div style="display: flex; gap: 10px; margin-bottom: 10px;">
                        <select id="p_enable" style="width: 80px;"><option value="0">OFF</option><option value="1">ON</option></select>
                        <input type="number" id="p_interval" placeholder="Giây" style="flex: 1;">
                    </div>
                    <input type="text" id="p_url" placeholder="https://your-app.onrender.com">
                </div>
            </div>
        </div>

        <form id="mainForm">
            <div class="card">
                <div class="section-title">QUẢN LÝ SHOP</div>
                <button type="button" class="btn btn-add" onclick="addAccount()">+ THÊM SHOP MỚI</button>
                <div id="acc_list" class="accounts-grid"></div>
            </div>

            <div style="position: sticky; bottom: 20px; z-index: 90; display: flex; justify-content: center; margin-top: 30px; pointer-events: none;">
                <button type="submit" class="btn btn-primary" style="padding: 15px 50px; font-size: 1.1rem; pointer-events: auto; box-shadow: 0 0 30px rgba(0,0,0,0.8);">
                    💾 LƯU CẤU HÌNH HỆ THỐNG
                </button>
            </div>
        </form>

        <div class="card" style="margin-top: 40px;">
            <div class="section-title">BACKUP & RESTORE</div>
            <div style="display: flex; gap: 20px; flex-wrap: wrap;">
                <a href="/api/backup/download" target="_blank" class="btn btn-success" style="text-decoration: none; flex: 1; text-align: center;">⬇️ TẢI BACKUP (JSON)</a>
                <div style="flex: 1; position: relative;">
                    <input type="file" id="restoreFile" accept=".json" style="position: absolute; opacity: 0; width: 100%; height: 100%; cursor: pointer;">
                    <button type="button" class="btn" style="width: 100%; background: rgba(255,255,255,0.1); border: 1px dashed #fff;">📂 CHỌN FILE RESTORE...</button>
                </div>
                <button type="button" onclick="doRestore()" class="btn" style="background: #ffaa00; color: #000; flex: 0 0 150px;">KHÔI PHỤC</button>
            </div>
        </div>

        <div class="footer">
            POWERED BY GALAXY CORE v30.0<br>
            DESIGNED BY ADMIN VAN LINH
        </div>
    </div>

    <script>
        // --- PARTICLE BACKGROUND ---
        const canvas = document.getElementById('particles');
        const ctx = canvas.getContext('2d');
        let w, h, particles = [];
        const resize = () => {{ w = canvas.width = window.innerWidth; h = canvas.height = window.innerHeight; }};
        window.addEventListener('resize', resize);
        
        class Particle {{
            constructor() {{ this.reset(); }}
            reset() {{ this.x = Math.random() * w; this.y = Math.random() * h; this.vx = (Math.random() - 0.5) * 0.5; this.vy = (Math.random() - 0.5) * 0.5; this.size = Math.random() * 2; this.alpha = Math.random() * 0.5 + 0.1; }}
            update() {{ this.x += this.vx; this.y += this.vy; if (this.x < 0 || this.x > w || this.y < 0 || this.y > h) this.reset(); }}
            draw() {{ ctx.fillStyle = `rgba(0, 243, 255, ${{this.alpha}})`; ctx.beginPath(); ctx.arc(this.x, this.y, this.size, 0, Math.PI * 2); ctx.fill(); }}
        }}
        
        const initParticles = () => {{ resize(); for(let i=0; i<100; i++) particles.push(new Particle()); loop(); }};
        const loop = () => {{ ctx.clearRect(0,0,w,h); particles.forEach(p => {{ p.update(); p.draw(); }}); requestAnimationFrame(loop); }};
        initParticles();

        // --- TOAST NOTIFICATION ---
        function showToast(msg, type='info') {{
            const c = document.getElementById('toast-container');
            const t = document.createElement('div');
            t.className = 'toast';
            t.innerHTML = `<span>${{type==='error'?'❌':'✅'}}</span><div>${{msg}}</div>`;
            c.appendChild(t);
            setTimeout(() => t.classList.add('show'), 10);
            setTimeout(() => {{ t.classList.remove('show'); setTimeout(() => t.remove(), 400); }}, 3000);
        }}

        // --- LOGIC ---
        const api = {{
            getConfig: async () => (await fetch('/api/config')).json(),
            saveConfig: async (d) => (await fetch('/api/config', {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }}, body: JSON.stringify(d) }})).json(),
            getStats: async () => (await fetch('/api/stats')).json()
        }};

        function renderAccount(id, d = {{}}) {{
            const el = document.createElement('div'); el.className = 'acc-card'; el.dataset.id = id;
            el.innerHTML = `
                <div class="acc-header">
                    <span class="acc-title">SHOP: ${{d.account_name || 'Chưa đặt tên'}}</span>
                    <button type="button" class="btn btn-danger" onclick="this.closest('.acc-card').remove()">XOÁ</button>
                </div>
                <div class="form-group">
                    <label>TÊN SHOP (TapHoaMMO)</label>
                    <input type="text" class="acc-name" value="${{d.account_name || ''}}" required placeholder="Nhập tên user...">
                </div>
                <div class="form-group">
                    <label>BOT TOKEN</label>
                    <input type="password" class="acc-token" value="${{d.bot_token || ''}}" required placeholder="123456:ABC-DEF...">
                </div>
                <div class="form-group">
                    <label>CURL NOTIFY</label>
                    <textarea class="acc-notify" rows="2" placeholder="Paste cURL check thông báo vào đây...">${{d.notify_curl || ''}}</textarea>
                </div>
                <div class="form-group">
                    <label>CURL CHAT</label>
                    <textarea class="acc-chat" rows="2" placeholder="Paste cURL check tin nhắn vào đây...">${{d.chat_curl || ''}}</textarea>
                </div>
            `;
            document.getElementById('acc_list').appendChild(el);
        }}

        function addAccount() {{ renderAccount(crypto.randomUUID()); }}

        async function init() {{
            try {{
                const conf = await api.getConfig();
                document.getElementById('gid').value = conf.global_chat_id;
                document.getElementById('poll_int').value = conf.poll_interval;
                document.getElementById('p_enable').value = conf.pinger.enabled ? "1" : "0";
                document.getElementById('p_url').value = conf.pinger.url;
                document.getElementById('p_interval').value = conf.pinger.interval;
                
                const list = document.getElementById('acc_list'); list.innerHTML = '';
                (conf.accounts || []).forEach(a => renderAccount(a.id, a));

                const stats = await api.getStats();
                const chart = document.getElementById('chart-area');
                if (stats.data && stats.data.length) {{
                    const max = Math.max(...stats.data, 5);
                    chart.innerHTML = '';
                    stats.data.forEach((val, i) => {{
                        const h = Math.max((val / max) * 100, 5);
                        const bar = document.createElement('div');
                        bar.className = 'chart-bar';
                        bar.style.height = `${{h}}%`;
                        bar.innerHTML = `<div class="chart-val">${{val}}</div><div class="chart-lbl">${{stats.labels[i].slice(5)}}</div>`;
                        chart.appendChild(bar);
                    }});
                }} else {{
                    chart.innerHTML = '<div style="width:100%; text-align:center; color:rgba(255,255,255,0.3);">Chưa có dữ liệu</div>';
                }}
            }} catch (e) {{
                console.error(e); showToast('Không thể tải dữ liệu', 'error');
            }} finally {{
                const l = document.getElementById('loader');
                l.style.opacity = '0'; setTimeout(() => l.remove(), 500);
            }}
        }}

        document.getElementById('mainForm').onsubmit = async (e) => {{
            e.preventDefault();
            const accounts = {{}};
            document.querySelectorAll('.acc-card').forEach(el => {{
                const id = el.dataset.id;
                accounts[id] = {{
                    account_name: el.querySelector('.acc-name').value,
                    bot_token: el.querySelector('.acc-token').value,
                    notify_curl: el.querySelector('.acc-notify').value,
                    chat_curl: el.querySelector('.acc-chat').value
                }};
            }});
            const payload = {{
                global_chat_id: document.getElementById('gid').value,
                poll_interval: parseInt(document.getElementById('poll_int').value),
                pinger: {{
                    enabled: document.getElementById('p_enable').value === "1",
                    url: document.getElementById('p_url').value,
                    interval: parseInt(document.getElementById('p_interval').value)
                }},
                accounts: accounts
            }};
            
            showToast('Đang lưu cấu hình...', 'info');
            try {{
                await api.saveConfig(payload);
                showToast('LƯU THÀNH CÔNG! Bot đang khởi động lại...', 'success');
                setTimeout(() => location.reload(), 2000);
            }} catch (e) {{
                showToast('Lỗi khi lưu: ' + e, 'error');
            }}
        }};
        
        // File input custom logic
        document.getElementById('restoreFile').addEventListener('change', function() {{
            const btn = this.nextElementSibling;
            if(this.files.length) btn.innerText = "📄 " + this.files[0].name;
        }});

        async function doRestore() {{
            const fileInput = document.getElementById('restoreFile');
            if(!fileInput.files.length) {{ showToast('Chưa chọn file backup!', 'error'); return; }}
            if(!confirm('Dữ liệu hiện tại sẽ bị ghi đè. Tiếp tục?')) return;
            
            const formData = new FormData();
            formData.append('file', fileInput.files[0]);
            
            try {{
                const res = await fetch('/api/backup/restore', {{ method: 'POST', body: formData }});
                const result = await res.json();
                if(result.status === 'success') {{
                    showToast(result.message, 'success');
                    setTimeout(() => location.reload(), 1500);
                }} else {{ showToast('Lỗi: ' + result.message, 'error'); }}
            }} catch(e) {{ showToast('Lỗi upload: ' + e, 'error'); }}
        }}

        init();
    </script>
</body>
</html>
"""

# ==============================================================================
# 7. RUNTIME
# ==============================================================================

if not SystemConfig.DISABLE_POLLER:
    t1 = threading.Thread(target=SERVICE.poller_loop, daemon=True); t1.start()
    t2 = threading.Thread(target=SERVICE.pinger_loop, daemon=True); t2.start()

if __name__ == "__main__":
    import uvicorn
    print(f"🌌 GALAXY ENTERPRISE v{SystemConfig.VERSION} STARTING...")
    uvicorn.run("server:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
