"""Server-side layout for loop graphs (rendered as inline SVG, no JS library).

Layered left→right layout: BFS from the start node assigns columns; edges that
point back to an earlier column are drawn as arcs underneath.
"""
from __future__ import annotations

from collections import deque

NODE_W, NODE_H = 164, 48
COL_W, ROW_H = 222, 84
PAD_X, PAD_Y = 20, 20


def layout(spec: dict, states: dict[str, dict] | None = None) -> dict:
    nodes = list(spec.get("nodes", {}))
    edges = [e for e in spec.get("edges", []) if e.get("from") in spec["nodes"] and e.get("to") in spec["nodes"]]
    start = spec.get("start") if spec.get("start") in spec.get("nodes", {}) else (nodes[0] if nodes else None)
    level: dict[str, int] = {}
    order: list[str] = []
    if start:
        level[start] = 0
        q = deque([start])
        while q:
            cur = q.popleft()
            order.append(cur)
            for e in edges:
                if e["from"] == cur and e["to"] not in level:
                    level[e["to"]] = level[cur] + 1
                    q.append(e["to"])
    max_level = max(level.values(), default=-1)
    for n in nodes:  # unreachable nodes go to a trailing column
        if n not in level:
            max_level += 1
            level[n] = max_level
            order.append(n)
    rows: dict[int, int] = {}
    pos: dict[str, tuple[int, int]] = {}
    for n in order:
        lv = level[n]
        idx = rows.get(lv, 0)
        rows[lv] = idx + 1
        pos[n] = (PAD_X + lv * COL_W, PAD_Y + idx * ROW_H)
    # positions saved by the visual editor win when every node has one
    ui = {n: (spec["nodes"][n] or {}).get("ui") for n in nodes}
    if nodes and all(isinstance(u, dict) and "x" in u and "y" in u for u in ui.values()):
        pos = {n: (int(ui[n]["x"]), int(ui[n]["y"])) for n in nodes}
    states = states or {}
    out_nodes = []
    for n in order:
        x, y = pos[n]
        node = spec["nodes"][n]
        st = states.get(n, {})
        out_nodes.append({
            "key": n, "x": x, "y": y, "w": NODE_W, "h": NODE_H,
            "type": node.get("type", "?"),
            "sub": node.get("suite") or node.get("provider") or node.get("role") or "",
            "status": (st.get("status") or "WAITING").lower(),
            "attempts": st.get("attempts", 0),
        })
    base_h = max((y for _, y in pos.values()), default=PAD_Y) + NODE_H + PAD_Y
    out_edges = []
    back = 0
    for e in edges:
        (x1, y1), (x2, y2) = pos[e["from"]], pos[e["to"]]
        when = e.get("when", "always")
        label = "" if when == "always" else when
        forward = x2 > x1 + NODE_W / 2
        if forward:
            sx, sy = x1 + NODE_W, y1 + NODE_H / 2
            tx, ty = x2, y2 + NODE_H / 2
            mx = (sx + tx) / 2
            path = f"M{sx},{sy} C{mx},{sy} {mx},{ty} {tx - 4},{ty}"
            lx, ly = mx, min(sy, ty) - 7
        else:
            back += 1
            sx, sy = x1 + NODE_W / 2 + 10, y1 + NODE_H
            tx, ty = x2 + NODE_W / 2 - 10, y2 + NODE_H
            depth = base_h - min(y1, y2) - NODE_H + 18 + back * 14
            path = f"M{sx},{sy} C{sx},{sy + depth} {tx},{ty + depth} {tx},{ty + 4}"
            lx, ly = (sx + tx) / 2, max(sy, ty) + depth * 0.75
        out_edges.append({"path": path, "label": label, "lx": lx, "ly": ly, "when": when,
                          "back": not forward})
    width = max((x for x, _ in pos.values()), default=PAD_X) + NODE_W + PAD_X
    height = base_h + (24 + back * 14 if back else 0)
    return {"nodes": out_nodes, "edges": out_edges, "width": width, "height": int(height)}
