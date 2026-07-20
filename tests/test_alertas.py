"""Destinatarios de alertas, re-armado de recurrentes y purga de avisos."""
from datetime import datetime

from sqlalchemy import select

from app.alerts import (
    _check_threshold_alerts,
    _local_today,
    _purge_old_notices,
    is_weekend_quiet_hours,
    recipient_chats,
)
from app.database import (
    Alert,
    BotUser,
    MoveNotice,
    Stock,
    SummaryMute,
    Watchlist,
    WatchlistMember,
    utcnow,
)


def test_admin_recibe_aunque_la_lista_este_compartida(session):
    admin = BotUser(chat_id="admin-chat", name="Admin", role="admin")
    david = BotUser(chat_id="david-chat", name="David", role="user")
    wl = Watchlist(name="Compartida", owner=admin)
    wl.memberships.append(WatchlistMember(user=david))
    stock = Stock(ticker="AAPL", watchlist=wl)
    session.add_all([admin, david, wl, stock])
    session.commit()

    chats = recipient_chats(stock)
    assert None in chats  # None = chat del admin
    assert "david-chat" in chats


def test_lista_sin_miembros_avisa_al_admin(session):
    wl = Watchlist(name="Propia")
    stock = Stock(ticker="MSFT", watchlist=wl)
    session.add_all([wl, stock])
    session.commit()
    assert recipient_chats(stock) == [None]


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


def test_cambio_brusco_un_solo_mensaje_aunque_este_en_dos_listas(session, monkeypatch):
    from app import alerts

    david = BotUser(chat_id="david-chat", name="David", role="user")
    wl1, wl2 = Watchlist(name="Lista A"), Watchlist(name="Lista B")
    wl1.memberships.append(WatchlistMember(user=david))
    wl2.memberships.append(WatchlistMember(user=david))
    session.add_all([
        david, wl1, wl2,
        Stock(ticker="AAPL", watchlist=wl1),
        Stock(ticker="AAPL", watchlist=wl2),
    ])
    session.commit()

    enviados = []
    monkeypatch.setattr(
        alerts.telegram, "send_message",
        lambda text, chat_id=None: enviados.append(chat_id) or True,
    )
    monkeypatch.setattr(
        alerts.prices, "get_quotes",
        lambda tickers, max_age=60: {"AAPL": {"price": 100.0, "currency": "USD", "change_pct": 50.0}},
    )

    alerts.check_alerts()
    # un solo mensaje por chat (admin y David), no uno por lista
    assert enviados == [None, "david-chat"]

    enviados.clear()
    alerts.check_alerts()  # segunda pasada del día: ya avisado, no repite
    assert enviados == []


def _mock_quotes(monkeypatch, alerts):
    monkeypatch.setattr(
        alerts.prices, "get_quotes",
        lambda tickers, max_age=60: {
            t: {"price": 10.0, "currency": "USD", "change_pct": 1.0} for t in tickers
        },
    )


def test_lista_silenciada_no_sale_en_el_resumen(session, monkeypatch):
    from app import alerts

    admin = BotUser(chat_id="1", name="Admin", role="admin")
    visible = Watchlist(name="Visible")
    callada = Watchlist(name="Silenciada")
    session.add_all([
        admin, visible, callada,
        Stock(ticker="AAPL", watchlist=visible),
        Stock(ticker="MSFT", watchlist=callada),
    ])
    session.commit()
    session.add(SummaryMute(user_id=admin.id, watchlist_id=callada.id))
    session.commit()

    mensajes = []
    monkeypatch.setattr(
        alerts.telegram, "send_message",
        lambda text, chat_id=None: mensajes.append(text) or True,
    )
    _mock_quotes(monkeypatch, alerts)

    assert alerts.send_summary_to("1") is True
    texto = "\n".join(mensajes)
    assert "AAPL" in texto
    assert "MSFT" not in texto  # la lista silenciada no aparece


def test_con_todas_las_listas_silenciadas_no_hay_resumen(session, monkeypatch):
    from app import alerts

    admin = BotUser(chat_id="1", name="Admin", role="admin")
    wl = Watchlist(name="Única")
    session.add_all([admin, wl, Stock(ticker="AAPL", watchlist=wl)])
    session.commit()
    session.add(SummaryMute(user_id=admin.id, watchlist_id=wl.id))
    session.commit()

    _mock_quotes(monkeypatch, alerts)
    monkeypatch.setattr(alerts.telegram, "send_message", lambda *a, **k: True)
    assert alerts.send_summary_to("1") is False


def test_el_silencio_es_por_usuario(session, monkeypatch):
    """David silencia una lista; el admin la sigue recibiendo."""
    from app import alerts

    admin = BotUser(chat_id="1", name="Admin", role="admin")
    david = BotUser(chat_id="2", name="David", role="user")
    wl = Watchlist(name="Compartida")
    wl.memberships.append(WatchlistMember(user=david))
    session.add_all([admin, david, wl, Stock(ticker="AAPL", watchlist=wl)])
    session.commit()
    session.add(SummaryMute(user_id=david.id, watchlist_id=wl.id))
    session.commit()

    _mock_quotes(monkeypatch, alerts)
    monkeypatch.setattr(alerts.telegram, "send_message", lambda *a, **k: True)
    assert alerts.send_summary_to("2") is False  # David: silenciada
    assert alerts.send_summary_to("1") is True  # Admin: la sigue viendo


def test_toggle_de_lista_en_resumen_desde_el_bot(session, monkeypatch):
    from sqlalchemy import select as sa_select

    from app import bot

    admin = BotUser(chat_id="1", name="Admin", role="admin")
    wl = Watchlist(name="Lista")
    session.add_all([admin, wl])
    session.commit()
    monkeypatch.setattr(bot, "_show", lambda *a, **k: None)
    ctx = {"uid": admin.id, "chat": "1", "role": "admin", "name": "Admin"}

    bot._cb_summary_list_toggle(ctx, [str(wl.id)], None)
    session.expire_all()
    assert session.scalars(sa_select(SummaryMute)).one().watchlist_id == wl.id

    bot._cb_summary_list_toggle(ctx, [str(wl.id)], None)
    session.expire_all()
    assert session.scalars(sa_select(SummaryMute)).all() == []


def test_silencio_fin_de_semana_franja():
    # viernes: nunca silencio
    assert is_weekend_quiet_hours(datetime(2026, 7, 17, 23, 59)) is False
    # sábado: silencio a partir de las 08:00, no antes
    assert is_weekend_quiet_hours(datetime(2026, 7, 18, 7, 59)) is False
    assert is_weekend_quiet_hours(datetime(2026, 7, 18, 8, 0)) is True
    assert is_weekend_quiet_hours(datetime(2026, 7, 18, 23, 59)) is True
    # domingo: todo el día
    assert is_weekend_quiet_hours(datetime(2026, 7, 19, 0, 0)) is True
    assert is_weekend_quiet_hours(datetime(2026, 7, 19, 23, 59)) is True
    # lunes: silencio hasta las 04:00, no después
    assert is_weekend_quiet_hours(datetime(2026, 7, 20, 3, 59)) is True
    assert is_weekend_quiet_hours(datetime(2026, 7, 20, 4, 0)) is False
    # martes: nunca silencio
    assert is_weekend_quiet_hours(datetime(2026, 7, 21, 12, 0)) is False


def test_resumen_periodico_se_silencia_en_fin_de_semana(session, monkeypatch):
    from app import alerts

    admin = BotUser(chat_id="1", name="Admin", role="admin", weekend_quiet=True)
    wl = Watchlist(name="L")
    session.add_all([admin, wl, Stock(ticker="AAPL", watchlist=wl)])
    session.commit()

    monkeypatch.setattr(alerts, "_local_now", lambda: datetime(2026, 7, 19, 12, 0))  # domingo
    monkeypatch.setattr(alerts.telegram, "send_message", lambda *a, **k: True)

    assert alerts.send_periodic_summary("1") is False


def test_resumen_periodico_sin_silencio_activado_si_sale_en_findes(session, monkeypatch):
    from app import alerts

    admin = BotUser(chat_id="1", name="Admin", role="admin", weekend_quiet=False)
    wl = Watchlist(name="L")
    session.add_all([admin, wl, Stock(ticker="AAPL", watchlist=wl)])
    session.commit()

    monkeypatch.setattr(alerts, "_local_now", lambda: datetime(2026, 7, 19, 12, 0))  # domingo
    monkeypatch.setattr(
        alerts.prices, "get_quotes",
        lambda tickers, max_age=60: {"AAPL": {"price": 10.0, "currency": "USD", "change_pct": 1.0}},
    )
    monkeypatch.setattr(alerts.telegram, "send_message", lambda *a, **k: True)

    assert alerts.send_periodic_summary("1") is True


def test_resumen_periodico_entre_semana_no_se_silencia(session, monkeypatch):
    from app import alerts

    admin = BotUser(chat_id="1", name="Admin", role="admin", weekend_quiet=True)
    wl = Watchlist(name="L")
    session.add_all([admin, wl, Stock(ticker="AAPL", watchlist=wl)])
    session.commit()

    monkeypatch.setattr(alerts, "_local_now", lambda: datetime(2026, 7, 21, 12, 0))  # martes
    monkeypatch.setattr(
        alerts.prices, "get_quotes",
        lambda tickers, max_age=60: {"AAPL": {"price": 10.0, "currency": "USD", "change_pct": 1.0}},
    )
    monkeypatch.setattr(alerts.telegram, "send_message", lambda *a, **k: True)

    assert alerts.send_periodic_summary("1") is True


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
