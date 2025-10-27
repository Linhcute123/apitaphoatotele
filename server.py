import os, json, time, threading, html, hashlib, requests
from typing import Any, Dict, List
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

# -------- ENV --------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
WEBHOOK_SECRET     = os.getenv("WEBHOOK_SECRET", "change-me-please")

API_URL    = os.getenv("TAPHOA_API_ORDERS_URL", "")
API_METHOD = os.getenv("TAPHOA_METHOD", "POST").upper()
HEADERS    = json.loads(os.getenv("HEADERS_JSON") or "{}")
BODY_JSON  = os.getenv("TAPHOA_BODY_JSON", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "12"))  # giãn nhịp một chút
VERIFY_TLS    = bool(int(os.getenv("VERIFY_TLS", "1")))

# -------- APP --------
app = FastAPI(title="TapHoa → Telegram (web+poller)")

# bộ nhớ đơn đã gửi (chống trùng)
SEEN: set[str] = set()
SEEN_MAX = 5000

def fmt_vnd(v: Any) -> str:
    try:
        if v is None: return "N/A"
        if isinstance(v, str):
            v = v.replace(".", "").replace(",", ".")
        n = float(v)
        return f"{int(n):,}".replace(",", ".") + "đ"
    except Exception:
        return str(v)

def pick(d: Dict, keys: List[str], default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default

def order_to_msg(o: Dict) -> str:
    oid   = pick(o, ["order_id","id","code","order_code","ma_don_hang"], "N/A")
    date  = pick(o, ["created_at","date","time","ngay_ban"], "N/A")
    buyer = pick(o, ["buyer_name","buyer","customer","username","nguoi_mua"], "N/A")
    shop  = pick(o, ["shop","store","seller","gian_hang"], "N/A")
    item  = pick(o, ["product_name","item_name","name","title","mat_hang"], "N/A")
    qty   = pick(o, ["quantity","qty","so_luong","count"], 1)
    price = pick(o, ["price","unit_price","gia"], None)
    total = pick(o, ["total","grand_total","tong_tien","amount","price_total"], None)
    st    = pick(o, ["status","state","trang_thai"], "N/A")

    # escape để gửi HTML an toàn
    e = lambda x: html.escape(str(x)) if x is not None else ""
    oid,date,buyer,shop,item,st = map(e, [oid,date,buyer,shop,item,st])

    return (
        "🛒 <b>ĐƠN MỚI</b>\n"
        f"• Mã đơn: <b>{oid}</b>\n"
        f"• Ngày bán: <b>{date}</b>\n"
        f"• Người mua: <b>{buyer}</b>\n"
        f"• Gian hàng: <b>{shop}</b>\n"
        f"• Mặt hàng: <b>{item}</b>\n"
        f"• Số lượng: <b>{qty}</b>  • Giá: <b>{fmt_vnd(price)}</b>  • Tổng: <b>{fmt_vnd(total)}</b>\n"
        f"• Trạng thái: <b>{st}</b>"
    )

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

def uniq_id(o: Dict) -> str:
    oid = pick(o, ["order_id","id","code","order_code"])
    if oid: return str(oid)
    # fallback hash
    return hashlib.md5(json.dumps(o, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

def extract_rows(resp_json: Any) -> List[Dict]:
    # các dạng phổ biến: list[...] hoặc {"data":[...]} hoặc {"items":[...]}
    if isinstance(resp_json, list):
        return [x for x in resp_json if isinstance(x, dict)]
    if isinstance(resp_json, dict):
        for key in ("data","items","rows","list","orders"):
            if key in resp_json and isinstance(resp_json[key], list):
                return [x for x in resp_json[key] if isinstance(x, dict)]
    return []

def poll_once():
    if not API_URL:
        return
    try:
        body = None
        if API_METHOD == "POST":
            body = json.loads(BODY_JSON) if BODY_JSON else None
            r = requests.post(API_URL, headers=HEADERS, json=body, verify=VERIFY_TLS, timeout=25)
        else:
            r = requests.get(API_URL, headers=HEADERS, verify=VERIFY_TLS, timeout=25)

        try:
            data = r.json()
        except Exception:
            print("Non-JSON:", (r.text or "")[:200])
            return

        rows = extract_rows(data)
        if not rows:
            print("No rows parsed.")
            return

        # đảo ngược để gửi theo thứ tự cũ->mới
        for o in rows:
            uid = uniq_id(o)
            if uid in SEEN: 
                continue
            # đánh dấu và cắt bớt bộ nhớ
            SEEN.add(uid)
            if len(SEEN) > SEEN_MAX:
                for _ in range(len(SEEN)-SEEN_MAX):
                    SEEN.pop()
            tg_send(order_to_msg(o))
    except Exception as e:
        print("poll_once error:", e)

def poller_loop():
    print("▶ poller started")
    # warm-up đầu tiên
    poll_once()
    while True:
        time.sleep(POLL_INTERVAL)
        poll_once()

# ---------- FastAPI routes ----------
@app.get("/healthz")
def health():
    return {"ok": True, "seen": len(SEEN)}

@app.post("/taphoammo")
async def taphoammo(request: Request):
    if request.headers.get("X-Auth-Secret") != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        data = await request.json()
    except Exception as ex:
        return JSONResponse({"ok": False, "error": f"bad json: {ex}"}, status_code=400)
    tg_send(order_to_msg(data))
    return {"ok": True}

# start background poller when app boots
def _maybe_start():
    if os.getenv("DISABLE_POLLER") == "1":
        print("Poller disabled by env.")
        return
    t = threading.Thread(target=poller_loop, daemon=True)
    t.start()

_maybe_start()

if __name__ == "__main__":
    # chạy local
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
