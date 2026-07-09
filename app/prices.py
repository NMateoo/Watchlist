"""Acceso a datos de mercado vía yfinance, con caché en memoria."""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import yfinance as yf

log = logging.getLogger(__name__)

# Caché de cotizaciones: {ticker: (timestamp, quote_dict)}
_cache: dict[str, tuple[float, dict]] = {}
CACHE_TTL_SECONDS = 120


def _market_state(trading_period: dict | None) -> str:
    """Estado del mercado según los horarios que devuelve Yahoo: pre/open/post/closed."""
    if not trading_period:
        return "unknown"
    try:
        now = time.time()
        regular = trading_period["regular"]
        if regular["start"] <= now < regular["end"]:
            return "open"
        pre = trading_period["pre"]
        if pre["start"] <= now < pre["end"]:
            return "pre"
        post = trading_period["post"]
        if post["start"] <= now < post["end"]:
            return "post"
        return "closed"
    except (KeyError, TypeError):
        return "unknown"


def _fetch_quote(ticker: str) -> dict | None:
    """Cotización actual (incluye pre/after-market), o None si no se pudo obtener."""
    try:
        t = yf.Ticker(ticker)
        # La API de gráficos con prepost=True trae el último precio también
        # fuera del horario regular (pre-market y after-hours).
        df = t.history(period="1d", interval="1m", prepost=True)
        meta = t.history_metadata or {}
        price = None
        if df is not None and not df.empty:
            closes = df["Close"].dropna()
            if not closes.empty:
                price = float(closes.iloc[-1])
        if price is None:
            price = meta.get("regularMarketPrice")
        if price is None:
            return None
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        change_pct = ((price - prev) / prev * 100) if prev else 0.0
        return {
            "ticker": ticker,
            "price": float(price),
            "prev_close": float(prev) if prev else None,
            "change_pct": round(change_pct, 2),
            "currency": (meta.get("currency") or "USD").upper(),
            "year_high": meta.get("fiftyTwoWeekHigh"),
            "year_low": meta.get("fiftyTwoWeekLow"),
            "market_state": _market_state(meta.get("currentTradingPeriod")),
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


def search_symbols(query: str, limit: int = 8) -> list[dict]:
    """Sugerencias de tickers para el buscador (búsqueda de Yahoo Finance)."""
    query = query.strip()
    if len(query) < 2:
        return []
    raw: list[dict] = []
    try:
        raw = yf.Search(query, max_results=limit).quotes
    except Exception as exc:
        log.warning("yf.Search falló (%s); probando API directa", exc)
        try:
            resp = httpx.get(
                "https://query2.finance.yahoo.com/v1/finance/search",
                params={"q": query, "quotesCount": limit, "newsCount": 0},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            raw = resp.json().get("quotes", [])
        except Exception as exc2:
            log.warning("Búsqueda de '%s' falló: %s", query, exc2)
    results = []
    for item in raw:
        symbol = item.get("symbol")
        if not symbol:
            continue
        results.append(
            {
                "symbol": symbol,
                "name": item.get("shortname") or item.get("longname") or "",
                "exchange": item.get("exchDisp") or item.get("exchange") or "",
                "type": item.get("typeDisp") or item.get("quoteType") or "",
            }
        )
    return results[:limit]


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
