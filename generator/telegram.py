"""Optional Telegram notifications (send only). Token and chat id come from env; with neither set this is a no-op."""
import os

import requests

LIMIT = 4000  # Telegram's hard limit is 4096 characters


def configured():
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def send(text, photo=None, post=requests.post):
    """Send `text` (plain text, optionally with a photo). Returns True if sent, False if not configured.
    Errors are raised without the token (it is part of the URL), so logs never leak it."""
    if not configured():
        return False
    token, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    text = text[:LIMIT]
    try:
        if photo:
            with open(photo, "rb") as f:
                r = post(f"https://api.telegram.org/bot{token}/sendPhoto",
                         data={"chat_id": chat, "caption": text[:1000]}, files={"photo": f}, timeout=60)
        else:
            r = post(f"https://api.telegram.org/bot{token}/sendMessage",
                     data={"chat_id": chat, "text": text, "disable_web_page_preview": "true"}, timeout=30)
    except requests.RequestException as e:
        raise RuntimeError(f"Telegram request failed: {type(e).__name__}") from None
    if r.status_code != 200:
        try:
            why = f": {str(r.json().get('description', ''))[:150]}"  # e.g. "chat not found"; never contains the token
        except Exception:
            why = ""
        raise RuntimeError(f"Telegram returned HTTP {r.status_code}{why}")
    return True


def chats(get=requests.get):
    """Who has messaged the bot recently: [(chat_id, type, name)]. For finding the right TELEGRAM_CHAT_ID."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    try:
        r = get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30)
    except requests.RequestException as e:
        raise RuntimeError(f"Telegram request failed: {type(e).__name__}") from None
    if r.status_code != 200:
        raise RuntimeError(f"Telegram returned HTTP {r.status_code}")
    seen = {}
    for u in r.json().get("result", []):
        c = (u.get("message") or u.get("edited_message") or {}).get("chat")
        if c:
            seen[c["id"]] = (c["id"], c.get("type", "?"), c.get("first_name") or c.get("title") or c.get("username") or "")
    return list(seen.values())
