"""Tareas programadas: comprobación de alertas y resúmenes por usuario."""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from app import alerts, config
from app.database import (
    BotUser,
    get_check_interval,
    get_user_summary_prefs,
    session_scope,
)

log = logging.getLogger(__name__)

scheduler = BackgroundScheduler(timezone=config.TIMEZONE)

# Configuración vigente de cada job de resumen. Solo se (re)programa un job si
# su spec cambió: reprogramar un intervalo reinicia su cuenta atrás, y si se
# hace más a menudo que el propio intervalo, el job no se dispararía jamás.
_job_specs: dict[str, tuple] = {}


def _ensure_job(job_id: str, spec: tuple, **add_kwargs) -> None:
    if _job_specs.get(job_id) == spec and scheduler.get_job(job_id):
        return
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    scheduler.add_job(id=job_id, **add_kwargs)
    _job_specs[job_id] = spec


def _sync_user_summaries() -> None:
    """Crea/actualiza un job periódico y uno diario por cada usuario del bot."""
    with session_scope() as session:
        users = session.scalars(
            select(BotUser).where(BotUser.role.in_(("admin", "user")))
        ).all()
        plans = [(u.id, u.chat_id, *get_user_summary_prefs(session, u)) for u in users]

    desired = set()
    for uid, chat, interval, stime in plans:
        periodic_id, daily_id = f"psum_{uid}", f"dsum_{uid}"
        if interval > 0:
            desired.add(periodic_id)
            _ensure_job(
                periodic_id, ("interval", interval, chat),
                func=alerts.send_summary_to, trigger="interval", minutes=interval,
                args=[chat], max_instances=1, coalesce=True,
            )
        hour, minute = stime.split(":")
        desired.add(daily_id)
        _ensure_job(
            daily_id, ("cron", stime, chat),
            func=alerts.send_summary_to, trigger="cron", hour=int(hour), minute=int(minute),
            args=[chat],
        )

    # quitar jobs de usuarios eliminados o con el periódico desactivado
    for job in scheduler.get_jobs():
        if (job.id.startswith("psum_") or job.id.startswith("dsum_")) and job.id not in desired:
            scheduler.remove_job(job.id)
            _job_specs.pop(job.id, None)


def start() -> None:
    if not config.SCHEDULER_ENABLED:
        log.info("SCHEDULER_ENABLED=0: esta instancia no comprueba alertas ni manda resúmenes")
        return
    with session_scope() as session:
        interval = get_check_interval(session)
    scheduler.add_job(
        alerts.check_alerts,
        "interval",
        minutes=interval,
        id="check_alerts",
        max_instances=1,
        coalesce=True,
    )
    # Re-sincronizar cada 15 min: recoge altas/bajas de usuarios o cambios de
    # preferencias hechos desde otra instancia (p. ej. la web en local).
    scheduler.add_job(_sync_user_summaries, "interval", minutes=15, id="sync_summaries")
    scheduler.start()
    _sync_user_summaries()
    log.info("Scheduler iniciado: alertas cada %d min (%s)", interval, config.TIMEZONE)


def reschedule() -> None:
    """Aplica en caliente los ajustes guardados en la base de datos."""
    if not scheduler.running:
        return
    with session_scope() as session:
        interval = get_check_interval(session)
    scheduler.reschedule_job("check_alerts", trigger="interval", minutes=interval)
    _sync_user_summaries()


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
