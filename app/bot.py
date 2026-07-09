"""Bot interactivo de Telegram multi-usuario.

- El chat de TELEGRAM_CHAT_ID es el administrador: ve y gestiona todo,
  aprueba a nuevos usuarios y puede asignar listas a cada uno.
- Cada usuario aprobado ve y gestiona solo sus listas, y recibe en su chat
  las alertas y resúmenes de lo suyo.
- Un desconocido que escriba al bot genera una solicitud de acceso que el
  admin aprueba o rechaza con un botón.

Usa long polling (getUpdates) en un hilo propio.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from html import escape as esc

import httpx
from sqlalchemy import select

from app import charts, config, prices, scheduler
from app import alerts as alerts_mod
from app import telegram
from app.database import (
    Alert,
    BotUser,
    SessionLocal,
    Stock,
    Watchlist,
    get_check_interval,
    get_move_threshold,
    get_summary_interval,
    get_summary_time,
    set_setting,
)

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"

MARKET_LABELS = {"open": "🟢 Abierto", "pre": "🟡 Pre-market", "post": "🟣 After-hours", "closed": "⚪ Cerrado"}

# Acción pendiente de respuesta de texto, por chat: {chat_id: {"action": ..., ...}}
_pending: dict[str, dict] = {}
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


def _send(text: str, keyboard: list | None = None, chat_id: str | None = None) -> dict:
    payload = {
        "chat_id": chat_id or config.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    return _call("sendMessage", **payload)


def _edit(chat_id: str, message_id: int, text: str, keyboard: list | None = None) -> None:
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    result = _call("editMessageText", **payload)
    if not result.get("ok"):
        _send(text, keyboard, chat_id)


def _send_photo(png: bytes, caption: str, keyboard: list | None = None, chat_id: str | None = None) -> None:
    data = {
        "chat_id": chat_id or config.TELEGRAM_CHAT_ID,
        "caption": caption,
        "parse_mode": "HTML",
    }
    if keyboard:
        data["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
    try:
        resp = httpx.post(
            API.format(token=config.TELEGRAM_BOT_TOKEN, method="sendPhoto"),
            data=data,
            files={"photo": ("chart.png", png, "image/png")},
            timeout=60,
        )
        if not resp.json().get("ok"):
            log.warning("sendPhoto: %s", resp.text[:200])
    except Exception as exc:
        log.error("sendPhoto: %s", exc)


def _btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def _show(ctx: dict, text: str, keyboard: list, message_id: int | None) -> None:
    if message_id:
        _edit(ctx["chat"], message_id, text, keyboard)
    else:
        _send(text, keyboard, ctx["chat"])


# ------------------------------------------------------------ usuarios/contexto


def _get_ctx(chat_id) -> dict | None:
    """Contexto del usuario aprobado, o None si es desconocido/pendiente."""
    chat = str(chat_id)
    with SessionLocal() as session:
        user = session.scalar(select(BotUser).where(BotUser.chat_id == chat))
        if user and user.role in ("admin", "user"):
            return {"uid": user.id, "chat": chat, "role": user.role, "name": user.name}
    return None


def _is_admin(ctx: dict) -> bool:
    return ctx["role"] == "admin"


def _handle_stranger(chat_id: str, from_info: dict) -> None:
    """Desconocido o pendiente: gestiona la solicitud de acceso."""
    name = " ".join(
        filter(None, [from_info.get("first_name", ""), from_info.get("last_name", "")])
    ).strip() or from_info.get("username", "") or f"chat {chat_id}"
    with SessionLocal() as session:
        user = session.scalar(select(BotUser).where(BotUser.chat_id == chat_id))
        if user:
            _send("⏳ Tu solicitud de acceso sigue pendiente de aprobación.", chat_id=chat_id)
            return
        user = BotUser(chat_id=chat_id, name=name[:80], role="pending")
        session.add(user)
        session.commit()
        uid = user.id
    _send(
        "👋 ¡Hola! Este bot es privado. He avisado al administrador para que "
        "apruebe tu acceso; te llegará un mensaje cuando esté listo.",
        chat_id=chat_id,
    )
    _send(
        f"👤 <b>{esc(name)}</b> (chat <code>{chat_id}</code>) quiere acceso al bot.",
        [[_btn("✅ Aprobar", f"uok:{uid}"), _btn("❌ Rechazar", f"uno:{uid}")]],
    )


# ------------------------------------------------------------ vistas/menús


def _fmt(value, currency="USD"):
    return alerts_mod.fmt_price(value, currency)


def _main_menu(ctx: dict, message_id: int | None = None) -> None:
    keyboard = [
        [_btn("📋 Mis listas", "lists")],
        [_btn("➕ Añadir valor", "add"), _btn("🔔 Alertas", "alerts")],
        [_btn("📊 Resumen ahora", "summary")],
    ]
    if _is_admin(ctx):
        keyboard.append([_btn("⚙️ Ajustes", "settings"), _btn("👥 Usuarios", "users")])
    greeting = "" if _is_admin(ctx) else f"\nHola, {esc(ctx['name'])} 👋"
    _show(ctx, f"<b>📈 Watchlist</b>{greeting}\n¿Qué quieres hacer?", keyboard, message_id)


def _user_lists(session, ctx: dict):
    query = select(Watchlist).order_by(Watchlist.id)
    if not _is_admin(ctx):
        query = query.where(Watchlist.owner_id == ctx["uid"])
    return session.scalars(query).all()


def _lists_view(ctx: dict, message_id: int | None = None) -> None:
    with SessionLocal() as session:
        watchlists = _user_lists(session, ctx)
        rows = []
        for wl in watchlists:
            label = f"📋 {wl.name} ({len(wl.stocks)})"
            if _is_admin(ctx) and wl.owner and wl.owner.role != "admin":
                label += f" · {wl.owner.name}"
            rows.append([_btn(label, f"l:{wl.id}")])
    rows.append([_btn("➕ Nueva lista", "lnew"), _btn("◀️ Menú", "menu")])
    if watchlists:
        text = "<b>Tus listas</b>"
    else:
        text = ("No tienes listas todavía. Pídele al administrador que te asigne "
                "una, o crea la tuya con ➕ Nueva lista.")
    _show(ctx, text, rows, message_id)


def _can_touch_list(ctx: dict, wl: Watchlist | None) -> bool:
    return wl is not None and (_is_admin(ctx) or wl.owner_id == ctx["uid"])


def _list_view(ctx: dict, list_id: int, message_id: int | None = None) -> None:
    with SessionLocal() as session:
        wl = session.get(Watchlist, list_id)
        if not _can_touch_list(ctx, wl):
            _lists_view(ctx, message_id)
            return
        stocks = sorted(wl.stocks, key=lambda s: s.ticker)
        name = wl.name
        owner_name = wl.owner.name if wl.owner and wl.owner.role != "admin" else None
        has_users = bool(session.scalar(select(BotUser).where(BotUser.role == "user")))
    quotes = prices.get_quotes([s.ticker for s in stocks])
    lines = [f"<b>📋 {esc(name)}</b>"]
    if _is_admin(ctx) and owner_name:
        lines.append(f"👤 Asignada a {esc(owner_name)}")
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
    action_row = [_btn("➕ Añadir aquí", f"addl:{list_id}"), _btn("🗑 Eliminar lista", f"ldel:{list_id}")]
    if _is_admin(ctx) and has_users:
        action_row.insert(1, _btn("👤 Asignar", f"lasg:{list_id}"))
    rows.append(action_row)
    rows.append([_btn("◀️ Listas", "lists"), _btn("🔄 Actualizar", f"l:{list_id}")])
    _show(ctx, "\n".join(lines), rows, message_id)


def _get_stock_checked(session, ctx: dict, stock_id: int) -> Stock | None:
    stock = session.get(Stock, stock_id)
    if not stock or not _can_touch_list(ctx, stock.watchlist):
        return None
    return stock


def _stock_view(ctx: dict, stock_id: int, message_id: int | None = None) -> None:
    with SessionLocal() as session:
        stock = _get_stock_checked(session, ctx, stock_id)
        if not stock:
            _lists_view(ctx, message_id)
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
    lines = [f"<b>{info['ticker']}</b> — {esc(info['name'])}"]
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
    # Notas completas: dentro de la ficha si caben, en mensajes aparte si no.
    notes_apart = None
    if info["notes"]:
        notes = esc(info["notes"])
        if sum(len(l) for l in lines) + len(notes) < 3800:
            lines.append(f"📝 {notes}")
        else:
            notes_apart = notes
    keyboard = [
        [_btn("📈 Gráfico", f"g:{info['ticker']}:6mo"), _btn("📰 Noticias", f"n:{info['ticker']}")],
        [_btn("🔔 Nueva alerta", f"alnew:{stock_id}"), _btn("🎯 Objetivo", f"target:{stock_id}")],
        [_btn("📝 Notas", f"notes:{stock_id}"), _btn("🗑 Quitar", f"sd:{stock_id}")],
        [_btn("◀️ Volver", f"l:{info['list_id']}"), _btn("🔄 Actualizar", f"s:{stock_id}")],
    ]
    _show(ctx, "\n".join(filter(None, lines)), keyboard, message_id)
    if notes_apart:
        for i in range(0, len(notes_apart), 3500):
            _send(f"📝 <b>Notas de {info['ticker']}</b>\n{notes_apart[i:i + 3500]}", chat_id=ctx["chat"])


def _alerts_view(ctx: dict, message_id: int | None = None) -> None:
    with SessionLocal() as session:
        query = select(Alert).join(Stock).join(Watchlist).order_by(Alert.created_at.desc())
        if not _is_admin(ctx):
            query = query.where(Watchlist.owner_id == ctx["uid"])
        alerts = session.scalars(query).all()
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
    _show(ctx, "\n".join(lines), rows, message_id)


def _settings_view(ctx: dict, message_id: int | None = None) -> None:
    with SessionLocal() as session:
        move = get_move_threshold(session)
        interval = get_check_interval(session)
        summary = get_summary_time(session)
        periodic = get_summary_interval(session)
    periodic_txt = f"cada <b>{periodic} min</b>" if periodic else "<b>desactivado</b>"
    text = (
        "<b>⚙️ Ajustes</b> (globales)\n"
        f"⚡ Aviso de cambio brusco: <b>±{move}%</b>\n"
        f"⏱ Comprobación de alertas: cada <b>{interval} min</b>\n"
        f"📊 Resumen automático: {periodic_txt}\n"
        f"🕙 Resumen diario: a las <b>{summary}</b> ({config.TIMEZONE})"
    )
    keyboard = [
        [_btn("⚡ Cambiar umbral %", "set:move")],
        [_btn("⏱ Cambiar intervalo alertas", "set:interval")],
        [_btn("📊 Cambiar resumen automático", "set:periodic")],
        [_btn("🕙 Cambiar hora resumen diario", "set:summary")],
        [_btn("◀️ Menú", "menu")],
    ]
    _show(ctx, text, keyboard, message_id)


def _users_view(ctx: dict, message_id: int | None = None) -> None:
    with SessionLocal() as session:
        users = session.scalars(select(BotUser).order_by(BotUser.created_at)).all()
        rows, lines = [], ["<b>👥 Usuarios del bot</b>"]
        for u in users:
            if u.role == "admin":
                lines.append(f"👑 {esc(u.name)} (tú)")
            elif u.role == "user":
                lists = len(u.watchlists)
                lines.append(f"👤 {esc(u.name)} — {lists} lista{'s' if lists != 1 else ''}")
                rows.append([_btn(f"🗑 Quitar a {u.name[:20]}", f"udel:{u.id}")])
            else:
                lines.append(f"⏳ {esc(u.name)} (pendiente)")
                rows.append([_btn(f"✅ Aprobar a {u.name[:16]}", f"uok:{u.id}"), _btn("❌", f"uno:{u.id}")])
    lines.append("\nPara invitar a alguien, pásale el enlace del bot y "
                 "cuando escriba te llegará su solicitud.")
    rows.append([_btn("◀️ Menú", "menu")])
    _show(ctx, "\n".join(lines), rows, message_id)


def _pick_list_keyboard(session, ctx: dict, symbol: str) -> list:
    watchlists = _user_lists(session, ctx)
    rows = [[_btn(f"📋 {wl.name}", f"addto:{symbol}:{wl.id}")] for wl in watchlists]
    rows.append([_btn("Cancelar", "menu")])
    return rows


def _send_news(chat_id: str, ticker: str) -> None:
    ticker = ticker.upper()
    items = prices.get_news(ticker, 5)
    if not items:
        _send(f"No hay noticias recientes de {ticker}.", chat_id=chat_id)
        return
    lines = [f"📰 <b>Noticias de {ticker}</b>"]
    for n in items:
        meta = " · ".join(filter(None, [esc(n["provider"]), n["date"]]))
        lines.append(f"• <a href=\"{n['url']}\">{esc(n['title'])}</a>\n   <i>{meta}</i>")
    _send("\n".join(lines), chat_id=chat_id)


CHART_PERIODS = [("1M", "1mo"), ("6M", "6mo"), ("1A", "1y"), ("5A", "5y"), ("Máx", "max")]


def _send_chart(chat_id: str, ticker: str, period: str = "6mo") -> None:
    ticker = ticker.upper()
    result = charts.render_chart(ticker, period)
    if not result:
        _send(f"No hay datos de histórico para {ticker}.", chat_id=chat_id)
        return
    png, _change = result
    quote = prices.get_quote(ticker)
    caption = f"📈 <b>{ticker}</b>"
    if quote:
        caption += (
            f"  {_fmt(quote['price'], quote['currency'])}"
            f"  ({alerts_mod.fmt_pct(quote['change_pct'])} hoy)"
        )
    keyboard = [[
        _btn(label if p != period else f"· {label} ·", f"g:{ticker}:{p}")
        for label, p in CHART_PERIODS
    ]]
    _send_photo(png, caption, keyboard, chat_id)


# ------------------------------------------------------------ acciones


def _add_stock(ctx: dict, symbol: str, list_id: int, message_id: int | None) -> None:
    symbol = symbol.upper()
    with SessionLocal() as session:
        wl = session.get(Watchlist, list_id)
        if not _can_touch_list(ctx, wl):
            _send("Esa lista ya no existe o no es tuya.", chat_id=ctx["chat"])
            return
        exists = session.scalar(
            select(Stock).where(Stock.ticker == symbol, Stock.watchlist_id == list_id)
        )
        if exists:
            _send(f"{symbol} ya está en «{esc(wl.name)}».", chat_id=ctx["chat"])
            _stock_view(ctx, exists.id, message_id)
            return
    quote = prices.get_quote(symbol)
    if not quote:
        _send(f"No encuentro cotización para {symbol}.", chat_id=ctx["chat"])
        return
    with SessionLocal() as session:
        stock = Stock(
            ticker=symbol, watchlist_id=list_id,
            name=prices.lookup_name(symbol), currency=quote["currency"],
        )
        session.add(stock)
        session.commit()
        stock_id = stock.id
    _stock_view(ctx, stock_id, message_id)


def _handle_search_text(ctx: dict, text: str, list_id: int | None) -> None:
    text = text.strip().splitlines()[0]  # buscar solo la primera línea
    results = prices.search_symbols(text, limit=6)
    if not results:
        _send(
            f"No encuentro nada para «{esc(text)}». Prueba con otro nombre o el ticker exacto.",
            chat_id=ctx["chat"],
        )
        return
    rows = []
    for r in results:
        label = f"{r['symbol']} — {r['name'][:28]}" if r["name"] else r["symbol"]
        target = f"addto:{r['symbol']}:{list_id}" if list_id else f"pick:{r['symbol']}"
        rows.append([_btn(label, target)])
    rows.append([_btn("Cancelar", "menu")])
    _send("Elige el valor:", rows, ctx["chat"])


# ------------------------------------------------------------ despacho


def _handle_callback(update: dict) -> None:
    query = update["callback_query"]
    chat_id = str(query.get("message", {}).get("chat", {}).get("id", ""))
    _call("answerCallbackQuery", callback_query_id=query["id"])
    ctx = _get_ctx(chat_id)
    if not ctx:
        return
    message_id = query["message"]["message_id"]
    data = query.get("data", "")
    parts = data.split(":")
    action, args = parts[0], parts[1:]
    _pending.pop(ctx["chat"], None)
    admin_only = {"settings", "set", "users", "uok", "uno", "udel", "lasg", "lasgto"}
    if action in admin_only and not _is_admin(ctx):
        return

    if action == "menu":
        _main_menu(ctx, message_id)
    elif action == "lists":
        _lists_view(ctx, message_id)
    elif action == "l":
        _list_view(ctx, int(args[0]), message_id)
    elif action == "lnew":
        _pending[ctx["chat"]] = {"action": "new_list"}
        _send(
            "Escríbeme el <b>nombre</b> de la nueva lista (solo el nombre — "
            "los valores se añaden después con ➕ Añadir):",
            [[_btn("Cancelar", "menu")]],
            ctx["chat"],
        )
    elif action == "ldel":
        keyboard = [[_btn("Sí, eliminar", f"ldel2:{args[0]}"), _btn("No", f"l:{args[0]}")]]
        _edit(ctx["chat"], message_id, "¿Eliminar la lista con todo su contenido?", keyboard)
    elif action == "ldel2":
        with SessionLocal() as session:
            wl = session.get(Watchlist, int(args[0]))
            if _can_touch_list(ctx, wl):
                session.delete(wl)
                session.commit()
        _lists_view(ctx, message_id)
    elif action == "lasg":
        with SessionLocal() as session:
            users = session.scalars(select(BotUser).where(BotUser.role.in_(("admin", "user")))).all()
            rows = [[_btn(f"👤 {u.name}" + (" (tú)" if u.role == "admin" else ""), f"lasgto:{args[0]}:{u.id}")]
                    for u in users]
        rows.append([_btn("Cancelar", f"l:{args[0]}")])
        _edit(ctx["chat"], message_id, "¿A quién asigno esta lista?", rows)
    elif action == "lasgto":
        list_id, uid = int(args[0]), int(args[1])
        with SessionLocal() as session:
            wl = session.get(Watchlist, list_id)
            user = session.get(BotUser, uid)
            if wl and user:
                wl.owner_id = uid
                session.commit()
                if user.role != "admin":
                    _send(
                        f"📬 Te han asignado la lista «{esc(wl.name)}». Escribe /menu para verla.",
                        chat_id=user.chat_id,
                    )
        _list_view(ctx, list_id, message_id)
    elif action == "s":
        _stock_view(ctx, int(args[0]), message_id)
    elif action == "sd":
        keyboard = [[_btn("Sí, quitar", f"sd2:{args[0]}"), _btn("No", f"s:{args[0]}")]]
        _edit(ctx["chat"], message_id, "¿Quitar este valor de la lista?", keyboard)
    elif action == "sd2":
        with SessionLocal() as session:
            stock = _get_stock_checked(session, ctx, int(args[0]))
            list_id = stock.watchlist_id if stock else None
            if stock:
                session.delete(stock)
                session.commit()
        _list_view(ctx, list_id, message_id) if list_id else _lists_view(ctx, message_id)
    elif action == "add":
        _pending[ctx["chat"]] = {"action": "search", "list_id": None}
        _send("Escríbeme el nombre o ticker (ej: apple, SAN.MC, oro):", [[_btn("Cancelar", "menu")]], ctx["chat"])
    elif action == "addl":
        _pending[ctx["chat"]] = {"action": "search", "list_id": int(args[0])}
        _send("Escríbeme el nombre o ticker (ej: apple, SAN.MC, oro):", [[_btn("Cancelar", "menu")]], ctx["chat"])
    elif action == "pick":
        symbol = args[0]
        with SessionLocal() as session:
            watchlists = _user_lists(session, ctx)
            keyboard = _pick_list_keyboard(session, ctx, symbol)
        if len(watchlists) == 1:
            _add_stock(ctx, symbol, watchlists[0].id, message_id)
        elif not watchlists:
            _send("No tienes ninguna lista; crea una primero desde 📋 Mis listas.", chat_id=ctx["chat"])
        else:
            _edit(ctx["chat"], message_id, f"¿A qué lista añado <b>{symbol}</b>?", keyboard)
    elif action == "addto":
        _add_stock(ctx, args[0], int(args[1]), message_id)
    elif action == "alnew":
        keyboard = [
            [_btn("⬆️ Si sube de…", f"alk:{args[0]}:above"), _btn("⬇️ Si baja de…", f"alk:{args[0]}:below")],
            [_btn("Cancelar", f"s:{args[0]}")],
        ]
        _edit(ctx["chat"], message_id, "¿Qué tipo de alerta?", keyboard)
    elif action == "alk":
        _pending[ctx["chat"]] = {"action": "alert_price", "stock_id": int(args[0]), "kind": args[1]}
        _send("Escríbeme el precio del umbral (ej: 150.50):", [[_btn("Cancelar", f"s:{args[0]}")]], ctx["chat"])
    elif action in ("ad", "ar"):
        with SessionLocal() as session:
            alert = session.get(Alert, int(args[0]))
            if alert and _can_touch_list(ctx, alert.stock.watchlist):
                if action == "ad":
                    session.delete(alert)
                else:
                    alert.active = True
                    alert.triggered_at = None
                session.commit()
        _alerts_view(ctx, message_id)
    elif action == "alerts":
        _alerts_view(ctx, message_id)
    elif action == "settings":
        _settings_view(ctx, message_id)
    elif action == "users":
        _users_view(ctx, message_id)
    elif action == "uok":
        with SessionLocal() as session:
            user = session.get(BotUser, int(args[0]))
            if user and user.role == "pending":
                user.role = "user"
                session.commit()
                _send(
                    "✅ ¡Acceso concedido! Escribe /menu para empezar.\n\n"
                    "ℹ️ Cómo funciona:\n"
                    "• El administrador te asignará tu lista de valores (o crea una tuya "
                    "con 📋 Mis listas → ➕ Nueva lista).\n"
                    "• Para añadir un valor: entra en la lista → ➕ Añadir y escribe el "
                    "nombre o ticker (apple, SAN.MC, oro…).\n"
                    "• Recibirás aquí las alertas y resúmenes de tus listas.",
                    chat_id=user.chat_id,
                )
        _users_view(ctx, message_id)
    elif action == "uno":
        with SessionLocal() as session:
            user = session.get(BotUser, int(args[0]))
            if user and user.role == "pending":
                chat = user.chat_id
                session.delete(user)
                session.commit()
                _send("❌ Tu solicitud de acceso ha sido rechazada.", chat_id=chat)
        _users_view(ctx, message_id)
    elif action == "udel":
        keyboard = [[_btn("Sí, quitar acceso", f"udel2:{args[0]}"), _btn("No", "users")]]
        _edit(ctx["chat"], message_id, "¿Quitar el acceso a este usuario? Sus listas pasarán a ti.", keyboard)
    elif action == "udel2":
        with SessionLocal() as session:
            user = session.get(BotUser, int(args[0]))
            if user and user.role == "user":
                admin = session.scalar(select(BotUser).where(BotUser.role == "admin"))
                for wl in user.watchlists:
                    wl.owner_id = admin.id
                session.delete(user)
                session.commit()
        _users_view(ctx, message_id)
    elif action == "set":
        prompts = {
            "move": ("move", "Nuevo umbral de cambio brusco en % (ej: 5):"),
            "interval": ("interval", "Cada cuántos minutos comprobar alertas (ej: 10):"),
            "periodic": ("summary_interval", "Cada cuántos minutos mando el resumen automático (0 para desactivarlo):"),
            "summary": ("summary_time", "Hora del resumen diario en formato HH:MM (ej: 22:10):"),
        }
        key, prompt = prompts[args[0]]
        _pending[ctx["chat"]] = {"action": key}
        _send(prompt, [[_btn("Cancelar", "settings")]], ctx["chat"])
    elif action == "target":
        _pending[ctx["chat"]] = {"action": "target", "stock_id": int(args[0])}
        _send("Escríbeme el precio objetivo (o «quitar» para borrarlo):", [[_btn("Cancelar", f"s:{args[0]}")]], ctx["chat"])
    elif action == "notes":
        _pending[ctx["chat"]] = {"action": "notes", "stock_id": int(args[0])}
        _send("Escríbeme las notas para este valor (o «quitar» para borrarlas):", [[_btn("Cancelar", f"s:{args[0]}")]], ctx["chat"])
    elif action == "g":
        _send_chart(ctx["chat"], args[0], args[1] if len(args) > 1 else "6mo")
    elif action == "n":
        _send_news(ctx["chat"], args[0])
    elif action == "summary":
        if not alerts_mod.send_summary_to(ctx["chat"]):
            _send("Aún no tienes listas con valores.", chat_id=ctx["chat"])


def _parse_number(text: str) -> float | None:
    try:
        return float(text.strip().replace(",", "."))
    except ValueError:
        return None


def _handle_pending(ctx: dict, text: str) -> bool:
    """Procesa la respuesta a una pregunta previa. True si había algo pendiente."""
    pending = _pending.pop(ctx["chat"], None)
    if not pending:
        return False
    action = pending["action"]
    chat = ctx["chat"]

    if action == "search":
        _handle_search_text(ctx, text, pending.get("list_id"))
    elif action == "new_list":
        # Solo la primera línea: si mandan varios renglones (p. ej. tickers),
        # no queremos un nombre de lista multilínea.
        name = text.strip().splitlines()[0].strip()[:60]
        if not name:
            _send("Necesito un nombre para la lista.", chat_id=chat)
            return True
        if len(text.strip().splitlines()) > 1:
            _send(
                f"Ojo: he usado solo «{esc(name)}» como nombre. "
                "Los valores se añaden después desde la lista con ➕ Añadir.",
                chat_id=chat,
            )
        with SessionLocal() as session:
            if session.scalar(select(Watchlist).where(Watchlist.name == name)):
                _send(f"Ya existe una lista llamada «{esc(name)}».", chat_id=chat)
            else:
                session.add(Watchlist(name=name, owner_id=ctx["uid"]))
                session.commit()
        _lists_view(ctx)
    elif action == "alert_price":
        value = _parse_number(text)
        if value is None or value <= 0:
            _send("Eso no parece un precio válido. Vuelve a intentarlo desde la ficha del valor.", chat_id=chat)
            return True
        with SessionLocal() as session:
            stock = _get_stock_checked(session, ctx, pending["stock_id"])
            if stock:
                session.add(Alert(stock_id=stock.id, kind=pending["kind"], threshold=value))
                session.commit()
        _stock_view(ctx, pending["stock_id"])
    elif action == "target":
        with SessionLocal() as session:
            stock = _get_stock_checked(session, ctx, pending["stock_id"])
            if stock:
                if text.strip().lower() in ("quitar", "borrar", "no"):
                    stock.target_price = None
                else:
                    value = _parse_number(text)
                    if value is None or value <= 0:
                        _send("Eso no parece un precio válido.", chat_id=chat)
                        return True
                    stock.target_price = value
                session.commit()
        _stock_view(ctx, pending["stock_id"])
    elif action == "notes":
        with SessionLocal() as session:
            stock = _get_stock_checked(session, ctx, pending["stock_id"])
            if stock:
                stock.notes = "" if text.strip().lower() in ("quitar", "borrar") else text.strip()
                session.commit()
        _stock_view(ctx, pending["stock_id"])
    elif action == "move":
        value = _parse_number(text)
        if value is None or value <= 0:
            _send("Debe ser un número positivo (ej: 5).", chat_id=chat)
            return True
        with SessionLocal() as session:
            set_setting(session, "move_threshold", str(value))
            session.commit()
        _settings_view(ctx)
    elif action == "interval":
        value = _parse_number(text)
        if value is None or not 1 <= value <= 720:
            _send("Debe ser un número de minutos entre 1 y 720.", chat_id=chat)
            return True
        with SessionLocal() as session:
            set_setting(session, "check_interval_minutes", str(int(value)))
            session.commit()
        scheduler.reschedule()
        _settings_view(ctx)
    elif action == "summary_interval":
        value = _parse_number(text)
        if value is None or not 0 <= value <= 1440:
            _send("Debe ser un número de minutos entre 0 (desactivado) y 1440.", chat_id=chat)
            return True
        with SessionLocal() as session:
            set_setting(session, "summary_interval_minutes", str(int(value)))
            session.commit()
        scheduler.reschedule()
        _settings_view(ctx)
    elif action == "summary_time":
        with SessionLocal() as session:
            set_setting(session, "daily_summary_time", text.strip())
            session.commit()
            saved = get_summary_time(session)
        if saved != text.strip():
            _send(f"No entendí «{esc(text.strip())}»; queda a las {saved}. Usa el formato HH:MM.", chat_id=chat)
        scheduler.reschedule()
        _settings_view(ctx)
    return True


def _handle_message(update: dict) -> None:
    message = update.get("message", {})
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = (message.get("text") or "").strip()
    if not chat_id or not text:
        return
    ctx = _get_ctx(chat_id)
    if not ctx:
        _handle_stranger(chat_id, message.get("from", {}))
        return

    if text.startswith("/"):
        _pending.pop(ctx["chat"], None)
        command, _, arg = text.partition(" ")
        command = command.lower().split("@")[0]
        if command in ("/start", "/menu"):
            _main_menu(ctx)
        elif command == "/precio" and arg.strip():
            quote = prices.get_quote(arg.strip().upper())
            if quote:
                emoji = "🟢" if quote["change_pct"] >= 0 else "🔴"
                _send(
                    f"{emoji} <b>{quote['ticker']}</b>  {_fmt(quote['price'], quote['currency'])}"
                    f"  ({alerts_mod.fmt_pct(quote['change_pct'])} hoy)\n"
                    f"{MARKET_LABELS.get(quote.get('market_state'), '')}",
                    chat_id=ctx["chat"],
                )
            else:
                _send(f"No encuentro cotización para {esc(arg.strip().upper())}.", chat_id=ctx["chat"])
        elif command == "/grafico" and arg.strip():
            pieces = arg.split()
            aliases = {"1m": "1mo", "3m": "3mo", "6m": "6mo", "1a": "1y", "1y": "1y", "5a": "5y", "5y": "5y", "max": "max"}
            period = aliases.get(pieces[1].lower(), "6mo") if len(pieces) > 1 else "6mo"
            _send_chart(ctx["chat"], pieces[0], period)
        elif command == "/noticias" and arg.strip():
            _send_news(ctx["chat"], arg.split()[0])
        elif command == "/resumen":
            if not alerts_mod.send_summary_to(ctx["chat"]):
                _send("Aún no tienes listas con valores.", chat_id=ctx["chat"])
        elif command in ("/ayuda", "/help", "/precio", "/grafico", "/noticias"):
            _send(
                "<b>Comandos</b>\n"
                "/menu — menú principal (todo se hace desde ahí)\n"
                "/precio TICKER — cotización al momento (ej: /precio AAPL)\n"
                "/grafico TICKER [1m|3m|6m|1a|5a|max] — gráfico (ej: /grafico XAUUSD 1a)\n"
                "/noticias TICKER — últimas noticias del valor\n"
                "/resumen — resumen de tus listas ahora\n"
                "/ayuda — esta ayuda",
                chat_id=ctx["chat"],
            )
        else:
            _main_menu(ctx)
        return

    if _handle_pending(ctx, text):
        return
    # Texto suelto sin contexto: lo tratamos como búsqueda rápida.
    _handle_search_text(ctx, text, None)


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
            {"command": "grafico", "description": "Gráfico: /grafico AAPL 1a"},
            {"command": "noticias", "description": "Noticias: /noticias AAPL"},
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
