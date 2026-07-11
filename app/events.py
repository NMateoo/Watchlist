"""Avisos de eventos corporativos: resultados y dividendos.

Un job diario consulta el calendario de Yahoo de cada valor en las listas y
avisa a los miembros (y al admin) cuando el evento es hoy o mañana. La tabla
event_notices evita repetir avisos: cada evento se anuncia una sola vez por
chat (normalmente la víspera).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from html import escape as esc
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select

from app import config, prices, telegram
from app.alerts import recipient_chats
from app.database import EventNotice, Stock, session_scope

log = logging.getLogger(__name__)

_MESSAGES = {
    ("earnings", 0): "📅 <b>{ticker}</b> ({name}) presenta resultados hoy{estimated}.",
    ("earnings", 1): "📅 <b>{ticker}</b> ({name}) presenta resultados mañana{estimated}.",
    ("ex_dividend", 0): (
        "💰 <b>{ticker}</b> ({name}) cotiza ex-dividendo hoy: comprada a partir "
        "de hoy ya no da derecho al próximo dividendo."
    ),
    ("ex_dividend", 1): (
        "💰 <b>{ticker}</b> ({name}) cotiza ex-dividendo mañana: para cobrar el "
        "próximo dividendo hay que tenerla en cartera hoy al cierre."
    ),
    ("dividend", 0): "💵 <b>{ticker}</b> ({name}) paga dividendo hoy.",
    ("dividend", 1): "💵 <b>{ticker}</b> ({name}) paga dividendo mañana.",
}


def _purge_old_notices(session, today: date) -> None:
    """Borra registros de eventos de hace más de un mes (solo sirven para no
    repetir avisos recientes; sin purga la tabla crecería para siempre)."""
    cutoff = (today - timedelta(days=30)).isoformat()
    session.execute(delete(EventNotice).where(EventNotice.day < cutoff))


def _notify(session, stocks: list[Stock], kind: str, delta: int, event_day: date, events: dict) -> None:
    """Avisa del evento a cada chat interesado que aún no haya sido avisado."""
    ticker, name = stocks[0].ticker, stocks[0].name
    estimated = " (fecha estimada)" if kind == "earnings" and events.get("earnings_estimated") else ""
    text = _MESSAGES[(kind, delta)].format(ticker=ticker, name=esc(name), estimated=estimated)
    day = event_day.isoformat()
    # El mismo ticker puede estar en varias listas: unimos los destinatarios.
    chats = {chat for stock in stocks for chat in recipient_chats(stock)}
    for chat in sorted(chats, key=lambda c: (c is not None, c)):
        already = session.scalar(
            select(EventNotice).where(
                EventNotice.ticker == ticker,
                EventNotice.kind == kind,
                EventNotice.day == day,
                EventNotice.chat_id == chat,
            )
        )
        if already:
            continue
        if telegram.send_message(text, chat_id=chat):
            session.add(EventNotice(ticker=ticker, kind=kind, day=day, chat_id=chat))
            log.info("Aviso de %s de %s (%s) a %s", kind, ticker, day, chat or "admin")


def check_events() -> None:
    """Job diario: avisa de resultados y dividendos de hoy y de mañana."""
    today = datetime.now(ZoneInfo(config.TIMEZONE)).date()
    with session_scope() as session:
        _purge_old_notices(session, today)
        by_ticker: dict[str, list[Stock]] = {}
        for stock in session.scalars(select(Stock)).all():
            by_ticker.setdefault(stock.ticker, []).append(stock)
        for ticker, stocks in sorted(by_ticker.items()):
            events = prices.get_corporate_events(ticker)
            for kind in ("earnings", "ex_dividend", "dividend"):
                event_day = events.get(kind)
                if not event_day:
                    continue
                delta = (event_day - today).days
                if delta in (0, 1):
                    _notify(session, stocks, kind, delta, event_day, events)
