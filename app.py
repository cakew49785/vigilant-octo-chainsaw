#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════╗
║   ChatBot — All-in-One Google Chat App (Flask)        ║
║   Firebase RTDB + OpenRouter AI                       ║
║   Single file deployment · GitHub + Render            ║
╚══════════════════════════════════════════════════════╝

Local setup:
  pip install flask firebase-admin requests psutil gunicorn

Environment variables (set these in Render → your service →
Environment, not in this file):
  OWNER_EMAIL          your Google account email (admin access)
  ADMIN_PASS           any password you choose
  FIREBASE_URL         e.g. https://your-project-default-rtdb.firebaseio.com
  FIREBASE_CRED_JSON   full contents of your Firebase service-account
                        JSON key, pasted as a single-line string
                        (Render → Environment → "Secret File" works too,
                        see load_firebase_credentials() below)
  OPENROUTER_API_KEY   your OpenRouter key
  OPENROUTER_MODEL     e.g. google/gemma-2-9b-it
  PORT                 set automatically by Render, defaults to 8080

Google Chat app configuration (Cloud Console → Chat API →
Configuration tab):
  Connection settings → "HTTP endpoint URL" →
  https://<your-render-service>.onrender.com/chat

Run locally:
  python app.py
"""

import os
import time
import json
import logging
import threading
from datetime import datetime

import requests
import psutil
from flask import Flask, request, jsonify

import firebase_admin
from firebase_admin import credentials, db as rtdb

# ════════════════════════════════════════════
#  CONFIGURATION
# ════════════════════════════════════════════

OWNER_EMAIL = os.getenv("OWNER_EMAIL", "you@example.com").lower()
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123")

FIREBASE_URL = os.getenv("FIREBASE_URL", "https://your-project-default-rtdb.firebaseio.com")
FIREBASE_CRED_JSON = os.getenv("FIREBASE_CRED_JSON", "")  # paste full service-account JSON here
FIREBASE_CRED_PATH = os.getenv("FIREBASE_CRED_PATH", "firebase_credentials.json")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemma-2-9b-it")
AI_INSTRUCTION = (
    "You are a helpful Google Chat bot assistant with self-respect. "
    "If the user is angry or disrespectful, respond firmly and calmly—don't take insults. "
    "If the user is happy or polite, respond warmly and helpfully. "
    "Answer basic questions honestly and concisely."
)

BOT_START_TIME = time.time()

# ════════════════════════════════════════════
#  LOGGING
# ════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger("ChatBot")

# ════════════════════════════════════════════
#  FIREBASE INIT
# ════════════════════════════════════════════

firebase_ok = False


def load_firebase_credentials():
    """Prefer a JSON-in-env-var (simplest on Render); fall back to a file
    on disk if you'd rather mount it as a Secret File."""
    if FIREBASE_CRED_JSON:
        try:
            return credentials.Certificate(json.loads(FIREBASE_CRED_JSON))
        except Exception as e:
            log.warning(f"⚠️ FIREBASE_CRED_JSON parse failed: {e}")
    if os.path.exists(FIREBASE_CRED_PATH):
        return credentials.Certificate(FIREBASE_CRED_PATH)
    return None


def init_firebase():
    global firebase_ok
    try:
        cred = load_firebase_credentials()
        if cred:
            firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_URL})
        else:
            # Anonymous access — only works if your DB rules allow it.
            firebase_admin.initialize_app(options={"databaseURL": FIREBASE_URL})
        firebase_ok = True
        log.info("✅ Firebase connected")
        fb_log("INFO", "Bot started and Firebase connected")
    except Exception as e:
        log.warning(f"⚠️ Firebase failed: {e} — running without Firebase")


def fb_ref(path: str):
    if not firebase_ok:
        return None
    try:
        return rtdb.reference(path)
    except Exception:
        return None


def fb_get(path: str, default=None):
    ref = fb_ref(path)
    if ref is None:
        return default
    try:
        val = ref.get()
        return val if val is not None else default
    except Exception:
        return default


def fb_set(path: str, value):
    ref = fb_ref(path)
    if ref:
        try:
            ref.set(value)
            return True
        except Exception:
            pass
    return False


def fb_push(path: str, value: dict):
    ref = fb_ref(path)
    if ref:
        try:
            ref.push(value)
            return True
        except Exception:
            pass
    return False


def fb_delete(path: str):
    ref = fb_ref(path)
    if ref:
        try:
            ref.delete()
            return True
        except Exception:
            pass
    return False


def fb_log(level: str, msg: str):
    fb_push("logs", {"type": level, "msg": msg, "time": int(time.time() * 1000)})


def update_stat(key: str, delta=1):
    cur = fb_get(f"stats/{key}", 0) or 0
    fb_set(f"stats/{key}", int(cur) + delta)


# ════════════════════════════════════════════
#  CACHED STATE  (refreshed every CACHE_TTL seconds so we
#  don't hit Firebase on every single message)
# ════════════════════════════════════════════

CACHE_TTL = 60
_cache = {"auto_replies": ({}, 0), "banned": ([], 0), "blocked_words": ([], 0)}
_cache_lock = threading.Lock()


def _cached(name, loader):
    with _cache_lock:
        value, ts = _cache[name]
        if time.time() - ts > CACHE_TTL:
            value = loader()
            _cache[name] = (value, time.time())
        return value


def get_auto_replies():
    def loader():
        raw = fb_get("autoreply", {}) or {}
        return {str(k).lower(): str(v) for k, v in raw.items()}
    return _cached("auto_replies", loader)


def get_banned_users():
    def loader():
        raw = fb_get("banned", {}) or {}
        return [v["user_email"] for v in raw.values() if isinstance(v, dict) and "user_email" in v]
    return _cached("banned", loader)


def get_blocked_words():
    def loader():
        raw = fb_get("blocked_words", []) or []
        if isinstance(raw, list):
            return [w.lower() for w in raw]
        return [str(w).lower() for w in raw.values()]
    return _cached("blocked_words", loader)


def invalidate_cache(name):
    with _cache_lock:
        value, _ = _cache[name]
        _cache[name] = (value, 0)


# ════════════════════════════════════════════
#  AUTH HELPERS
# ════════════════════════════════════════════

def is_owner(user_email: str) -> bool:
    return bool(user_email) and user_email.lower() == OWNER_EMAIL


def is_banned(user_email: str) -> bool:
    return user_email in get_banned_users()


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def sanitize_key(s: str) -> str:
    return "".join(c if c not in ".#$[]/" else "_" for c in s)


# ════════════════════════════════════════════
#  CHAT MESSAGE HELPERS
# ════════════════════════════════════════════

def text_message(text: str):
    return {"text": text}


def card_message(title: str, subtitle: str, body_text: str):
    return {
        "cardsV2": [{
            "cardId": f"card_{int(time.time() * 1000)}",
            "card": {
                "header": {"title": title, "subtitle": subtitle or ""},
                "sections": [{"widgets": [{"textParagraph": {"text": body_text}}]}]
            }
        }]
    }


CHAT_API_BASE = "https://chat.googleapis.com/v1"


def get_chat_access_token():
    """OAuth token for the service account, used to call the Chat API
    for async sends (announcements, panel commands)."""
    try:
        import google.auth
        import google.auth.transport.requests
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/chat.bot"])
        creds.refresh(google.auth.transport.requests.Request())
        return creds.token
    except Exception as e:
        log.error(f"Chat auth token error: {e}")
        return None


def send_to_space(space_name: str, payload: dict) -> bool:
    token = get_chat_access_token()
    if not token:
        return False
    try:
        resp = requests.post(
            f"{CHAT_API_BASE}/{space_name}/messages",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=15
        )
        return resp.status_code == 200
    except Exception as e:
        log.error(f"send_to_space error: {e}")
        return False


# ════════════════════════════════════════════
#  AI (OpenRouter)
# ════════════════════════════════════════════

def handle_ai(user_msg: str, user_name: str):
    if not OPENROUTER_API_KEY:
        return text_message("❌ AI is not configured (missing OPENROUTER_API_KEY).")
    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://chat.google.com",
                "X-Title": "ChatBot"
            },
            json={
                "model": OPENROUTER_MODEL,
                "messages": [
                    {"role": "system", "content": AI_INSTRUCTION},
                    {"role": "user", "content": f"{user_name}: {user_msg}"}
                ],
                "max_tokens": 500
            },
            timeout=30
        )
        data = resp.json()
        reply = data["choices"][0]["message"]["content"].strip()
        fb_log("INFO", f"AI query from {user_name}: {user_msg[:40]}")
        return text_message(f"🧠 {reply}")
    except Exception as e:
        log.error(f"AI error: {e}")
        return text_message("❌ AI temporarily unavailable. Try again later.")


# ════════════════════════════════════════════
#  COMMAND ROUTER
# ════════════════════════════════════════════

def handle_command(text: str, user_email: str, user_name: str, space: dict):
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    args_text = parts[1].strip() if len(parts) > 1 else ""
    owner = is_owner(user_email)

    if cmd in ("/help", "/start"):
        lines = [
            "/help — show this menu",
            "/afk [reason] — mark yourself AFK",
            "/ai [question] — ask the AI directly",
        ]
        if owner:
            lines += [
                "/announcement [msg] — broadcast to all spaces (owner)",
                "/serverstatus — bot health (owner)",
                "/ban [email] — ban a user (owner)",
                "/unban [email] — unban a user (owner)",
                "/autoreply add [word] | [reply] — add auto-reply (owner)",
                "/blockword add [word] — block a word (owner)",
            ]
        return card_message("🤖 Bot Commands", "", "\n".join(lines))

    if cmd == "/afk":
        fb_set(f"afk/{sanitize_key(user_email)}", {"reason": args_text or "AFK", "time": int(time.time() * 1000)})
        suffix = f": {args_text}" if args_text else "."
        return text_message(f"💤 *{user_name}* is now AFK{suffix}")

    if cmd == "/ai":
        if not args_text:
            return text_message("🧠 Usage: /ai [your question]")
        return handle_ai(args_text, user_name)

    if cmd == "/announcement":
        if not owner:
            return text_message("⛔ Owner only.")
        if not args_text:
            return text_message("📡 Usage: /announcement [message]")
        return handle_announcement(args_text)

    if cmd == "/serverstatus":
        if not owner:
            return text_message("⛔ Owner only.")
        return handle_server_status()

    if cmd == "/ban":
        if not owner:
            return text_message("⛔ Owner only.")
        if not args_text:
            return text_message("Usage: /ban [email]")
        fb_push("banned", {"user_email": args_text.strip()})
        invalidate_cache("banned")
        fb_log("INFO", f"Banned: {args_text.strip()}")
        return text_message(f"🚫 Banned {args_text.strip()}")

    if cmd == "/unban":
        if not owner:
            return text_message("⛔ Owner only.")
        if not args_text:
            return text_message("Usage: /unban [email]")
        remove_banned_by_email(args_text.strip())
        invalidate_cache("banned")
        return text_message(f"✅ Unbanned {args_text.strip()}")

    if cmd == "/autoreply":
        if not owner:
            return text_message("⛔ Owner only.")
        return handle_autoreply_cmd(args_text)

    if cmd == "/blockword":
        if not owner:
            return text_message("⛔ Owner only.")
        return handle_blockword_cmd(args_text)

    return text_message("❓ Unknown command. Try /help.")


def remove_banned_by_email(email: str):
    raw = fb_get("banned", {}) or {}
    for k, v in raw.items():
        if isinstance(v, dict) and v.get("user_email") == email:
            fb_delete(f"banned/{k}")


def handle_autoreply_cmd(args_text: str):
    # /autoreply add [word] | [reply]
    # /autoreply del [word]
    bits = args_text.split(maxsplit=1)
    sub = bits[0] if bits else ""
    rest = bits[1] if len(bits) > 1 else ""

    if sub == "add":
        if "|" not in rest:
            return text_message("Usage: /autoreply add [word] | [reply]")
        word, reply = rest.split("|", 1)
        word, reply = word.strip().lower(), reply.strip()
        raw = fb_get("autoreply", {}) or {}
        raw[word] = reply
        fb_set("autoreply", raw)
        invalidate_cache("auto_replies")
        return text_message(f'✅ Auto-reply added for "{word}"')

    if sub == "del":
        word = rest.strip().lower()
        raw = fb_get("autoreply", {}) or {}
        raw.pop(word, None)
        fb_set("autoreply", raw)
        invalidate_cache("auto_replies")
        return text_message(f'🗑️ Auto-reply removed for "{word}"')

    return text_message("Usage: /autoreply add [word] | [reply]  OR  /autoreply del [word]")


def handle_blockword_cmd(args_text: str):
    bits = args_text.split(maxsplit=1)
    sub = bits[0] if bits else ""
    word = (bits[1] if len(bits) > 1 else "").strip().lower()

    words = fb_get("blocked_words", []) or []
    if not isinstance(words, list):
        words = list(words.values())

    if sub == "add":
        if word not in words:
            words.append(word)
        fb_set("blocked_words", words)
        invalidate_cache("blocked_words")
        return text_message(f'✅ Blocked word added: "{word}"')

    if sub == "del":
        words = [w for w in words if w != word]
        fb_set("blocked_words", words)
        invalidate_cache("blocked_words")
        return text_message(f'🗑️ Blocked word removed: "{word}"')

    return text_message("Usage: /blockword add [word]  OR  /blockword del [word]")


# ════════════════════════════════════════════
#  ANNOUNCEMENT
# ════════════════════════════════════════════

def handle_announcement(msg_text: str):
    fb_push("announcements", {"msg": msg_text, "priority": "urgent", "time": int(time.time() * 1000)})
    fb_log("INFO", f"Announcement sent: {msg_text[:60]}")

    spaces = fb_get("spaces", {}) or {}
    sent = 0
    for key, space_data in spaces.items():
        space_name = space_data.get("name", f"spaces/{key}") if isinstance(space_data, dict) else f"spaces/{key}"
        if send_to_space(space_name, text_message(f"📡 *ANNOUNCEMENT:*\n\n{msg_text}")):
            sent += 1
    return text_message(f"📡 Announcement sent to {sent} space(s).")


# ════════════════════════════════════════════
#  SERVER STATUS
# ════════════════════════════════════════════

def handle_server_status():
    uptime_sec = int(time.time() - BOT_START_TIME)
    h, rem = divmod(uptime_sec, 3600)
    m, s = divmod(rem, 60)

    try:
        cpu = psutil.cpu_percent(interval=1)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        mem_used, mem_total = mem.used // (1024 ** 2), mem.total // (1024 ** 2)
        disk_used, disk_total = disk.used // (1024 ** 3), disk.total // (1024 ** 3)
    except Exception:
        cpu = mem_used = mem_total = disk_used = disk_total = 0

    status_txt = (
        "📊 *SERVER STATUS*\n"
        "─────────────────\n"
        f"⏰ Uptime: {h}h {m}m {s}s\n"
        f"⚡ CPU: {cpu:.1f}%\n"
        f"💾 Memory: {mem_used}/{mem_total} MB\n"
        f"💿 Disk: {disk_used}/{disk_total} GB\n"
        f"🔥 Firebase: {'✅ Connected' if firebase_ok else '❌ Offline'}\n"
        f"🤖 AutoReplies: {len(get_auto_replies())}\n"
        f"🚫 Banned: {len(get_banned_users())}\n"
        f"🕒 Checked: {now_str()}"
    )
    return text_message(status_txt)


# ════════════════════════════════════════════
#  CORE EVENT HANDLING
# ════════════════════════════════════════════

def handle_added_to_space(event: dict):
    space = event.get("space", {})
    user = event.get("user", {})
    fb_set(f"spaces/{sanitize_key(space.get('name', ''))}", {
        "name": space.get("name"),
        "displayName": space.get("displayName", space.get("name")),
        "type": space.get("type"),
        "joined": int(time.time() * 1000)
    })
    update_stat("total_spaces")
    fb_log("INFO", f"Bot added to space: {space.get('displayName', space.get('name'))}")

    if space.get("type") == "DM" and is_owner(user.get("email", "")):
        fb_set("config/owner_space", space.get("name"))

    return text_message("👋 Hi! I'm online. Type \"/help\" to see what I can do.")


def handle_removed_from_space(event: dict):
    space = event.get("space", {})
    fb_delete(f"spaces/{sanitize_key(space.get('name', ''))}")
    fb_log("INFO", f"Bot removed from space: {space.get('displayName', space.get('name'))}")
    return {}


def handle_message_event(event: dict):
    update_stat("total_messages")

    user = event.get("user", {}) or {}
    user_email = (user.get("email") or "").lower()
    user_name = user.get("displayName", "User")
    space = event.get("space", {}) or {}
    message = event.get("message", {}) or {}
    raw_text = message.get("argumentText") or message.get("text") or ""
    text = raw_text.strip()
    text_lower = text.lower()

    if space.get("type") == "DM" and is_owner(user_email):
        fb_set("config/owner_space", space.get("name"))

    if is_banned(user_email):
        return text_message("🚫 You are banned from using this bot.")

    # ── AFK return notice
    afk_info = fb_get(f"afk/{sanitize_key(user_email)}", None)
    if afk_info:
        fb_delete(f"afk/{sanitize_key(user_email)}")
        elapsed = int(time.time() * 1000 - afk_info["time"]) // 1000
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        send_to_space(space.get("name"), text_message(f"👋 *{user_name}* is back after {h}h {m}m {s}s!"))

    # ── Slash commands
    if text.startswith("/"):
        return handle_command(text, user_email, user_name, space)

    # ── Blocked words
    for word in get_blocked_words():
        if word in text_lower:
            fb_log("WARN", f"Blocked word '{word}' detected from {user_name}")
            return text_message(f"🚫 *{user_name}*, restricted word detected.")

    # ── Auto-reply
    compact = "".join(text_lower.split())
    for key, reply in get_auto_replies().items():
        if key in compact or key == compact:
            fb_log("INFO", f"AutoReply triggered: {key}")
            return text_message(reply)

    # ── @mention or DM → AI fallback
    annotations = message.get("annotations", []) or []
    was_mentioned = any(a.get("type") == "USER_MENTION" for a in annotations)
    if was_mentioned or space.get("type") == "DM":
        return handle_ai(text, user_name)

    return {}  # ignore plain group chatter that matched nothing


# ════════════════════════════════════════════
#  FLASK APP / WEBHOOK
# ════════════════════════════════════════════

app = Flask(__name__)


@app.route("/", methods=["GET"])
def health_check():
    return jsonify({"status": "ok", "firebase": firebase_ok, "uptime_sec": int(time.time() - BOT_START_TIME)})


@app.route("/chat", methods=["POST"])
def chat_webhook():
    try:
        event = request.get_json(force=True, silent=True) or {}
        event_type = event.get("type", "")

        if event_type == "ADDED_TO_SPACE":
            return jsonify(handle_added_to_space(event))
        if event_type == "REMOVED_FROM_SPACE":
            return jsonify(handle_removed_from_space(event))
        if event_type == "MESSAGE":
            return jsonify(handle_message_event(event))

        return jsonify({})
    except Exception as e:
        log.error(f"Webhook error: {e}")
        return jsonify(text_message("❌ Something went wrong handling that message."))


# ════════════════════════════════════════════
#  PANEL COMMAND POLLER  (background thread, works natively
#  here since this is a real persistent process — unlike Apps Script)
# ════════════════════════════════════════════

last_cmd_check = 0


def poll_panel_commands():
    global last_cmd_check
    while True:
        try:
            if firebase_ok:
                cmds = fb_get("commands", {}) or {}
                owner_space = fb_get("config/owner_space", None)
                for key, cmd_data in cmds.items():
                    if isinstance(cmd_data, dict):
                        ts = cmd_data.get("time", 0)
                        if ts > last_cmd_check:
                            log.info(f"Panel command: {cmd_data.get('cmd')}")
                            fb_delete(f"commands/{key}")
                            if owner_space:
                                send_to_space(owner_space, text_message(f"📤 Panel command: `{cmd_data.get('cmd')}`"))
                last_cmd_check = int(time.time() * 1000)
        except Exception as e:
            log.error(f"poll_panel_commands error: {e}")
        time.sleep(5)


# ════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════

def bootstrap():
    log.info("═" * 50)
    log.info("  ChatBot Starting...")
    log.info("═" * 50)

    init_firebase()
    fb_set("stats/last_start", int(time.time() * 1000))

    t = threading.Thread(target=poll_panel_commands, daemon=True)
    t.start()

    log.info(f"✅ Bot ready | Owner: {OWNER_EMAIL}")


bootstrap()  # runs on import, so it also works under gunicorn

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
