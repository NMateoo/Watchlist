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


# Telegram rechaza mensajes de más de 4096 caracteres (el envío entero falla
# y el aviso se pierde). Troceamos con margen antes de llegar a ese límite.
MAX_MESSAGE_CHARS = 4000


def split_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Trocea un mensaje largo en varios de como mucho `limit` caracteres.
    Corta por saltos de línea para no partir etiquetas HTML (en nuestros
    mensajes ninguna etiqueta cruza de una línea a otra)."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        if current and len(current) + 1 + len(line) > limit:
            parts.append(current)
            current = ""
        while len(line) > limit:  # línea suelta más larga que el límite
            parts.append(line[:limit])
            line = line[limit:]
        current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    return parts


def _post_message(text: str, chat_id: str | None) -> bool:
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


def send_message(text: str, chat_id: str | None = None) -> bool:
    """Envía `text` (HTML) a `chat_id` (por defecto, el admin), troceándolo en
    varios mensajes si supera el límite de Telegram. True si todo se envió."""
    if not is_configured():
        log.info("Telegram sin configurar; mensaje omitido: %.60s...", text)
        return False
    ok = True
    for part in split_message(text):
        ok = _post_message(part, chat_id) and ok
    return ok


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
