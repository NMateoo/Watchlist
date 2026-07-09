"""Bot interactivo de Telegram: toda la watchlist manejable con menús y comandos.

Usa long polling (getUpdates) en un hilo propio. Solo atiende al chat
configurado en TELEGRAM_CHAT_ID; el resto de chats se ignoran.
"""
from __future__ import annotations

import logging
import threading
import time

import httpx
from sqlalchemy import select

from app import config, prices, scheduler
from app import alerts as alerts_mod
from app import telegram
from app.database import (
    Alert,
    SessionLocal,
    Stock,
    Watchlist,
    get_check_interval,
    get_move_threshold,
    get_summary_time,
    set_setting,
)

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"

MARKET_LABELS = {"open": "🟢 Abierto", "pre": "🟡 Pre-market", "post": "🟣 After-hours", "closed": "⚪ Cerrado"}

# Acción pendiente de respuesta de texto: {"action": ..., datos extra}
_pending: dict = {}
_stop_event = threading.Event()


# ------------------------------------------------------------ API helpers


def _call(method: str, **payload) -> dict:
    try:
        resp = httpx.post(
            API.format(token=config.TELEGRAM_BOT_TOKEN, method=method), json=payload, timeout=35
        )
        data = resp.json()
        if not data.get("ok"):
            log.warning("Telegram %s: %s", method, data.get("description"))
        return data
    except Exception as exc:
        log.error("Telegram %s: %s", method, exc)
        return {"ok": False}


def _send(text: str, keyboard: list | None = None) -> dict:
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    return _call("sendMessage", **payload)


def _edit(message_id: int, text: str, keyboard: list | None = None) -> None:
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    result = _call("editMessageText", **payload)
    # Si el mensaje es viejo y ya no se puede editar, mandamos uno nuevo.
    if not result.get("ok"):
        _send(text, keyboard)


def _btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def _show(text: str, keyboard: list, message_id: int | None) -> None:
    if message_id:
        _edit(message_id, text, keyboard)
    else:
        _send(text, keyboard)


# ------------------------------------------------------------ vistas/menús


def _fmt(value, currency="USD"):
    return alerts_mod.fmt_price(value, currency)


def _main_menu(message_id: int | None = None) -> None:
    keyboard = [
        [_btn("📋 Mis listas", "lists")],
        [_btn("➕ Añadir valor", "add"), _btn("🔔 Alertas", "alerts")],
        [_btn("⚙️ Ajustes", "settings"), _btn("📊 Resumen ahora", "summary")],
    ]
    _show("<b>📈 Watchlist</b>\n¿Qué quieres hacer?", keyboard, message_id)


def _lists_view(message_id: int | None = None) -> None:
    with SessionLocal() as session:
        watchlists = session.scalars(select(Watchlist).order_by(Watchlist.id)).all()
        rows = [[_btn(f"📋 {wl.name} ({len(wl.stocks)})", f"l:{wl.id}")] for wl in watchlists]
    rows.append([_btn("➕ Nueva lista", "lnew"), _btn("◀️ Menú", "menu")])
    _lists_msg = "<b>Tus listas</b>" if watchlists else "No tienes listas todavía."
    _show(_lists_msg, rows, message_id)


def _list_view(list_id: int, message_id: int | None = None) -> None:
    with SessionLocal() as session:
        wl = session.get(Watchlist, list_id)
        if not wl:
            _lists_view(message_id)
            return
        stocks = sorted(wl.stocks, key=lambda s: s.ticker)
        name = wl.name
    quotes = prices.get_quotes([s.ticker for s in stocks])
    lines = [f"<b>📋 {name}</b>"]
    for stock in stocks:
        q = quotes.get(stock.ticker)
        if q:
            emoji = "🟢" if q["change_pct"] >= 0 else "🔴"
            lines.append(
                f"{emoji} <b>{stock.ticker}</b>  {_fmt(q['price'], q['currency'])}"
                f"  ({alerts_mod.fmt_pct(q['change_pct'])})"
            )
        else:
            lines.append(f"• <b>{stock.ticker}</b>: sin datos")
    if not stocks:
        lines.append("Lista vacía.")
    rows = [[_btn(s.ticker, f"s:{s.id}")] for s in stocks]
    rows.append([_btn("➕ Añadir aquí", f"addl:{list_id}"), _btn("🗑 Eliminar lista", f"ldel:{list_id}")])
    rows.append([_btn("◀️ Listas", "lists"), _btn("🔄 Actualizar", f"l:{list_id}")])
    _show("\n".join(lines), rows, message_id)


def _stock_view(stock_id: int, message_id: int | None = None) -> None:
    with SessionLocal() as session:
        stock = session.get(Stock, stock_id)
        if not stock:
            _lists_view(message_id)
            return
        alerts = sorted(stock.alerts, key=lambda a: a.created_at, reverse=True)
        info = {
            "ticker": stock.ticker, "name": stock.name, "currency": stock.currency,
            "notes": stock.notes, "target": stock.target_price, "list_id": stock.watchlist_id,
        }
        alert_lines = []
        for a in alerts:
            arrow = "⬆️ sube de" if a.kind == "above" else "⬇️ baja de"
            state = "" if a.active else " (disparada)"
            alert_lines.append(f"  {arrow} {_fmt(a.threshold, stock.currency)}{state}")
    q = prices.get_quote(info["ticker"])
    lines = [f"<b>{info['ticker']}</b> — {info['name']}"]
    if q:
        emoji = "🟢" if q["change_pct"] >= 0 else "🔴"
        lines.append(f"{emoji} <b>{_fmt(q['price'], q['currency'])}</b>  ({alerts_mod.fmt_pct(q['change_pct'])} hoy)")
        lines.append(MARKET_LABELS.get(q.get("market_state"), ""))
        if q.get("year_low") and q.get("year_high"):
            lines.append(f"Rango 52 sem.: {_fmt(q['year_low'], q['currency'])} – {_fmt(q['year_high'], q['currency'])}")
    if info["target"]:
        lines.append(f"🎯 Objetivo: {_fmt(info['target'], info['currency'])}")
    if alert_lines:
        lines.append("🔔 Alertas:")
        lines.extend(alert_lines)
    if info["notes"]:
        notes = info["notes"][:300] + ("…" if len(info["notes"]) > 300 else "")
        lines.append(f"📝 {notes}")
    keyboard = [
        [_btn("🔔 Nueva alerta", f"alnew:{stock_id}"), _btn("🎯 Objetivo", f"target:{stock_id}")],
        [_btn("📝 Notas", f"notes:{stock_id}"), _btn("🗑 Quitar", f"sd:{stock_id}")],
        [_btn("◀️ Volver", f"l:{info['list_id']}"), _btn("🔄 Actualizar", f"s:{stock_id}")],
    ]
    _show("\n".join(filter(None, lines)), keyboard, message_id)


def _alerts_view(message_id: int | None = None) -> None:
    with SessionLocal() as session:
        alerts = session.scalars(select(Alert).order_by(Alert.created_at.desc())).all()
        rows, lines = [], ["<b>🔔 Tus alertas</b>"]
        for a in alerts:
            arrow = ">" if a.kind == "above" else "<"
            label = f"{a.stock.ticker} {arrow} {_fmt(a.threshold, a.stock.currency)}"
            if a.active:
                rows.append([_btn(f"❌ {label}", f"ad:{a.id}")])
            else:
                rows.append([_btn(f"🔄 Reactivar {label}", f"ar:{a.id}"), _btn("❌", f"ad:{a.id}")])
        if not alerts:
            lines.append("No tienes alertas. Créalas desde la ficha de un valor.")
        else:
            lines.append("Toca ❌ para eliminar o 🔄 para reactivar una disparada.")
    rows.append([_btn("◀️ Menú", "menu")])
    _show("\n".join(lines), rows, message_id)


def _settings_view(message_id: int | None = None) -> None:
    with SessionLocal() as session:
        move = get_move_threshold(session)
        interval = get_check_interval(session)
        summary = get_summary_time(session)
    text = (
        "<b>⚙️ Ajustes</b>\n"
        f"⚡ Aviso de cambio brusco: <b>±{move}%</b>\n"
        f"⏱ Comprobación de alertas: cada <b>{interval} min</b>\n"
        f"🕙 Resumen diario: a las <b>{summary}</b> ({config.TIMEZONE})"
    )
    keyboard = [
        [_btn("⚡ Cambiar umbral %", "set:move")],
        [_btn("⏱ Cambiar intervalo", "set:interval")],
        [_btn("🕙 Cambiar hora resumen", "set:summary")],
        [_btn("◀️ Menú", "menu")],
    ]
    _show(text, keyboard, message_id)


def _pick_list_keyboard(symbol: str) -> list:
    with SessionLocal() as session:
        watchlists = session.scalars(select(Watchlist).order_by(Watchlist.id)).all()
        rows = [[_btn(f"📋 {wl.name}", f"addto:{symbol}:{wl.id}")] for wl in watchlists]
    rows.append([_btn("Cancelar", "menu")])
    return rows


# ------------------------------------------------------------ acciones


def _add_stock(symbol: str, list_id: int, message_id: int | None) -> None:
    symbol = symbol.upper()
    with SessionLocal() as session:
        wl = session.get(Watchlist, list_id)
        if not wl:
            _send("Esa lista ya no existe.")
            return
        exists = session.scalar(
            select(Stock).where(Stock.ticker == symbol, Stock.watchlist_id == list_id)
        )
        if exists:
            _send(f"{symbol} ya está en «{wl.name}».")
            _stock_view(exists.id, message_id)
            return
    quote = prices.get_quote(symbol)
    if not quote:
        _send(f"No encuentro cotización para {symbol}.")
        return
    with SessionLocal() as session:
        stock = Stock(
            ticker=symbol, watchlist_id=list_id,
            name=prices.lookup_name(symbol), currency=quote["currency"],
        )
        session.add(stock)
        session.commit()
        stock_id = stock.id
    _stock_view(stock_id, message_id)


def _handle_search_text(text: str, list_id: int | None) -> None:
    results = prices.search_symbols(text, limit=6)
    if not results:
        _send(f"No encuentro nada para «{text}». Prueba con otro nombre o el ticker exacto.")
        return
    rows = []
    for r in results:
        label = f"{r['symbol']} — {r['name'][:28]}" if r["name"] else r["symbol"]
        target = f"addto:{r['symbol']}:{list_id}" if list_id else f"pick:{r['symbol']}"
        rows.append([_btn(label, target)])
    rows.append([_btn("Cancelar", "menu")])
    _send("Elige el valor:", rows)


# ------------------------------------------------------------ despacho


def _handle_callback(update: dict) -> None:
    query = update["callback_query"]
    chat_id = str(query.get("message", {}).get("chat", {}).get("id", ""))
    if chat_id != str(config.TELEGRAM_CHAT_ID):
        return
    _call("answerCallbackQuery", callback_query_id=query["id"])
    message_id = query["message"]["message_id"]
    data = query.get("data", "")
    parts = data.split(":")
    action, args = parts[0], parts[1:]
    _pending.clear()

    if action == "menu":
        _main_menu(message_id)
    elif action == "lists":
        _lists_view(message_id)
    elif action == "l":
        _list_view(int(args[0]), message_id)
    elif action == "lnew":
        _pending.update({"action": "new_list"})
        _send("Escríbeme el nombre de la nueva lista:", [[_btn("Cancelar", "menu")]])
    elif action == "ldel":
        keyboard = [[_btn("Sí, eliminar", f"ldel2:{args[0]}"), _btn("No", f"l:{args[0]}")]]
        _edit(message_id, "¿Eliminar la lista con todo su contenido?", keyboard)
    elif action == "ldel2":
        with SessionLocal() as session:
            wl = session.get(Watchlist, int(args[0]))
            if wl:
                session.delete(wl)
                session.commit()
        _lists_view(message_id)
    elif action == "s":
        _stock_view(int(args[0]), message_id)
    elif action == "sd":
        keyboard = [[_btn("Sí, quitar", f"sd2:{args[0]}"), _btn("No", f"s:{args[0]}")]]
        _edit(message_id, "¿Quitar este valor de la lista?", keyboard)
    elif action == "sd2":
        with SessionLocal() as session:
            stock = session.get(Stock, int(args[0]))
            list_id = stock.watchlist_id if stock else None
            if stock:
                session.delete(stock)
                session.commit()
        _list_view(list_id, message_id) if list_id else _lists_view(message_id)
    elif action == "add":
        _pending.update({"action": "search", "list_id": None})
        _send("Escríbeme el nombre o ticker (ej: apple, SAN.MC, oro):", [[_btn("Cancelar", "menu")]])
    elif action == "addl":
        _pending.update({"action": "search", "list_id": int(args[0])})
        _send("Escríbeme el nombre o ticker (ej: apple, SAN.MC, oro):", [[_btn("Cancelar", "menu")]])
    elif action == "pick":
        symbol = args[0]
        with SessionLocal() as session:
            watchlists = session.scalars(select(Watchlist)).all()
        if len(watchlists) == 1:
            _add_stock(symbol, watchlists[0].id, message_id)
        else:
            _edit(message_id, f"¿A qué lista añado <b>{symbol}</b>?", _pick_list_keyboard(symbol))
    elif action == "addto":
        _add_stock(args[0], int(args[1]), message_id)
    elif action == "alnew":
        keyboard = [
            [_btn("⬆️ Si sube de…", f"alk:{args[0]}:above"), _btn("⬇️ Si baja de…", f"alk:{args[0]}:below")],
            [_btn("Cancelar", f"s:{args[0]}")],
        ]
        _edit(message_id, "¿Qué tipo de alerta?", keyboard)
    elif action == "alk":
        _pending.update({"action": "alert_price", "stock_id": int(args[0]), "kind": args[1]})
        _send("Escríbeme el precio del umbral (ej: 150.50):", [[_btn("Cancelar", f"s:{args[0]}")]])
    elif action == "ad":
        with SessionLocal() as session:
            alert = session.get(Alert, int(args[0]))
            if alert:
                session.delete(alert)
                session.commit()
        _alerts_view(message_id)
    elif action == "ar":
        with SessionLocal() as session:
            alert = session.get(Alert, int(args[0]))
            if alert:
                alert.active = True
                alert.triggered_at = None
                session.commit()
        _alerts_view(message_id)
    elif action == "alerts":
        _alerts_view(message_id)
    elif action == "settings":
        _settings_view(message_id)
    elif action == "set":
        prompts = {
            "move": ("move", "Nuevo umbral de cambio brusco en % (ej: 5):"),
            "interval": ("interval", "Cada cuántos minutos comprobar alertas (ej: 10):"),
            "summary": ("summary_time", "Hora del resumen diario en formato HH:MM (ej: 22:10):"),
        }
        key, prompt = prompts[args[0]]
        _pending.update({"action": key})
        _send(prompt, [[_btn("Cancelar", "settings")]])
    elif action == "target":
        _pending.update({"action": "target", "stock_id": int(args[0])})
        _send("Escríbeme el precio objetivo (o «quitar» para borrarlo):", [[_btn("Cancelar", f"s:{args[0]}")]])
    elif action == "notes":
        _pending.update({"action": "notes", "stock_id": int(args[0])})
        _send("Escríbeme las notas para este valor (o «quitar» para borrarlas):", [[_btn("Cancelar", f"s:{args[0]}")]])
    elif action == "summary":
        alerts_mod.send_daily_summary()


def _parse_number(text: str) -> float | None:
    try:
        return float(text.strip().replace(",", "."))
    except ValueError:
        return None


def _handle_pending(text: str) -> bool:
    """Procesa la respuesta a una pregunta previa. True si había algo pendiente."""
    if not _pending:
        return False
    pending = dict(_pending)
    _pending.clear()
    action = pending["action"]

    if action == "search":
        _handle_search_text(text, pending.get("list_id"))
    elif action == "new_list":
        name = text.strip()[:60]
        with SessionLocal() as session:
            if session.scalar(select(Watchlist).where(Watchlist.name == name)):
                _send(f"Ya existe una lista llamada «{name}».")
            else:
                session.add(Watchlist(name=name))
                session.commit()
        _lists_view()
    elif action == "alert_price":
        value = _parse_number(text)
        if value is None or value <= 0:
            _send("Eso no parece un precio válido. Vuelve a intentarlo desde la ficha del valor.")
            return True
        with SessionLocal() as session:
            session.add(Alert(stock_id=pending["stock_id"], kind=pending["kind"], threshold=value))
            session.commit()
        _stock_view(pending["stock_id"])
    elif action == "target":
        with SessionLocal() as session:
            stock = session.get(Stock, pending["stock_id"])
            if stock:
                if text.strip().lower() in ("quitar", "borrar", "no"):
                    stock.target_price = None
                else:
                    value = _parse_number(text)
                    if value is None or value <= 0:
                        _send("Eso no parece un precio válido.")
                        return True
                    stock.target_price = value
                session.commit()
        _stock_view(pending["stock_id"])
    elif action == "notes":
        with SessionLocal() as session:
            stock = session.get(Stock, pending["stock_id"])
            if stock:
                stock.notes = "" if text.strip().lower() in ("quitar", "borrar") else text.strip()
                session.commit()
        _stock_view(pending["stock_id"])
    elif action == "move":
        value = _parse_number(text)
        if value is None or value <= 0:
            _send("Debe ser un número positivo (ej: 5).")
            return True
        with SessionLocal() as session:
            set_setting(session, "move_threshold", str(value))
            session.commit()
        _settings_view()
    elif action == "interval":
        value = _parse_number(text)
        if value is None or not 1 <= value <= 720:
            _send("Debe ser un número de minutos entre 1 y 720.")
            return True
        with SessionLocal() as session:
            set_setting(session, "check_interval_minutes", str(int(value)))
            session.commit()
        scheduler.reschedule()
        _settings_view()
    elif action == "summary_time":
        with SessionLocal() as session:
            set_setting(session, "daily_summary_time", text.strip())
            session.commit()
            saved = get_summary_time(session)
        if saved != text.strip():
            _send(f"No entendí «{text.strip()}»; queda a las {saved}. Usa el formato HH:MM.")
        scheduler.reschedule()
        _settings_view()
    return True


def _handle_message(update: dict) -> None:
    message = update.get("message", {})
    chat_id = str(message.get("chat", {}).get("id", ""))
    if chat_id != str(config.TELEGRAM_CHAT_ID):
        return
    text = (message.get("text") or "").strip()
    if not text:
        return

    if text.startswith("/"):
        _pending.clear()
        command, _, arg = text.partition(" ")
        command = command.lower().split("@")[0]
        if command in ("/start", "/menu"):
            _main_menu()
        elif command == "/precio" and arg.strip():
            quote = prices.get_quote(arg.strip().upper())
            if quote:
                emoji = "🟢" if quote["change_pct"] >= 0 else "🔴"
                _send(
                    f"{emoji} <b>{quote['ticker']}</b>  {_fmt(quote['price'], quote['currency'])}"
                    f"  ({alerts_mod.fmt_pct(quote['change_pct'])} hoy)\n"
                    f"{MARKET_LABELS.get(quote.get('market_state'), '')}"
                )
            else:
                _send(f"No encuentro cotización para {arg.strip().upper()}.")
        elif command == "/resumen":
            alerts_mod.send_daily_summary()
        elif command in ("/ayuda", "/help", "/precio"):
            _send(
                "<b>Comandos</b>\n"
                "/menu — menú principal (todo se hace desde ahí)\n"
                "/precio TICKER — cotización al momento (ej: /precio AAPL)\n"
                "/resumen — resumen de tus listas ahora\n"
                "/ayuda — esta ayuda"
            )
        else:
            _main_menu()
        return

    if _handle_pending(text):
        return
    # Texto suelto sin contexto: lo tratamos como búsqueda rápida.
    _handle_search_text(text, None)


def _dispatch(update: dict) -> None:
    if "callback_query" in update:
        _handle_callback(update)
    elif "message" in update:
        _handle_message(update)


# ------------------------------------------------------------ polling


class _Poller(threading.Thread):
    daemon = True

    def run(self) -> None:
        offset = self._skip_backlog()
        log.info("Bot de Telegram escuchando (long polling)")
        while not _stop_event.is_set():
            try:
                resp = httpx.get(
                    API.format(token=config.TELEGRAM_BOT_TOKEN, method="getUpdates"),
                    params={"timeout": 25, "offset": offset},
                    timeout=35,
                )
                if resp.status_code == 409:
                    log.warning("Otra instancia del bot está escuchando (409); reintento en 60 s")
                    time.sleep(60)
                    continue
                for update in resp.json().get("result", []):
                    offset = update["update_id"] + 1
                    try:
                        _dispatch(update)
                    except Exception:
                        log.exception("Error procesando update de Telegram")
            except Exception as exc:
                log.warning("Polling de Telegram falló: %s", exc)
                time.sleep(5)

    @staticmethod
    def _skip_backlog() -> int:
        """Descarta los mensajes acumulados antes de arrancar."""
        try:
            resp = httpx.get(
                API.format(token=config.TELEGRAM_BOT_TOKEN, method="getUpdates"),
                params={"offset": -1, "timeout": 0},
                timeout=15,
            )
            updates = resp.json().get("result", [])
            return updates[-1]["update_id"] + 1 if updates else 0
        except Exception:
            return 0


def _register_commands() -> None:
    _call(
        "setMyCommands",
        commands=[
            {"command": "menu", "description": "Menú principal"},
            {"command": "precio", "description": "Cotización: /precio AAPL"},
            {"command": "resumen", "description": "Resumen de tus listas ahora"},
            {"command": "ayuda", "description": "Ayuda"},
        ],
    )


def start() -> None:
    if not telegram.is_configured():
        log.info("Bot de Telegram sin configurar; no se arranca el polling")
        return
    if not config.BOT_POLLING:
        log.info("BOT_POLLING=0: esta instancia no escucha comandos de Telegram")
        return
    _register_commands()
    _Poller().start()


def stop() -> None:
    _stop_event.set()
