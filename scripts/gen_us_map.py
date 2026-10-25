#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成「美国轮廓」SVG 片段（本土 48 州 + 阿拉斯加 + 夏威夷），供 #view-us 抬头做地理底纹。

用法：
    curl -sL -o /tmp/ne110.json \\
      https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_110m_admin_0_countries.geojson
    python3 scripts/gen_us_map.py [输入 geojson] [输出 svg]

默认：输入 /tmp/ne110.json，输出 /tmp/us-map.svg。
输出为 <svg class="us-map" viewBox="0 0 600 380"> + 三条 <path>，仅描边，
样式（描边色/透明度/尺寸）写在 index.html 的 #view-us .us-head .us-map 里。

数据：Natural Earth 110m（公有领域）。110m 精度足够做背景轮廓，点数约 380。
"""
import json
import math
import sys

SRC = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ne110.json"
OUT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/us-map.svg"

VB_W, VB_H = 600.0, 380.0          # 输出画布
BOX_CONUS = (146.0, 6.0, 594.0, 322.0)
BOX_ALASKA = (8.0, 214.0, 140.0, 322.0)
BOX_HAWAII = (58.0, 332.0, 140.0, 372.0)


def albers(phi1, phi2, phi0, lam0):
    """Albers 等积圆锥投影，返回 (lon, lat) -> (x, y)，y 轴向下。"""
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


def bbox(rings):
    xs = [x for r in rings for x, _ in r]
    ys = [y for r in rings for _, y in r]
    return min(xs), min(ys), max(xs), max(ys)


def project(polys, fn):
    return [[fn(p[0], p[1]) for p in poly[0]] for poly in polys]


def fit(rings, box, eps):
    x0, y0, x1, y1 = bbox(rings)
    s = min((box[2] - box[0]) / (x1 - x0), (box[3] - box[1]) / (y1 - y0))
    ox = box[0] + ((box[2] - box[0]) - (x1 - x0) * s) / 2 - x0 * s
    oy = box[1] + ((box[3] - box[1]) - (y1 - y0) * s) / 2 - y0 * s
    out = []
    for r in rings:
        pts = rdp([(x * s + ox, y * s + oy) for x, y in r], eps)
        if len(pts) > 2:
            out.append(pts)
    return out


def to_d(rings):
    return " ".join("M" + " ".join("%.1f %.1f" % (x, y) for x, y in r) + "Z" for r in rings)


def main():
    data = json.load(open(SRC, encoding="utf-8"))
    usa = [f for f in data["features"] if f["properties"].get("ISO_A3") == "USA"][0]
    geom = usa["geometry"]
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]

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

    groups = [
        ("conus", fit(project(conus, albers(29.5, 45.5, 37.5, -96)), BOX_CONUS, 0.55)),
        ("alaska", fit(project(alaska, albers(55, 65, 64, -152)), BOX_ALASKA, 0.4)),
        ("hawaii", fit(project(hawaii, albers(8, 18, 20.5, -157)), BOX_HAWAII, 0.18)),
    ]

    body = "\n".join('  <path class="usm-%s" d="%s"/>' % (k, to_d(v)) for k, v in groups)
    svg = ('<svg class="us-map" viewBox="0 0 %.0f %.0f" xmlns="http://www.w3.org/2000/svg" '
           'aria-hidden="true" focusable="false">\n%s\n</svg>\n') % (VB_W, VB_H, body)
    open(OUT, "w", encoding="utf-8").write(svg)
    total = sum(len(r) for _, g in groups for r in g)
    print("已生成 %s：%d 个点，%d 字节" % (OUT, total, len(svg)))


if __name__ == "__main__":
    main()
