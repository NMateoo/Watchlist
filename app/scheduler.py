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
            if scheduler.get_job(periodic_id):
                scheduler.reschedule_job(periodic_id, trigger="interval", minutes=interval)
            else:
                scheduler.add_job(
                    alerts.send_summary_to, "interval", minutes=interval,
                    id=periodic_id, args=[chat], max_instances=1, coalesce=True,
                )
        hour, minute = stime.split(":")
        desired.add(daily_id)
        if scheduler.get_job(daily_id):
            scheduler.reschedule_job(daily_id, trigger="cron", hour=int(hour), minute=int(minute))
        else:
            scheduler.add_job(
                alerts.send_summary_to, "cron", hour=int(hour), minute=int(minute),
                id=daily_id, args=[chat],
            )
        log.info("Resúmenes de uid %d: cada %s, diario a las %s", uid,
                 f"{interval} min" if interval else "— (off)", stime)

    # quitar jobs de usuarios eliminados o con el periódico desactivado
    for job in scheduler.get_jobs():
        if (job.id.startswith("psum_") or job.id.startswith("dsum_")) and job.id not in desired:
            scheduler.remove_job(job.id)


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
