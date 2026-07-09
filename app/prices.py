"""Acceso a datos de mercado vía yfinance, con caché en memoria."""
from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx
import yfinance as yf

log = logging.getLogger(__name__)

# Caché de cotizaciones: {ticker: (timestamp, quote_dict)}
_cache: dict[str, tuple[float, dict]] = {}
CACHE_TTL_SECONDS = 120

# ---- metales spot (Yahoo retiró XAUUSD=X; el precio sale de gold-api.com) --

SPOT_PATTERN = re.compile(r"^X(AU|AG|PT|PD)USD$")
SPOT_CATALOG = [
    ("XAUUSD", "Oro spot XAU/USD", ["oro", "gold", "xau"]),
    ("XAGUSD", "Plata spot XAG/USD", ["plata", "silver", "xag"]),
    ("XPTUSD", "Platino spot XPT/USD", ["platino", "platinum", "xpt"]),
    ("XPDUSD", "Paladio spot XPD/USD", ["paladio", "palladium", "xpd"]),
]
# Futuros de materias primas de Yahoo, buscables por nombre en español/inglés.
COMMODITY_CATALOG = [
    ("GC=F", "Oro (futuros)", ["oro", "gold"]),
    ("SI=F", "Plata (futuros)", ["plata", "silver"]),
    ("HG=F", "Cobre (futuros)", ["cobre", "copper"]),
    ("PL=F", "Platino (futuros)", ["platino", "platinum"]),
    ("PA=F", "Paladio (futuros)", ["paladio", "palladium"]),
    ("ALI=F", "Aluminio (futuros)", ["aluminio", "aluminum"]),
    ("CL=F", "Petróleo WTI (futuros)", ["petroleo", "petróleo", "crudo", "oil", "wti"]),
    ("BZ=F", "Petróleo Brent (futuros)", ["petroleo", "petróleo", "crudo", "brent"]),
    ("NG=F", "Gas natural (futuros)", ["gas", "natural gas"]),
    ("ZW=F", "Trigo (futuros)", ["trigo", "wheat"]),
    ("ZC=F", "Maíz (futuros)", ["maiz", "maíz", "corn"]),
    ("ZS=F", "Soja (futuros)", ["soja", "soybean"]),
    ("KC=F", "Café (futuros)", ["cafe", "café", "coffee"]),
    ("SB=F", "Azúcar (futuros)", ["azucar", "azúcar", "sugar"]),
    ("CC=F", "Cacao (futuros)", ["cacao", "cocoa"]),
    ("CT=F", "Algodón (futuros)", ["algodon", "algodón", "cotton"]),
    ("LE=F", "Ganado vacuno (futuros)", ["ganado", "vacuno", "cattle"]),
]
SPOT_NAMES = {sym: name for sym, name, _ in SPOT_CATALOG}
CATALOG_NAMES = {**SPOT_NAMES, **{sym: name for sym, name, _ in COMMODITY_CATALOG}}
# Futuro más cercano en Yahoo: referencia para variación, rango 52 sem.,
# horario de mercado y gráfico (cotiza prácticamente pegado al spot).
SPOT_FUTURES = {"XAUUSD": "GC=F", "XAGUSD": "SI=F", "XPTUSD": "PL=F", "XPDUSD": "PA=F"}


def is_spot(ticker: str) -> bool:
    return bool(SPOT_PATTERN.match(ticker.upper()))


# Relación spot/futuro por metal, recalibrada contra gold-api cada 10 min.
# Entre calibraciones el precio spot se mueve tick a tick con el futuro.
_spot_ratio: dict[str, tuple[float, float]] = {}
SPOT_RATIO_TTL = 600


def _fetch_spot_quote(ticker: str) -> dict | None:
    future = get_quote(SPOT_FUTURES[ticker], max_age=5)
    calib = _spot_ratio.get(ticker)
    if not calib or time.time() - calib[0] > SPOT_RATIO_TTL:
        try:
            resp = httpx.get(f"https://api.gold-api.com/price/{ticker[:3]}", timeout=10)
            spot = float(resp.json()["price"])
            if future and future["price"]:
                _spot_ratio[ticker] = (time.time(), spot / future["price"])
            elif calib is None:
                # Sin futuro no hay tick en vivo, pero al menos damos el spot puro.
                weekday_open = datetime.now(timezone.utc).weekday() < 5
                return {
                    "ticker": ticker, "price": spot, "prev_close": None, "change_pct": 0.0,
                    "currency": "USD", "year_high": None, "year_low": None,
                    "market_state": "open" if weekday_open else "closed",
                }
        except Exception as exc:
            log.warning("gold-api: sin calibración de %s: %s", ticker, exc)
        calib = _spot_ratio.get(ticker)
    if not future:
        return None
    ratio = calib[1] if calib else 1.0
    price = future["price"] * ratio
    change_pct = future["change_pct"]
    return {
        "ticker": ticker,
        "price": round(price, 4),
        "prev_close": round(price / (1 + change_pct / 100), 4) if change_pct else None,
        "change_pct": change_pct,
        "currency": "USD",
        "year_high": round(future["year_high"] * ratio, 4) if future.get("year_high") else None,
        "year_low": round(future["year_low"] * ratio, 4) if future.get("year_low") else None,
        "market_state": future["market_state"],
    }


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
    quote = _fetch_spot_quote(ticker) if is_spot(ticker) else _fetch_quote(ticker)
    if quote:
        _cache[ticker] = (time.time(), quote)
        return quote
    # Si la fuente falla, mejor una cotización algo vieja que ninguna.
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
    ticker = ticker.upper()
    if ticker in CATALOG_NAMES:
        return CATALOG_NAMES[ticker]
    if is_spot(ticker):
        return f"{ticker[:3]}/{ticker[3:]} spot"
    try:
        info = yf.Ticker(ticker).info
        return info.get("shortName") or info.get("longName") or ticker
    except Exception:
        return ticker


def _catalog_matches(query: str) -> list[dict]:
    """Metales spot y commodities del catálogo que encajan con lo escrito."""
    q_upper, q_lower = query.upper(), query.lower()

    def matches(sym: str, keywords: list[str]) -> bool:
        return sym.startswith(q_upper) or any(kw.startswith(q_lower) for kw in keywords)

    results = [
        {"symbol": sym, "name": name, "exchange": "Spot", "type": "Metal"}
        for sym, name, keywords in SPOT_CATALOG
        if matches(sym, keywords)
    ]
    results += [
        {"symbol": sym, "name": name, "exchange": "COMEX/NYMEX", "type": "Futuros"}
        for sym, name, keywords in COMMODITY_CATALOG
        if matches(sym, keywords)
    ]
    return results


def search_symbols(query: str, limit: int = 8) -> list[dict]:
    """Sugerencias de tickers: metales spot + búsqueda de Yahoo Finance."""
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
    results = _catalog_matches(query)
    seen = {r["symbol"] for r in results}
    for item in raw:
        symbol = item.get("symbol")
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        results.append(
            {
                "symbol": symbol,
                "name": item.get("shortname") or item.get("longname") or "",
                "exchange": item.get("exchDisp") or item.get("exchange") or "",
                "type": item.get("typeDisp") or item.get("quoteType") or "",
            }
        )
    # Pares de divisas escritos completos (EURUSD → EURUSD=X de Yahoo).
    if re.fullmatch(r"[A-Za-z]{6}", query) and not is_spot(query.upper()):
        fx_symbol = query.upper() + "=X"
        if fx_symbol not in seen and get_quote(fx_symbol):
            results.insert(
                0,
                {
                    "symbol": fx_symbol,
                    "name": f"{query[:3].upper()}/{query[3:].upper()}",
                    "exchange": "FX",
                    "type": "Divisa",
                },
            )
    return results[:limit]


VALID_RANGES = {"1mo": "1d", "3mo": "1d", "6mo": "1d", "1y": "1d", "5y": "1wk", "max": "1mo"}


def get_history(ticker: str, period: str = "6mo") -> list[dict]:
    """Serie histórica de cierres para el gráfico: [{date, close}, ...]."""
    if period not in VALID_RANGES:
        period = "6mo"
    ticker = ticker.upper()
    if is_spot(ticker):
        # El gráfico del spot usa el futuro más cercano (misma forma y nivel).
        return get_history(SPOT_FUTURES[ticker], period)
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
