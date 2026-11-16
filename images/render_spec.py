#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""渲染 images/*_spec.json → 同名 .svg + .png(离线,纯 matplotlib)。

用法: python3 render_spec.py spec.json [spec2.json ...]
"""
import json
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Polygon
from matplotlib.lines import Line2D

plt.rcParams["font.sans-serif"] = ["Noto Sans CJK JP", "Noto Sans", "Droid Sans Fallback"]
plt.rcParams["axes.unicode_minus"] = False


# contrast 调色板:fill / edge / text
PAL = {
    "blue":   ("#dce8f7", "#2f5d9e", "#1a3c6e"),
    "green":  ("#d8efdc", "#2e7d32", "#1b5e20"),
    "red":    ("#fbdcda", "#b03027", "#7a1f19"),
    "orange": ("#fdeacc", "#c26a1c", "#8a4a10"),
    "purple": ("#e8def5", "#6a3ca0", "#4a2873"),
    "yellow": ("#fdf6d2", "#a8890a", "#6d5a04"),
    "teal":   ("#d6eef0", "#12787f", "#0a565c"),
    "gray":   ("#eeeeee", "#8a8a8a", "#555555"),
    "white":  ("#ffffff", "#8a8a8a", "#333333"),
}
EMPH_LW = 2.6
NORM_LW = 1.5


def rrect(ax, x, y, w, h, fc, ec, lw, r=0.7, z=2, ls="-"):
    p = FancyBboxPatch((x + r, y + r), w - 2 * r, h - 2 * r,
                       boxstyle=f"round,pad={r}", fc=fc, ec=ec, lw=lw,
                       linestyle=ls, zorder=z)
    ax.add_patch(p)


def node_box(ax, n):
    x, y, w, h = n["x"], n["y"], n["w"], n["h"]
    fc, ec, tc = PAL.get(n.get("color", "white"), PAL["white"])
    emph = n.get("emph", False)
    rrect(ax, x, y, w, h, fc, ec, EMPH_LW if emph else NORM_LW,
          r=0.9 if emph else 0.6, z=3 if emph else 2)
    cx = x + w / 2
    ts = n.get("ts", 9.2)
    ss = n.get("ss", 7.8)
    title = n.get("title", "")
    sub = n.get("sub")
    if sub:
        ax.text(cx, y + h * 0.62, title, ha="center", va="center",
                fontsize=ts, fontweight="bold", color=tc, zorder=5)
        ax.text(cx, y + h * 0.28, sub, ha="center", va="center",
                fontsize=ss, color=tc, zorder=5, linespacing=1.25)
    else:
        ax.text(cx, y + h / 2, title, ha="center", va="center",
                fontsize=ts, fontweight="bold", color=tc, zorder=5,
                linespacing=1.3)


def group_box(ax, g):
    x, y, w, h = g["x"], g["y"], g["w"], g["h"]
    fc, ec, tc = PAL.get(g.get("color", "blue"), PAL["blue"])
    # 外框
    rrect(ax, x, y, w, h, "#ffffff", ec, 1.8, r=0.8, z=1)
    # 左上标签条(不透明,盖住边框)
    rrect(ax, x + 1.5, y + h - 4.6, len(g["title"]) * 1.45 + 3.5, 4.2,
          "#ffffff", ec, 1.2, r=0.5, z=4)
    ax.text(x + 3.2, y + h - 2.5, g["title"], ha="left", va="center",
            fontsize=9.0, fontweight="bold", color=tc, zorder=5)


def anchor(node, spec):
    """'id.right@12.5' → (x, y);'wave:x,y' → 直接坐标。"""
    s = spec
    if s.startswith("wave:"):
        _, xy = s.split(":", 1)
        a, b = xy.split(",")
        return float(a), float(b)
    if "@" in s:
        body, v = s.split("@", 1)
        off = float(v)
    else:
        off = None
        body = s
    nid, side = body.split(".", 1)
    n = NODES[nid]
    x, y, w, h = n["x"], n["y"], n["w"], n["h"]
    if side == "left":
        p = (x, y + h / 2)
    elif side == "right":
        p = (x + w, y + h / 2)
    elif side == "top":
        p = (x + w / 2, y + h)
    elif side == "bottom":
        p = (x + w / 2, y)
    elif side == "center":
        p = (x + w / 2, y + h / 2)
    else:
        raise ValueError(side)
    if off is not None:
        if side in ("left", "right"):
            p = (p[0], off)
        else:
            p = (off, p[1])
    return p


def draw_arrow(ax, a):
    color = a.get("color", "#333333")
    ls = (0, (4, 3)) if a.get("dash") else "-"
    p0 = anchor(None, a["from"])
    p1 = anchor(None, a["to"])
    via = a.get("via") or []
    pts = [p0] + [tuple(v) for v in via] + [p1]
    for i in range(len(pts) - 1):
        (x0, y0), (x1, y1) = pts[i], pts[i + 1]
        last = i == len(pts) - 2
        if last:
            ar = FancyArrowPatch((x0, y0), (x1, y1),
                                 arrowstyle="-|>", mutation_scale=11,
                                 color=color, lw=1.4, linestyle=ls,
                                 shrinkA=0, shrinkB=1.5, zorder=6)
            ax.add_patch(ar)
        else:
            ax.add_line(Line2D([x0, x1], [y0, y1], color=color, lw=1.4,
                               linestyle=ls, zorder=6, solid_capstyle="round"))
    if "label" in a:
        # 取最长一段的中点放标签(白底垫片压住线条)
        seg = max(( (pts[i], pts[i + 1]) for i in range(len(pts) - 1) ),
                  key=lambda s: (s[1][0] - s[0][0]) ** 2 + (s[1][1] - s[0][1]) ** 2)
        (x0, y0), (x1, y1) = seg
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        ax.text(mx, my, a["label"], ha="center", va="center",
                fontsize=a.get("fs", 7.6), color=color, zorder=8,
                bbox=dict(boxstyle="round,pad=0.18", fc="#ffffff", ec="none"))


def draw_wave(ax, wv):
    color = wv.get("color", "#2f5d9e")
    pts = wv["pts"]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    base = wv.get("base", min(ys))
    # 主线 + 线下浅填充(到 base)
    ax.add_line(Line2D(xs, ys, color=color, lw=2.0, zorder=7,
                       solid_capstyle="round", solid_joinstyle="round"))
    ax.fill_between(xs, ys, base, color=color, alpha=0.12, zorder=4, lw=0)
    # 垂直跳变画竖线(polylines 已含,fill_between 对垂直段也能闭合)
    # 断口标记
    if "break" in wv:
        bx = wv["break"]
        # 找该 x 附近的 y 范围
        ys_here = [p[1] for p in pts if abs(p[0] - bx) < 30]
        y0 = min(ys_here) - 1.2
        y1 = max(ys_here) + 1.2
        g = 0.9
        for yy in (y0, y1):
            ax.add_line(Line2D([bx - 1.6, bx + 1.6], [yy, yy + (g if yy == y0 else -g)],
                               color=color, lw=1.4, zorder=8))
        ax.text(bx, (y0 + y1) / 2, "≈", ha="center", va="center",
                fontsize=8, color=color, zorder=8)
    for t in wv.get("texts", []):
        ax.text(t["x"], t["y"], t["s"], ha="center", va="center",
                fontsize=t.get("fs", 7.5), color=t.get("color", color),
                zorder=9, fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.18", fc="#ffffff", ec="none"))
    for tk in wv.get("ticks", []):
        x, label = tk[0], tk[1]
        ax.add_line(Line2D([x, x], [base, base - 1.2], color="#555555",
                           lw=1.0, zorder=7))
        ax.text(x, base - 2.0, label, ha="center", va="top",
                fontsize=6.8, color="#555555", zorder=8)


def render(spec_path):
    global NODES
    with open(spec_path, encoding="utf-8") as f:
        spec = json.load(f)
    W, H = spec["width"], spec["height"]
    NODES = {n["id"]: n for n in spec.get("nodes", [])}
    fig = plt.figure(figsize=(spec["figsize_w"], spec["figsize_w"] * H / W))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(0, H)
    ax.axis("off")
    ax.set_facecolor("#ffffff")
    # 标题
    if spec.get("title"):
        ax.text(W / 2, H - 1.6, spec["title"], ha="center", va="top",
                fontsize=13.5, fontweight="bold", color="#1a1a1a", zorder=9)
    if spec.get("subtitle"):
        ax.text(W / 2, H - 4.6, spec["subtitle"], ha="center", va="top",
                fontsize=8.6, color="#555555", zorder=9)

    for g in spec.get("groups", []):
        group_box(ax, g)
    for wv in spec.get("waves", []):
        draw_wave(ax, wv)
    for n in spec.get("nodes", []):
        node_box(ax, n)
    for a in spec.get("arrows", []):
        draw_arrow(ax, a)

    base = os.path.splitext(spec_path)[0]
    if base.endswith("_spec"):
        base = base[: -len("_spec")]
    fig.savefig(base + ".svg", format="svg")
    fig.savefig(base + ".png", dpi=150, facecolor="#ffffff")
    plt.close(fig)
    print(f"OK {spec_path} -> {base}.png/.svg  ({W}x{H})")


if __name__ == "__main__":
    for p in sys.argv[1:]:
        render(p)
