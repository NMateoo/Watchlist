"""Genera gráficos PNG de un valor para enviarlos por Telegram."""
from __future__ import annotations

import io
import logging
from datetime import datetime

import matplotlib

matplotlib.use("Agg")  # sin pantalla: solo renderizado a fichero
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

from app import prices

log = logging.getLogger(__name__)

PERIOD_LABELS = {
    "1mo": "1 mes", "3mo": "3 meses", "6mo": "6 meses",
    "1y": "1 año", "5y": "5 años", "max": "máximo",
}

BG = "#0f1420"
SURFACE = "#171e2e"
TEXT = "#e8ecf4"
MUTED = "#8b93a7"
UP = "#22c55e"
DOWN = "#ef4444"


def render_chart(ticker: str, period: str = "6mo") -> tuple[bytes, float] | None:
    """PNG del histórico de cierres y % de variación en el periodo."""
    if period not in PERIOD_LABELS:
        period = "6mo"
    data = prices.get_history(ticker, period)
    if len(data) < 2:
        return None
    dates = [datetime.strptime(d["date"], "%Y-%m-%d") for d in data]
    closes = [d["close"] for d in data]
    change = (closes[-1] / closes[0] - 1) * 100
    color = UP if change >= 0 else DOWN

    fig, ax = plt.subplots(figsize=(8, 4.2), dpi=150)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(SURFACE)
    ax.plot(dates, closes, color=color, linewidth=1.8)
    ax.fill_between(dates, closes, min(closes), color=color, alpha=0.12)

    ax.grid(color=MUTED, alpha=0.15, linewidth=0.6)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.tick_params(colors=MUTED, labelsize=8)
    locator = mdates.AutoDateLocator(maxticks=8)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))

    sign = "+" if change >= 0 else ""
    ax.set_title(
        f"{ticker}   ·   {PERIOD_LABELS[period]}   ·   {sign}{change:.2f}%",
        color=TEXT, fontsize=12, fontweight="bold", pad=12,
    )
    # marcar el último precio
    ax.scatter([dates[-1]], [closes[-1]], color=color, s=18, zorder=5)
    ax.annotate(
        f"{closes[-1]:,.2f}", (dates[-1], closes[-1]),
        textcoords="offset points", xytext=(6, 4), color=TEXT, fontsize=9,
    )

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    return buf.getvalue(), change
