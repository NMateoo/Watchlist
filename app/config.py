"""Configuración de la aplicación vía variables de entorno (.env en local)."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# Base de datos: SQLite en local; en Render se puede apuntar a Postgres.
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'watchlist.db'}")
# Render entrega URLs "postgres://..."; SQLAlchemy necesita el driver explícito.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Cada cuántos minutos se comprueban precios y alertas.
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "10"))

# Hora local del resumen diario (HH:MM) y zona horaria.
DAILY_SUMMARY_TIME = os.getenv("DAILY_SUMMARY_TIME", "22:10")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Madrid")

# Umbral por defecto (%) para avisar de cambios bruscos; editable en Ajustes.
DEFAULT_MOVE_THRESHOLD = float(os.getenv("MOVE_ALERT_THRESHOLD", "5"))
