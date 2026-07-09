"""Copia los datos de watchlist.db (SQLite local) a la base de datos DATABASE_URL.

Uso (desde la raíz del proyecto, con DATABASE_URL de Neon ya puesto en .env):
    .venv\\Scripts\\python scripts\\copy_sqlite_to_db.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app import config
from app.database import Alert, Base, MoveNotice, Setting, Stock, Watchlist

SQLITE_PATH = Path(__file__).resolve().parent.parent / "watchlist.db"


def main() -> None:
    if config.DATABASE_URL.startswith("sqlite"):
        sys.exit(
            "DATABASE_URL sigue apuntando a SQLite. Pon la URL de Neon en el .env "
            "(DATABASE_URL=postgresql://...) y vuelve a ejecutar este script."
        )
    if not SQLITE_PATH.exists():
        sys.exit(f"No existe {SQLITE_PATH}; no hay nada que copiar.")

    source = sessionmaker(bind=create_engine(f"sqlite:///{SQLITE_PATH}"))()
    target_engine = create_engine(config.DATABASE_URL, pool_pre_ping=True)
    Base.metadata.create_all(target_engine)
    target = sessionmaker(bind=target_engine)()

    if target.scalar(select(Watchlist).limit(1)):
        sys.exit("La base de datos de destino ya tiene datos; no copio nada para no duplicar.")

    copied = {}
    for model in (Watchlist, Stock, Alert, MoveNotice, Setting):
        rows = source.scalars(select(model)).all()
        for row in rows:
            data = {c.name: getattr(row, c.name) for c in model.__table__.columns}
            target.add(model(**data))
        copied[model.__tablename__] = len(rows)
    target.commit()

    # Los ids se insertaron a mano: avanzar las secuencias de Postgres.
    with target_engine.begin() as conn:
        for table in ("watchlists", "stocks", "alerts", "move_notices"):
            conn.execute(text(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"(SELECT COALESCE(MAX(id), 1) FROM {table}))"
            ))

    print("Copiado a", config.DATABASE_URL.split("@")[-1].split("?")[0])
    for table, count in copied.items():
        print(f"  {table}: {count} filas")


if __name__ == "__main__":
    main()
