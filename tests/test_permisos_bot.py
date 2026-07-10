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
