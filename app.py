import os
import base64
import sqlite3
import time
from typing import Optional, Tuple, List, Dict, Any

import requests
from flask import Flask, request

app = Flask(__name__)

# --- ENV ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

# Секрет в URL вебхука (чтобы никто не мог просто так спамить твоему серверу)
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change_me").strip()

# Модель Gemini (можешь менять при желании)
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()

# Файл базы данных
DB_PATH = os.environ.get("DB_PATH", "memory.db").strip()

# Gemini endpoint
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

# --- Basic checks ---
if not TELEGRAM_BOT_TOKEN:
    print("WARNING: TELEGRAM_BOT_TOKEN is empty")
if not GEMINI_API_KEY:
    print("WARNING: GEMINI_API_KEY is empty")


# ----------------------------
# DB (SQLite) - simple memory
# ----------------------------
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def db_init():
    conn = db_connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            mode TEXT DEFAULT 'strict',
            memory TEXT DEFAULT '',
            updated_at INTEGER
        )
        """
    )
    conn.commit()
    conn.close()


def db_upsert_user(chat_id: int, username: str, first_name: str, last_name: str):
    now = int(time.time())
    conn = db_connect()
    conn.execute(
        """
        INSERT INTO users(chat_id, username, first_name, last_name, updated_at)
        VALUES(?, ?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            last_name=excluded.last_name,
            updated_at=excluded.updated_at
        """,
        (chat_id, username, first_name, last_name, now),
    )
    conn.commit()
    conn.close()


def db_get_user(chat_id: int) -> Dict[str, Any]:
    conn = db_connect()
    cur = conn.execute(
        "SELECT chat_id, username, first_name, last_name, mode, memory, updated_at FROM users WHERE chat_id=?",
        (chat_id,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return {
            "chat_id": chat_id,
            "username": "",
            "first_name": "",
            "last_name": "",
            "mode": "strict",
            "memory": "",
            "updated_at": 0,
        }
    return {
        "chat_id": row[0],
        "username": row[1] or "",
        "first_name": row[2] or "",
        "last_name": row[3] or "",
        "mode": row[4] or "strict",
        "memory": row[5] or "",
        "updated_at": row[6] or 0,
    }


def db_set_mode(chat_id: int, mode: str):
    now = int(time.time())
    conn = db_connect()
    conn.execute(
        """
        INSERT INTO users(chat_id, mode, updated_at) VALUES(?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET mode=excluded.mode, updated_at=excluded.updated_at
        """,
        (chat_id, mode, now),
    )
    conn.commit()
    conn.close()


def db_append_memory(chat_id: int, note: str):
    now = int(time.time())
    conn = db_connect()
    cur = conn.execute("SELECT memory FROM users WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    current = (row[0] if row and row[0] else "").strip()

    if current:
        new_mem = current + "\n- " + note.strip()
    else:
        new_mem = "- " + note.strip()

    conn.execute(
        """
        INSERT INTO users(chat_id, memory, updated_at) VALUES(?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET memory=excluded.memory, updated_at=excluded.updated_at
        """,
        (chat_id, new_mem, now),
    )
    conn.commit()
    conn.close()


def db_forget(chat_id: int):
    now = int(time.time())
    conn = db_connect()
    conn.execute(
        """
        INSERT INTO users(chat_id, memory, updated_at) VALUES(?, '', ?)
        ON CONFLICT(chat_id) DO UPDATE SET memory='', updated_at=excluded.updated_at
        """,
        (chat_id, now),
    )
    conn.commit()
    conn.close()


# ----------------------------
# Telegram helpers
# ----------------------------
def tg_post(method: str, payload: dict):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    return requests.post(url, json=payload, timeout=60)


def tg_get(method: str, params: dict):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    return requests.get(url, params=params, timeout=60)


def tg_send(chat_id: int, text: str):
    # Telegram limit ~4096, оставим запас
    text = text or ""
    if len(text) <= 3900:
        tg_post("sendMessage", {"chat_id": chat_id, "text": text})
        return

    # Если очень длинно — режем на части
    chunk = 3800
    for i in range(0, len(text), chunk):
        tg_post("sendMessage", {"chat_id": chat_id, "text": text[i : i + chunk]})


# ----------------------------
# Gemini helpers
# ----------------------------
def gemini_generate(parts: List[Dict[str, Any]]) -> str:
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    body = {"contents": [{"role": "user", "parts": parts}]}
    r = requests.post(GEMINI_URL, headers=headers, json=body, timeout=90)
    r.raise_for_status()
    data = r.json()

    # Берём первый candidate
    cand = data.get("candidates", [{}])[0]
    content = cand.get("content", {})
    out_parts = content.get("parts", [])
    if out_parts and isinstance(out_parts[0], dict):
        return out_parts[0].get("text", "").strip() or "(пустой ответ)"
    return "(пустой ответ)"


def download_telegram_photo(file_id: str) -> Tuple[bytes, str]:
    info = tg_get("getFile", {"file_id": file_id}).json()
    file_path = info["result"]["file_path"]
    file_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    img = requests.get(file_url, timeout=60).content
    # Telegram фото обычно jpeg
    return img, "image/jpeg"


# ----------------------------
# Prompts / modes
# ----------------------------
def system_prompt_for_mode(mode: str) -> str:
    """
    Важно: мы можем делать "дерзко" и "саркастично", но НЕ травить людей/группы,
    НЕ разжигать ненависть, НЕ призывать к насилию.
    """
    base_rules = (
        "Ты — помощник в Telegram. Отвечай по-русски, если пользователь пишет по-русски.\n"
        "Помогай как репетитор: объясняй шаги, логику, проверки, давай понятное решение.\n"
        "Не собирай лишние персональные данные. Не проси пароли/коды.\n"
        "Если пользователь просит сделать что-то незаконное/опасное — откажись.\n"
    )

    if mode == "polite":
        style = (
            "Тон: очень вежливый, спокойный, поддерживающий. Без мата. "
            "Больше пояснений, аккуратные формулировки."
        )
    elif mode == "savage":
        style = (
            "Тон: дерзкий 'как кореш', допускается сарказм и крепкие слова, "
            "НО без унижения людей/групп и без травли третьих лиц. "
            "Если пользователь ошибается — говори прямо. Коротко и по делу."
        )
    else:  # strict (default)
        style = (
            "Тон: деловой, прямой, без лишней воды. Без мата. "
            "Фокус на точности и ясности."
        )

    return base_rules + "\n" + style


def build_parts(user_text: str, user_mode: str, user_memory: str, image_inline: Optional[Dict[str, Any]]):
    parts: List[Dict[str, Any]] = []

    # System-like instruction (Gemini принимает как обычный текст в начале)
    sys_text = system_prompt_for_mode(user_mode)
    if user_memory.strip():
        sys_text += "\n\nПамять о пользователе (используй только для персонализации ответов):\n" + user_memory.strip()

    parts.append({"text": sys_text})

    if image_inline:
        parts.append(image_inline)

    if user_text.strip():
        parts.append({"text": user_text.strip()})
    else:
        # Если пришло только фото
        parts.append({"text": "Разбери изображение. Объясни задачу и дай решение шагами. В конце — краткий ответ."})

    return parts


# ----------------------------
# Commands
# ----------------------------
HELP_TEXT = (
    "Команды:\n"
    "/help — помощь\n"
    "/mode polite|strict|savage — режим тона\n"
    "/remember <факт> — запомнить о тебе (предпочтения/контекст)\n"
    "/whoami — что я знаю о тебе\n"
    "/forget — очистить память о тебе\n\n"
    "Можно просто прислать текст или фото задания — отвечу."
)


def handle_command(chat_id: int, text: str):
    t = text.strip()
    if t.startswith("/help"):
        tg_send(chat_id, HELP_TEXT)
        return True

    if t.startswith("/mode"):
        parts = t.split(maxsplit=1)
        if len(parts) < 2:
            tg_send(chat_id, "Напиши так: /mode polite или /mode strict или /mode savage")
            return True
        mode = parts[1].strip().lower()
        if mode not in ("polite", "strict", "savage"):
            tg_send(chat_id, "Доступно только: polite, strict, savage. Пример: /mode savage")
            return True
        db_set_mode(chat_id, mode)
        tg_send(chat_id, f"Ок. Режим теперь: {mode}")
        return True

    if t.startswith("/remember"):
        parts = t.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            tg_send(chat_id, "Напиши так: /remember я учусь на юрфаке, люблю короткие ответы")
            return True
        note = parts[1].strip()
        # минимальная защита: не поощряем хранение секретов
        if any(x in note.lower() for x in ["пароль", "password", "код", "2fa", "otp", "секрет", "private key", "ключ"]):
            tg_send(chat_id, "Не сохраняю пароли/коды/ключи. Сохрани что-то безопасное: предпочтения, контекст, формат ответов.")
            return True
        db_append_memory(chat_id, note)
        tg_send(chat_id, "Запомнил.")
        return True

    if t.startswith("/whoami"):
        u = db_get_user(chat_id)
        name = " ".join([p for p in [u.get("first_name", ""), u.get("last_name", "")] if p]).strip()
        uname = u.get("username", "")
        mode = u.get("mode", "strict")
        mem = u.get("memory", "").strip() or "(пусто)"
        tg_send(
            chat_id,
            f"Я вижу тебя так:\n"
            f"- имя: {name or '(неизвестно)'}\n"
            f"- username: @{uname}" if uname else f"Я вижу тебя так:\n- имя: {name or '(неизвестно)'}\n- username: (нет)\n"
        )
        # чтобы красиво вывести вместе:
        if uname:
            tg_send(chat_id, f"- режим: {mode}\n- память:\n{mem}")
        else:
            tg_send(chat_id, f"- режим: {mode}\n- память:\n{mem}")
        return True

    if t.startswith("/forget"):
        db_forget(chat_id)
        tg_send(chat_id, "Ок, память очищена.")
        return True

    return False


# ----------------------------
# Flask routes
# ----------------------------
@app.get("/")
def health():
    return "OK"


@app.post(f"/webhook/{WEBHOOK_SECRET}")
def webhook():
    update = request.get_json(silent=True) or {}

    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return "no message", 200

    chat = msg.get("chat", {})
    chat_id = int(chat.get("id"))

    from_user = msg.get("from", {}) or {}
    username = from_user.get("username", "") or ""
    first_name = from_user.get("first_name", "") or ""
    last_name = from_user.get("last_name", "") or ""

    # Ensure user exists
    db_upsert_user(chat_id, username, first_name, last_name)
    user = db_get_user(chat_id)

    text = (msg.get("text") or "").strip()

    # Команды
    if text.startswith("/"):
        handled = handle_command(chat_id, text)
        if handled:
            return "ok", 200
        # неизвестная команда
        tg_send(chat_id, "Не понял команду. /help")
        return "ok", 200

    # Фото (берём самое большое)
    image_inline = None
    if msg.get("photo"):
        largest = msg["photo"][-1]
        file_id = largest["file_id"]
        try:
            img_bytes, mime = download_telegram_photo(file_id)
            b64 = base64.b64encode(img_bytes).decode("utf-8")
            image_inline = {"inline_data": {"mime_type": mime, "data": b64}}
        except Exception as e:
            tg_send(chat_id, f"Не смог скачать фото: {e}")
            return "ok", 200

    # Если совсем ничего
    if not text and not image_inline:
        tg_send(chat_id, "Пришли текст или фото. /help")
        return "ok", 200

    # Собираем запрос
    parts = build_parts(
        user_text=text,
        user_mode=user.get("mode", "strict"),
        user_memory=user.get("memory", ""),
        image_inline=image_inline,
    )

    try:
        answer = gemini_generate(parts)
    except Exception as e:
        tg_send(chat_id, f"Ошибка Gemini: {e}")
        return "ok", 200

    tg_send(chat_id, answer)
    return "ok", 200


# Init DB on import
db_init()

if __name__ == "__main__":
    # локально: python app.py
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
