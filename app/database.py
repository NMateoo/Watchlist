"""Modelos y sesión de base de datos (SQLAlchemy 2.0)."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

from app import config

connect_args = {"check_same_thread": False} if config.DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(config.DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Watchlist(Base):
    __tablename__ = "watchlists"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    stocks: Mapped[list["Stock"]] = relationship(
        back_populates="watchlist", cascade="all, delete-orphan"
    )


class Stock(Base):
    __tablename__ = "stocks"
    # Un mismo ticker puede estar en varias listas, pero no repetido en una.
    __table_args__ = (Index("ux_stocks_ticker_list", "ticker", "watchlist_id", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(20), index=True)
    watchlist_id: Mapped[int] = mapped_column(ForeignKey("watchlists.id"))
    name: Mapped[str] = mapped_column(String(120), default="")
    currency: Mapped[str] = mapped_column(String(10), default="USD")
    notes: Mapped[str] = mapped_column(Text, default="")
    target_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    watchlist: Mapped[Watchlist] = relationship(back_populates="stocks")
    alerts: Mapped[list["Alert"]] = relationship(
        back_populates="stock", cascade="all, delete-orphan"
    )


class Alert(Base):
    """Alerta de umbral: avisa cuando el precio cruza `threshold`."""

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"))
    kind: Mapped[str] = mapped_column(String(10))  # "above" | "below"
    threshold: Mapped[float] = mapped_column(Float)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    stock: Mapped[Stock] = relationship(back_populates="alerts")


class MoveNotice(Base):
    """Registro de avisos de cambio brusco ya enviados (1 por ticker y día)."""

    __tablename__ = "move_notices"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(20), index=True)
    day: Mapped[str] = mapped_column(String(10), index=True)  # YYYY-MM-DD local
    pct: Mapped[float] = mapped_column(Float)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(String(200))


def init_db() -> None:
    # Migración desde el esquema v1 (sin listas): añadir watchlist_id a stocks.
    inspector = inspect(engine)
    needs_migration = inspector.has_table("stocks") and "watchlist_id" not in [
        c["name"] for c in inspector.get_columns("stocks")
    ]
    Base.metadata.create_all(engine)
    if needs_migration:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO watchlists (id, name, created_at) VALUES (1, 'Mi lista', CURRENT_TIMESTAMP)"
            ))
            conn.execute(text("DROP INDEX IF EXISTS ix_stocks_ticker"))
            conn.execute(text("ALTER TABLE stocks ADD COLUMN watchlist_id INTEGER REFERENCES watchlists(id)"))
            conn.execute(text("UPDATE stocks SET watchlist_id = 1"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_stocks_ticker ON stocks (ticker)"))
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_stocks_ticker_list ON stocks (ticker, watchlist_id)"
            ))


@contextmanager
def session_scope():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_setting(session, key: str, default: str) -> str:
    row = session.get(Setting, key)
    return row.value if row else default


def set_setting(session, key: str, value: str) -> None:
    row = session.get(Setting, key)
    if row:
        row.value = value
    else:
        session.add(Setting(key=key, value=value))


def get_move_threshold(session) -> float:
    return float(get_setting(session, "move_threshold", str(config.DEFAULT_MOVE_THRESHOLD)))


def get_refresh_seconds(session) -> int:
    """Cada cuántos segundos refresca precios la web (mínimo 5)."""
    try:
        value = int(float(get_setting(session, "ui_refresh_seconds", "10")))
    except ValueError:
        value = 10
    return max(5, value)
