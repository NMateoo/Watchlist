"""Formateo de precios/porcentajes y utilidades de hora."""
from datetime import datetime

from app.alerts import fmt_pct, fmt_price, to_local
from app.database import normalize_time


def test_fmt_price_usd():
    assert fmt_price(1234.5, "USD") == "1.234,50 $"


def test_fmt_price_peniques():
    # "GBp" son peniques de Londres: no debe confundirse con libras (GBP).
    assert fmt_price(54.0, "GBp") == "54,00 p"


def test_fmt_price_sin_valor():
    assert fmt_price(None) == "—"


def test_fmt_pct():
    assert fmt_pct(3.456) == "+3,46%"
    assert fmt_pct(-2.1) == "-2,10%"


def test_normalize_time():
    assert normalize_time("9:5") == "09:05"
    assert normalize_time("22:10") == "22:10"
    assert normalize_time("25:00") is None
    assert normalize_time("mediodía") is None
    assert normalize_time(None) is None


def test_to_local_convierte_de_utc():
    # Las 12:00 UTC de un día de julio son las 14:00 en Madrid (CEST).
    assert to_local(datetime(2026, 7, 10, 12, 0)).strftime("%H:%M") == "14:00"
