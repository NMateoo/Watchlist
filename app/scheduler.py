"""Tareas programadas: comprobación de alertas y resúmenes."""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app import alerts, config
from app.database import (
    get_check_interval,
    get_summary_interval,
    get_summary_time,
    session_scope,
)

log = logging.getLogger(__name__)

scheduler = BackgroundScheduler(timezone=config.TIMEZONE)


def _current_settings() -> tuple[int, int, int, int]:
    with session_scope() as session:
        interval = get_check_interval(session)
        summary_interval = get_summary_interval(session)
        hour, minute = get_summary_time(session).split(":")
    return interval, summary_interval, int(hour), int(minute)


def _apply_periodic_summary(minutes: int) -> None:
    job = scheduler.get_job("periodic_summary")
    if minutes > 0:
        if job:
            scheduler.reschedule_job("periodic_summary", trigger="interval", minutes=minutes)
        else:
            scheduler.add_job(
                alerts.send_daily_summary,
                "interval",
                minutes=minutes,
                id="periodic_summary",
                max_instances=1,
                coalesce=True,
            )
    elif job:
        scheduler.remove_job("periodic_summary")


def start() -> None:
    if not config.SCHEDULER_ENABLED:
        log.info("SCHEDULER_ENABLED=0: esta instancia no comprueba alertas ni manda resúmenes")
        return
    interval, summary_interval, hour, minute = _current_settings()
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
    _apply_periodic_summary(summary_interval)
    log.info(
        "Scheduler iniciado: alertas cada %d min, resumen periódico cada %s, diario a las %02d:%02d (%s)",
        interval, f"{summary_interval} min" if summary_interval else "— (off)", hour, minute, config.TIMEZONE,
    )


def reschedule() -> None:
    """Aplica en caliente los ajustes guardados en la base de datos."""
    if not scheduler.running:
        return
    interval, summary_interval, hour, minute = _current_settings()
    scheduler.reschedule_job("check_alerts", trigger="interval", minutes=interval)
    scheduler.reschedule_job("daily_summary", trigger="cron", hour=hour, minute=minute)
    _apply_periodic_summary(summary_interval)
    log.info(
        "Scheduler actualizado: alertas cada %d min, resumen periódico cada %s, diario a las %02d:%02d",
        interval, f"{summary_interval} min" if summary_interval else "— (off)", hour, minute,
    )


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
