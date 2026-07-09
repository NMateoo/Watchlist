"""Tareas programadas: comprobación de alertas y resumen diario."""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app import alerts, config

log = logging.getLogger(__name__)

scheduler = BackgroundScheduler(timezone=config.TIMEZONE)


def start() -> None:
    hour, minute = config.DAILY_SUMMARY_TIME.split(":")
    scheduler.add_job(
        alerts.check_alerts,
        "interval",
        minutes=config.CHECK_INTERVAL_MINUTES,
        id="check_alerts",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        alerts.send_daily_summary,
        "cron",
        hour=int(hour),
        minute=int(minute),
        id="daily_summary",
    )
    scheduler.start()
    log.info(
        "Scheduler iniciado: alertas cada %d min, resumen a las %s (%s)",
        config.CHECK_INTERVAL_MINUTES,
        config.DAILY_SUMMARY_TIME,
        config.TIMEZONE,
    )


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
