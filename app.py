import os
import base64
import requests
from flask import Flask, request

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "secret")

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

def tg(method: str, payload: dict | None = None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    return requests.post(url, json=payload or {}, timeout=60)

def tg_get(method: str, params: dict | None = None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    return requests.get(url, params=params or {}, timeout=60)

def gemini_generate(parts: list[dict]) -> str:
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    body = {"contents": [{"role": "user", "parts": parts}]}
    r = requests.post(GEMINI_URL, headers=headers, json=body, timeout=90)
    r.raise_for_status()
    data = r.json()
    # Берём первый ответ
    return data["candidates"][0]["content"]["parts"][0].get("text", "(пустой ответ)")

def download_telegram_photo(file_id: str) -> tuple[bytes, str]:
    # 1) получаем путь файла
    info = tg_get("getFile", {"file_id": file_id}).json()
    file_path = info["result"]["file_path"]
    # 2) скачиваем bytes
    file_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    img = requests.get(file_url, timeout=60).content
    # telegram обычно отдаёт jpg для фото
    return img, "image/jpeg"

@app.get("/")
def health():
    return "OK"

@app.post(f"/webhook/{WEBHOOK_SECRET}")
def webhook():
    update = request.get_json(silent=True) or {}

    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return "no message", 200

    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()

    # Если пришла фотка — берём самую большую
    parts = []
    if msg.get("photo"):
        largest = msg["photo"][-1]
        file_id = largest["file_id"]
        img_bytes, mime = download_telegram_photo(file_id)
        b64 = base64.b64encode(img_bytes).decode("utf-8")
        parts.append({"inline_data": {"mime_type": mime, "data": b64}})

    if text:
        parts.append({"text": text})
    else:
        # если текста нет, но есть фото — добавим вопрос по умолчанию
        if parts:
            parts.append({"text": "Реши/объясни задание с фото. Дай понятное решение и вывод."})
        else:
            tg("sendMessage", {"chat_id": chat_id, "text": "Пришли текст или фото задания."})
            return "ok", 200

    # Небольшая “этика”: помогать объяснять/решать — ок, но лучше переформулировать под обучение
    system_hint = (
        "Помоги как репетитор: объясняй шаги, логику и проверки. "
        "Если это домашнее задание, сделай так, чтобы студент понял и смог сам оформить ответ."
    )
    parts.insert(0, {"text": system_hint})

    try:
        answer = gemini_generate(parts)
    except Exception as e:
        tg("sendMessage", {"chat_id": chat_id, "text": f"Ошибка при запросе к Gemini: {e}"})
        return "ok", 200

    tg("sendMessage", {"chat_id": chat_id, "text": answer[:3900]})
    return "ok", 200
