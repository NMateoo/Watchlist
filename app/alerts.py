"""Lógica de alertas: umbrales de precio, cambios bruscos y resumen diario."""
from __future__ import annotations

import logging
from datetime import datetime
from html import escape as esc
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app import config, prices, telegram
from app.database import (
    Alert,
    BotUser,
    MoveNotice,
    Stock,
    Watchlist,
    get_move_threshold,
    session_scope,
    utcnow,
)

log = logging.getLogger(__name__)

CURRENCY_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "GBp": "p"}


def fmt_price(value: float | None, currency: str = "USD") -> str:
    if value is None:
        return "—"
    symbol = CURRENCY_SYMBOLS.get(currency, currency + " ")
    return f"{value:,.2f} {symbol}".replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_pct(value: float) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.2f}%".replace(".", ",")


def _local_today() -> str:
    return datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%Y-%m-%d")


def _owner_chat(stock: Stock) -> str | None:
    """Chat del dueño de la lista del valor (None → chat del admin)."""
    owner = stock.watchlist.owner if stock.watchlist else None
    return owner.chat_id if owner else None


def check_alerts() -> None:
    """Job principal: comprueba umbrales y cambios bruscos de todas las listas."""
    with session_scope() as session:
        stocks = session.scalars(select(Stock)).all()
        if not stocks:
            return
        quotes = prices.get_quotes([s.ticker for s in stocks], max_age=60)
        threshold_pct = get_move_threshold(session)
        today = _local_today()

        for stock in stocks:
            quote = quotes.get(stock.ticker)
            if not quote:
                continue
            _check_threshold_alerts(session, stock, quote)
            _check_big_move(session, stock, quote, threshold_pct, today)


def _check_threshold_alerts(session, stock: Stock, quote: dict) -> None:
    price = quote["price"]
    for alert in stock.alerts:
        if not alert.active:
            continue
        crossed = (alert.kind == "above" and price >= alert.threshold) or (
            alert.kind == "below" and price <= alert.threshold
        )
        if not crossed:
            continue
        direction = "por encima de" if alert.kind == "above" else "por debajo de"
        sent = telegram.send_message(
            f"🔔 <b>{stock.ticker}</b> ({esc(stock.name)})\n"
            f"Ha cruzado {direction} tu umbral de {fmt_price(alert.threshold, quote['currency'])}\n"
            f"Precio actual: <b>{fmt_price(price, quote['currency'])}</b> "
            f"({fmt_pct(quote['change_pct'])} hoy)",
            chat_id=_owner_chat(stock),
        )
        if sent:
            alert.active = False
            alert.triggered_at = utcnow()
            log.info("Alerta %s de %s disparada a %.2f", alert.kind, stock.ticker, price)


def _check_big_move(session, stock: Stock, quote: dict, threshold_pct: float, today: str) -> None:
    change = quote["change_pct"]
    if abs(change) < threshold_pct:
        return
    chat = _owner_chat(stock)
    already = session.scalar(
        select(MoveNotice).where(
            MoveNotice.ticker == stock.ticker,
            MoveNotice.day == today,
            MoveNotice.chat_id == chat,
        )
    )
    if already:
        return
    emoji = "📈" if change > 0 else "📉"
    sent = telegram.send_message(
        f"{emoji} <b>{stock.ticker}</b> ({esc(stock.name)})\n"
        f"Movimiento brusco hoy: <b>{fmt_pct(change)}</b>\n"
        f"Precio actual: {fmt_price(quote['price'], quote['currency'])}",
        chat_id=chat,
    )
    if sent:
        session.add(MoveNotice(ticker=stock.ticker, day=today, chat_id=chat, pct=change))
        log.info("Aviso de cambio brusco de %s (%.2f%%)", stock.ticker, change)


MARKET_EMOJIS = {"pre": " 🟡", "post": " 🟣", "closed": " ⚪"}


def _summary_line(info: dict, quote: dict | None) -> str:
    if not quote:
        return f"• <b>{info['ticker']}</b>: sin datos"
    emoji = "🟢" if quote["change_pct"] >= 0 else "🔴"
    state = MARKET_EMOJIS.get(quote.get("market_state"), "")
    line = (
        f"{emoji} <b>{info['ticker']}</b>  {fmt_price(quote['price'], quote['currency'])}"
        f"  ({fmt_pct(quote['change_pct'])}){state}"
    )
    extras = []
    if quote.get("year_high"):
        from_high = (quote["price"] / quote["year_high"] - 1) * 100
        extras.append(f"máx 52s: {fmt_pct(from_high)}")
    if info["target"]:
        to_target = (info["target"] / quote["price"] - 1) * 100
        extras.append(f"🎯 {fmt_price(info['target'], quote['currency'])} ({fmt_pct(to_target)})")
    if info["alerts"]:
        extras.append(f"🔔 {info['alerts']}")
    if extras:
        line += "\n      " + " · ".join(extras)
    return line


def send_summary_to(chat_id: str | None) -> bool:
    """Resumen de las listas de un usuario: variación, distancia al máximo de
    52 semanas, precio objetivo, alertas activas y estado del mercado."""
    with session_scope() as session:
        query = select(Watchlist).order_by(Watchlist.id)
        if chat_id is not None:
            query = query.join(BotUser).where(BotUser.chat_id == str(chat_id))
        watchlists = session.scalars(query).all()
        groups = []
        for wl in watchlists:
            if not wl.stocks:
                continue
            stocks = [
                {
                    "ticker": s.ticker,
                    "target": s.target_price,
                    "alerts": sum(1 for a in s.alerts if a.active),
                }
                for s in wl.stocks
            ]
            groups.append((wl.name, stocks))
    if not groups:
        return False
    all_tickers = {s["ticker"] for _, stocks in groups for s in stocks}
    quotes = prices.get_quotes(sorted(all_tickers), max_age=60)

    sections = []
    total = 0
    for name, stocks in groups:
        ordered = sorted(
            stocks, key=lambda s: quotes.get(s["ticker"], {}).get("change_pct", 0), reverse=True
        )
        rows = [_summary_line(info, quotes.get(info["ticker"])) for info in ordered]
        total += len(stocks)
        header = f"<u>{esc(name)}</u>\n" if len(groups) > 1 else ""
        sections.append(header + "\n".join(rows))

    # pie con estadísticas del conjunto
    changes = {t: q["change_pct"] for t, q in quotes.items()}
    footer = ""
    if changes:
        ups = sum(1 for c in changes.values() if c >= 0)
        downs = len(changes) - ups
        best = max(changes, key=changes.get)
        worst = min(changes, key=changes.get)
        footer = (
            f"\n\n🟢 {ups} suben · 🔴 {downs} bajan\n"
            f"Mejor: <b>{best}</b> {fmt_pct(changes[best])} · "
            f"Peor: <b>{worst}</b> {fmt_pct(changes[worst])}"
        )

    stamp = datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%d/%m/%Y %H:%M")
    sent = telegram.send_message(
        f"📊 <b>Resumen — {stamp}</b>\n\n" + "\n\n".join(sections) + footer,
        chat_id=chat_id,
    )
    log.info("Resumen a %s (%d valores en %d listas)", chat_id or "admin", total, len(groups))
    return sent
