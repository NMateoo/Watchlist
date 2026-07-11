"""Respuestas de texto del bot (_handle_pending): guardado, validación y permisos."""
import pytest
from sqlalchemy import select

from app import bot
from app.database import Alert, BotUser, Stock, Watchlist, WatchlistMember, get_setting


@pytest.fixture()
def silent_bot(monkeypatch):
    """Silencia los envíos a Telegram y las vistas (que piden precios a Yahoo)."""
    sent = []
    monkeypatch.setattr(bot, "_send", lambda text, *a, **k: sent.append(text) or {})
    for view in ("_main_menu", "_lists_view", "_list_view", "_stock_view",
                 "_alerts_view", "_settings_view", "_users_view", "_user_detail_view"):
        monkeypatch.setattr(bot, view, lambda *a, **k: None)
    monkeypatch.setattr(bot.scheduler, "reschedule", lambda: None)
    return sent


def _datos(session):
    """Admin, un usuario de solo lectura y una lista con un valor."""
    admin = BotUser(chat_id="1", name="Admin", role="admin")
    lector = BotUser(chat_id="2", name="Lector", role="user")
    wl = Watchlist(name="Lista", owner=admin)
    wl.memberships.append(WatchlistMember(user=lector, can_edit=False))
    stock = Stock(ticker="AAPL", name="Apple", watchlist=wl)
    session.add_all([admin, lector, wl, stock])
    session.commit()
    ctx_admin = {"uid": admin.id, "chat": "1", "role": "admin", "name": "Admin"}
    ctx_lector = {"uid": lector.id, "chat": "2", "role": "user", "name": "Lector"}
    return ctx_admin, ctx_lector, stock.id


def _responder(ctx, pending: dict, text: str) -> None:
    bot._pending[ctx["chat"]] = pending
    assert bot._handle_pending(ctx, text) is True


def test_sin_pendiente_devuelve_false(session, silent_bot):
    ctx, _, _ = _datos(session)
    assert bot._handle_pending(ctx, "hola") is False


def test_posicion_se_guarda_y_se_quita(session, silent_bot):
    ctx, _, stock_id = _datos(session)
    _responder(ctx, {"action": "position", "stock_id": stock_id}, "10 120,50")
    session.expire_all()
    stock = session.get(Stock, stock_id)
    assert (stock.quantity, stock.buy_price) == (10.0, 120.5)

    _responder(ctx, {"action": "position", "stock_id": stock_id}, "quitar")
    session.expire_all()
    stock = session.get(Stock, stock_id)
    assert (stock.quantity, stock.buy_price) == (None, None)


def test_posicion_invalida_no_cambia_nada(session, silent_bot):
    ctx, _, stock_id = _datos(session)
    _responder(ctx, {"action": "position", "stock_id": stock_id}, "muchas a buen precio")
    session.expire_all()
    assert session.get(Stock, stock_id).quantity is None
    assert any("No lo entendí" in msg for msg in silent_bot)


def test_objetivo_y_notas(session, silent_bot):
    ctx, _, stock_id = _datos(session)
    _responder(ctx, {"action": "target", "stock_id": stock_id}, "150,5")
    _responder(ctx, {"action": "notes", "stock_id": stock_id}, "vigilar resultados")
    session.expire_all()
    stock = session.get(Stock, stock_id)
    assert stock.target_price == 150.5
    assert stock.notes == "vigilar resultados"

    _responder(ctx, {"action": "target", "stock_id": stock_id}, "quitar")
    _responder(ctx, {"action": "notes", "stock_id": stock_id}, "borrar")
    session.expire_all()
    stock = session.get(Stock, stock_id)
    assert stock.target_price is None
    assert stock.notes == ""


def test_alerta_por_porcentaje_usa_el_precio_actual(session, silent_bot, monkeypatch):
    ctx, _, stock_id = _datos(session)
    monkeypatch.setattr(
        "app.prices.get_quote", lambda t: {"price": 100.0, "currency": "USD", "change_pct": 0.0}
    )
    _responder(ctx, {"action": "alert_price", "stock_id": stock_id, "kind": "above", "repeat": True}, "5%")
    alert = session.scalar(select(Alert))
    assert (alert.kind, alert.threshold, alert.repeat) == ("above", 105.0, True)


def test_lector_no_puede_crear_alertas(session, silent_bot, monkeypatch):
    _, ctx_lector, stock_id = _datos(session)
    monkeypatch.setattr(
        "app.prices.get_quote", lambda t: {"price": 100.0, "currency": "USD", "change_pct": 0.0}
    )
    _responder(ctx_lector, {"action": "alert_price", "stock_id": stock_id, "kind": "above"}, "5%")
    assert session.scalar(select(Alert)) is None


def test_ajustes_globales_con_validacion(session, silent_bot):
    ctx, _, _ = _datos(session)
    _responder(ctx, {"action": "move"}, "7,5")
    assert get_setting(session, "move_threshold", "") == "7.5"

    _responder(ctx, {"action": "interval"}, "0")  # fuera de rango: no guarda
    assert get_setting(session, "check_interval_minutes", "") == ""
    _responder(ctx, {"action": "interval"}, "15")
    assert get_setting(session, "check_interval_minutes", "") == "15"
