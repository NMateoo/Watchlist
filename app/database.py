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


class WatchlistMember(Base):
    """Qué usuarios comparten cada lista y con qué permiso.
    El admin ve y edita todo sin necesidad de fila."""

    __tablename__ = "watchlist_members"

    watchlist_id: Mapped[int] = mapped_column(
        ForeignKey("watchlists.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("bot_users.id", ondelete="CASCADE"), primary_key=True
    )
    can_edit: Mapped[bool] = mapped_column(Boolean, default=True)

    watchlist: Mapped["Watchlist"] = relationship(back_populates="memberships")
    user: Mapped["BotUser"] = relationship(back_populates="memberships")


class BotUser(Base):
    """Usuario del bot de Telegram. El primero (chat de TELEGRAM_CHAT_ID) es admin."""

    __tablename__ = "bot_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(80), default="")
    role: Mapped[str] = mapped_column(String(10), default="pending")  # admin | user | pending
    # Preferencias de resúmenes; NULL → usar el valor global de settings.
    summary_interval: Mapped[int | None] = mapped_column(nullable=True)
    summary_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    watchlists: Mapped[list["Watchlist"]] = relationship(back_populates="owner")
    memberships: Mapped[list[WatchlistMember]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def shared_lists(self) -> list["Watchlist"]:
        return [m.watchlist for m in self.memberships]


class Watchlist(Base):
    __tablename__ = "watchlists"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("bot_users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    owner: Mapped[BotUser | None] = relationship(back_populates="watchlists")
    memberships: Mapped[list[WatchlistMember]] = relationship(
        back_populates="watchlist", cascade="all, delete-orphan"
    )
    stocks: Mapped[list["Stock"]] = relationship(
        back_populates="watchlist", cascade="all, delete-orphan"
    )

    @property
    def members(self) -> list[BotUser]:
        return [m.user for m in self.memberships]


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
    chat_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pct: Mapped[float] = mapped_column(Float)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(String(200))


def init_db() -> None:
    inspector = inspect(engine)
    # Migración v1 → v2 (sin listas): añadir watchlist_id a stocks.
    needs_v2 = inspector.has_table("stocks") and "watchlist_id" not in [
        c["name"] for c in inspector.get_columns("stocks")
    ]
    # Migración v2 → v3 (multi-usuario): owner en listas y chat en avisos.
    needs_owner = inspector.has_table("watchlists") and "owner_id" not in [
        c["name"] for c in inspector.get_columns("watchlists")
    ]
    needs_chat = inspector.has_table("move_notices") and "chat_id" not in [
        c["name"] for c in inspector.get_columns("move_notices")
    ]
    # Migración v3 → v4: preferencias de resúmenes por usuario.
    needs_prefs = inspector.has_table("bot_users") and "summary_interval" not in [
        c["name"] for c in inspector.get_columns("bot_users")
    ]
    # Migración v4 → v5: listas compartidas con varios usuarios.
    needs_members = inspector.has_table("watchlists") and not inspector.has_table("watchlist_members")
    # Migración v5 → v6: permiso de edición por miembro.
    needs_can_edit = inspector.has_table("watchlist_members") and "can_edit" not in [
        c["name"] for c in inspector.get_columns("watchlist_members")
    ]
    Base.metadata.create_all(engine)
    if needs_v2:
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
    if needs_owner:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE watchlists ADD COLUMN owner_id INTEGER REFERENCES bot_users(id)"))
    if needs_chat:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE move_notices ADD COLUMN chat_id VARCHAR(32)"))
    if needs_prefs:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE bot_users ADD COLUMN summary_interval INTEGER"))
            conn.execute(text("ALTER TABLE bot_users ADD COLUMN summary_time VARCHAR(5)"))
    if needs_members:
        # Las asignaciones antiguas (owner) pasan a ser membresías.
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO watchlist_members (watchlist_id, user_id) "
                "SELECT w.id, w.owner_id FROM watchlists w "
                "JOIN bot_users u ON u.id = w.owner_id WHERE u.role != 'admin'"
            ))
    if needs_can_edit:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE watchlist_members ADD COLUMN can_edit BOOLEAN NOT NULL DEFAULT TRUE"
            ))


def ensure_admin() -> None:
    """Crea el usuario admin (TELEGRAM_CHAT_ID) y adopta las listas sin dueño."""
    from app import config

    if not config.TELEGRAM_CHAT_ID:
        return
    from sqlalchemy import select, update

    with session_scope() as session:
        admin = session.scalar(select(BotUser).where(BotUser.role == "admin"))
        if not admin:
            admin = BotUser(chat_id=str(config.TELEGRAM_CHAT_ID), name="Admin", role="admin")
            session.add(admin)
            session.flush()
        session.execute(
            update(Watchlist).where(Watchlist.owner_id.is_(None)).values(owner_id=admin.id)
        )


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


def get_check_interval(session) -> int:
    """Cada cuántos minutos se comprueban las alertas (1–720)."""
    try:
        value = int(float(get_setting(session, "check_interval_minutes", str(config.CHECK_INTERVAL_MINUTES))))
    except ValueError:
        value = config.CHECK_INTERVAL_MINUTES
    return min(max(value, 1), 720)


def get_summary_interval(session) -> int:
    """Minutos entre resúmenes automáticos (0 = desactivado)."""
    try:
        value = int(float(get_setting(session, "summary_interval_minutes", "30")))
    except ValueError:
        value = 30
    return min(max(value, 0), 1440)


def normalize_time(value: str | None) -> str | None:
    """'9:5' → '09:05'; None si no es una hora válida."""
    parts = (value or "").strip().split(":")
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        hour, minute = int(parts[0]), int(parts[1])
        if 0 <= hour < 24 and 0 <= minute < 60:
            return f"{hour:02d}:{minute:02d}"
    return None


def get_summary_time(session) -> str:
    """Hora HH:MM del resumen diario (valor global por defecto)."""
    value = get_setting(session, "daily_summary_time", config.DAILY_SUMMARY_TIME)
    return normalize_time(value) or "22:10"


def get_user_summary_prefs(session, user) -> tuple[int, str]:
    """(minutos entre resúmenes automáticos, hora del diario) de un usuario.
    Si no ha configurado nada, usa los valores globales."""
    interval = user.summary_interval
    if interval is None:
        interval = get_summary_interval(session)
    interval = min(max(int(interval), 0), 1440)
    stime = normalize_time(user.summary_time) or get_summary_time(session)
    return interval, stime
