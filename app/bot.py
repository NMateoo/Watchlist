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

from app import charts, config, prices, scheduler, services
from app import alerts as alerts_mod
from app import telegram
from app.database import (
    Alert,
    BotUser,
    SessionLocal,
    Stock,
    Watchlist,
    WatchlistMember,
    get_check_interval,
    get_move_threshold,
    get_user_summary_prefs,
    normalize_time,
    set_setting,
)

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"

MARKET_LABELS = {"open": "🟢 Abierto", "pre": "🟡 Pre-market", "post": "🟣 After-hours", "closed": "⚪ Cerrado"}

# Acción pendiente de respuesta de texto, por chat: {chat_id: {"action": ..., ...}}
_pending: dict[str, dict] = {}
_stop_event = threading.Event()

# Cliente HTTP compartido: reutiliza conexiones en vez de abrir una por llamada.
_client = httpx.Client()


# ------------------------------------------------------------ API helpers


def _call(method: str, **payload) -> dict:
    try:
        resp = _client.post(
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
    # Los textos que superan el límite de Telegram van en varios mensajes;
    # el teclado se adjunta solo al último.
    parts = telegram.split_message(text)
    result: dict = {}
    for i, part in enumerate(parts):
        payload = {
            "chat_id": chat_id or config.TELEGRAM_CHAT_ID,
            "text": part,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if keyboard and i == len(parts) - 1:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        result = _call("sendMessage", **payload)
    return result


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
        resp = _client.post(
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
    else:
        keyboard.append([_btn("⚙️ Mis resúmenes", "settings")])
    greeting = "" if _is_admin(ctx) else f"\nHola, {esc(ctx['name'])} 👋"
    _show(ctx, f"<b>📈 Watchlist</b>{greeting}\n¿Qué quieres hacer?", keyboard, message_id)


def _user_lists(session, ctx: dict):
    if _is_admin(ctx):
        return session.scalars(select(Watchlist).order_by(Watchlist.id)).all()
    user = session.get(BotUser, ctx["uid"])
    return sorted(user.shared_lists, key=lambda w: w.id) if user else []


def _lists_view(ctx: dict, message_id: int | None = None) -> None:
    with SessionLocal() as session:
        watchlists = _user_lists(session, ctx)
        rows = []
        for wl in watchlists:
            label = f"📋 {wl.name} ({len(wl.stocks)})"
            if _is_admin(ctx) and wl.members:
                label += " · " + ", ".join(m.name for m in wl.members)
            rows.append([_btn(label[:60], f"l:{wl.id}")])
    rows.append([_btn("➕ Nueva lista", "lnew"), _btn("◀️ Menú", "menu")])
    if watchlists:
        text = "<b>Tus listas</b>"
    else:
        text = ("No tienes listas todavía. Pídele al administrador que te comparta "
                "una, o crea la tuya con ➕ Nueva lista.")
    _show(ctx, text, rows, message_id)


def _membership(ctx: dict, wl: Watchlist) -> WatchlistMember | None:
    return next((m for m in wl.memberships if m.user_id == ctx["uid"]), None)


def _can_view_list(ctx: dict, wl: Watchlist | None) -> bool:
    return wl is not None and (_is_admin(ctx) or _membership(ctx, wl) is not None)


def _can_edit_list(ctx: dict, wl: Watchlist | None) -> bool:
    """Editar contenido: añadir/quitar valores, alertas, notas, objetivo."""
    if wl is None:
        return False
    if _is_admin(ctx):
        return True
    member = _membership(ctx, wl)
    return member is not None and member.can_edit


def _can_delete_list(ctx: dict, wl: Watchlist | None) -> bool:
    """Eliminar la lista entera: solo el admin o quien la creó."""
    return wl is not None and (_is_admin(ctx) or wl.owner_id == ctx["uid"])


def _list_view(ctx: dict, list_id: int, message_id: int | None = None) -> None:
    with SessionLocal() as session:
        wl = session.get(Watchlist, list_id)
        if not _can_view_list(ctx, wl):
            _lists_view(ctx, message_id)
            return
        stocks = sorted(wl.stocks, key=lambda s: s.ticker)
        name = wl.name
        can_edit = _can_edit_list(ctx, wl)
        can_delete = _can_delete_list(ctx, wl)
        member_names = ", ".join(
            f"{m.user.name}{'' if m.can_edit else ' (solo ver)'}" for m in wl.memberships
        )
        has_users = bool(session.scalar(select(BotUser).where(BotUser.role == "user")))
    quotes = prices.get_quotes([s.ticker for s in stocks])
    lines = [f"<b>📋 {esc(name)}</b>"]
    if _is_admin(ctx) and member_names:
        lines.append(f"👥 Compartida con {esc(member_names)}")
    if not can_edit:
        lines.append("👁️ Solo lectura")
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
    action_row = []
    if can_edit:
        action_row.append(_btn("➕ Añadir aquí", f"addl:{list_id}"))
    if _is_admin(ctx) and has_users:
        action_row.append(_btn("👥 Compartir", f"lasg:{list_id}"))
    if action_row:
        rows.append(action_row)
    if can_delete:
        rows.append([_btn("✏️ Renombrar", f"lren:{list_id}"), _btn("🗑 Eliminar lista", f"ldel:{list_id}")])
    rows.append([_btn("◀️ Listas", "lists"), _btn("🔄 Actualizar", f"l:{list_id}")])
    _show(ctx, "\n".join(lines), rows, message_id)


def _get_stock_checked(session, ctx: dict, stock_id: int, edit: bool = False) -> Stock | None:
    stock = session.get(Stock, stock_id)
    if not stock:
        return None
    allowed = _can_edit_list(ctx, stock.watchlist) if edit else _can_view_list(ctx, stock.watchlist)
    return stock if allowed else None


def _stock_view(ctx: dict, stock_id: int, message_id: int | None = None) -> None:
    with SessionLocal() as session:
        stock = _get_stock_checked(session, ctx, stock_id)
        if not stock:
            _lists_view(ctx, message_id)
            return
        alerts = sorted(stock.alerts, key=lambda a: a.created_at, reverse=True)
        can_edit = _can_edit_list(ctx, stock.watchlist)
        info = {
            "ticker": stock.ticker, "name": stock.name, "currency": stock.currency,
            "notes": stock.notes, "target": stock.target_price, "list_id": stock.watchlist_id,
            "qty": stock.quantity, "buy": stock.buy_price,
        }
        alert_lines = []
        for a in alerts:
            arrow = "⬆️ sube de" if a.kind == "above" else "⬇️ baja de"
            rep = " 🔁" if a.repeat else ""
            state = "" if a.active else (" (esperando re-cruce)" if a.repeat else " (disparada)")
            alert_lines.append(f"  {arrow} {_fmt(a.threshold, stock.currency)}{rep}{state}")
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
    if info["qty"] and info["buy"] and q:
        pl_pct = (q["price"] / info["buy"] - 1) * 100
        pl = (q["price"] - info["buy"]) * info["qty"]
        lines.append(
            f"💼 Posición: {info['qty']:g} × {_fmt(info['buy'], info['currency'])} → "
            f"{alerts_mod.fmt_pct(pl_pct)} ({_fmt(pl, info['currency'])})"
        )
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
    ]
    if can_edit:
        keyboard += [
            [_btn("🔔 Nueva alerta", f"alnew:{stock_id}"), _btn("🎯 Objetivo", f"target:{stock_id}")],
            [_btn("💼 Posición", f"pos:{stock_id}"), _btn("📝 Notas", f"notes:{stock_id}")],
            [_btn("🗑 Quitar", f"sd:{stock_id}")],
        ]
    keyboard.append([_btn("◀️ Volver", f"l:{info['list_id']}"), _btn("🔄 Actualizar", f"s:{stock_id}")])
    _show(ctx, "\n".join(filter(None, lines)), keyboard, message_id)
    if notes_apart:
        for i in range(0, len(notes_apart), 3500):
            _send(f"📝 <b>Notas de {info['ticker']}</b>\n{notes_apart[i:i + 3500]}", chat_id=ctx["chat"])


def _alerts_view(ctx: dict, message_id: int | None = None) -> None:
    with SessionLocal() as session:
        query = select(Alert).join(Stock).join(Watchlist).order_by(Alert.created_at.desc())
        if not _is_admin(ctx):
            query = query.join(WatchlistMember).where(WatchlistMember.user_id == ctx["uid"])
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
        user = session.get(BotUser, ctx["uid"])
        periodic, summary = get_user_summary_prefs(session, user)
    periodic_txt = f"cada <b>{periodic} min</b>" if periodic else "<b>desactivado</b>"
    lines = ["<b>⚙️ Ajustes</b>"]
    keyboard = []
    if _is_admin(ctx):
        lines += [
            "\n<u>Globales (para todos)</u>",
            f"⚡ Aviso de cambio brusco: <b>±{move}%</b>",
            f"⏱ Comprobación de alertas: cada <b>{interval} min</b>",
        ]
        keyboard += [
            [_btn("⚡ Cambiar umbral %", "set:move")],
            [_btn("⏱ Cambiar intervalo alertas", "set:interval")],
        ]
    lines += [
        "\n<u>Tus resúmenes</u>",
        f"📊 Resumen automático: {periodic_txt}",
        f"🕙 Resumen diario: a las <b>{summary}</b> ({config.TIMEZONE})",
    ]
    keyboard += [
        [_btn("📊 Cambiar resumen automático", "set:periodic")],
        [_btn("🕙 Cambiar hora resumen diario", "set:summary")],
        [_btn("◀️ Menú", "menu")],
    ]
    _show(ctx, "\n".join(lines), keyboard, message_id)


def _users_view(ctx: dict, message_id: int | None = None) -> None:
    with SessionLocal() as session:
        users = session.scalars(select(BotUser).order_by(BotUser.created_at)).all()
        rows, lines = [], ["<b>👥 Usuarios del bot</b>"]
        for u in users:
            if u.role == "admin":
                lines.append(f"👑 {esc(u.name)} (tú)")
            elif u.role == "user":
                lists = len(u.shared_lists)
                lines.append(f"👤 {esc(u.name)} — {lists} lista{'s' if lists != 1 else ''}")
                rows.append([_btn(f"⚙️ Gestionar a {u.name[:20]}", f"uv:{u.id}")])
            else:
                lines.append(f"⏳ {esc(u.name)} (pendiente)")
                rows.append([_btn(f"✅ Aprobar a {u.name[:16]}", f"uok:{u.id}"), _btn("❌", f"uno:{u.id}")])
    lines.append("\nPara invitar a alguien, pásale el enlace del bot y "
                 "cuando escriba te llegará su solicitud.")
    rows.append([_btn("◀️ Menú", "menu")])
    _show(ctx, "\n".join(lines), rows, message_id)


def _user_detail_view(ctx: dict, user_id: int, message_id: int | None = None) -> None:
    """Ficha de un usuario (solo admin): sus listas con lo que contienen y
    sus preferencias de resúmenes, todo editable."""
    with SessionLocal() as session:
        user = session.get(BotUser, user_id)
        if not user or user.role != "user":
            _users_view(ctx, message_id)
            return
        periodic, summary = get_user_summary_prefs(session, user)
        periodic_txt = f"cada <b>{periodic} min</b>" if periodic else "<b>desactivado</b>"
        lines = [
            f"👤 <b>{esc(user.name)}</b>",
            f"📊 Resumen automático: {periodic_txt}",
            f"🕙 Resumen diario: a las <b>{summary}</b> ({config.TIMEZONE})",
        ]
        rows = [[
            _btn("📊 Cambiar automático", f"uset:{user_id}:periodic"),
            _btn("🕙 Cambiar diario", f"uset:{user_id}:summary"),
        ]]
        memberships = sorted(user.memberships, key=lambda m: m.watchlist_id)
        if memberships:
            lines.append("\n<u>Sus listas</u>")
            for m in memberships:
                perm = "✏️" if m.can_edit else "👁"
                tickers = ", ".join(sorted(s.ticker for s in m.watchlist.stocks)) or "vacía"
                lines.append(f"📋 <b>{esc(m.watchlist.name)}</b> {perm}: {esc(tickers)}")
                rows.append([_btn(f"📋 Abrir {m.watchlist.name[:40]}", f"l:{m.watchlist_id}")])
        else:
            lines.append("\nNo tiene listas: compártele una desde 📋 Mis listas → 👥 Compartir.")
    rows.append([_btn("🗑 Quitar acceso", f"udel:{user_id}")])
    rows.append([_btn("◀️ Usuarios", "users")])
    _show(ctx, "\n".join(lines), rows, message_id)


def _share_view(ctx: dict, list_id: int, message_id: int | None) -> None:
    """Compartir una lista: toca el nombre para añadir/quitar y el permiso para cambiarlo."""
    with SessionLocal() as session:
        wl = session.get(Watchlist, list_id)
        if not wl:
            _lists_view(ctx, message_id)
            return
        users = session.scalars(select(BotUser).where(BotUser.role == "user")).all()
        memberships = {m.user_id: m for m in wl.memberships}
        rows = []
        for u in users:
            member = memberships.get(u.id)
            row = [_btn(("✅ " if member else "▫️ ") + u.name, f"lasgto:{list_id}:{u.id}")]
            if member:
                perm = "✏️ Edita" if member.can_edit else "👁️ Solo ver"
                row.append(_btn(perm, f"lperm:{list_id}:{u.id}"))
            rows.append(row)
        name = wl.name
    rows.append([_btn("✔️ Listo", f"l:{list_id}")])
    _show(
        ctx,
        f"👥 <b>Compartir «{esc(name)}»</b>\n"
        "Toca un nombre para añadirlo o quitarlo, y su permiso para alternar "
        "entre ✏️ editar y 👁️ solo ver. Tú (admin) siempre lo ves y editas todo.",
        rows,
        message_id,
    )


def _editable_lists(session, ctx: dict):
    return [wl for wl in _user_lists(session, ctx) if _can_edit_list(ctx, wl)]


def _pick_list_keyboard(session, ctx: dict, symbol: str) -> list:
    watchlists = _editable_lists(session, ctx)
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
    with SessionLocal() as session:
        wl = session.get(Watchlist, list_id)
        if not _can_edit_list(ctx, wl):
            _send("Esa lista ya no existe o no tienes permiso para editarla.", chat_id=ctx["chat"])
            return
        stock, error = services.add_stock(session, wl, symbol)
        stock_id = stock.id if stock else None
    if error:
        _send(esc(error), chat_id=ctx["chat"])
    if stock_id:
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


# Cada acción de un botón tiene su función (ctx, args, message_id); el
# diccionario CALLBACK_HANDLERS del final las despacha por nombre.


def _cb_menu(ctx, args, message_id):
    _main_menu(ctx, message_id)


def _cb_lists(ctx, args, message_id):
    _lists_view(ctx, message_id)


def _cb_list(ctx, args, message_id):
    _list_view(ctx, int(args[0]), message_id)


def _cb_list_new(ctx, args, message_id):
    _pending[ctx["chat"]] = {"action": "new_list"}
    _send(
        "Escríbeme el <b>nombre</b> de la nueva lista (solo el nombre — "
        "los valores se añaden después con ➕ Añadir):",
        [[_btn("Cancelar", "menu")]],
        ctx["chat"],
    )


def _cb_list_rename(ctx, args, message_id):
    list_id = int(args[0])
    with SessionLocal() as session:
        wl = session.get(Watchlist, list_id)
        allowed = _can_delete_list(ctx, wl)
        name = wl.name if wl else ""
    if not allowed:
        _send("Solo el administrador o quien creó la lista puede renombrarla.", chat_id=ctx["chat"])
        return
    _pending[ctx["chat"]] = {"action": "rename_list", "list_id": list_id}
    _send(f"Escríbeme el nuevo nombre para «{esc(name)}»:", [[_btn("Cancelar", f"l:{list_id}")]], ctx["chat"])


def _cb_list_delete(ctx, args, message_id):
    with SessionLocal() as session:
        wl = session.get(Watchlist, int(args[0]))
        allowed = _can_delete_list(ctx, wl)
    if not allowed:
        _send("Solo el administrador o quien creó la lista puede eliminarla.", chat_id=ctx["chat"])
        return
    keyboard = [[_btn("Sí, eliminar", f"ldel2:{args[0]}"), _btn("No", f"l:{args[0]}")]]
    _edit(ctx["chat"], message_id, "¿Eliminar la lista con todo su contenido?", keyboard)


def _cb_list_delete_confirm(ctx, args, message_id):
    with SessionLocal() as session:
        wl = session.get(Watchlist, int(args[0]))
        if _can_delete_list(ctx, wl):
            session.delete(wl)
            session.commit()
    _lists_view(ctx, message_id)


def _cb_share(ctx, args, message_id):
    _share_view(ctx, int(args[0]), message_id)


def _cb_share_toggle(ctx, args, message_id):
    list_id, uid = int(args[0]), int(args[1])
    with SessionLocal() as session:
        wl = session.get(Watchlist, list_id)
        user = session.get(BotUser, uid)
        if wl and user and user.role == "user":
            member = next((m for m in wl.memberships if m.user_id == uid), None)
            if member:
                services.unshare_list(session, member)
            else:
                services.share_list(session, wl, user)
    scheduler.reschedule()
    _share_view(ctx, list_id, message_id)


def _cb_share_permission(ctx, args, message_id):
    list_id, uid = int(args[0]), int(args[1])
    with SessionLocal() as session:
        wl = session.get(Watchlist, list_id)
        member = next((m for m in wl.memberships if m.user_id == uid), None) if wl else None
        if member:
            services.toggle_member_edit(session, member)
    _share_view(ctx, list_id, message_id)


def _cb_stock(ctx, args, message_id):
    _stock_view(ctx, int(args[0]), message_id)


def _cb_stock_delete(ctx, args, message_id):
    keyboard = [[_btn("Sí, quitar", f"sd2:{args[0]}"), _btn("No", f"s:{args[0]}")]]
    _edit(ctx["chat"], message_id, "¿Quitar este valor de la lista?", keyboard)


def _cb_stock_delete_confirm(ctx, args, message_id):
    with SessionLocal() as session:
        stock = _get_stock_checked(session, ctx, int(args[0]), edit=True)
        list_id = stock.watchlist_id if stock else None
        if stock:
            session.delete(stock)
            session.commit()
    _list_view(ctx, list_id, message_id) if list_id else _lists_view(ctx, message_id)


def _cb_add(ctx, args, message_id):
    _pending[ctx["chat"]] = {"action": "search", "list_id": None}
    _send("Escríbeme el nombre o ticker (ej: apple, SAN.MC, oro):", [[_btn("Cancelar", "menu")]], ctx["chat"])


def _cb_add_to_list(ctx, args, message_id):
    with SessionLocal() as session:
        wl = session.get(Watchlist, int(args[0]))
        allowed = _can_edit_list(ctx, wl)
    if not allowed:
        _send("En esta lista solo tienes permiso de lectura.", chat_id=ctx["chat"])
        return
    _pending[ctx["chat"]] = {"action": "search", "list_id": int(args[0])}
    _send("Escríbeme el nombre o ticker (ej: apple, SAN.MC, oro):", [[_btn("Cancelar", "menu")]], ctx["chat"])


def _cb_pick_list(ctx, args, message_id):
    symbol = args[0]
    with SessionLocal() as session:
        watchlists = _editable_lists(session, ctx)
        keyboard = _pick_list_keyboard(session, ctx, symbol)
    if len(watchlists) == 1:
        _add_stock(ctx, symbol, watchlists[0].id, message_id)
    elif not watchlists:
        _send("No tienes ninguna lista editable; crea una desde 📋 Mis listas.", chat_id=ctx["chat"])
    else:
        _edit(ctx["chat"], message_id, f"¿A qué lista añado <b>{symbol}</b>?", keyboard)


def _cb_add_to(ctx, args, message_id):
    _add_stock(ctx, args[0], int(args[1]), message_id)


def _cb_alert_new(ctx, args, message_id):
    with SessionLocal() as session:
        allowed = _get_stock_checked(session, ctx, int(args[0]), edit=True) is not None
    if not allowed:
        return
    keyboard = [
        [_btn("⬆️ Si sube de…", f"alk:{args[0]}:above:0"), _btn("⬇️ Si baja de…", f"alk:{args[0]}:below:0")],
        [_btn("🔁⬆️ Sube (se repite)", f"alk:{args[0]}:above:1"), _btn("🔁⬇️ Baja (se repite)", f"alk:{args[0]}:below:1")],
        [_btn("Cancelar", f"s:{args[0]}")],
    ]
    _edit(
        ctx["chat"], message_id,
        "¿Qué tipo de alerta? Las 🔁 se re-arman solas cuando el precio vuelve a cruzar el umbral.",
        keyboard,
    )


def _cb_alert_kind(ctx, args, message_id):
    repeat = len(args) > 2 and args[2] == "1"
    _pending[ctx["chat"]] = {
        "action": "alert_price", "stock_id": int(args[0]), "kind": args[1], "repeat": repeat,
    }
    _send(
        "Escríbeme el umbral: un precio (ej: 150.50) o un % desde el precio actual (ej: 5%):",
        [[_btn("Cancelar", f"s:{args[0]}")]],
        ctx["chat"],
    )


def _alert_update(ctx, alert_id: int, rearm: bool, message_id):
    with SessionLocal() as session:
        alert = session.get(Alert, alert_id)
        if alert and _can_edit_list(ctx, alert.stock.watchlist):
            if rearm:
                alert.active = True
                alert.triggered_at = None
            else:
                session.delete(alert)
            session.commit()
    _alerts_view(ctx, message_id)


def _cb_alert_delete(ctx, args, message_id):
    _alert_update(ctx, int(args[0]), rearm=False, message_id=message_id)


def _cb_alert_rearm(ctx, args, message_id):
    _alert_update(ctx, int(args[0]), rearm=True, message_id=message_id)


def _cb_alerts(ctx, args, message_id):
    _alerts_view(ctx, message_id)


def _cb_settings(ctx, args, message_id):
    _settings_view(ctx, message_id)


def _cb_users(ctx, args, message_id):
    _users_view(ctx, message_id)


def _cb_user_approve(ctx, args, message_id):
    with SessionLocal() as session:
        user = session.get(BotUser, int(args[0]))
        if user and user.role == "pending":
            services.approve_user(session, user)
    scheduler.reschedule()  # programar los resúmenes del nuevo usuario
    _users_view(ctx, message_id)


def _cb_user_reject(ctx, args, message_id):
    with SessionLocal() as session:
        user = session.get(BotUser, int(args[0]))
        if user and user.role == "pending":
            chat = user.chat_id
            session.delete(user)
            session.commit()
            _send("❌ Tu solicitud de acceso ha sido rechazada.", chat_id=chat)
    _users_view(ctx, message_id)


def _cb_user_view(ctx, args, message_id):
    _user_detail_view(ctx, int(args[0]), message_id)


def _cb_user_setting(ctx, args, message_id):
    user_id, which = int(args[0]), args[1]
    with SessionLocal() as session:
        user = session.get(BotUser, user_id)
        if not user or user.role != "user":
            return
        name = user.name
    if which == "periodic":
        action, prompt = "summary_interval", (
            f"Cada cuántos minutos le mando el resumen automático a {esc(name)} "
            "(0 para desactivarlo):"
        )
    else:
        action, prompt = "summary_time", (
            f"Hora del resumen diario de {esc(name)} en formato HH:MM (ej: 22:10):"
        )
    _pending[ctx["chat"]] = {"action": action, "target_uid": user_id}
    _send(prompt, [[_btn("Cancelar", f"uv:{user_id}")]], ctx["chat"])


def _cb_user_delete(ctx, args, message_id):
    keyboard = [[_btn("Sí, quitar acceso", f"udel2:{args[0]}"), _btn("No", "users")]]
    _edit(ctx["chat"], message_id, "¿Quitar el acceso a este usuario? Sus listas pasarán a ti.", keyboard)


def _cb_user_delete_confirm(ctx, args, message_id):
    with SessionLocal() as session:
        user = session.get(BotUser, int(args[0]))
        if user and user.role == "user":
            services.remove_user(session, user)
    scheduler.reschedule()  # retirar los jobs de resumen del usuario
    _users_view(ctx, message_id)


def _cb_setting(ctx, args, message_id):
    if args[0] in ("move", "interval") and not _is_admin(ctx):
        return
    prompts = {
        "move": ("move", "Nuevo umbral de cambio brusco en % (ej: 5):"),
        "interval": ("interval", "Cada cuántos minutos comprobar alertas (ej: 10):"),
        "periodic": ("summary_interval", "Cada cuántos minutos te mando el resumen automático (0 para desactivarlo):"),
        "summary": ("summary_time", "Hora de tu resumen diario en formato HH:MM (ej: 22:10):"),
    }
    key, prompt = prompts[args[0]]
    _pending[ctx["chat"]] = {"action": key}
    _send(prompt, [[_btn("Cancelar", "settings")]], ctx["chat"])


def _cb_target(ctx, args, message_id):
    _pending[ctx["chat"]] = {"action": "target", "stock_id": int(args[0])}
    _send("Escríbeme el precio objetivo (o «quitar» para borrarlo):", [[_btn("Cancelar", f"s:{args[0]}")]], ctx["chat"])


def _cb_notes(ctx, args, message_id):
    _pending[ctx["chat"]] = {"action": "notes", "stock_id": int(args[0])}
    _send("Escríbeme las notas para este valor (o «quitar» para borrarlas):", [[_btn("Cancelar", f"s:{args[0]}")]], ctx["chat"])


def _cb_position(ctx, args, message_id):
    _pending[ctx["chat"]] = {"action": "position", "stock_id": int(args[0])}
    _send(
        "Escríbeme cantidad y precio de compra separados por un espacio "
        "(ej: <code>10 120.50</code>), o «quitar» para borrar la posición:",
        [[_btn("Cancelar", f"s:{args[0]}")]],
        ctx["chat"],
    )


def _cb_chart(ctx, args, message_id):
    _send_chart(ctx["chat"], args[0], args[1] if len(args) > 1 else "6mo")


def _cb_news(ctx, args, message_id):
    _send_news(ctx["chat"], args[0])


def _cb_summary(ctx, args, message_id):
    if not alerts_mod.send_summary_to(ctx["chat"]):
        _send("Aún no tienes listas con valores.", chat_id=ctx["chat"])


CALLBACK_HANDLERS = {
    "menu": _cb_menu,
    "lists": _cb_lists,
    "l": _cb_list,
    "lnew": _cb_list_new,
    "lren": _cb_list_rename,
    "ldel": _cb_list_delete,
    "ldel2": _cb_list_delete_confirm,
    "lasg": _cb_share,
    "lasgto": _cb_share_toggle,
    "lperm": _cb_share_permission,
    "s": _cb_stock,
    "sd": _cb_stock_delete,
    "sd2": _cb_stock_delete_confirm,
    "add": _cb_add,
    "addl": _cb_add_to_list,
    "pick": _cb_pick_list,
    "addto": _cb_add_to,
    "alnew": _cb_alert_new,
    "alk": _cb_alert_kind,
    "ad": _cb_alert_delete,
    "ar": _cb_alert_rearm,
    "alerts": _cb_alerts,
    "settings": _cb_settings,
    "users": _cb_users,
    "uv": _cb_user_view,
    "uset": _cb_user_setting,
    "uok": _cb_user_approve,
    "uno": _cb_user_reject,
    "udel": _cb_user_delete,
    "udel2": _cb_user_delete_confirm,
    "set": _cb_setting,
    "target": _cb_target,
    "notes": _cb_notes,
    "pos": _cb_position,
    "g": _cb_chart,
    "n": _cb_news,
    "summary": _cb_summary,
}

ADMIN_ONLY_ACTIONS = {"users", "uv", "uset", "uok", "uno", "udel", "udel2", "lasg", "lasgto", "lperm"}


def _handle_callback(update: dict) -> None:
    query = update["callback_query"]
    chat_id = str(query.get("message", {}).get("chat", {}).get("id", ""))
    _call("answerCallbackQuery", callback_query_id=query["id"])
    ctx = _get_ctx(chat_id)
    if not ctx:
        return
    message_id = query["message"]["message_id"]
    action, *args = query.get("data", "").split(":")
    _pending.pop(ctx["chat"], None)
    handler = CALLBACK_HANDLERS.get(action)
    if not handler or (action in ADMIN_ONLY_ACTIONS and not _is_admin(ctx)):
        return
    handler(ctx, args, message_id)


def _parse_number(text: str) -> float | None:
    try:
        return float(text.strip().replace(",", "."))
    except ValueError:
        return None


# Cada acción pendiente de respuesta de texto tiene su función
# (ctx, text, pending); el diccionario PENDING_HANDLERS del final las
# despacha por nombre, igual que CALLBACK_HANDLERS con los botones.


def _pending_search(ctx, text, pending):
    _handle_search_text(ctx, text, pending.get("list_id"))


def _pending_new_list(ctx, text, pending):
    # Solo la primera línea: si mandan varios renglones (p. ej. tickers),
    # no queremos un nombre de lista multilínea.
    name = text.strip().splitlines()[0].strip()[:60]
    if not name:
        _send("Necesito un nombre para la lista.", chat_id=ctx["chat"])
        return
    if len(text.strip().splitlines()) > 1:
        _send(
            f"Ojo: he usado solo «{esc(name)}» como nombre. "
            "Los valores se añaden después desde la lista con ➕ Añadir.",
            chat_id=ctx["chat"],
        )
    with SessionLocal() as session:
        creator = session.get(BotUser, ctx["uid"])
        _, error = services.create_list(session, name, creator)
    if error:
        _send(esc(error), chat_id=ctx["chat"])
    _lists_view(ctx)


def _pending_rename_list(ctx, text, pending):
    name = text.strip().splitlines()[0]
    with SessionLocal() as session:
        wl = session.get(Watchlist, pending["list_id"])
        if not wl or not _can_delete_list(ctx, wl):
            _send("Esa lista ya no existe o no puedes renombrarla.", chat_id=ctx["chat"])
            return
        error = services.rename_list(session, wl, name)
    if error:
        _send(esc(error), chat_id=ctx["chat"])
        return
    _list_view(ctx, pending["list_id"])


def _pending_alert_price(ctx, text, pending):
    with SessionLocal() as session:
        stock = _get_stock_checked(session, ctx, pending["stock_id"], edit=True)
        if stock:
            quote = prices.get_quote(stock.ticker)
            value = services.parse_threshold(
                text, pending["kind"], quote["price"] if quote else None
            )
            if value is None:
                _send(
                    "Eso no parece un umbral válido. Usa un precio (150.50) o un porcentaje (5%).",
                    chat_id=ctx["chat"],
                )
                return
            session.add(Alert(
                stock_id=stock.id, kind=pending["kind"], threshold=value,
                repeat=pending.get("repeat", False),
            ))
            session.commit()
    _stock_view(ctx, pending["stock_id"])


def _pending_target(ctx, text, pending):
    with SessionLocal() as session:
        stock = _get_stock_checked(session, ctx, pending["stock_id"], edit=True)
        if stock:
            if text.strip().lower() in ("quitar", "borrar", "no"):
                stock.target_price = None
            else:
                value = _parse_number(text)
                if value is None or value <= 0:
                    _send("Eso no parece un precio válido.", chat_id=ctx["chat"])
                    return
                stock.target_price = value
            session.commit()
    _stock_view(ctx, pending["stock_id"])


def _pending_notes(ctx, text, pending):
    with SessionLocal() as session:
        stock = _get_stock_checked(session, ctx, pending["stock_id"], edit=True)
        if stock:
            stock.notes = "" if text.strip().lower() in ("quitar", "borrar") else text.strip()
            session.commit()
    _stock_view(ctx, pending["stock_id"])


def _pending_position(ctx, text, pending):
    with SessionLocal() as session:
        stock = _get_stock_checked(session, ctx, pending["stock_id"], edit=True)
        if stock:
            if text.strip().lower() in ("quitar", "borrar", "no"):
                stock.quantity = None
                stock.buy_price = None
            else:
                parts = text.split()
                qty = _parse_number(parts[0]) if parts else None
                buy = _parse_number(parts[1]) if len(parts) > 1 else None
                if not qty or not buy or qty <= 0 or buy <= 0:
                    _send("No lo entendí. Escribe cantidad y precio (ej: 10 120.50), o «quitar».", chat_id=ctx["chat"])
                    return
                stock.quantity = qty
                stock.buy_price = buy
            session.commit()
    _stock_view(ctx, pending["stock_id"])


def _pending_move(ctx, text, pending):
    value = _parse_number(text)
    if value is None or value <= 0:
        _send("Debe ser un número positivo (ej: 5).", chat_id=ctx["chat"])
        return
    with SessionLocal() as session:
        set_setting(session, "move_threshold", str(value))
        session.commit()
    _settings_view(ctx)


def _pending_interval(ctx, text, pending):
    value = _parse_number(text)
    if value is None or not 1 <= value <= 720:
        _send("Debe ser un número de minutos entre 1 y 720.", chat_id=ctx["chat"])
        return
    with SessionLocal() as session:
        set_setting(session, "check_interval_minutes", str(int(value)))
        session.commit()
    scheduler.reschedule()
    _settings_view(ctx)


def _save_user_pref(ctx, pending, field: str, value) -> None:
    """Guarda una preferencia de resúmenes y muestra la vista que toca.
    target_uid: el admin puede estar editando los resúmenes de otro usuario."""
    target_uid = pending.get("target_uid", ctx["uid"])
    with SessionLocal() as session:
        user = session.get(BotUser, target_uid)
        if user:
            setattr(user, field, value)
            session.commit()
    scheduler.reschedule()
    _user_detail_view(ctx, target_uid) if target_uid != ctx["uid"] else _settings_view(ctx)


def _pending_summary_interval(ctx, text, pending):
    value = _parse_number(text)
    if value is None or not 0 <= value <= 1440:
        _send("Debe ser un número de minutos entre 0 (desactivado) y 1440.", chat_id=ctx["chat"])
        return
    _save_user_pref(ctx, pending, "summary_interval", int(value))


def _pending_summary_time(ctx, text, pending):
    normalized = normalize_time(text)
    if not normalized:
        _send(f"No entendí «{esc(text.strip())}». Usa el formato HH:MM (ej: 22:10).", chat_id=ctx["chat"])
        return
    _save_user_pref(ctx, pending, "summary_time", normalized)


PENDING_HANDLERS = {
    "search": _pending_search,
    "new_list": _pending_new_list,
    "rename_list": _pending_rename_list,
    "alert_price": _pending_alert_price,
    "target": _pending_target,
    "notes": _pending_notes,
    "position": _pending_position,
    "move": _pending_move,
    "interval": _pending_interval,
    "summary_interval": _pending_summary_interval,
    "summary_time": _pending_summary_time,
}


def _handle_pending(ctx: dict, text: str) -> bool:
    """Procesa la respuesta a una pregunta previa. True si había algo pendiente."""
    pending = _pending.pop(ctx["chat"], None)
    if not pending:
        return False
    handler = PENDING_HANDLERS.get(pending["action"])
    if handler:
        handler(ctx, text, pending)
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
                resp = _client.get(
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
            resp = _client.get(
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
