"""
PROJECT: TAPHOAMMO GALAXY ENTERPRISE
VERSION: 26.0 (Final Notification UI)
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
    from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
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
    VERSION = "26.0.0"
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
        mapping = { 0: "Đơn hàng sản phẩm", 1: "Đánh giá", 7: "Khiếu nại", 8: "Tin nhắn" }
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
        self.last_error_time = defaultdict(float)
        self.daily_date = ""

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
                    if not is_baseline: new_msgs.append(f"<b>✉️ {html.escape(uid)}:</b> <i>{html.escape(msg)}</i>")
            self.seen_chat_dates.intersection_update(curr_ids)
            return new_msgs
        except: return []

    def check_notify(self, global_chat_id, is_baseline=False):
        if not self.notify_config.get("url"): return
        try:
            r = self.make_request(self.notify_config)
            text = (r.text or "").strip()
            if "<html" in text.lower():
                if not is_baseline and (time.time() - self.last_error_time['html'] > 3600):
                    self.send_tele(global_chat_id, f"⚠️ <b>[{self.name}] Cookie hết hạn.</b>")
                    self.last_error_time['html'] = time.time()
                return
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
                         alerts.append(f"{Utils.get_icon(lbl)} {lbl}: 1") # Luôn hiện 1 nếu có tăng (đơn giản hóa cho icon)
                         # Nhưng để chính xác thì nên hiện val (tổng) hoặc (val-old)
                         # Theo yêu cầu: "Đơn hàng sản phẩm: 1"
                         # Mình sẽ hiện tổng số đơn mới tăng thêm
                         alerts[-1] = f"{Utils.get_icon(lbl)} {lbl}: {val - old}"
                         
                    # Logic cũ: Hiện tổng
                    if val > 0 and val > old:
                        alerts[-1] = f"{Utils.get_icon(lbl)} {lbl}: {val}"

                chat_msgs = self.fetch_chats(is_baseline) if check_chat else []
                
                # [CẬP NHẬT v26.0] UI Giống ảnh mẫu
                if has_change and not is_baseline:
                    # Format giờ: 7:43 SA
                    now = datetime.now(timezone(timedelta(hours=7)))
                    time_str = now.strftime("%I:%M %p").replace("AM", "SA").replace("PM", "CH")
                    if time_str.startswith("0"): time_str = time_str[1:] # Bỏ số 0 đầu (07:43 -> 7:43)

                    msg_lines = [f"⭐ <b>BÁO CÁO NHANH - [{html.escape(self.name)}]</b>"]
                    msg_lines.append("<code>- - - - - - - - - - - -</code>") # Dòng kẻ đứt
                    msg_lines.append("🔔 <b>BẠN CÓ THÔNG BÁO MỚI:</b>")
                    
                    if alerts: msg_lines.extend(alerts)
                    if chat_msgs:
                        msg_lines.append("<b>💬 Tin nhắn:</b>")
                        msg_lines.extend(chat_msgs)
                    
                    # Thời gian ở cuối cùng
                    msg_lines.append(f"\n<i>{time_str}</i>")
                    
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
                    self.processors[aid] = new
            for aid in list(self.processors.keys()):
                if aid not in current_ids: del self.processors[aid]
    
    def send_startup_message(self, global_chat_id):
        if not global_chat_id: return
        token_to_use = ""
        with self.lock:
            if self.processors:
                token_to_use = list(self.processors.values())[0].bot_token
        
        if not token_to_use: return

        timestamp = datetime.now(timezone(timedelta(hours=7))).strftime("%H:%M:%S - %d/%m/%Y")
        msg = (
            f"🚀 <b>HỆ THỐNG ĐÃ KHỞI ĐỘNG!</b> 🚀\n\n"
            f"👑 <b>Bot đã sẵn sàng phục vụ Chủ Nhân!</b>\n"
            f"💎 Trạng thái: <code>ONLINE</code>\n"
            f"⏱️ Thời gian: {timestamp}\n\n"
            f"<i>Chúc Chủ Nhân một ngày bão đơn! 💸💸💸</i>"
        )
        try:
            requests.post(f"https://api.telegram.org/bot{token_to_use}/sendMessage", json={"chat_id": global_chat_id, "text": msg, "parse_mode": "HTML"}, timeout=10)
        except: pass

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
    
    # Gửi thông báo khởi động
    threading.Thread(target=SERVICE.send_startup_message, args=(global_chat_id,)).start()
    
    return {"status": "success"}

@app.get("/api/stats")
def get_stats(authorized: bool = Depends(verify_session)):
    conn = DB.get_connection()
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
# 6. FRONTEND
# ==============================================================================

HTML_LOGIN = f"""
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Đăng nhập Hệ thống</title>
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@700&family=Rajdhani:wght@500&display=swap" rel="stylesheet">
    <style>
        body {{ margin: 0; height: 100vh; background: #050510; display: flex; justify-content: center; align-items: center; font-family: 'Rajdhani', sans-serif; color: #fff; overflow: hidden; }}
        .stars {{ position: fixed; width: 100%; height: 100%; z-index: -1; background: radial-gradient(circle at center, #1a1a3a 0%, #000 100%); }}
        .login-card {{ background: rgba(255,255,255,0.05); backdrop-filter: blur(15px); border: 1px solid rgba(0, 243, 255, 0.3); padding: 40px; border-radius: 20px; width: 350px; text-align: center; box-shadow: 0 0 30px rgba(0,0,0,0.5); animation: slideUp 0.8s ease-out; }}
        @keyframes slideUp {{ from {{ opacity: 0; transform: translateY(50px); }} to {{ opacity: 1; transform: translateY(0); }} }}
        h1 {{ font-family: 'Orbitron'; color: #00f3ff; margin-bottom: 30px; text-shadow: 0 0 10px rgba(0,243,255,0.5); }}
        input {{ width: 100%; padding: 15px; background: rgba(0,0,0,0.5); border: 1px solid #333; color: #fff; border-radius: 8px; font-size: 1.1rem; margin-bottom: 20px; text-align: center; transition: 0.3s; box-sizing: border-box; }}
        input:focus {{ border-color: #00f3ff; box-shadow: 0 0 15px rgba(0,243,255,0.2); outline: none; }}
        button {{ width: 100%; padding: 15px; background: linear-gradient(90deg, #00f3ff, #bc13fe); border: none; color: #fff; font-weight: bold; border-radius: 8px; cursor: pointer; font-size: 1.1rem; font-family: 'Orbitron'; transition: 0.3s; }}
        button:hover {{ transform: scale(1.05); box-shadow: 0 0 20px rgba(188,19,254,0.6); }}
        .copyright {{ margin-top: 20px; font-size: 0.8rem; color: #aaa; }}
    </style>
</head>
<body>
    <div class="stars"></div>
    <div class="login-card">
        <h1>GALAXY ACCESS</h1>
        <form action="/login" method="POST">
            <input type="password" name="secret" placeholder="NHẬP MÃ BẢO MẬT" required autofocus>
            <button type="submit">MỞ KHÓA HỆ THỐNG</button>
        </form>
        <div class="copyright">Bản quyền thuộc về Admin Văn Linh</div>
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
    <title>Dashboard - Admin Văn Linh</title>
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=Rajdhani:wght@300;500;700&display=swap" rel="stylesheet">
    <style>
        :root {{ --bg-space: #050510; --glass: rgba(255, 255, 255, 0.05); --neon-cyan: #00f3ff; --neon-pink: #ff00ff; --text-main: #ffffff; }}
        * {{ box-sizing: border-box; outline: none; }}
        body {{ margin: 0; background-color: var(--bg-space); color: var(--text-main); font-family: 'Rajdhani', sans-serif; min-height: 100vh; }}
        #starfield {{ position: fixed; top: 0; left: 0; width: 100%; height: 100%; z-index: -1; }}
        .app-container {{ max-width: 1200px; margin: 0 auto; padding: 20px; position: relative; z-index: 1; }}
        header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 40px; padding-bottom: 20px; border-bottom: 1px solid rgba(255,255,255,0.1); }}
        .brand {{ font-family: 'Orbitron'; font-size: 2rem; font-weight: 900; background: linear-gradient(90deg, var(--neon-cyan), var(--neon-pink)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
        .panel {{ background: var(--glass); backdrop-filter: blur(10px); border: 1px solid rgba(255,255,255,0.1); border-radius: 15px; padding: 30px; margin-bottom: 30px; }}
        h2 {{ font-family: 'Orbitron'; color: var(--neon-cyan); border-bottom: 1px solid rgba(255,255,255,0.1); padding-bottom: 10px; margin-bottom: 20px; margin-top: 0; }}
        label {{ display: block; color: #a0a0c0; margin-bottom: 8px; font-weight: bold; }}
        input, select, textarea {{ width: 100%; background: rgba(0,0,0,0.6); border: 1px solid #333; color: #fff; padding: 12px; border-radius: 8px; font-family: monospace; transition: 0.3s; }}
        input:focus, textarea:focus {{ border-color: var(--neon-cyan); box-shadow: 0 0 15px rgba(0,243,255,0.2); }}
        .row {{ display: flex; gap: 20px; flex-wrap: wrap; }} .col {{ flex: 1; min-width: 250px; }}
        .btn {{ padding: 12px 30px; border: none; border-radius: 5px; font-family: 'Orbitron'; font-weight: bold; cursor: pointer; color: #fff; background: linear-gradient(135deg, var(--neon-cyan), #0066ff); }}
        .btn:hover {{ transform: scale(1.02); box-shadow: 0 0 20px rgba(0,243,255,0.5); }}
        .btn-danger {{ background: transparent; border: 1px solid #ff3333; color: #ff3333; }}
        .btn-danger:hover {{ background: #ff3333; color: white; }}
        .btn-highlight {{ background: linear-gradient(90deg, #00f3ff, #0066ff); color: #fff; font-weight: 900; border: 1px solid #fff; box-shadow: 0 0 15px #00f3ff; text-shadow: 0 0 5px rgba(0,0,0,0.5); font-size: 1rem; }}
        .btn-highlight:hover {{ background: #fff; color: #000; box-shadow: 0 0 25px #00f3ff; }}
        .btn-logout {{ background: rgba(255,255,255,0.1); border: 1px solid #fff; padding: 8px 20px; font-size: 0.9rem; text-decoration: none; display: inline-block; color: #fff; border-radius: 5px; }}
        .account-card {{ background: rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.1); padding: 20px; border-radius: 10px; margin-bottom: 20px; position: relative; }}
        .chart-container {{ height: 300px; width: 100%; background: rgba(0,0,0,0.3); border-radius: 10px; padding: 10px; margin-top: 20px; display: flex; align-items: flex-end; gap: 10px; }}
        .footer {{ text-align: center; margin-top: 50px; color: #666; padding-top: 20px; border-top: 1px solid rgba(255,255,255,0.1); }}
        #loader {{ position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: #000; z-index: 9999; display: flex; justify-content: center; align-items: center; transition: opacity 0.5s; }}
        .spinner {{ width: 60px; height: 60px; border: 5px solid rgba(255,255,255,0.1); border-top: 5px solid var(--neon-cyan); border-radius: 50%; animation: spin 1s linear infinite; }}
        @keyframes spin {{ 0% {{ transform: rotate(0deg); }} 100% {{ transform: rotate(360deg); }} }}
        input[type="file"] {{ display: none; }}
        .upload-label {{ display: inline-block; padding: 12px 30px; background: rgba(255,255,255,0.1); border: 1px dashed #aaa; border-radius: 5px; cursor: pointer; font-family: 'Orbitron'; font-size: 0.9rem; color: #ccc; transition: 0.3s; text-align: center; }}
        .upload-label:hover {{ background: rgba(255,255,255,0.2); color: #fff; border-color: #fff; }}
    </style>
</head>
<body>
    <div id="loader"><div class="spinner"></div></div>
    <canvas id="starfield"></canvas>
    <div class="app-container">
        <header>
            <div class="brand">GALAXY ENTERPRISE</div>
            <div style="display:flex; gap:15px; align-items:center;">
                <span style="color: #00ff99; font-weight:bold; border:1px solid #00ff99; padding:5px 10px; border-radius:20px;">● ADMIN VĂN LINH</span>
                <a href="/logout" class="btn-logout">ĐĂNG XUẤT</a>
            </div>
        </header>

        <div class="panel">
            <h2>📊 THỐNG KÊ ĐƠN HÀNG (7 NGÀY)</h2>
            <div class="chart-container" id="chart-area"></div>
        </div>

        <form id="mainForm">
            <div class="panel">
                <h2>🔮 CẤU HÌNH CHUNG</h2>
                <div class="form-group"><label>TELEGRAM MASTER ID:</label><input type="text" id="gid" required></div>
                <div class="row">
                    <div class="col"><label>TỐC ĐỘ QUÉT (Giây):</label><input type="number" id="poll_int" value="10" min="3"></div>
                    <div class="col">
                        <div style="border:1px dashed var(--neon-cyan); padding:15px; border-radius:8px;">
                            <label style="color:var(--neon-cyan)">PINGER (CHỐNG NGỦ ĐÔNG)</label>
                            <div class="row"><select id="p_enable" style="flex:1"><option value="0">OFF</option><option value="1">ON</option></select><input type="number" id="p_interval" value="300" style="flex:1" placeholder="Giây"></div>
                            <input type="text" id="p_url" placeholder="https://..." style="margin-top:10px;">
                        </div>
                    </div>
                </div>
            </div>

            <div class="panel">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:20px;">
                    <h2 style="border:none; margin:0;">🚀 DANH SÁCH SHOP</h2>
                    <button type="button" class="btn btn-highlight" onclick="addAccount()">+ THÊM SHOP</button>
                </div>
                <div id="acc_list"></div>
            </div>

            <div style="position:sticky; bottom:20px; text-align:center;">
                <button type="submit" class="btn" style="width:80%; max-width:400px; font-size:1.2rem;">LƯU CẤU HÌNH</button>
            </div>
        </form>
        
        <div class="panel" style="margin-top: 50px;">
            <h2>💾 SAO LƯU & KHÔI PHỤC (JSON)</h2>
            <div style="color: #aaa; margin-bottom: 15px; font-size: 0.9rem;">* Backup sẽ chỉ lưu: User TapHoaMMO, Token, Chat ID và Cấu hình chung. <br>* cURL sẽ <b>KHÔNG</b> được lưu (để trống) vì cookie thay đổi liên tục.</div>
            <div class="row">
                <div class="col"><a href="/api/backup/download" target="_blank" class="btn" style="display:block; text-align:center; text-decoration:none; background: #28a745;">⬇️ TẢI FILE BACKUP</a></div>
                <div class="col" style="display:flex; gap:10px; align-items:center;">
                    <label for="restoreFile" class="upload-label" style="flex:1">📂 CHỌN FILE RESTORE...</label>
                    <input type="file" id="restoreFile" accept=".json">
                    <button type="button" class="btn" onclick="doRestore()" style="background: #e0a800;">⬆️ KHÔI PHỤC NGAY</button>
                </div>
            </div>
        </div>

        <div class="footer">Bản quyền thuộc về Admin Văn Linh &copy; 2025</div>
    </div>

    <script>
        const canvas = document.getElementById('starfield'); const ctx = canvas.getContext('2d');
        let width, height, stars = [];
        function resize() {{ width=window.innerWidth; height=window.innerHeight; canvas.width=width; canvas.height=height; }}
        class Star {{ constructor() {{ this.reset(); }} reset() {{ this.x=Math.random()*width; this.y=Math.random()*height; this.z=Math.random()*width; }} update() {{ this.z-=5; if(this.z<1) {{ this.reset(); this.z=width; }} }} draw() {{ let sx=(this.x-width/2)*(width/this.z)+width/2, sy=(this.y-height/2)*(width/this.z)+height/2, r=width/this.z; ctx.beginPath(); ctx.arc(sx,sy,r,0,2*Math.PI); ctx.fillStyle="#fff"; ctx.fill(); }} }}
        function loop() {{ ctx.fillStyle="rgba(5,5,16,0.4)"; ctx.fillRect(0,0,width,height); stars.forEach(s=>{{ s.update(); s.draw(); }}); requestAnimationFrame(loop); }}
        window.addEventListener('resize',resize); resize(); for(let i=0;i<800;i++) stars.push(new Star()); loop();

        const api={{ getConfig:async()=>(await fetch('/api/config')).json(), saveConfig:async(d)=>(await fetch('/api/config',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(d)}})).json(), getStats:async()=>(await fetch('/api/stats')).json() }};
        
        function renderAccount(id, d={{}}) {{
            const el=document.createElement('div'); el.className='account-card'; el.dataset.id=id;
            el.innerHTML=`<div style="display:flex; justify-content:space-between; margin-bottom:10px;"><strong>${{d.account_name||'Shop Mới'}}</strong><button type="button" class="btn btn-danger" onclick="this.closest('.account-card').remove()">XOÁ</button></div>
            <div class="row"><div class="col"><label>User TapHoaMMO:</label><input type="text" class="acc-name" value="${{d.account_name||''}}" required></div><div class="col"><label>Token:</label><input type="password" class="acc-token" value="${{d.bot_token||''}}" required></div></div>
            <div style="margin-top:10px"><label>Notify cURL:</label><textarea class="acc-notify" rows="2">${{d.notify_curl||''}}</textarea></div>
            <div style="margin-top:10px"><label>Chat cURL:</label><textarea class="acc-chat" rows="2">${{d.chat_curl||''}}</textarea></div>`;
            document.getElementById('acc_list').appendChild(el);
        }}
        
        function addAccount(){{ renderAccount(crypto.randomUUID()); }}
        
        async function init() {{
            try {{
                const conf = await api.getConfig();
                document.getElementById('gid').value = conf.global_chat_id;
                document.getElementById('poll_int').value = conf.poll_interval;
                document.getElementById('p_enable').value = conf.pinger.enabled?"1":"0";
                document.getElementById('p_url').value = conf.pinger.url;
                document.getElementById('p_interval').value = conf.pinger.interval;
                document.getElementById('acc_list').innerHTML=''; (conf.accounts||[]).forEach(a=>renderAccount(a.id, a));
                const stats = await api.getStats();
                const chart = document.getElementById('chart-area');
                if(stats.data && stats.data.length) {{
                    const max = Math.max(...stats.data, 10); chart.innerHTML = '';
                    stats.data.forEach((val, i) => {{
                        const bar = document.createElement('div');
                        bar.style.cssText = `flex:1; background:linear-gradient(to top, var(--neon-cyan), var(--neon-pink)); height:${{(val/max)*100}}%; border-radius:4px 4px 0 0; position:relative; min-height:5px; transition:height 1s;`;
                        bar.innerHTML = `<div style="position:absolute; top:-20px; width:100%; text-align:center; color:#fff; font-weight:bold">${{val}}</div><div style="position:absolute; bottom:-25px; width:100%; text-align:center; font-size:10px; color:#aaa">${{stats.labels[i].split('-').slice(1).join('/')}}</div>`;
                        chart.appendChild(bar);
                    }});
                }} else {{ chart.innerHTML = '<div style="width:100%; text-align:center; color:#666;">Chưa có dữ liệu</div>'; }}
            }} catch(e){{ console.error(e); }} finally {{ document.getElementById('loader').style.opacity='0'; setTimeout(()=>document.getElementById('loader').remove(),500); }}
        }}
        
        document.getElementById('mainForm').onsubmit = async(e) => {{
            e.preventDefault();
            const accounts={{}}; 
            document.querySelectorAll('.account-card').forEach(el=>{{ 
                const id = el.dataset.id;
                accounts[id]={{
                    account_name: el.querySelector('.acc-name').value,
                    bot_token: el.querySelector('.acc-token').value,
                    notify_curl: el.querySelector('.acc-notify').value,
                    chat_curl: el.querySelector('.acc-chat').value
                }}; 
            }});
            const payload={{ 
                global_chat_id:document.getElementById('gid').value, 
                poll_interval:parseInt(document.getElementById('poll_int').value), 
                pinger:{{
                    enabled:document.getElementById('p_enable').value==="1", 
                    url:document.getElementById('p_url').value, 
                    interval:parseInt(document.getElementById('p_interval').value)
                }}, 
                accounts:accounts 
            }};
            await api.saveConfig(payload); alert('✅ CẤU HÌNH ĐÃ LƯU THÀNH CÔNG!'); location.reload();
        }};
        
        document.getElementById('restoreFile').addEventListener('change', function() {{
            const label = document.querySelector('.upload-label');
            if(this.files && this.files.length > 0) label.innerText = "📄 " + this.files[0].name;
        }});
        async function doRestore() {{
            const fileInput = document.getElementById('restoreFile');
            if(!fileInput.files.length) {{ alert('Vui lòng chọn file JSON backup!'); return; }}
            if(!confirm('Bạn có chắc muốn khôi phục? Dữ liệu hiện tại sẽ bị thay thế!')) return;
            const formData = new FormData(); formData.append('file', fileInput.files[0]);
            try {{
                const res = await fetch('/api/backup/restore', {{ method: 'POST', body: formData }});
                const result = await res.json();
                if(result.status === 'success') {{ alert(result.message); location.reload(); }} else {{ alert('Lỗi: ' + result.message); }}
            }} catch(e) {{ alert('Lỗi upload: ' + e); }}
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
