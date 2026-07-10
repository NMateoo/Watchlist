"""Aplicación web: dashboard de la watchlist y gestión de alertas."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app import alerts as alerts_mod
from app import bot, config, prices, scheduler, telegram
from app.database import (
    Alert,
    BotUser,
    SessionLocal,
    Stock,
    Watchlist,
    WatchlistMember,
    ensure_admin,
    get_check_interval,
    get_move_threshold,
    get_refresh_seconds,
    get_summary_time,
    init_db,
    set_setting,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    ensure_admin()
    scheduler.start()
    bot.start()
    yield
    bot.stop()
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
def index(request: Request, list_id: int | None = Query(None, alias="list")):
    with SessionLocal() as session:
        watchlists = session.scalars(select(Watchlist).order_by(Watchlist.id)).all()
        if not watchlists:
            admin = session.scalar(select(BotUser).where(BotUser.role == "admin"))
            default = Watchlist(name="Mi lista", owner_id=admin.id if admin else None)
            session.add(default)
            session.commit()
            watchlists = [default]
        active = next((w for w in watchlists if w.id == list_id), watchlists[0])
        stocks = session.scalars(
            select(Stock).where(Stock.watchlist_id == active.id).order_by(Stock.ticker)
        ).all()
        _ = [s.alerts for s in stocks]  # cargar relación antes de cerrar sesión
        members = [
            {"id": m.user_id, "name": m.user.name, "can_edit": m.can_edit}
            for m in active.memberships
        ]
        member_ids = {m["id"] for m in members}
        available_users = [
            u for u in session.scalars(select(BotUser).where(BotUser.role == "user"))
            if u.id not in member_ids
        ]
        has_users = bool(members or available_users)
        refresh_seconds = get_refresh_seconds(session)
    quotes = prices.get_quotes([s.ticker for s in stocks])
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "stocks": stocks,
            "quotes": quotes,
            "watchlists": watchlists,
            "active_list": active,
            "members": members,
            "available_users": available_users,
            "has_users": has_users,
            "telegram_ok": telegram.is_configured(),
            "refresh_seconds": refresh_seconds,
        },
    )


@app.post("/stocks/add")
def add_stock(ticker: str = Form(...), watchlist_id: int = Form(...)):
    ticker = ticker.strip().upper()
    home = f"/?list={watchlist_id}"
    if not ticker:
        return redirect(home, err="Escribe un ticker.")
    with SessionLocal() as session:
        if not session.get(Watchlist, watchlist_id):
            return redirect("/", err="Esa lista ya no existe.")
        exists = session.scalar(
            select(Stock).where(Stock.ticker == ticker, Stock.watchlist_id == watchlist_id)
        )
        if exists:
            return redirect(home, err=f"{ticker} ya está en esta lista.")
        quote_data = prices.get_quote(ticker)
        if not quote_data:
            return redirect(
                home,
                err=f"No encuentro '{ticker}' en Yahoo Finance. "
                "Recuerda los sufijos: SAN.MC (Madrid), BTC-USD (cripto), GC=F (futuros).",
            )
        session.add(
            Stock(
                ticker=ticker,
                watchlist_id=watchlist_id,
                name=prices.lookup_name(ticker),
                currency=quote_data["currency"],
            )
        )
        session.commit()
    return redirect(home, msg=f"{ticker} añadido a la lista.")


@app.post("/api/stocks/{stock_id}/delete")
def api_delete_stock(stock_id: int):
    with SessionLocal() as session:
        stock = session.get(Stock, stock_id)
        if not stock:
            return JSONResponse({"ok": False, "error": "Valor no encontrado."}, status_code=404)
        session.delete(stock)
        session.commit()
    return {"ok": True}


# ------------------------------------------------------------------ listas


@app.post("/lists/add")
def add_list(name: str = Form(...)):
    name = name.strip()[:60]
    if not name:
        return redirect("/", err="La lista necesita un nombre.")
    with SessionLocal() as session:
        if session.scalar(select(Watchlist).where(Watchlist.name == name)):
            return redirect("/", err=f"Ya existe una lista llamada '{name}'.")
        admin = session.scalar(select(BotUser).where(BotUser.role == "admin"))
        wl = Watchlist(name=name, owner_id=admin.id if admin else None)
        session.add(wl)
        session.commit()
        return redirect(f"/?list={wl.id}", msg=f"Lista '{name}' creada.")


@app.post("/lists/{list_id}/delete")
def delete_list(list_id: int):
    with SessionLocal() as session:
        wl = session.get(Watchlist, list_id)
        if wl:
            session.delete(wl)  # membresías y valores caen en cascada
            session.commit()
    return redirect("/", msg="Lista eliminada.")


@app.post("/lists/{list_id}/members/add")
def add_member(list_id: int, user_id: int = Form(...)):
    with SessionLocal() as session:
        wl = session.get(Watchlist, list_id)
        user = session.get(BotUser, user_id)
        if not wl or not user or user.role != "user":
            return redirect("/", err="Lista o usuario no válidos.")
        if not any(m.user_id == user.id for m in wl.memberships):
            wl.memberships.append(WatchlistMember(user=user, can_edit=True))
            session.commit()
            telegram.send_message(
                f"📬 Te han compartido la lista «{wl.name}». Escribe /menu para verla.",
                chat_id=user.chat_id,
            )
        name = user.name
    scheduler.reschedule()
    return redirect(f"/?list={list_id}", msg=f"Lista compartida con {name}.")


@app.post("/lists/{list_id}/members/{user_id}/remove")
def remove_member(list_id: int, user_id: int):
    with SessionLocal() as session:
        member = session.get(WatchlistMember, (list_id, user_id))
        if member:
            session.delete(member)
            session.commit()
    scheduler.reschedule()
    return redirect(f"/?list={list_id}", msg="Usuario quitado de la lista.")


@app.post("/lists/{list_id}/members/{user_id}/toggle-edit")
def toggle_member_edit(list_id: int, user_id: int):
    with SessionLocal() as session:
        member = session.get(WatchlistMember, (list_id, user_id))
        if not member:
            return redirect(f"/?list={list_id}", err="Ese usuario no está en la lista.")
        member.can_edit = not member.can_edit
        session.commit()
        mode = "✏️ puedes editarla" if member.can_edit else "👁 es de solo lectura para ti"
        telegram.send_message(
            f"El administrador ha cambiado tu permiso en «{member.watchlist.name}»: {mode}.",
            chat_id=member.user.chat_id,
        )
        label = "puede editar" if member.can_edit else "solo lectura"
        name = member.user.name
    return redirect(f"/?list={list_id}", msg=f"{name}: {label}.")


# ---------------------------------------------------------------- usuarios


@app.get("/users")
def users_page(request: Request):
    with SessionLocal() as session:
        users = session.scalars(select(BotUser).order_by(BotUser.created_at)).all()
        data = [
            {
                "id": u.id,
                "name": u.name,
                "chat_id": u.chat_id,
                "role": u.role,
                "lists": ", ".join(wl.name for wl in u.shared_lists),
            }
            for u in users
        ]
    return templates.TemplateResponse(
        request, "users.html", {"users": data, "telegram_ok": telegram.is_configured()}
    )


@app.post("/users/{user_id}/approve")
def approve_user(user_id: int):
    with SessionLocal() as session:
        user = session.get(BotUser, user_id)
        if not user or user.role != "pending":
            return redirect("/users", err="Ese usuario no está pendiente.")
        user.role = "user"
        session.commit()
        telegram.send_message(
            "✅ ¡Acceso concedido! Escribe /menu para empezar.", chat_id=user.chat_id
        )
        name = user.name
    scheduler.reschedule()
    return redirect("/users", msg=f"{name} aprobado.")


@app.post("/users/{user_id}/delete")
def delete_user(user_id: int):
    with SessionLocal() as session:
        user = session.get(BotUser, user_id)
        if not user or user.role == "admin":
            return redirect("/users", err="No se puede eliminar ese usuario.")
        admin = session.scalar(select(BotUser).where(BotUser.role == "admin"))
        for wl in user.watchlists:
            wl.owner_id = admin.id if admin else None
        session.delete(user)  # sus membresías caen en cascada
        session.commit()
    scheduler.reschedule()
    return redirect("/users", msg="Usuario eliminado.")


# ------------------------------------------------------------- ficha valor


def _alerts_json(session, stock_id: int) -> list[dict]:
    stock = session.get(Stock, stock_id)
    return [
        {
            "id": a.id,
            "kind": a.kind,
            "threshold": a.threshold,
            "active": a.active,
            "triggered_at": a.triggered_at.strftime("%d/%m %H:%M") if a.triggered_at else None,
        }
        for a in sorted(stock.alerts, key=lambda a: a.created_at, reverse=True)
    ]


@app.get("/stocks/{stock_id}")
def stock_detail(request: Request, stock_id: int):
    with SessionLocal() as session:
        stock = session.get(Stock, stock_id)
        if not stock:
            return redirect("/", err="Ese valor no está en tus listas.")
        _ = stock.watchlist  # cargar relación antes de cerrar sesión
        alerts_json = _alerts_json(session, stock.id)
        refresh_seconds = get_refresh_seconds(session)
    quote = prices.get_quote(stock.ticker)
    return templates.TemplateResponse(
        request,
        "stock.html",
        {
            "stock": stock,
            "quote": quote,
            "alerts_json": alerts_json,
            "telegram_ok": telegram.is_configured(),
            "refresh_seconds": refresh_seconds,
        },
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
            return redirect(f"/stocks/{stock.id}", err="Precio objetivo no válido.")
        session.commit()
        return redirect(f"/stocks/{stock.id}", msg="Notas guardadas.")


@app.get("/api/history/{ticker}")
def api_history(ticker: str, period: str = "6mo"):
    return JSONResponse(prices.get_history(ticker, period))


@app.get("/api/quotes")
def api_quotes():
    """Cotizaciones frescas de toda la watchlist, para el refresco en vivo."""
    with SessionLocal() as session:
        tickers = list(session.scalars(select(Stock.ticker)))
        refresh = get_refresh_seconds(session)
    return prices.get_quotes(tickers, max_age=max(3, refresh - 1))


@app.get("/api/quote/{ticker}")
def api_quote(ticker: str):
    with SessionLocal() as session:
        refresh = get_refresh_seconds(session)
    quote = prices.get_quote(ticker.upper(), max_age=max(3, refresh - 1))
    return quote or JSONResponse({"error": "sin datos"}, status_code=404)


@app.get("/api/search")
def api_search(q: str = ""):
    return prices.search_symbols(q)


@app.get("/api/news/{ticker}")
def api_news(ticker: str):
    return prices.get_news(ticker, limit=6)


# ----------------------------------------------------------------- alertas


@app.post("/api/alerts")
def api_add_alert(stock_id: int = Form(...), kind: str = Form(...), threshold: str = Form(...)):
    if kind not in ("above", "below"):
        return JSONResponse({"ok": False, "error": "Tipo de alerta no válido."}, status_code=400)
    with SessionLocal() as session:
        stock = session.get(Stock, stock_id)
        if not stock:
            return JSONResponse({"ok": False, "error": "Valor no encontrado."}, status_code=404)
        try:
            value = float(threshold.replace(",", "."))
        except ValueError:
            return JSONResponse({"ok": False, "error": "Umbral no válido."}, status_code=400)
        session.add(Alert(stock_id=stock.id, kind=kind, threshold=value))
        session.commit()
        return {"ok": True, "alerts": _alerts_json(session, stock.id)}


@app.post("/api/alerts/{alert_id}/delete")
def api_delete_alert(alert_id: int):
    with SessionLocal() as session:
        alert = session.get(Alert, alert_id)
        if not alert:
            return JSONResponse({"ok": False, "error": "Alerta no encontrada."}, status_code=404)
        stock_id = alert.stock_id
        session.delete(alert)
        session.commit()
        return {"ok": True, "alerts": _alerts_json(session, stock_id)}


@app.post("/api/alerts/{alert_id}/rearm")
def api_rearm_alert(alert_id: int):
    with SessionLocal() as session:
        alert = session.get(Alert, alert_id)
        if not alert:
            return JSONResponse({"ok": False, "error": "Alerta no encontrada."}, status_code=404)
        alert.active = True
        alert.triggered_at = None
        session.commit()
        return {"ok": True, "alerts": _alerts_json(session, alert.stock_id)}


# ----------------------------------------------------------------- ajustes


@app.get("/settings")
def settings_page(request: Request):
    with SessionLocal() as session:
        move_threshold = get_move_threshold(session)
        refresh_seconds = get_refresh_seconds(session)
        check_interval = get_check_interval(session)
        summary_time = get_summary_time(session)
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
            "refresh_seconds": refresh_seconds,
            "check_interval": check_interval,
            "summary_time": summary_time,
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


@app.post("/settings/refresh")
def save_refresh_seconds(refresh_seconds: str = Form(...)):
    try:
        value = int(refresh_seconds)
        if value < 5:
            raise ValueError
    except ValueError:
        return redirect("/settings", err="El intervalo debe ser un número entero de 5 o más segundos.")
    with SessionLocal() as session:
        set_setting(session, "ui_refresh_seconds", str(value))
        session.commit()
    return redirect("/settings", msg=f"La web actualizará precios cada {value} s.")


@app.post("/settings/test-telegram")
def test_telegram():
    if not telegram.is_configured():
        return redirect("/settings", err="Configura TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID en .env.")
    ok = telegram.send_message("✅ ¡Hola! Tu watchlist está conectada a Telegram.")
    if ok:
        return redirect("/settings", msg="Mensaje de prueba enviado. Mira tu Telegram.")
    return redirect("/settings", err="No se pudo enviar. Revisa el token y el chat_id.")


# UptimeRobot usa HEAD; hay que aceptarlo además de GET.
# "v" permite comprobar qué versión hay desplegada; "jobs" diagnostica el scheduler.
@app.api_route("/health", methods=["GET", "HEAD"])
def health():
    jobs = []
    if scheduler.scheduler.running:
        jobs = [
            {"id": j.id, "next": j.next_run_time.strftime("%d %H:%M:%S") if j.next_run_time else None}
            for j in scheduler.scheduler.get_jobs()
        ]
    return {"status": "ok", "v": 11, "scheduler": scheduler.scheduler.running, "jobs": jobs}
