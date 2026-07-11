"""Migraciones de esquema: versionado en settings, idempotencia y BDs antiguas."""
from sqlalchemy import inspect, text

from app.database import SCHEMA_VERSION, engine, get_setting, init_db


def test_init_db_versiona_y_es_idempotente(session):
    init_db()
    init_db()  # segunda pasada: no debe fallar ni re-aplicar nada
    assert get_setting(session, "schema_version", "") == str(SCHEMA_VERSION)


def test_migra_una_bd_antigua_sin_version(session):
    # Simular una BD v6 (anterior a posición y alertas recurrentes) sin
    # schema_version guardada, como las de antes de este cambio.
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE stocks DROP COLUMN quantity"))
        conn.execute(text("ALTER TABLE stocks DROP COLUMN buy_price"))
        conn.execute(text("ALTER TABLE alerts DROP COLUMN repeat"))
        conn.execute(text("DELETE FROM settings"))

    init_db()

    columns = {c["name"] for c in inspect(engine).get_columns("stocks")}
    assert {"quantity", "buy_price"} <= columns
    assert get_setting(session, "schema_version", "") == str(SCHEMA_VERSION)
