"""Operaciones compartidas entre la web (main.py) y el bot (bot.py).

Antes cada interfaz tenía su propia copia de esta lógica y era fácil que
divergieran. Cada función recibe una sesión abierta y objetos ya cargados,
hace commit y manda las notificaciones de Telegram que tocan.
"""
from __future__ import annotations

from html import escape as esc

from sqlalchemy import select

from app import prices, telegram
from app.database import BotUser, Stock, Watchlist, WatchlistMember


def get_admin(session) -> BotUser | None:
    return session.scalar(select(BotUser).where(BotUser.role == "admin"))


# ------------------------------------------------------------------ listas


def create_list(session, name: str, creator: BotUser | None) -> tuple[Watchlist | None, str | None]:
    """Crea una lista. Devuelve (lista, None) o (None, mensaje de error).
    Si la crea un usuario normal, queda como miembro con permiso de edición."""
    name = name.strip()[:60]
    if not name:
        return None, "La lista necesita un nombre."
    if session.scalar(select(Watchlist).where(Watchlist.name == name)):
        return None, f"Ya existe una lista llamada «{name}»."
    wl = Watchlist(name=name, owner_id=creator.id if creator else None)
    if creator and creator.role != "admin":
        wl.memberships.append(WatchlistMember(user=creator, can_edit=True))
    session.add(wl)
    session.commit()
    return wl, None


def rename_list(session, wl: Watchlist, name: str) -> str | None:
    """Renombra la lista. Devuelve un mensaje de error, o None si fue bien."""
    name = name.strip()[:60]
    if not name:
        return "La lista necesita un nombre."
    clash = session.scalar(
        select(Watchlist).where(Watchlist.name == name, Watchlist.id != wl.id)
    )
    if clash:
        return f"Ya existe una lista llamada «{name}»."
    wl.name = name
    session.commit()
    return None


def add_stock(session, wl: Watchlist, ticker: str) -> tuple[Stock | None, str | None]:
    """Añade un ticker a la lista. Devuelve (valor, None) si se añadió,
    (valor existente, error) si ya estaba, o (None, error) si no se encontró."""
    ticker = ticker.strip().upper()
    if not ticker:
        return None, "Escribe un ticker."
    exists = session.scalar(
        select(Stock).where(Stock.ticker == ticker, Stock.watchlist_id == wl.id)
    )
    if exists:
        return exists, f"{ticker} ya está en «{wl.name}»."
    quote = prices.get_quote(ticker)
    if not quote:
        return None, (
            f"No encuentro '{ticker}' en Yahoo Finance. "
            "Recuerda los sufijos: SAN.MC (Madrid), BTC-USD (cripto), GC=F (futuros)."
        )
    stock = Stock(
        ticker=ticker,
        watchlist_id=wl.id,
        name=prices.lookup_name(ticker),
        currency=quote["currency"],
    )
    session.add(stock)
    session.commit()
    return stock, None


# ------------------------------------------------------------- membresías


def share_list(session, wl: Watchlist, user: BotUser) -> bool:
    """Añade a `user` como miembro (con edición) y le avisa. True si se añadió."""
    if user.role != "user" or any(m.user_id == user.id for m in wl.memberships):
        return False
    wl.memberships.append(WatchlistMember(user=user, can_edit=True))
    session.commit()
    telegram.send_message(
        f"📬 Te han compartido la lista «{esc(wl.name)}». Escribe /menu para verla.",
        chat_id=user.chat_id,
    )
    return True


def unshare_list(session, member: WatchlistMember) -> None:
    session.delete(member)
    session.commit()


def toggle_member_edit(session, member: WatchlistMember) -> bool:
    """Alterna el permiso de edición del miembro y le avisa. Devuelve el nuevo estado."""
    member.can_edit = not member.can_edit
    session.commit()
    mode = "✏️ puedes editarla" if member.can_edit else "👁 es de solo lectura para ti"
    telegram.send_message(
        f"El administrador ha cambiado tu permiso en «{esc(member.watchlist.name)}»: {mode}.",
        chat_id=member.user.chat_id,
    )
    return member.can_edit


# ---------------------------------------------------------------- usuarios


ONBOARDING_MESSAGE = (
    "✅ ¡Acceso concedido! Escribe /menu para empezar.\n\n"
    "ℹ️ Cómo funciona:\n"
    "• El administrador te asignará tu lista de valores (o crea una tuya "
    "con 📋 Mis listas → ➕ Nueva lista).\n"
    "• Para añadir un valor: entra en la lista → ➕ Añadir y escribe el "
    "nombre o ticker (apple, SAN.MC, oro…).\n"
    "• Recibirás aquí las alertas y resúmenes de tus listas."
)


def approve_user(session, user: BotUser) -> None:
    user.role = "user"
    session.commit()
    telegram.send_message(ONBOARDING_MESSAGE, chat_id=user.chat_id)


def remove_user(session, user: BotUser) -> None:
    """Elimina al usuario: sus listas pasan al admin y sus membresías caen en cascada."""
    admin = get_admin(session)
    # Reasignar vía la relación (no tocando owner_id a mano): así la lista sale
    # de user.watchlists y el borrado del usuario no vuelve a dejarla sin dueño.
    for wl in list(user.watchlists):
        wl.owner = admin
    session.delete(user)
    session.commit()
