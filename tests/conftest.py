"""Configuración común de los tests: BD SQLite temporal y Telegram apagado.

Las variables de entorno se fijan ANTES de importar nada de `app`, porque
config.py las lee al importarse (y load_dotenv no pisa las ya definidas).
"""
import os
import tempfile

_fd, _path = tempfile.mkstemp(prefix="watchlist-test-", suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _path.replace("\\", "/")
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = ""
os.environ["TIMEZONE"] = "Europe/Madrid"

import pytest  # noqa: E402


@pytest.fixture()
def session():
    """Sesión sobre una base de datos recién creada y vacía."""
    from app.database import Base, SessionLocal, engine

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as s:
        yield s
