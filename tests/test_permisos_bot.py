"""Permisos por lista tal y como los aplica el bot."""
from app import bot
from app.database import BotUser, Watchlist, WatchlistMember


def test_permisos_de_lista(session):
    admin = BotUser(chat_id="1", name="Admin", role="admin")
    creador = BotUser(chat_id="2", name="Creador", role="user")
    lector = BotUser(chat_id="3", name="Lector", role="user")
    ajeno = BotUser(chat_id="4", name="Ajeno", role="user")
    wl = Watchlist(name="Lista", owner=creador)
    wl.memberships.append(WatchlistMember(user=creador, can_edit=True))
    wl.memberships.append(WatchlistMember(user=lector, can_edit=False))
    session.add_all([admin, creador, lector, ajeno, wl])
    session.commit()

    ctx_admin = {"uid": admin.id, "role": "admin"}
    ctx_creador = {"uid": creador.id, "role": "user"}
    ctx_lector = {"uid": lector.id, "role": "user"}
    ctx_ajeno = {"uid": ajeno.id, "role": "user"}

    # el admin y el creador editan; el lector solo ve; el ajeno ni ve
    assert bot._can_edit_list(ctx_admin, wl)
    assert bot._can_edit_list(ctx_creador, wl)
    assert bot._can_view_list(ctx_lector, wl)
    assert not bot._can_edit_list(ctx_lector, wl)
    assert not bot._can_view_list(ctx_ajeno, wl)

    # borrar/renombrar la lista entera: solo el admin o quien la creó
    assert bot._can_delete_list(ctx_admin, wl)
    assert bot._can_delete_list(ctx_creador, wl)
    assert not bot._can_delete_list(ctx_lector, wl)


def test_admin_edita_los_resumenes_de_otro_usuario(session, monkeypatch):
    admin = BotUser(chat_id="1", name="Admin", role="admin")
    ana = BotUser(chat_id="2", name="Ana", role="user")
    session.add_all([admin, ana])
    session.commit()
    monkeypatch.setattr(bot, "_send", lambda *a, **k: {})
    monkeypatch.setattr(bot, "_show", lambda *a, **k: None)
    monkeypatch.setattr(bot.scheduler, "reschedule", lambda: None)
    ctx = {"uid": admin.id, "chat": "1", "role": "admin", "name": "Admin"}

    bot._pending["1"] = {"action": "summary_interval", "target_uid": ana.id}
    bot._handle_pending(ctx, "45")
    bot._pending["1"] = {"action": "summary_time", "target_uid": ana.id}
    bot._handle_pending(ctx, "9:5")

    session.expire_all()
    assert session.get(BotUser, ana.id).summary_interval == 45
    assert session.get(BotUser, ana.id).summary_time == "09:05"


def test_sin_target_el_usuario_edita_sus_propios_resumenes(session, monkeypatch):
    admin = BotUser(chat_id="1", name="Admin", role="admin")
    session.add(admin)
    session.commit()
    monkeypatch.setattr(bot, "_send", lambda *a, **k: {})
    monkeypatch.setattr(bot, "_show", lambda *a, **k: None)
    monkeypatch.setattr(bot.scheduler, "reschedule", lambda: None)
    ctx = {"uid": admin.id, "chat": "1", "role": "admin", "name": "Admin"}

    bot._pending["1"] = {"action": "summary_interval"}
    bot._handle_pending(ctx, "0")

    session.expire_all()
    assert session.get(BotUser, admin.id).summary_interval == 0


def test_weekend_quiet_activado_por_defecto_y_alternable(session, monkeypatch):
    admin = BotUser(chat_id="1", name="Admin", role="admin")
    session.add(admin)
    session.commit()
    assert admin.weekend_quiet is True  # activado por defecto

    monkeypatch.setattr(bot, "_show", lambda *a, **k: None)
    ctx = {"uid": admin.id, "chat": "1", "role": "admin", "name": "Admin"}

    bot._cb_weekend_quiet_toggle(ctx, [], None)
    session.expire_all()
    assert session.get(BotUser, admin.id).weekend_quiet is False

    bot._cb_weekend_quiet_toggle(ctx, [], None)
    session.expire_all()
    assert session.get(BotUser, admin.id).weekend_quiet is True


def test_admin_puede_alternar_el_weekend_quiet_de_otro_usuario(session, monkeypatch):
    admin = BotUser(chat_id="1", name="Admin", role="admin")
    ana = BotUser(chat_id="2", name="Ana", role="user")
    session.add_all([admin, ana])
    session.commit()

    monkeypatch.setattr(bot, "_show", lambda *a, **k: None)
    ctx = {"uid": admin.id, "chat": "1", "role": "admin", "name": "Admin"}

    bot._cb_user_weekend_quiet_toggle(ctx, [str(ana.id)], None)
    session.expire_all()
    assert session.get(BotUser, ana.id).weekend_quiet is False
