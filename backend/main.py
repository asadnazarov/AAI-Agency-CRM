import os, json, hmac, hashlib, threading, time, io, re, csv
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from dotenv import load_dotenv
import requests as http
import psycopg2
from psycopg2.extras import RealDictCursor

load_dotenv()

app = FastAPI(title="Insta ManyChat")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

GRAPH_API = "https://graph.facebook.com/v21.0"
DATABASE_URL = os.getenv("DATABASE_URL", "")

# ── Database ──────────────────────────────────────────────────────────────────

def db():
    con = psycopg2.connect(DATABASE_URL)
    return con

def init_db():
    with db() as con:
        cur = con.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            id SERIAL PRIMARY KEY,
            instagram_post_id TEXT UNIQUE NOT NULL,
            post_url TEXT DEFAULT '',
            title TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            active INTEGER DEFAULT 1
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS triggers (
            id SERIAL PRIMARY KEY,
            post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
            keyword TEXT NOT NULL,
            reply_comment TEXT DEFAULT '',
            dm_text TEXT NOT NULL DEFAULT '',
            active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id SERIAL PRIMARY KEY,
            trigger_id INTEGER REFERENCES triggers(id),
            post_id INTEGER,
            instagram_user_id TEXT,
            username TEXT DEFAULT '',
            comment_text TEXT DEFAULT '',
            triggered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT DEFAULT ''
        )""")
        for k, v in [
            ('APP_ID', ''), ('APP_SECRET', ''),
            ('VERIFY_TOKEN', 'insta_secret_777'),
            ('PAGE_ACCESS_TOKEN', ''), ('INSTAGRAM_ACCOUNT_ID', ''),
        ]:
            cur.execute("INSERT INTO settings(key,value) VALUES(%s,%s) ON CONFLICT(key) DO NOTHING", (k, v))
        con.commit()

init_db()

# ── Settings helpers ──────────────────────────────────────────────────────────

def get_setting(key: str) -> str:
    with db() as con:
        cur = con.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT value FROM settings WHERE key=%s", (key,))
        row = cur.fetchone()
        return (row["value"] if row else None) or os.getenv(key, "")

def set_setting(key: str, value: str):
    with db() as con:
        cur = con.cursor()
        cur.execute("INSERT INTO settings(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", (key, value))
        con.commit()

def get_token() -> str:
    return get_setting("PAGE_ACCESS_TOKEN")

# ── Auto token refresh ────────────────────────────────────────────────────────

def _refresh_loop():
    while True:
        time.sleep(50 * 24 * 3600)
        token = get_token()
        if not token:
            continue
        r = http.get("https://graph.instagram.com/refresh_access_token",
                     params={"grant_type": "ig_refresh_token", "access_token": token})
        data = r.json()
        if "access_token" in data:
            set_setting("PAGE_ACCESS_TOKEN", data["access_token"])
            print("✅ Token auto-refreshed")

threading.Thread(target=_refresh_loop, daemon=True).start()

_processed = set()

# ── OAuth ─────────────────────────────────────────────────────────────────────

@app.get("/auth")
async def auth_start(request: Request):
    app_id = get_setting("APP_ID")
    if not app_id:
        return HTMLResponse("<h2>❌ APP_ID не задан</h2>")
    base = str(request.base_url).rstrip("/")
    url = (
        f"https://www.instagram.com/oauth/authorize"
        f"?client_id={app_id}&redirect_uri={base}/auth/callback"
        f"&response_type=code"
        f"&scope=instagram_business_basic,instagram_manage_comments,instagram_business_manage_messages"
    )
    return RedirectResponse(url)

@app.get("/auth/callback")
async def auth_callback(request: Request, code: str = None, error: str = None):
    if error or not code:
        return HTMLResponse(f"<h2>❌ Ошибка: {error}</h2>")
    app_id = get_setting("APP_ID")
    app_secret = get_setting("APP_SECRET")
    base = str(request.base_url).rstrip("/")
    r = http.post("https://api.instagram.com/oauth/access_token", data={
        "client_id": app_id, "client_secret": app_secret,
        "grant_type": "authorization_code",
        "redirect_uri": f"{base}/auth/callback", "code": code,
    })
    short_token = r.json().get("access_token")
    if not short_token:
        return HTMLResponse(f"<h2>❌ {r.json()}</h2>")
    r2 = http.get("https://graph.instagram.com/access_token", params={
        "grant_type": "ig_exchange_token", "client_secret": app_secret,
        "access_token": short_token,
    })
    long_token = r2.json().get("access_token", short_token)
    set_setting("PAGE_ACCESS_TOKEN", long_token)
    r3 = http.get(f"{GRAPH_API}/me", params={"access_token": long_token, "fields": "id,username"})
    me = r3.json()
    if "id" in me:
        set_setting("INSTAGRAM_ACCOUNT_ID", me["id"])
    return HTMLResponse("""
    <html><body style="font-family:sans-serif;text-align:center;padding:60px;background:#0d0e1a;color:#fff">
    <h1>✅ Авторизация успешна!</h1><p>Токен сохранён. Можешь закрыть это окно.</p>
    </body></html>
    """)

# ── Webhook ───────────────────────────────────────────────────────────────────

@app.get("/webhook")
async def verify_webhook(request: Request):
    p = request.query_params
    if p.get("hub.mode") == "subscribe" and p.get("hub.verify_token") == get_setting("VERIFY_TOKEN"):
        return PlainTextResponse(p.get("hub.challenge"))
    raise HTTPException(403)

@app.post("/webhook")
async def handle_webhook(request: Request):
    body = await request.body()
    app_secret = get_setting("APP_SECRET")
    sig = request.headers.get("X-Hub-Signature-256", "")
    if app_secret and sig:
        expected = "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            raise HTTPException(403)
    data = json.loads(body)
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") == "comments":
                _handle_comment(change.get("value", {}))
    return {"status": "ok"}

def _get_media_shortcode(media_id: str) -> str:
    token = get_token()
    if not token:
        return ""
    r = http.get(f"https://graph.instagram.com/v21.0/{media_id}",
                 params={"fields": "shortcode", "access_token": token})
    return r.json().get("shortcode", "")

def _handle_comment(value: dict):
    comment_id = value.get("id")
    comment_text = value.get("text", "")
    commenter_id = value.get("from", {}).get("id")
    commenter_name = value.get("from", {}).get("name", "")
    media_id = value.get("media", {}).get("id", "")
    if not comment_id or not commenter_id or comment_id in _processed:
        return
    print(f"💬 [{media_id}] {commenter_name}: '{comment_text}'")
    with db() as con:
        cur = con.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT id FROM posts WHERE instagram_post_id=%s AND active=1", (media_id,))
        post = cur.fetchone()
        if not post:
            shortcode = _get_media_shortcode(media_id)
            print(f"🔍 shortcode lookup: {media_id} → {shortcode}")
            if shortcode:
                cur.execute("SELECT id FROM posts WHERE instagram_post_id=%s AND active=1", (shortcode,))
                post = cur.fetchone()
        if not post:
            print(f"⚠️ No active post found for media {media_id}")
            return
        post_id = post["id"]
        cur.execute("SELECT * FROM triggers WHERE post_id=%s AND active=1", (post_id,))
        triggers = cur.fetchall()
        for t in triggers:
            if t["keyword"].lower() in comment_text.lower():
                _processed.add(comment_id)
                print(f"🎯 '{t['keyword']}' matched → DM to {commenter_name}")
                if t["reply_comment"]:
                    _reply_comment(comment_id, t["reply_comment"])
                _send_dm(commenter_id, t["dm_text"])
                cur.execute(
                    "INSERT INTO leads (trigger_id,post_id,instagram_user_id,username,comment_text) VALUES (%s,%s,%s,%s,%s)",
                    (t["id"], post_id, commenter_id, commenter_name, comment_text)
                )
                con.commit()
                break

def _reply_comment(comment_id: str, text: str):
    r = http.post(f"{GRAPH_API}/{comment_id}/replies",
                  params={"access_token": get_token(), "message": text})
    print(f"{'✅' if r.ok else '❌'} reply")

def _send_dm(user_id: str, text: str):
    account_id = get_setting("INSTAGRAM_ACCOUNT_ID")
    token = get_token()
    r = http.post(f"https://graph.instagram.com/v21.0/{account_id}/messages",
                  json={"recipient": {"id": user_id}, "message": {"text": text}},
                  params={"access_token": token})
    print(f"{'✅' if r.ok else '❌'} DM → {r.status_code}: {r.text[:200]}")

# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "insta-manychat"}

@app.get("/")
async def root():
    token = get_token()
    return {"status": "running", "token": "ok" if len(token) > 20 else "missing"}

# ── API — Posts ───────────────────────────────────────────────────────────────

@app.get("/api/posts")
async def api_list_posts():
    with db() as con:
        cur = con.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT p.*, COUNT(DISTINCT t.id) AS trigger_count, COUNT(DISTINCT l.id) AS lead_count
            FROM posts p
            LEFT JOIN triggers t ON t.post_id=p.id AND t.active=1
            LEFT JOIN leads l ON l.post_id=p.id
            GROUP BY p.id ORDER BY p.created_at DESC
        """)
        return [dict(r) for r in cur.fetchall()]

@app.post("/api/posts")
async def api_add_post(request: Request):
    data = await request.json()
    raw = (data.get("instagram_post_id") or "").strip()
    if not raw:
        raise HTTPException(400, "instagram_post_id required")
    if "instagram.com" in raw:
        m = re.search(r'/(?:p|reel|tv)/([A-Za-z0-9_-]+)', raw)
        post_id = m.group(1) if m else raw
    else:
        post_id = raw
    with db() as con:
        cur = con.cursor()
        try:
            cur.execute(
                "INSERT INTO posts (instagram_post_id,post_url,title) VALUES (%s,%s,%s) RETURNING id",
                (post_id, data.get("post_url",""), data.get("title",""))
            )
            new_id = cur.fetchone()[0]
            con.commit()
            return {"id": new_id, "instagram_post_id": post_id}
        except psycopg2.IntegrityError:
            raise HTTPException(409, "Post already added")

@app.put("/api/posts/{pid}")
async def api_update_post(pid: int, request: Request):
    data = await request.json()
    with db() as con:
        cur = con.cursor()
        cur.execute("UPDATE posts SET title=%s,active=%s WHERE id=%s",
                    (data.get("title",""), int(data.get("active",1)), pid))
        con.commit()
    return {"ok": True}

@app.delete("/api/posts/{pid}")
async def api_delete_post(pid: int):
    with db() as con:
        cur = con.cursor()
        cur.execute("DELETE FROM posts WHERE id=%s", (pid,))
        con.commit()
    return {"ok": True}

# ── API — Triggers ────────────────────────────────────────────────────────────

@app.get("/api/posts/{pid}/triggers")
async def api_list_triggers(pid: int):
    with db() as con:
        cur = con.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM triggers WHERE post_id=%s ORDER BY created_at DESC", (pid,))
        return [dict(r) for r in cur.fetchall()]

@app.post("/api/posts/{pid}/triggers")
async def api_add_trigger(pid: int, request: Request):
    data = await request.json()
    if not data.get("keyword") or not data.get("dm_text"):
        raise HTTPException(400, "keyword and dm_text required")
    with db() as con:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO triggers (post_id,keyword,reply_comment,dm_text) VALUES (%s,%s,%s,%s) RETURNING id",
            (pid, data["keyword"].strip(), data.get("reply_comment",""), data["dm_text"])
        )
        new_id = cur.fetchone()[0]
        con.commit()
        return {"id": new_id}

@app.put("/api/triggers/{tid}")
async def api_update_trigger(tid: int, request: Request):
    data = await request.json()
    with db() as con:
        cur = con.cursor()
        cur.execute(
            "UPDATE triggers SET keyword=%s,reply_comment=%s,dm_text=%s,active=%s WHERE id=%s",
            (data.get("keyword"), data.get("reply_comment",""), data.get("dm_text"), int(data.get("active",1)), tid)
        )
        con.commit()
    return {"ok": True}

@app.delete("/api/triggers/{tid}")
async def api_delete_trigger(tid: int):
    with db() as con:
        cur = con.cursor()
        cur.execute("DELETE FROM triggers WHERE id=%s", (tid,))
        con.commit()
    return {"ok": True}

# ── API — Leads ───────────────────────────────────────────────────────────────

@app.get("/api/leads")
async def api_list_leads(post_id: int = None, limit: int = 200):
    with db() as con:
        cur = con.cursor(cursor_factory=RealDictCursor)
        if post_id:
            cur.execute("""
                SELECT l.*, t.keyword, p.title AS post_title FROM leads l
                LEFT JOIN triggers t ON t.id=l.trigger_id
                LEFT JOIN posts p ON p.id=l.post_id
                WHERE l.post_id=%s ORDER BY l.triggered_at DESC LIMIT %s
            """, (post_id, limit))
        else:
            cur.execute("""
                SELECT l.*, t.keyword, p.title AS post_title FROM leads l
                LEFT JOIN triggers t ON t.id=l.trigger_id
                LEFT JOIN posts p ON p.id=l.post_id
                ORDER BY l.triggered_at DESC LIMIT %s
            """, (limit,))
        return [dict(r) for r in cur.fetchall()]

@app.get("/api/leads/export")
async def api_export_leads():
    with db() as con:
        cur = con.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT l.username, l.instagram_user_id, l.comment_text,
                   t.keyword, p.title AS post_title, l.triggered_at
            FROM leads l
            LEFT JOIN triggers t ON t.id=l.trigger_id
            LEFT JOIN posts p ON p.id=l.post_id
            ORDER BY l.triggered_at DESC
        """)
        rows = cur.fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Username","User ID","Comment","Keyword","Post","Date"])
    for r in rows:
        w.writerow([r["username"],r["instagram_user_id"],r["comment_text"],
                    r["keyword"],r["post_title"],r["triggered_at"]])
    buf.seek(0)
    return StreamingResponse(io.BytesIO(buf.read().encode("utf-8-sig")),
                             media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=leads.csv"})

# ── API — Stats ───────────────────────────────────────────────────────────────

@app.get("/api/stats")
async def api_stats():
    with db() as con:
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM posts WHERE active=1"); posts = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM triggers WHERE active=1"); trigs = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM leads"); leads = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM leads WHERE triggered_at::date = CURRENT_DATE"); today = cur.fetchone()[0]
        return {"posts": posts, "triggers": trigs, "leads": leads, "today": today}

# ── API — Settings ────────────────────────────────────────────────────────────

@app.get("/api/settings")
async def api_get_settings():
    with db() as con:
        cur = con.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT key, value FROM settings")
        rows = cur.fetchall()
    result = {r["key"]: r["value"] for r in rows}
    tok = result.get("PAGE_ACCESS_TOKEN", "")
    result["PAGE_ACCESS_TOKEN"] = (tok[:20] + "…") if len(tok) > 20 else tok
    result["token_ok"] = len(tok) > 20
    return result

@app.post("/api/settings")
async def api_save_settings(request: Request):
    data = await request.json()
    allowed = {"APP_ID","APP_SECRET","VERIFY_TOKEN","INSTAGRAM_ACCOUNT_ID","PAGE_ACCESS_TOKEN"}
    with db() as con:
        cur = con.cursor()
        for k, v in data.items():
            if k in allowed:
                cur.execute("INSERT INTO settings(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", (k, v))
        con.commit()
    return {"ok": True}
