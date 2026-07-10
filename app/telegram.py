"""Envío de mensajes al bot de Telegram (solo salida, no escucha comandos)."""
from __future__ import annotations

import logging

import httpx

from app import config

log = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/{method}"

# Cliente HTTP compartido: reutiliza conexiones en vez de abrir una por mensaje.
_client = httpx.Client()


def is_configured() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


def send_message(text: str, chat_id: str | None = None) -> bool:
    """Envía `text` (HTML) a `chat_id` (por defecto, el admin). True si se envió."""
    if not is_configured():
        log.info("Telegram sin configurar; mensaje omitido: %.60s...", text)
        return False
    url = API_URL.format(token=config.TELEGRAM_BOT_TOKEN, method="sendMessage")
    payload = {
        "chat_id": chat_id or config.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = _client.post(url, json=payload, timeout=15)
        if resp.status_code != 200:
            log.error("Telegram respondió %s: %s", resp.status_code, resp.text)
            return False
        return True
    except Exception as exc:
        log.error("Error enviando a Telegram: %s", exc)
        return False


def get_chat_id_hint() -> str | None:
    """Intenta descubrir el chat_id leyendo los últimos mensajes al bot."""
    if not config.TELEGRAM_BOT_TOKEN:
        return None
    url = API_URL.format(token=config.TELEGRAM_BOT_TOKEN, method="getUpdates")
    try:
        data = _client.get(url, timeout=15).json()
        for update in reversed(data.get("result", [])):
            chat = update.get("message", {}).get("chat")
            if chat:
                return str(chat["id"])
    except Exception as exc:
        log.warning("No se pudo consultar getUpdates: %s", exc)
    return None
