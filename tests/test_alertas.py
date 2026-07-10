"""Destinatarios de alertas, re-armado de recurrentes y purga de avisos."""
from sqlalchemy import select

from app.alerts import _check_threshold_alerts, _local_today, _purge_old_notices, _recipient_chats
from app.database import Alert, BotUser, MoveNotice, Stock, Watchlist, WatchlistMember, utcnow


def test_admin_recibe_aunque_la_lista_este_compartida(session):
    admin = BotUser(chat_id="admin-chat", name="Admin", role="admin")
    david = BotUser(chat_id="david-chat", name="David", role="user")
    wl = Watchlist(name="Compartida", owner=admin)
    wl.memberships.append(WatchlistMember(user=david))
    stock = Stock(ticker="AAPL", watchlist=wl)
    session.add_all([admin, david, wl, stock])
    session.commit()

    chats = _recipient_chats(stock)
    assert None in chats  # None = chat del admin
    assert "david-chat" in chats


def test_lista_sin_miembros_avisa_al_admin(session):
    wl = Watchlist(name="Propia")
    stock = Stock(ticker="MSFT", watchlist=wl)
    session.add_all([wl, stock])
    session.commit()
    assert _recipient_chats(stock) == [None]


def _alerta_disparada(session, repeat: bool) -> tuple[Stock, Alert]:
    wl = Watchlist(name="L")
    stock = Stock(ticker="AAPL", watchlist=wl)
    alert = Alert(stock=stock, kind="above", threshold=100.0, active=False,
                  repeat=repeat, triggered_at=utcnow())
    session.add_all([wl, stock, alert])
    session.commit()
    return stock, alert


def _quote(price: float) -> dict:
    return {"price": price, "currency": "USD", "change_pct": 0.0}


def test_alerta_recurrente_se_rearma_al_recruzar(session):
    stock, alert = _alerta_disparada(session, repeat=True)
    _check_threshold_alerts(session, stock, _quote(90.0))  # vuelve bajo el umbral
    assert alert.active is True
    assert alert.triggered_at is None


def test_alerta_recurrente_no_se_rearma_sin_recruzar(session):
    stock, alert = _alerta_disparada(session, repeat=True)
    _check_threshold_alerts(session, stock, _quote(110.0))  # sigue por encima
    assert alert.active is False


def test_alerta_normal_no_se_rearma(session):
    stock, alert = _alerta_disparada(session, repeat=False)
    _check_threshold_alerts(session, stock, _quote(90.0))
    assert alert.active is False


def test_purga_avisos_antiguos(session):
    hoy = _local_today()
    session.add_all([
        MoveNotice(ticker="AAPL", day="2020-01-01", pct=6.0),
        MoveNotice(ticker="AAPL", day=hoy, pct=6.0),
    ])
    session.commit()
    _purge_old_notices(session)
    session.commit()
    assert session.scalars(select(MoveNotice.day)).all() == [hoy]
