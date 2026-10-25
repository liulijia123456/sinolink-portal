#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成「美国轮廓」SVG 片段（本土 48 州 + 阿拉斯加 + 夏威夷），供 #view-us 抬头做地理底纹。

用法：
    curl -sL -o /tmp/ne110.json \\
      https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_110m_admin_0_countries.geojson
    python3 scripts/gen_us_map.py [输入 geojson] [输出 svg]

默认：输入 /tmp/ne110.json，输出 /tmp/us-map.svg。
版式：**同一投影、同一比例，按真实地理方位摆放**——阿拉斯加在西北、夏威夷在西南、
本土居中偏东；阿留申群岛一直延伸到西经 170° 之外，若嫌地图太宽可用 --clip-lon 截断。
输出为 <svg class="us-map" viewBox="0 0 600 380"> + 三条 <path>，仅描边，
样式（描边色/透明度/尺寸）写在 index.html 的 #view-us .us-head .us-map 里。

数据：Natural Earth 110m（公有领域）。
"""
import json
import math
import sys

SRC = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ne110.json"
OUT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/us-map.svg"

VB_W, VB_H = 600.0, 380.0
PAD = 10.0                 # 画布内留白
CLIP_LON = -172.0          # 阿留申群岛截断经度（更西的岛不画，避免地图被拉得过宽）
AK_SCALE = 1.0             # 阿拉斯加缩放，1.0 = 与本土同比例（真实）

# 单一 Albers 等积圆锥投影：标准纬线取美国本土，经线中心取 -100
PHI1, PHI2, PHI0, LAM0 = 29.5, 45.5, 38.0, -100.0


def albers(phi1, phi2, phi0, lam0):
    phi1, phi2, phi0, lam0 = map(math.radians, (phi1, phi2, phi0, lam0))
    n = 0.5 * (math.sin(phi1) + math.sin(phi2))
    c = math.cos(phi1) ** 2 + 2 * n * math.sin(phi1)
    rho0 = math.sqrt(c - 2 * n * math.sin(phi0)) / n

    def fn(lon, lat):
        lam, phi = math.radians(lon), math.radians(lat)
        rho = math.sqrt(max(c - 2 * n * math.sin(phi), 0.0)) / n
        th = n * (lam - lam0)
        return rho * math.sin(th), -(rho0 - rho * math.cos(th))

    return fn


def rdp(pts, eps):
    """Douglas–Peucker 简化；闭合环（首尾重合）先切成两段再各自简化。"""
    if len(pts) < 3:
        return pts
    ax, ay = pts[0]
    bx, by = pts[-1]
    dx, dy = bx - ax, by - ay
    norm = math.hypot(dx, dy)
    if norm < 1e-9:
        idx = max(range(1, len(pts) - 1),
                  key=lambda i: (pts[i][0] - ax) ** 2 + (pts[i][1] - ay) ** 2)
        if (pts[idx][0] - ax) ** 2 + (pts[idx][1] - ay) ** 2 <= eps * eps:
            return [pts[0], pts[-1]]
        return rdp(pts[:idx + 1], eps)[:-1] + rdp(pts[idx:], eps)
    idx, dmax = 0, -1.0
    for i in range(1, len(pts) - 1):
        px, py = pts[i]
        dist = abs(dy * (px - ax) - dx * (py - ay)) / norm
        if dist > dmax:
            idx, dmax = i, dist
    if dmax <= eps:
        return [pts[0], pts[-1]]
    return rdp(pts[:idx + 1], eps)[:-1] + rdp(pts[idx:], eps)


def classify(polys):
    conus, alaska, hawaii = [], [], []
    for poly in polys:
        ring = poly[0]
        lons = [p[0] for p in ring]
        lats = [p[1] for p in ring]
        lo, hi, la, lb = min(lons), max(lons), min(lats), max(lats)
        if la < 28 and lo < -150:          # 夏威夷群岛（先判，否则会被阿拉斯加规则吃掉）
            hawaii.append(poly)
        elif hi < -140 or la > 50:         # 阿拉斯加（含阿留申）
            alaska.append(poly)
        elif hi > -130 and lb > 24:        # 本土 48 州
            conus.append(poly)
    return conus, alaska, hawaii


def to_d(rings):
    return " ".join("M" + " ".join("%.1f %.1f" % (x, y) for x, y in r) + "Z" for r in rings)


def main():
    data = json.load(open(SRC, encoding="utf-8"))
    usa = [f for f in data["features"] if f["properties"].get("ISO_A3") == "USA"][0]
    geom = usa["geometry"]
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    conus, alaska, hawaii = classify(polys)

    fn = albers(PHI1, PHI2, PHI0, LAM0)

    def project(group, clip=None):
        out = []
        for poly in group:
            ring = poly[0]
            if clip is not None and min(p[0] for p in ring) < clip:
                continue          # 整块岛礁略去；逐点截断会把形状切坏
            pts = [fn(lon, lat) for lon, lat in ring]
            if len(pts) > 2:
                out.append(pts)
        return out

    conus_p = project(conus)
    alaska_p = project(alaska, CLIP_LON)
    hawaii_p = project(hawaii)

    # 同一投影、同一比例：先求所有组的整体包围盒，再统一缩放
    all_pts = [p for g in (conus_p, alaska_p, hawaii_p) for r in g for p in r]
    xs = [p[0] for p in all_pts]
    ys = [p[1] for p in all_pts]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    s = min((VB_W - 2 * PAD) / (x1 - x0), (VB_H - 2 * PAD) / (y1 - y0))
    ox = PAD + ((VB_W - 2 * PAD) - (x1 - x0) * s) / 2 - x0 * s
    oy = PAD + ((VB_H - 2 * PAD) - (y1 - y0) * s) / 2 - y0 * s

    def fit(group, scale=1.0, pivot=None):
        out = []
        for r in group:
            pts = []
            for x, y in r:
                if pivot is not None:
                    # 以该组自身重心为基准缩放，位置保持不变
                    px, py = pivot
                    x, y = px + (x - px) * scale, py + (y - py) * scale
                pts.append((x * s + ox, y * s + oy))
            pts = rdp(pts, 0.5)
            if len(pts) > 2:
                out.append(pts)
        return out

    groups = [("conus", fit(conus_p)),
              ("alaska", fit(alaska_p, AK_SCALE, _center(alaska_p))),
              ("hawaii", fit(hawaii_p))]

    body = "\n".join('  <path class="usm-%s" d="%s"/>' % (k, to_d(v)) for k, v in groups)
    svg = ('<svg class="us-map" viewBox="0 0 %.0f %.0f" xmlns="http://www.w3.org/2000/svg" '
           'aria-hidden="true" focusable="false">\n%s\n</svg>\n') % (VB_W, VB_H, body)
    open(OUT, "w", encoding="utf-8").write(svg)
    total = sum(len(r) for _, g in groups for r in g)
    print("已生成 %s：%d 个点，%d 字节" % (OUT, total, len(svg)))


def _center(group):
    pts = [p for r in group for p in r]
    if not pts:
        return None
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


if __name__ == "__main__":
    main()
