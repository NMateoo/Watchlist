"""Acceso a datos de mercado vía yfinance, con caché en memoria."""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

import yfinance as yf

log = logging.getLogger(__name__)

# Caché de cotizaciones: {ticker: (timestamp, quote_dict)}
_cache: dict[str, tuple[float, dict]] = {}
CACHE_TTL_SECONDS = 120


def _fetch_quote(ticker: str) -> dict | None:
    """Cotización actual de un ticker, o None si no se pudo obtener."""
    try:
        info = yf.Ticker(ticker).fast_info
        price = info.last_price
        prev = info.previous_close
        if price is None:
            return None
        change_pct = ((price - prev) / prev * 100) if prev else 0.0
        return {
            "ticker": ticker,
            "price": float(price),
            "prev_close": float(prev) if prev else None,
            "change_pct": round(change_pct, 2),
            "currency": (info.currency or "USD").upper(),
            "year_high": float(info.year_high) if info.year_high else None,
            "year_low": float(info.year_low) if info.year_low else None,
        }
    except Exception as exc:
        log.warning("No se pudo obtener cotización de %s: %s", ticker, exc)
        return None


def get_quote(ticker: str, max_age: int = CACHE_TTL_SECONDS) -> dict | None:
    ticker = ticker.upper()
    cached = _cache.get(ticker)
    if cached and time.time() - cached[0] < max_age:
        return cached[1]
    quote = _fetch_quote(ticker)
    if quote:
        _cache[ticker] = (time.time(), quote)
        return quote
    # Si Yahoo falla, mejor una cotización algo vieja que ninguna.
    return cached[1] if cached else None


def get_quotes(tickers: list[str], max_age: int = CACHE_TTL_SECONDS) -> dict[str, dict]:
    """Cotizaciones de varios tickers en paralelo. Devuelve {ticker: quote}."""
    if not tickers:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(tickers))) as pool:
        results = pool.map(lambda t: get_quote(t, max_age), tickers)
    return {q["ticker"]: q for q in results if q}


def lookup_name(ticker: str) -> str:
    """Nombre de la empresa/activo; se consulta una sola vez al añadirlo."""
    try:
        info = yf.Ticker(ticker).info
        return info.get("shortName") or info.get("longName") or ticker.upper()
    except Exception:
        return ticker.upper()


VALID_RANGES = {"1mo": "1d", "3mo": "1d", "6mo": "1d", "1y": "1d", "5y": "1wk", "max": "1mo"}


def get_history(ticker: str, period: str = "6mo") -> list[dict]:
    """Serie histórica de cierres para el gráfico: [{date, close}, ...]."""
    if period not in VALID_RANGES:
        period = "6mo"
    try:
        df = yf.Ticker(ticker.upper()).history(period=period, interval=VALID_RANGES[period])
    except Exception as exc:
        log.warning("No se pudo obtener histórico de %s: %s", ticker, exc)
        return []
    if df is None or df.empty:
        return []
    return [
        {"date": idx.strftime("%Y-%m-%d"), "close": round(float(row["Close"]), 4)}
        for idx, row in df.iterrows()
    ]
