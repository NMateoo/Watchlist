"""Destinatarios de alertas y purga de avisos de cambio brusco."""
from sqlalchemy import select

from app.alerts import _local_today, _purge_old_notices, _recipient_chats
from app.database import BotUser, MoveNotice, Stock, Watchlist, WatchlistMember


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
