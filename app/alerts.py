"""Lógica de alertas: umbrales de precio, cambios bruscos y resumen diario."""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app import config, prices, telegram
from app.database import Alert, MoveNotice, Stock, get_move_threshold, session_scope, utcnow

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


def check_alerts() -> None:
    """Job principal: comprueba umbrales y cambios bruscos de toda la watchlist."""
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
            f"🔔 <b>{stock.ticker}</b> ({stock.name})\n"
            f"Ha cruzado {direction} tu umbral de {fmt_price(alert.threshold, quote['currency'])}\n"
            f"Precio actual: <b>{fmt_price(price, quote['currency'])}</b> "
            f"({fmt_pct(quote['change_pct'])} hoy)"
        )
        if sent:
            alert.active = False
            alert.triggered_at = utcnow()
            log.info("Alerta %s de %s disparada a %.2f", alert.kind, stock.ticker, price)


def _check_big_move(session, stock: Stock, quote: dict, threshold_pct: float, today: str) -> None:
    change = quote["change_pct"]
    if abs(change) < threshold_pct:
        return
    already = session.scalar(
        select(MoveNotice).where(MoveNotice.ticker == stock.ticker, MoveNotice.day == today)
    )
    if already:
        return
    emoji = "📈" if change > 0 else "📉"
    sent = telegram.send_message(
        f"{emoji} <b>{stock.ticker}</b> ({stock.name})\n"
        f"Movimiento brusco hoy: <b>{fmt_pct(change)}</b>\n"
        f"Precio actual: {fmt_price(quote['price'], quote['currency'])}"
    )
    if sent:
        session.add(MoveNotice(ticker=stock.ticker, day=today, pct=change))
        log.info("Aviso de cambio brusco de %s (%.2f%%)", stock.ticker, change)


def send_daily_summary() -> None:
    """Resumen diario con toda la watchlist, ordenada por variación."""
    with session_scope() as session:
        stocks = session.scalars(select(Stock)).all()
    if not stocks:
        return
    quotes = prices.get_quotes([s.ticker for s in stocks], max_age=60)
    rows = []
    for stock in sorted(
        stocks, key=lambda s: quotes.get(s.ticker, {}).get("change_pct", 0), reverse=True
    ):
        quote = quotes.get(stock.ticker)
        if not quote:
            rows.append(f"• {stock.ticker}: sin datos")
            continue
        emoji = "🟢" if quote["change_pct"] >= 0 else "🔴"
        rows.append(
            f"{emoji} <b>{stock.ticker}</b>  {fmt_price(quote['price'], quote['currency'])}"
            f"  ({fmt_pct(quote['change_pct'])})"
        )
    date_str = datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%d/%m/%Y")
    telegram.send_message(f"📊 <b>Resumen diario — {date_str}</b>\n\n" + "\n".join(rows))
    log.info("Resumen diario enviado (%d valores)", len(stocks))
