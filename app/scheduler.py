"""Tareas programadas: comprobación de alertas y resumen diario."""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app import alerts, config
from app.database import get_check_interval, get_summary_time, session_scope

log = logging.getLogger(__name__)

scheduler = BackgroundScheduler(timezone=config.TIMEZONE)


def _current_settings() -> tuple[int, int, int]:
    with session_scope() as session:
        interval = get_check_interval(session)
        hour, minute = get_summary_time(session).split(":")
    return interval, int(hour), int(minute)


def start() -> None:
    interval, hour, minute = _current_settings()
    scheduler.add_job(
        alerts.check_alerts,
        "interval",
        minutes=interval,
        id="check_alerts",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(alerts.send_daily_summary, "cron", hour=hour, minute=minute, id="daily_summary")
    scheduler.start()
    log.info(
        "Scheduler iniciado: alertas cada %d min, resumen a las %02d:%02d (%s)",
        interval, hour, minute, config.TIMEZONE,
    )


def reschedule() -> None:
    """Aplica en caliente los ajustes guardados en la base de datos."""
    if not scheduler.running:
        return
    interval, hour, minute = _current_settings()
    scheduler.reschedule_job("check_alerts", trigger="interval", minutes=interval)
    scheduler.reschedule_job("daily_summary", trigger="cron", hour=hour, minute=minute)
    log.info("Scheduler actualizado: alertas cada %d min, resumen a las %02d:%02d", interval, hour, minute)


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
