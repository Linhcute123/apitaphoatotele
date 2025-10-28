import os, json, time, threading, html, hashlib, requests, re, shlex
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

# Load .env khi chạy local; trên Render biến môi trường sẽ được inject sẵn
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

# ===== ENV =====
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
WEBHOOK_SECRET     = os.getenv("WEBHOOK_SECRET", "change-me-please")

API_URL       = os.getenv("TAPHOA_API_ORDERS_URL", "")         # ví dụ: https://taphoammo.net/api/getNotify
API_METHOD    = os.getenv("TAPHOA_METHOD", "POST").upper()      # GET/POST
HEADERS_ENV   = os.getenv("HEADERS_JSON") or "{}"               # JSON 1 dòng từ cURL
BODY_JSON_ENV = os.getenv("TAPHOA_BODY_JSON", "")               # nếu POST và có payload JSON
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "12"))
VERIFY_TLS    = bool(int(os.getenv("VERIFY_TLS", "1")))
DISABLE_POLLER = os.getenv("DISABLE_POLLER", "0") == "1"

# Parse headers an toàn
try:
    HEADERS: Dict[str, str] = json.loads(HEADERS_ENV)
except Exception:
    HEADERS = {}

app = FastAPI(title="TapHoa → Telegram (getNotify + cURL parser)")

# ===== Trạng thái bộ nhớ =====
SEEN_JSON_IDS: set[str] = set()    # (nếu sau này bạn dùng API JSON list-orders)
LAST_NOTIFY: Optional[str] = None  # chuỗi getNotify lần gần nhất

# ===== Utils =====
def tg_send(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Missing TELEGRAM_* env")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    r = requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }, timeout=20)
    if r.status_code >= 400:
        print("Telegram error:", r.status_code, r.text)

def _labels_for_notify(parts_len: int) -> List[str]:
    # Đặt nhãn thân thiện nếu độ dài 7 (thường gặp 0|0|0|0|0|1|0)
    if parts_len == 7:
        return ["c1","c2","c3","c4","c5","so_moi","c7"]
    return [f"c{i+1}" for i in range(parts_len)]

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
    """
    Nhận 'Copy as cURL (bash)' từ Chrome DevTools.
    Trả về: {"url","method","headers","body"}
    """
    args = shlex.split(curl_text)
    method = "GET"
    headers: Dict[str, str] = {}
    data = None
    url = ""

    # Cho phép cURL dạng: curl 'https://...' -X POST -H 'k:v' --data '{...}'
    i = 0
    while i < len(args):
        a = args[i]
        if a == "curl":
            i += 1
            if i < len(args):
                url = args[i]
        elif a in ("-X", "--request"):
            i += 1
            if i < len(args):
                method = args[i].upper()
        elif a in ("-H", "--header"):
            i += 1
            if i < len(args):
                h = args[i]
                # Header có thể là "k: v" hoặc "k:    v"
                if ":" in h:
                    k, v = h.split(":", 1)
                    headers[k.strip()] = v.strip()
        elif a in ("--data", "--data-raw", "--data-binary", "-d"):
            i += 1
            if i < len(args):
                data = args[i]
        i += 1

    # Nếu không có -X nhưng có --data thì mặc định POST
    if method == "GET" and data is not None:
        method = "POST"

    return {"url": url, "method": method, "headers": headers, "body": data}

# ====== Poller chính ======
def poll_once():
    """
    Một vòng polling:
    - Nếu response parse được JSON → (để tương lai dùng list-orders).
    - Không phải JSON → coi là getNotify (text).
    """
    global LAST_NOTIFY, API_URL, API_METHOD, HEADERS, BODY_JSON_ENV

    if not API_URL:
        print("No API_URL set")
        return

    try:
        body_json = None
        if API_METHOD == "POST" and BODY_JSON_ENV:
            try:
                body_json = json.loads(BODY_JSON_ENV)
            except Exception:
                body_json = None

        # Call
        if API_METHOD == "POST":
            r = requests.post(API_URL, headers=HEADERS, json=body_json, verify=VERIFY_TLS, timeout=25)
        else:
            r = requests.get(API_URL, headers=HEADERS, verify=VERIFY_TLS, timeout=25)

        # 1) Thử JSON trước (để không phá nếu sau này bạn đổi sang API JSON)
        try:
            data = r.json()
        except Exception:
            data = None

        if data is not None:
            rows: List[Dict[str, Any]] = []
            if isinstance(data, list):
                rows = [x for x in data if isinstance(x, dict)]
            elif isinstance(data, dict):
                for key in ("data","items","rows","list","orders","result","content"):
                    v = data.get(key)
                    if isinstance(v, list):
                        rows = [x for x in v if isinstance(x, dict)]
                        break
                if not rows:
                    for v in data.values():
                        if isinstance(v, dict):
                            for key in ("data","items","rows","list","orders","result","content"):
                                vv = v.get(key)
                                if isinstance(vv, list):
                                    rows = [x for x in vv if isinstance(x, dict)]
                                    break
                        if rows:
                            break

            if rows:
                sent = 0
                for o in rows:
                    uid = str(o.get("order_id") or o.get("id") or hashlib.md5(json.dumps(o, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest())
                    if uid in SEEN_JSON_IDS:
                        continue
                    SEEN_JSON_IDS.add(uid)
                    buyer = html.escape(str(o.get("buyer_name") or o.get("buyer") or o.get("customer") or "N/A"))
                    total = o.get("total") or o.get("grand_total") or o.get("price_total")
                    msg = f"🛒 <b>ĐƠN MỚI</b>\n• Mã: <b>{html.escape(uid)}</b>\n• Người mua: <b>{buyer}</b>\n• Tổng: <b>{total}</b>"
                    tg_send(msg)
                    sent += 1
                if sent:
                    print(f"Sent {sent} order(s) from JSON API.")
                return  # đã xong JSON

        # 2) Không phải JSON → coi là getNotify (text)
        text = (r.text or "").strip()
        if not text:
            print("getNotify: empty response")
            return

        if text != LAST_NOTIFY:
            LAST_NOTIFY = text
            parsed = parse_notify_text(text)
            if "numbers" in parsed:
                tbl = parsed["table"]
                lines = [f"{k}: <b>{v}</b>" for k, v in tbl.items()]
                detail = "\n".join(lines)
                msg = f"🔔 <b>TapHoa getNotify thay đổi</b>\n{detail}\n(raw: <code>{html.escape(text)}</code>)"
            else:
                msg = f"🔔 <b>TapHoa getNotify thay đổi</b>\n<code>{html.escape(text)}</code>"
            tg_send(msg)
            print("getNotify changed -> Telegram sent.")
        else:
            print("getNotify unchanged.")

    except Exception as e:
        print("poll_once error:", e)

def poller_loop():
    print("▶ poller started (getNotify compatible)")
    poll_once()
    while True:
        time.sleep(POLL_INTERVAL)
        poll_once()

# ====== API ======
@app.get("/healthz")
def health():
    return {
        "ok": True,
        "poller": not DISABLE_POLLER,
        "seen_json": len(SEEN_JSON_IDS),
        "last_notify": LAST_NOTIFY,
        "api": {"url": API_URL, "method": API_METHOD}
    }

@app.get("/debug/notify-now")
def debug_notify(secret: str):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    before = LAST_NOTIFY
    poll_once()
    after = LAST_NOTIFY
    return {"ok": True, "last_before": before, "last_after": after}

@app.post("/debug/parse-curl")
async def debug_parse_curl(req: Request, secret: str):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    body = await req.json()
    curl_txt = str(body.get("curl") or "")
    parsed = parse_curl_command(curl_txt)
    # Trả về để bạn copy vào env
    return {
        "ok": True,
        "parsed": parsed,
        "env_suggestion": {
            "TAPHOA_API_ORDERS_URL": parsed["url"],
            "TAPHOA_METHOD": parsed["method"],
            "HEADERS_JSON": parsed["headers"],
            "TAPHOA_BODY_JSON": parsed["body"] or ""
        }
    }

@app.post("/debug/set-curl")
async def debug_set_curl(req: Request, secret: str):
    """
    Apply cURL tạm thời trong process (không ghi file), rồi poll ngay 1 vòng.
    Dùng để test nhanh trên Render.
    """
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    body = await req.json()
    curl_txt = str(body.get("curl") or "")
    parsed = parse_curl_command(curl_txt)

    global API_URL, API_METHOD, HEADERS, BODY_JSON_ENV
    API_URL = parsed["url"]
    API_METHOD = parsed["method"]
    HEADERS = parsed["headers"]
    BODY_JSON_ENV = parsed["body"] or ""

    poll_once()
    return {
        "ok": True,
        "using": {
            "url": API_URL,
            "method": API_METHOD,
            "headers": HEADERS,
            "body": BODY_JSON_ENV
        },
        "note": "Applied for this process only. Update env on Render to persist."
    }

@app.post("/taphoammo")
async def taphoammo(request: Request):
    """Giữ webhook để bạn test thủ công nếu cần (không bắt buộc dùng)."""
    if request.headers.get("X-Auth-Secret") != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        data = await request.json()
    except Exception as ex:
        return JSONResponse({"ok": False, "error": f"bad json: {ex}"}, status_code=400)
    buyer = html.escape(str(data.get("buyer_name") or data.get("buyer") or "N/A"))
    total = data.get("total") or data.get("grand_total")
    msg = f"🛒 <b>ĐƠN MỚI (webhook)</b>\n• Người mua: <b>{buyer}</b>\n• Tổng: <b>{total}</b>"
    tg_send(msg)
    return {"ok": True}

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
