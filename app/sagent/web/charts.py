"""Geometry for server-rendered SVG charts (the CSP forbids inline JS/styles,
so charts are plain SVG with classes for color and <title> for hover)."""
from __future__ import annotations

import math

SERIES = ("claude", "codex")  # fixed order → fixed categorical slots 1, 2


def nice_ticks(max_value: float, count: int = 4) -> list[float]:
    if max_value <= 0:
        return [0, 1]
    raw = max_value / count
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    top = step * math.ceil(max_value / step)
    n = int(round(top / step))
    return [round(step * i, 10) for i in range(n + 1)]


def compact(v: float) -> str:
    v = float(v or 0)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            s = f"{v / div:.1f}".rstrip("0").rstrip(".")
            return f"{s}{unit}"
    return f"{v:,.0f}" if v >= 1 or v == 0 else f"{v:.2f}"


def stacked_columns(days: list[dict], width: int = 760, height: int = 230) -> dict:
    """Daily tokens stacked by provider."""
    left, right, top, bottom = 52, 12, 12, 30
    pw, ph = width - left - right, height - top - bottom
    totals = [sum(d.get(s, 0) for s in SERIES) for d in days]
    ticks = nice_ticks(max(totals, default=0))
    ymax = ticks[-1] or 1
    n = max(len(days), 1)
    slot = pw / n
    bar_w = max(4.0, min(28.0, slot * 0.62))
    cols = []
    label_every = 1 if n <= 10 else (2 if n <= 20 else 7)
    for i, d in enumerate(days):
        x = left + slot * i + (slot - bar_w) / 2
        y = top + ph
        segs = []
        for s in SERIES:
            v = d.get(s, 0)
            if v <= 0:
                continue
            h = ph * v / ymax
            gap = 2 if segs else 0  # 2px surface gap between stacked segments
            segs.append({"series": s, "x": round(x, 1), "y": round(y - h, 1), "w": round(bar_w, 1),
                         "h": round(max(h - gap, 1), 1), "value": v})
            y -= h
        cols.append({
            "day": d["day"], "segments": segs, "total": totals[i], "cost": d.get("cost", 0),
            "hit_x": round(left + slot * i, 1), "hit_w": round(slot, 1),
            "label": d["day"][5:] if i % label_every == 0 or i == n - 1 else "",
            "label_x": round(x + bar_w / 2, 1), "values": {s: d.get(s, 0) for s in SERIES},
        })
    yticks = [{"y": round(top + ph - ph * t / ymax, 1), "label": compact(t)} for t in ticks]
    return {"width": width, "height": height, "left": left, "right": width - right, "top": top,
            "base": top + ph, "cols": cols, "yticks": yticks,
            "series_present": [s for s in SERIES if any(d.get(s, 0) for d in days)]}


def hbars(rows: list[dict], key: str = "tokens", width: int = 320, row_h: int = 26) -> dict:
    """Horizontal bars, single series; value labels at the tip."""
    label_w, value_w = 100, 48
    pw = width - label_w - value_w - 8
    vmax = max((r[key] for r in rows), default=0) or 1
    out = []
    for i, r in enumerate(rows):
        w = pw * r[key] / vmax
        out.append({**r, "y": i * row_h, "w": round(max(w, 2 if r[key] else 0), 1),
                    "bar_x": label_w, "value_x": round(label_w + max(w, 2) + 6, 1),
                    "text_y": i * row_h + row_h / 2 + 4})
    return {"width": width, "height": max(len(rows), 1) * row_h, "rows": out, "label_w": label_w,
            "bar_h": row_h - 8}
