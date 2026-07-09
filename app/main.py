"""Aplicación web: dashboard de la watchlist y gestión de alertas."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app import alerts as alerts_mod
from app import config, prices, scheduler, telegram
from app.database import (
    Alert,
    SessionLocal,
    Stock,
    get_move_threshold,
    init_db,
    set_setting,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(title="Watchlist", lifespan=lifespan)

BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.filters["price"] = alerts_mod.fmt_price
templates.env.filters["pct"] = alerts_mod.fmt_pct


def redirect(url: str, msg: str = "", err: str = "") -> RedirectResponse:
    if msg:
        url += ("&" if "?" in url else "?") + "msg=" + quote(msg)
    if err:
        url += ("&" if "?" in url else "?") + "err=" + quote(err)
    return RedirectResponse(url, status_code=303)


# ---------------------------------------------------------------- dashboard


@app.get("/")
def index(request: Request):
    with SessionLocal() as session:
        stocks = session.scalars(select(Stock).order_by(Stock.ticker)).all()
        _ = [s.alerts for s in stocks]  # cargar relación antes de cerrar sesión
    quotes = prices.get_quotes([s.ticker for s in stocks])
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "stocks": stocks,
            "quotes": quotes,
            "telegram_ok": telegram.is_configured(),
        },
    )


@app.post("/stocks/add")
def add_stock(ticker: str = Form(...)):
    ticker = ticker.strip().upper()
    if not ticker:
        return redirect("/", err="Escribe un ticker.")
    with SessionLocal() as session:
        exists = session.scalar(select(Stock).where(Stock.ticker == ticker))
        if exists:
            return redirect("/", err=f"{ticker} ya está en tu watchlist.")
        quote_data = prices.get_quote(ticker)
        if not quote_data:
            return redirect(
                "/",
                err=f"No encuentro '{ticker}' en Yahoo Finance. "
                "Recuerda los sufijos: SAN.MC (Madrid), BTC-USD (cripto).",
            )
        session.add(
            Stock(
                ticker=ticker,
                name=prices.lookup_name(ticker),
                currency=quote_data["currency"],
            )
        )
        session.commit()
    return redirect("/", msg=f"{ticker} añadido a la watchlist.")


@app.post("/stocks/{stock_id}/delete")
def delete_stock(stock_id: int):
    with SessionLocal() as session:
        stock = session.get(Stock, stock_id)
        if stock:
            session.delete(stock)
            session.commit()
    return redirect("/", msg="Eliminado de la watchlist.")


# ------------------------------------------------------------- ficha valor


@app.get("/stocks/{ticker}")
def stock_detail(request: Request, ticker: str):
    ticker = ticker.upper()
    with SessionLocal() as session:
        stock = session.scalar(select(Stock).where(Stock.ticker == ticker))
        if not stock:
            return redirect("/", err=f"{ticker} no está en tu watchlist.")
        _ = stock.alerts
    quote = prices.get_quote(ticker)
    return templates.TemplateResponse(
        request,
        "stock.html",
        {"stock": stock, "quote": quote, "telegram_ok": telegram.is_configured()},
    )


@app.post("/stocks/{stock_id}/notes")
def save_notes(stock_id: int, notes: str = Form(""), target_price: str = Form("")):
    with SessionLocal() as session:
        stock = session.get(Stock, stock_id)
        if not stock:
            return redirect("/", err="Valor no encontrado.")
        stock.notes = notes.strip()
        try:
            stock.target_price = float(target_price.replace(",", ".")) if target_price.strip() else None
        except ValueError:
            return redirect(f"/stocks/{stock.ticker}", err="Precio objetivo no válido.")
        session.commit()
        return redirect(f"/stocks/{stock.ticker}", msg="Notas guardadas.")


@app.get("/api/history/{ticker}")
def api_history(ticker: str, period: str = "6mo"):
    return JSONResponse(prices.get_history(ticker, period))


# ----------------------------------------------------------------- alertas


@app.post("/alerts/add")
def add_alert(stock_id: int = Form(...), kind: str = Form(...), threshold: str = Form(...)):
    if kind not in ("above", "below"):
        return redirect("/", err="Tipo de alerta no válido.")
    with SessionLocal() as session:
        stock = session.get(Stock, stock_id)
        if not stock:
            return redirect("/", err="Valor no encontrado.")
        try:
            value = float(threshold.replace(",", "."))
        except ValueError:
            return redirect(f"/stocks/{stock.ticker}", err="Umbral no válido.")
        session.add(Alert(stock_id=stock.id, kind=kind, threshold=value))
        session.commit()
        return redirect(f"/stocks/{stock.ticker}", msg="Alerta creada.")


@app.post("/alerts/{alert_id}/delete")
def delete_alert(alert_id: int):
    with SessionLocal() as session:
        alert = session.get(Alert, alert_id)
        ticker = alert.stock.ticker if alert else None
        if alert:
            session.delete(alert)
            session.commit()
    return redirect(f"/stocks/{ticker}" if ticker else "/", msg="Alerta eliminada.")


@app.post("/alerts/{alert_id}/rearm")
def rearm_alert(alert_id: int):
    with SessionLocal() as session:
        alert = session.get(Alert, alert_id)
        if not alert:
            return redirect("/", err="Alerta no encontrada.")
        alert.active = True
        alert.triggered_at = None
        ticker = alert.stock.ticker
        session.commit()
    return redirect(f"/stocks/{ticker}", msg="Alerta reactivada.")


# ----------------------------------------------------------------- ajustes


@app.get("/settings")
def settings_page(request: Request):
    with SessionLocal() as session:
        move_threshold = get_move_threshold(session)
    chat_id_hint = None
    if config.TELEGRAM_BOT_TOKEN and not config.TELEGRAM_CHAT_ID:
        chat_id_hint = telegram.get_chat_id_hint()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "telegram_ok": telegram.is_configured(),
            "has_token": bool(config.TELEGRAM_BOT_TOKEN),
            "chat_id_hint": chat_id_hint,
            "move_threshold": move_threshold,
            "check_interval": config.CHECK_INTERVAL_MINUTES,
            "summary_time": config.DAILY_SUMMARY_TIME,
            "timezone": config.TIMEZONE,
        },
    )


@app.post("/settings/move-threshold")
def save_move_threshold(move_threshold: str = Form(...)):
    try:
        value = float(move_threshold.replace(",", "."))
        if value <= 0:
            raise ValueError
    except ValueError:
        return redirect("/settings", err="El umbral debe ser un número positivo.")
    with SessionLocal() as session:
        set_setting(session, "move_threshold", str(value))
        session.commit()
    return redirect("/settings", msg=f"Umbral de cambio brusco: {value}%.")


@app.post("/settings/test-telegram")
def test_telegram():
    if not telegram.is_configured():
        return redirect("/settings", err="Configura TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID en .env.")
    ok = telegram.send_message("✅ ¡Hola! Tu watchlist está conectada a Telegram.")
    if ok:
        return redirect("/settings", msg="Mensaje de prueba enviado. Mira tu Telegram.")
    return redirect("/settings", err="No se pudo enviar. Revisa el token y el chat_id.")


@app.get("/health")
def health():
    return {"status": "ok"}
