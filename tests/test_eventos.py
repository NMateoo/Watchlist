"""Avisos de resultados/dividendos: destinatarios, ventana de fechas y dedupe."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app import events
from app.database import BotUser, EventNotice, Stock, Watchlist, WatchlistMember


def _hoy():
    return datetime.now(ZoneInfo("Europe/Madrid")).date()


def _lista_compartida(session):
    admin = BotUser(chat_id="admin-chat", name="Admin", role="admin")
    ana = BotUser(chat_id="ana-chat", name="Ana", role="user")
    wl = Watchlist(name="Lista", owner=admin)
    wl.memberships.append(WatchlistMember(user=ana))
    stock = Stock(ticker="AAPL", name="Apple Inc.", watchlist=wl)
    session.add_all([admin, ana, wl, stock])
    session.commit()


def test_avisa_a_todos_una_sola_vez(session, monkeypatch):
    _lista_compartida(session)
    manana = _hoy() + timedelta(days=1)
    monkeypatch.setattr("app.prices.get_corporate_events", lambda t: {"earnings": manana})
    sent = []
    monkeypatch.setattr(
        "app.telegram.send_message", lambda text, chat_id=None: sent.append((chat_id, text)) or True
    )

    events.check_events()
    assert {chat for chat, _ in sent} == {None, "ana-chat"}  # None = admin
    assert all("mañana" in text for _, text in sent)

    sent.clear()
    events.check_events()  # segunda pasada: ya avisados, no repite
    assert sent == []
    assert len(session.scalars(select(EventNotice)).all()) == 2


def test_evento_lejano_o_pasado_no_avisa(session, monkeypatch):
    _lista_compartida(session)
    monkeypatch.setattr(
        "app.prices.get_corporate_events",
        lambda t: {"earnings": _hoy() + timedelta(days=10), "ex_dividend": _hoy() - timedelta(days=1)},
    )
    sent = []
    monkeypatch.setattr(
        "app.telegram.send_message", lambda text, chat_id=None: sent.append(text) or True
    )
    events.check_events()
    assert sent == []


def test_si_el_envio_falla_se_reintenta_en_la_siguiente_pasada(session, monkeypatch):
    _lista_compartida(session)
    monkeypatch.setattr("app.prices.get_corporate_events", lambda t: {"dividend": _hoy()})
    monkeypatch.setattr("app.telegram.send_message", lambda text, chat_id=None: False)
    events.check_events()
    assert session.scalars(select(EventNotice)).all() == []  # nada marcado como enviado

    sent = []
    monkeypatch.setattr(
        "app.telegram.send_message", lambda text, chat_id=None: sent.append(text) or True
    )
    events.check_events()
    assert len(sent) == 2
    assert all("paga dividendo hoy" in text for text in sent)
