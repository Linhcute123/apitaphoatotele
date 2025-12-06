"""
PROJECT: TAPHOAMMO GALAXY ENTERPRISE
VERSION: 33.0 (ULTIMATE UI - MATCHING SCREENSHOTS)
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
# 1. CONFIGURATION & TIMEZONE
# ==============================================================================

class SystemConfig:
    APP_NAME = "TapHoaMMO Enterprise"
    VERSION = "33.0.0"
    DATABASE_FILE = "galaxy_data.db"
    LOG_FILE = "system_run.log"
    ADMIN_SECRET = os.getenv("ADMIN_SECRET", "admin").strip()
    BACKUP_DIR = os.getenv("BACKUP_DIR", "") 
    DEFAULT_POLL_INTERVAL = 10
    VERIFY_TLS = bool(int(os.getenv("VERIFY_TLS", "1")))
    DISABLE_POLLER = os.getenv("DISABLE_POLLER", "0") == "1"

# TIMEZONE VIETNAM (UTC+7)
VN_TZ = timezone(timedelta(hours=7))

def get_vn_time():
    return datetime.now(VN_TZ)

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
        formatter.converter = lambda *args: get_vn_time().timetuple()
        
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
            "meta": {"version": SystemConfig.VERSION, "date": get_vn_time().strftime("%Y-%m-%d %H:%M:%S"), "type": "clean" if clean_curl else "full"},
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
            filename = f"auto_backup_{get_vn_time().strftime('%Y%m%d_%H%M%S')}.json"
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
                if mid not in self.seen_chat_dates:
                    self.seen_chat_dates.add(mid)
                    if not is_baseline: 
                        new_msgs.append(f"<b>✉️ {html.escape(uid)}:</b> <i>{html.escape(msg)}</i>")
            self.seen_chat_dates.intersection_update(curr_ids)
            return new_msgs
        except: return []

    def check_notify(self, global_chat_id, is_baseline=False):
        if not self.notify_config.get("url"): return
        try:
            r = self.make_request(self.notify_config)
            text = (r.text or "").strip()
            
            if "<html" in text.lower():
                if not self.cookie_alert_sent and not is_baseline:
                    self.send_tele(global_chat_id, f"⚠️ <b>[{html.escape(self.name)}] Cookie đã hết hạn!</b>\nVui lòng cập nhật ngay.")
                    self.cookie_alert_sent = True
                return
            
            self.cookie_alert_sent = False
            parsed = Utils.parse_notify_text(text)
            
            if "numbers" in parsed:
                nums = parsed["numbers"]
                today = get_vn_time().strftime("%Y-%m-%d")
                
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
                        diff = val - old
                        # Update Stats DB
                        cat_code = 'msg' if "tin nhắn" in lbl.lower() else ('order' if "đơn hàng" in lbl.lower() else 'other')
                        DB.update_stat(self.id, today, cat_code, diff)
                        
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
                    new.cookie_alert_sent = old.cookie_alert_sent 
                    self.processors[aid] = new
            for aid in list(self.processors.keys()):
                if aid not in current_ids: del self.processors[aid]
    
    def broadcast_config_success(self, global_chat_id):
        if not global_chat_id: return
        # Format lại tin nhắn khởi động theo yêu cầu mới
        msg = (
            f"🚀 <b>HỆ THỐNG ĐÃ KHỞI ĐỘNG!</b> 🚀\n\n"
            f"👑 <b>Bot đã sẵn sàng phục vụ Chủ Nhân!</b>\n"
            f"💎 Trạng thái: <code>ONLINE</code>\n\n"
            f"<i>Chúc Chủ Nhân một ngày bão đơn! 💸💸💸</i>"
        )
        with self.lock:
            for proc in self.processors.values():
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
    
    full_data = BackupManager.create_backup_data(clean_curl=False) 
    BackupManager.auto_backup_to_disk(full_data)
    threading.Thread(target=SERVICE.broadcast_config_success, args=(global_chat_id,)).start()
    
    return {"status": "success"}

@app.get("/api/stats")
def get_stats(authorized: bool = Depends(verify_session)):
    conn = DB.get_connection()
    # Lấy dữ liệu 7 ngày gần nhất cho đơn hàng và tin nhắn
    rows = conn.execute("SELECT date, category, SUM(count) as total FROM stats GROUP BY date, category ORDER BY date DESC LIMIT 21").fetchall()
    conn.close()
    
    dates = []
    orders = {}
    msgs = {}
    
    for r in rows:
        d = r['date']
        c = r['category']
        t = r['total']
        if d not in dates: dates.append(d)
        if c == 'order': orders[d] = t
        if c == 'msg': msgs[d] = t
    
    dates.sort()
    
    order_data = [orders.get(d, 0) for d in dates]
    msg_data = [msgs.get(d, 0) for d in dates]
    
    total_orders = sum(order_data)
    total_msgs = sum(msg_data)
    
    return {
        "labels": dates,
        "datasets": {
            "orders": order_data,
            "msgs": msg_data
        },
        "totals": {
            "orders": total_orders,
            "msgs": total_msgs
        }
    }

@app.get("/api/backup/download")
def download_backup(authorized: bool = Depends(verify_session)):
    data = BackupManager.create_backup_data(clean_curl=True)
    filename = f"galaxy_backup_clean_{get_vn_time().strftime('%Y%m%d')}.json"
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
# 6. FRONTEND (ULTIMATE UI)
# ==============================================================================

HTML_LOGIN = f"""
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>GALAXY ACCESS</title>
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700;900&family=Rajdhani:wght@400;600&display=swap" rel="stylesheet">
    <style>
        :root {{ --neon-blue: #00f3ff; --neon-purple: #bc13fe; --dark-bg: #0b0b15; }}
        body {{ margin: 0; height: 100vh; background: var(--dark-bg); display: flex; justify-content: center; align-items: center; font-family: 'Rajdhani', sans-serif; overflow: hidden; }}
        .stars {{ position: fixed; inset: 0; z-index: -1; background: radial-gradient(circle at center, #1a1a3a 0%, #000 100%); }}
        .login-box {{ background: rgba(16, 16, 28, 0.8); border: 1px solid rgba(255, 255, 255, 0.1); padding: 60px 40px; border-radius: 30px; box-shadow: 0 0 60px rgba(0, 243, 255, 0.05); width: 400px; text-align: center; backdrop-filter: blur(10px); }}
        .logo {{ font-family: 'Orbitron'; font-weight: 900; font-size: 2.2rem; color: #fff; text-transform: uppercase; margin-bottom: 40px; letter-spacing: 2px; text-shadow: 0 0 15px var(--neon-blue); }}
        .logo span {{ color: var(--neon-blue); }}
        input {{ width: 100%; padding: 18px; background: #08080c; border: 1px solid #333; color: #fff; border-radius: 8px; font-size: 1rem; text-align: center; outline: none; margin-bottom: 20px; transition: 0.3s; box-sizing: border-box; }}
        input:focus {{ border-color: var(--neon-purple); box-shadow: 0 0 15px rgba(188, 19, 254, 0.2); }}
        button {{ width: 100%; padding: 18px; background: linear-gradient(90deg, var(--neon-blue), var(--neon-purple)); border: none; color: #fff; font-weight: 800; border-radius: 8px; cursor: pointer; font-size: 1.1rem; font-family: 'Orbitron'; transition: 0.3s; text-transform: uppercase; letter-spacing: 1px; }}
        button:hover {{ transform: scale(1.02); box-shadow: 0 0 30px rgba(0, 243, 255, 0.4); }}
        .copy {{ margin-top: 30px; color: #555; font-size: 0.8rem; }}
    </style>
</head>
<body>
    <div class="stars"></div>
    <div class="login-box">
        <div class="logo">GALAXY <span>ACCESS</span></div>
        <form action="/login" method="POST">
            <input type="password" name="secret" placeholder="NHẬP MÃ BẢO MẬT" required autofocus>
            <button type="submit">MỞ KHÓA HỆ THỐNG</button>
        </form>
        <div class="copy">Bản quyền thuộc về Admin Văn Linh</div>
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
    <title>GALAXY ENTERPRISE</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;600;900&family=Rajdhani:wght@400;600;700&display=swap" rel="stylesheet">
    <style>
        :root {{ --neon-cyan: #00f3ff; --neon-pink: #bc13fe; --bg-dark: #050510; --card-bg: rgba(255,255,255,0.03); --border: rgba(255,255,255,0.1); }}
        * {{ box-sizing: border-box; outline: none; }}
        body {{ margin: 0; background: var(--bg-dark); color: #fff; font-family: 'Rajdhani', sans-serif; min-height: 100vh; overflow-x: hidden; }}
        #bg-canvas {{ position: fixed; inset: 0; z-index: -1; }}
        
        .container {{ max-width: 1200px; margin: 0 auto; padding: 20px; }}
        
        /* HEADER */
        header {{ display: flex; justify-content: space-between; align-items: center; padding: 20px 0; border-bottom: 1px solid var(--border); margin-bottom: 30px; }}
        .brand {{ font-family: 'Orbitron'; font-size: 2rem; font-weight: 900; background: linear-gradient(90deg, var(--neon-cyan), var(--neon-pink)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
        .user-badge {{ border: 1px solid #0f0; color: #0f0; padding: 5px 15px; border-radius: 20px; font-weight: bold; font-size: 0.8rem; margin-right: 15px; }}
        .btn-logout {{ border: 1px solid #fff; color: #fff; padding: 5px 15px; text-decoration: none; font-size: 0.8rem; transition: 0.3s; }}
        .btn-logout:hover {{ background: #fff; color: #000; }}

        /* CHART SECTION */
        .chart-box {{ background: var(--card-bg); border: 1px solid var(--border); padding: 20px; border-radius: 10px; margin-bottom: 30px; }}
        .section-head {{ font-family: 'Orbitron'; font-size: 1.2rem; color: var(--neon-cyan); margin-bottom: 20px; border-left: 4px solid var(--neon-pink); padding-left: 10px; display: flex; align-items: center; gap: 10px; }}

        /* GENERAL SETTINGS */
        .settings-box {{ background: var(--card-bg); border: 1px solid var(--border); padding: 20px; border-radius: 10px; margin-bottom: 30px; }}
        .form-row {{ display: flex; gap: 20px; margin-bottom: 15px; }}
        .form-group {{ flex: 1; }}
        label {{ display: block; color: #aaa; margin-bottom: 8px; font-weight: 600; font-size: 0.85rem; text-transform: uppercase; }}
        input, select, textarea {{ width: 100%; background: #000; border: 1px solid #333; color: #fff; padding: 12px; border-radius: 5px; font-family: monospace; transition: 0.3s; }}
        input:focus {{ border-color: var(--neon-cyan); }}

        /* SHOP LIST (ACCORDION) */
        .shop-list-header {{ display: flex; justify-content: space-between; align-items: center; background: #111; padding: 15px 20px; border-radius: 8px; margin-bottom: 30px; border: 1px solid var(--border); }}
        .btn-add {{ background: linear-gradient(90deg, var(--neon-cyan), #00aaff); border: none; color: #fff; padding: 10px 25px; border-radius: 5px; cursor: pointer; font-weight: bold; font-family: 'Orbitron'; font-size: 0.9rem; box-shadow: 0 0 15px rgba(0, 243, 255, 0.3); }}
        
        .shop-item {{ margin-bottom: 15px; border: 1px solid var(--neon-pink); border-radius: 8px; background: rgba(20, 0, 40, 0.3); overflow: hidden; transition: 0.3s; }}
        .shop-item:hover {{ box-shadow: 0 0 15px rgba(188, 19, 254, 0.2); }}
        .shop-header {{ display: flex; justify-content: space-between; align-items: center; padding: 15px 20px; cursor: pointer; background: rgba(255,255,255,0.02); }}
        .shop-name {{ font-family: 'Orbitron'; color: var(--neon-pink); font-size: 1.2rem; letter-spacing: 1px; }}
        .btn-del {{ background: transparent; border: 1px solid #ff4444; color: #ff4444; padding: 8px 20px; border-radius: 4px; font-size: 0.9rem; cursor: pointer; transition: 0.3s; text-transform: uppercase; font-weight: bold; }}
        .btn-del:hover {{ background: #ff4444; color: #fff; }}
        .shop-body {{ padding: 20px; border-top: 1px solid rgba(188, 19, 254, 0.2); display: none; background: rgba(0,0,0,0.2); }}
        .shop-item.active .shop-body {{ display: block; animation: slideDown 0.3s ease; }}
        
        @keyframes slideDown {{ from {{ opacity: 0; transform: translateY(-10px); }} to {{ opacity: 1; transform: translateY(0); }} }}

        /* ACTION BAR */
        .action-bar {{ display: flex; justify-content: center; margin-top: 30px; margin-bottom: 50px; }}
        .btn-save {{ width: 100%; max-width: 400px; padding: 15px; background: linear-gradient(90deg, var(--neon-cyan), #0066ff); border: none; border-radius: 8px; color: #fff; font-family: 'Orbitron'; font-size: 1.1rem; font-weight: bold; cursor: pointer; box-shadow: 0 0 20px rgba(0, 243, 255, 0.3); }}
        .btn-save:hover {{ transform: scale(1.02); }}

        /* BACKUP SECTION */
        .backup-box {{ border-top: 1px solid #333; margin-top: 50px; padding-top: 30px; }}
        .backup-controls {{ display: flex; gap: 20px; }}
        .btn-backup {{ flex: 1; background: #222; border: 1px solid #555; color: #fff; padding: 15px; border-radius: 8px; cursor: pointer; text-decoration: none; display: block; text-align: center; font-weight: bold; transition: 0.3s; }}
        .btn-backup:hover {{ background: #333; border-color: #fff; }}
        .btn-restore {{ flex: 1; background: #e6a23c; border: none; color: #000; padding: 15px; border-radius: 8px; cursor: pointer; font-weight: bold; transition: 0.3s; }}
        .btn-restore:hover {{ filter: brightness(1.1); }}

        /* TOAST */
        #toast {{ position: fixed; top: 20px; right: 20px; background: #111; border-left: 4px solid var(--neon-cyan); color: #fff; padding: 15px 25px; border-radius: 5px; transform: translateX(150%); transition: 0.3s; z-index: 9999; box-shadow: 0 5px 20px rgba(0,0,0,0.5); }}
        #toast.show {{ transform: translateX(0); }}
    </style>
</head>
<body>
    <canvas id="bg-canvas"></canvas>
    <div id="toast">Thông báo hệ thống</div>

    <div class="container">
        <header>
            <div class="brand">GALAXY ENTERPRISE</div>
            <div>
                <span class="user-badge">● ADMIN VĂN LINH</span>
                <a href="/logout" class="btn-logout">ĐĂNG XUẤT</a>
            </div>
        </header>

        <div class="chart-box">
            <div class="section-head">📊 THỐNG KÊ ĐƠN HÀNG (7 NGÀY)</div>
            <canvas id="mainChart" style="width:100%; height:300px;"></canvas>
        </div>

        <form id="mainForm">
            <div class="settings-box">
                <div class="section-head">🔮 CẤU HÌNH CHUNG</div>
                <div class="form-group" style="margin-bottom: 20px;">
                    <label>TELEGRAM MASTER ID</label>
                    <input type="text" id="gid">
                </div>
                <div class="form-row">
                    <div class="form-group">
                        <label>TỐC ĐỘ QUÉT (Giây)</label>
                        <input type="number" id="poll_int" value="10">
                    </div>
                    <div class="form-group" style="border: 1px dashed var(--neon-cyan); padding: 10px; border-radius: 5px;">
                        <label style="color:var(--neon-cyan)">PINGER (CHỐNG NGỦ ĐÔNG)</label>
                        <div style="display:flex; gap:10px;">
                            <select id="p_enable" style="width:80px;"><option value="0">OFF</option><option value="1">ON</option></select>
                            <input type="number" id="p_interval" placeholder="300" style="flex:1">
                        </div>
                        <input type="text" id="p_url" placeholder="https://..." style="margin-top:5px;">
                    </div>
                </div>
            </div>

            <div class="shop-list-header">
                <div style="font-family:'Orbitron'; font-size:1.2rem; color: var(--neon-cyan);">🚀 DANH SÁCH SHOP</div>
                <button type="button" class="btn-add" onclick="addAccount()">+ THÊM SHOP</button>
            </div>
            
            <div id="acc_list"></div>

            <div class="action-bar">
                <button type="submit" class="btn-save">LƯU CẤU HÌNH</button>
            </div>
        </form>
        
        <div class="backup-box">
            <div class="section-head" style="border-left-color: #fff; color: #fff;">💾 BACKUP & RESTORE</div>
            <div class="backup-controls">
                <a href="/api/backup/download" target="_blank" class="btn-backup">⬇️ TẢI FILE BACKUP (JSON)</a>
                <input type="file" id="restoreFile" style="display:none;" onchange="doRestore(this)" accept=".json">
                <button type="button" class="btn-restore" onclick="document.getElementById('restoreFile').click()">⬆️ KHÔI PHỤC DỮ LIỆU</button>
            </div>
        </div>
        
        <div style="text-align:center; margin-top:50px; color:#555; font-size:0.8rem;">
            POWERED BY GALAXY CORE v33.0
        </div>
    </div>

    <script>
        // --- ANIMATED BG ---
        const canvas = document.getElementById('bg-canvas');
        const ctx = canvas.getContext('2d');
        let w, h, particles = [];
        const resize = () => {{ w = canvas.width = window.innerWidth; h = canvas.height = window.innerHeight; }};
        window.addEventListener('resize', resize);
        class P {{
            constructor() {{ this.reset(); }}
            reset() {{ this.x = Math.random() * w; this.y = Math.random() * h; this.vx = (Math.random() - 0.5) * 0.5; this.vy = (Math.random() - 0.5) * 0.5; this.s = Math.random() * 2; }}
            update() {{ this.x+=this.vx; this.y+=this.vy; if(this.x<0||this.x>w||this.y<0||this.y>h) this.reset(); }}
            draw() {{ ctx.fillStyle=`rgba(255,255,255,${{Math.random()*0.5}})`; ctx.beginPath(); ctx.arc(this.x,this.y,this.s,0,Math.PI*2); ctx.fill(); }}
        }}
        const initBg = () => {{ resize(); for(let i=0;i<100;i++) particles.push(new P()); loop(); }};
        const loop = () => {{ ctx.clearRect(0,0,w,h); particles.forEach(p=>{{ p.update(); p.draw(); }}); requestAnimationFrame(loop); }};
        initBg();

        // --- CHART JS ---
        let myChart;
        function renderChart(data) {{
            const ctx = document.getElementById('mainChart').getContext('2d');
            if(myChart) myChart.destroy();
            myChart = new Chart(ctx, {{
                type: 'line',
                data: {{
                    labels: data.labels,
                    datasets: [
                        {{ label: 'Đơn Hàng', data: data.datasets.orders, borderColor: '#00f3ff', tension: 0.4, fill: true, backgroundColor: 'rgba(0, 243, 255, 0.1)' }},
                        {{ label: 'Tin Nhắn', data: data.datasets.msgs, borderColor: '#bc13fe', tension: 0.4, fill: true, backgroundColor: 'rgba(188, 19, 254, 0.1)' }}
                    ]
                }},
                options: {{ 
                    responsive: true, maintainAspectRatio: false,
                    plugins: {{ legend: {{ labels: {{ color: '#fff' }} }} }},
                    scales: {{ x: {{ ticks: {{ color: '#aaa' }} }}, y: {{ ticks: {{ color: '#aaa' }}, grid: {{ color: '#333' }} }} }}
                }}
            }});
        }}

        // --- APP LOGIC ---
        const api = {{
            getConfig: async()=>(await fetch('/api/config')).json(),
            saveConfig: async(d)=>(await fetch('/api/config',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(d)}})).json(),
            getStats: async()=>(await fetch('/api/stats')).json()
        }};

        function toast(msg) {{
            const t = document.getElementById('toast');
            t.innerText = msg; t.classList.add('show');
            setTimeout(()=>t.classList.remove('show'), 3000);
        }}

        function toggleAcc(header) {{
            header.parentElement.classList.toggle('active');
        }}

        function renderAccount(id, d={{}}) {{
            const div = document.createElement('div'); div.className = 'shop-item'; div.dataset.id = id;
            div.innerHTML = `
                <div class="shop-header" onclick="toggleAcc(this)">
                    <div class="shop-name">SHOP: ${{d.account_name||'Mới'}}</div>
                    <button type="button" class="btn-del" onclick="event.stopPropagation(); this.closest('.shop-item').remove();">XOÁ</button>
                </div>
                <div class="shop-body">
                    <div class="form-row">
                        <div class="form-group"><label>TÊN SHOP:</label><input type="text" class="acc-name" value="${{d.account_name||''}}" oninput="this.closest('.shop-item').querySelector('.shop-name').innerText='SHOP: '+this.value"></div>
                        <div class="form-group"><label>TOKEN:</label><input type="password" class="acc-token" value="${{d.bot_token||''}}"></div>
                    </div>
                    <div class="form-group" style="margin-bottom:15px"><label>NOTIFY CURL:</label><textarea class="acc-notify" rows="2">${{d.notify_curl||''}}</textarea></div>
                    <div class="form-group"><label>CHAT CURL:</label><textarea class="acc-chat" rows="2">${{d.chat_curl||''}}</textarea></div>
                </div>
            `;
            document.getElementById('acc_list').appendChild(div);
        }}

        function addAccount() {{ renderAccount(crypto.randomUUID()); }}

        async function init() {{
            try {{
                const conf = await api.getConfig();
                document.getElementById('gid').value = conf.global_chat_id;
                document.getElementById('poll_int').value = conf.poll_interval;
                document.getElementById('p_enable').value = conf.pinger.enabled?'1':'0';
                document.getElementById('p_url').value = conf.pinger.url;
                document.getElementById('p_interval').value = conf.pinger.interval;
                document.getElementById('acc_list').innerHTML='';
                (conf.accounts||[]).forEach(a=>renderAccount(a.id, a));
                const stats = await api.getStats();
                renderChart(stats);
            }} catch(e) {{ console.error(e); }}
        }}

        document.getElementById('mainForm').onsubmit = async(e) => {{
            e.preventDefault();
            const accounts = {{}};
            document.querySelectorAll('.shop-item').forEach(el=>{{
                accounts[el.dataset.id] = {{
                    account_name: el.querySelector('.acc-name').value,
                    bot_token: el.querySelector('.acc-token').value,
                    notify_curl: el.querySelector('.acc-notify').value,
                    chat_curl: el.querySelector('.acc-chat').value
                }};
            }});
            const pl = {{
                global_chat_id: document.getElementById('gid').value,
                poll_interval: parseInt(document.getElementById('poll_int').value),
                pinger: {{
                    enabled: document.getElementById('p_enable').value==='1',
                    url: document.getElementById('p_url').value,
                    interval: parseInt(document.getElementById('p_interval').value)
                }},
                accounts: accounts
            }};
            toast('Đang lưu...');
            await api.saveConfig(pl);
            toast('Lưu thành công!');
            setTimeout(()=>location.reload(), 1500);
        }};

        async function doRestore(input) {{
            if(!input.files.length) return;
            if(!confirm('Dữ liệu hiện tại sẽ bị ghi đè! Tiếp tục?')) return;
            const fd = new FormData(); fd.append('file', input.files[0]);
            try {{
                const res = await fetch('/api/backup/restore', {{method:'POST',body:fd}});
                const d = await res.json();
                toast(d.status==='success'?'Khôi phục thành công!':'Lỗi: '+d.message);
                if(d.status==='success') setTimeout(()=>location.reload(), 1500);
            }} catch(e) {{ toast('Lỗi upload'); }}
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
