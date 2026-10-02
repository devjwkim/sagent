"""Jinja filters for numbers, money and time."""
from __future__ import annotations


def tokens(n) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def usd(v) -> str:
    if v is None:
        return "-"
    return f"${float(v):.4f}" if float(v) < 1 else f"${float(v):,.2f}"


def duration(ms) -> str:
    if ms is None:
        return "-"
    s = int(ms) // 1000
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {sec}s"
    return f"{sec}s" if s else f"{int(ms)}ms"


def ts(value) -> str:
    """ISO8601 UTC → 'YYYY-MM-DD HH:MM:SS' (still UTC; the UI labels it)."""
    if not value:
        return "-"
    return str(value).replace("T", " ").split("+")[0][:19]


def register(app) -> None:
    for fn in (tokens, usd, duration, ts):
        app.jinja_env.filters[fn.__name__] = fn
