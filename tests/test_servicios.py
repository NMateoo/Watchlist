"""Operaciones compartidas web/bot: listas, membresías y usuarios."""
from sqlalchemy import select

from app import services
from app.database import BotUser, Watchlist, WatchlistMember


def _admin_y_usuario(session):
    admin = BotUser(chat_id="1", name="Admin", role="admin")
    user = BotUser(chat_id="2", name="David", role="user")
    session.add_all([admin, user])
    session.commit()
    return admin, user


def test_lista_creada_por_usuario_lo_hace_miembro_editor(session):
    _, user = _admin_y_usuario(session)
    wl, error = services.create_list(session, "Mi lista", user)
    assert error is None
    assert wl.owner_id == user.id
    assert [(m.user_id, m.can_edit) for m in wl.memberships] == [(user.id, True)]


def test_lista_creada_por_admin_sin_membresia(session):
    admin, _ = _admin_y_usuario(session)
    wl, error = services.create_list(session, "Global", admin)
    assert error is None
    assert wl.memberships == []


def test_crear_lista_nombre_duplicado(session):
    admin, _ = _admin_y_usuario(session)
    services.create_list(session, "Repetida", admin)
    wl, error = services.create_list(session, "Repetida", admin)
    assert wl is None
    assert "Ya existe" in error


def test_renombrar_lista(session):
    admin, _ = _admin_y_usuario(session)
    wl, _ = services.create_list(session, "Vieja", admin)
    assert services.rename_list(session, wl, "Nueva") is None
    assert wl.name == "Nueva"


def test_renombrar_no_puede_chocar_con_otra(session):
    admin, _ = _admin_y_usuario(session)
    services.create_list(session, "Una", admin)
    wl, _ = services.create_list(session, "Otra", admin)
    assert "Ya existe" in services.rename_list(session, wl, "Una")
    # renombrarla a su propio nombre sí se permite
    assert services.rename_list(session, wl, "Otra") is None


def test_compartir_lista_no_duplica_miembros(session):
    admin, user = _admin_y_usuario(session)
    wl, _ = services.create_list(session, "Lista", admin)
    assert services.share_list(session, wl, user) is True
    assert services.share_list(session, wl, user) is False
    assert len(wl.memberships) == 1


def test_alternar_permiso_de_edicion(session):
    admin, user = _admin_y_usuario(session)
    wl, _ = services.create_list(session, "Lista", admin)
    services.share_list(session, wl, user)
    member = wl.memberships[0]
    assert services.toggle_member_edit(session, member) is False
    assert services.toggle_member_edit(session, member) is True


def test_parse_threshold_precio():
    assert services.parse_threshold("150.50", "above", None) == 150.5
    assert services.parse_threshold("150,50", "below", None) == 150.5
    assert services.parse_threshold("abc", "above", None) is None
    assert services.parse_threshold("-5", "above", None) is None
    assert services.parse_threshold("0", "above", None) is None


def test_parse_threshold_porcentaje():
    # above → +5% sobre el precio actual; below → −5%
    assert services.parse_threshold("5%", "above", 100.0) == 105.0
    assert services.parse_threshold("5%", "below", 100.0) == 95.0
    assert services.parse_threshold("2,5%", "below", 200.0) == 195.0
    assert services.parse_threshold("5%", "above", None) is None  # sin precio actual
    assert services.parse_threshold("-5%", "above", 100.0) is None
    assert services.parse_threshold("x%", "above", 100.0) is None


def test_eliminar_usuario_pasa_sus_listas_al_admin(session):
    admin, user = _admin_y_usuario(session)
    wl, _ = services.create_list(session, "De David", user)
    services.remove_user(session, user)
    assert wl.owner_id == admin.id
    assert session.scalars(select(WatchlistMember)).all() == []
    assert session.scalars(select(BotUser.name)).all() == ["Admin"]
