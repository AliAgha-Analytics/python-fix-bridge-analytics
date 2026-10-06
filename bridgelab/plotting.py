"""Shared chart style: one fixed colour per LP (categorical slots 1-3, validated as a set),
recessive axes, and a helper to shade incident windows."""
import matplotlib.pyplot as plt
import pandas as pd

LP_COLORS = {"LP_A": "#2a78d6", "LP_B": "#eb6834", "LP_C": "#1baf7a"}
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
NEUTRAL = "#8a8985"
INK = "#0b0b0b"
INK_2 = "#52514e"
ALERT = "#e34948"           # reserved for incidents / problems only


def setup():
    plt.rcParams.update({
        "figure.figsize": (11, 4), "figure.dpi": 110, "axes.spines.top": False, "axes.spines.right": False,
        "axes.edgecolor": "#c9c8c3", "axes.labelcolor": INK_2, "xtick.color": INK_2, "ytick.color": INK_2,
        "axes.grid": True, "grid.color": "#ecebe7", "grid.linewidth": 0.8, "lines.linewidth": 1.6,
        "axes.titlesize": 12, "axes.titleweight": "bold", "axes.titlecolor": INK, "legend.frameon": False,
        "font.size": 10,
    })


def shade(ax, start, end, label=None, color=ALERT, alpha=0.10):
    ax.axvspan(pd.Timestamp(start), pd.Timestamp(end), color=color, alpha=alpha, lw=0, label=label)


def time_axis(ax, fmt="%H:%M"):
    import matplotlib.dates as mdates
    ax.xaxis.set_major_formatter(mdates.DateFormatter(fmt))
