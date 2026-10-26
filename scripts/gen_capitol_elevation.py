#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成美国国会大厦东立面（正投影）剪影 SVG。

用途
    输出可直接粘贴进 index.html 条目列表抬头 `<div class="cap-art">` 的整段 <svg>。
    与白宫那幅同一套画法：纯黑填充、洞口镂空、贴底对齐。

形制与依据
    美国国会大厦东立面（East Front）：主体南北长 751 ft 4 in；穹顶鼓座（drum）外径 135 ft、36 根柱；
    中央门廊为 10 根科林斯柱的巨柱式，上承额枋与极浅的实心山花（内含雕塑组）；
    两翼三段：粗石基座层（连拱）、主层（高窗）、阁楼层小窗，檐上为栏杆女儿墙。
    纵向按参考照片（Wikimedia Commons 东立面全景）以 19.5 ft 的自由雕像标定，模型总高取 262 ft；
    公共资料常引「至雕像顶 288 ft」，与照片口径不同，不要混用。比例以照片实测为准。
    1 单位 = 0.1 ft。

用法
    python3 scripts/gen_capitol_elevation.py            # 打印
    python3 scripts/gen_capitol_elevation.py out.svg    # 写文件

退出码
    0 成功；2 写盘失败。
"""
import re
import sys

FT = 10.0            # 1 ft = 10 单位
W_TOTAL = 7514.0     # 751 ft 4 in
CX = W_TOTAL / 2
GY = 2620.0          # 地面（东广场标高，至雕像顶 228 ft）

def H(ft):
    """标高（英尺，自地面）-> y"""
    return GY - ft * FT

def rect(x, y, w, h, r=0):
    if r:
        return f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{r}"/>'
    return f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}"/>'

# ---------------- 标高（英尺，自东广场地面）----------------
L_WB_TOP      = 18.0     # 联系体量：基座拱廊顶
L_WM_TOP      = 62.0     # 联系体量：主层顶
L_WA_TOP      = 78.0     # 联系体量：阁楼顶
L_WC_TOP      = 86.0     # 联系体量：檐口顶 —— 与三段殿宇的额枋顶同高，横向线条贯通
L_WBAL_TOP    = 94.0     # 联系体量：女儿墙顶（山花由此以上升起）

L_PORT_FLOOR  = 40.0     # 殿宇：基座顶 / 柱础
L_COL_TOP     = 78.0     # 殿宇：柱头
L_ENTAB_TOP   = 86.0     # 殿宇：额枋顶 / 山花底
L_PED_APEX    = 103.0    # 殿宇：山花顶（三段同高，坡度统一约 12°——实测极浅，非 25°）

L_DRUM_BASE   = 115.0    # 鼓座底
L_DRUM_TOP    = 152.0    # 鼓座柱列顶
L_DOME_BASE   = 156.0    # 穹顶起拱
L_DOME_APEX   = 218.0    # 穹顶顶
L_LANTERN_TOP = 244.0    # 小亭顶（雕像座）
L_STATUE_TOP  = 262.0    # 雕像顶

PAV_W   = 1730.0         # 两端殿宇宽（约占立面 23%）
PORT_W  = 1650.0         # 中央门廊宽（约占立面 22%）
DRUM_W  = 1400.0         # 鼓座外径（略窄于中央山花）
WING_W  = (W_TOTAL - 2 * PAV_W - PORT_W) / 2   # 联系体量

X_PAV_L0 = 0.0
X_PAV_L1 = PAV_W
X_WL0, X_WL1 = PAV_W, PAV_W + WING_W
X_P0, X_P1 = PAV_W + WING_W, PAV_W + WING_W + PORT_W
X_WR0, X_WR1 = X_P1, X_P1 + WING_W
X_PAV_R0, X_PAV_R1 = W_TOTAL - PAV_W, W_TOTAL

# 三段柱廊的 (起点, 宽度)：两端占殿宇 74%，中央占 89%
PORTICOES = (
    (X_PAV_L0 + PAV_W * 0.13, PAV_W * 0.74),
    (X_P0 + PORT_W * 0.055, PORT_W * 0.89),
    (X_PAV_R0 + PAV_W * 0.13, PAV_W * 0.74),
)

# ---------------- 洞口 ----------------
def wing_holes(x0, w):
    """联系体量：6 开间，基座拱窗 + 主层高窗 + 阁楼小窗。"""
    out = []
    bay = w / 6
    for i in range(6):
        cx = x0 + (i + 0.5) * bay
        out.append((cx - 30, H(16), 60, 80))                 # 基座窗（下部）
        out.append(('ARC', cx, H(16), 30))                   # 拱顶
        out.append((cx - 40, H(54), 80, 200))                # 主层窗
        out.append((cx - 22, H(76), 44, 46))                 # 阁楼小窗
    return out


def holes():
    out = []
    out += wing_holes(X_WL0, WING_W)
    out += wing_holes(X_WR0, WING_W)
    # 两端殿宇块体：柱廊两翼各一列窗（主层 20–44 ft、阁楼 66–76 ft）
    for x0 in (X_PAV_L0, X_PAV_R0):
        for f in (0.065, 0.935):
            cx = x0 + PAV_W * f
            out.append((cx - 34, H(44), 68, 240))            # 主层窗
            out.append((cx - 26, H(76), 52, 100))            # 阁楼层窗
    # 两端殿宇基座：粗石层上的连拱（参考照片里柱廊下方是一排矮拱洞）。
    # 拱顶必须明显低于柱础（40 ft），否则拱洞和柱廊镂空会连成一片白，柱子就成了悬空的黑条。
    for x0, w in PORTICOES[:1] + PORTICOES[2:]:
        n, aw = 6, 96.0
        for i in range(n):
            cx = x0 + w * (i + 0.5) / n
            out.append((cx - aw / 2, H(24), aw, 160))        # 拱洞身 8–24 ft
            out.append(('ARC', cx, H(24), aw / 2))           # 半圆拱顶，顶到 28.8 ft

    # 三段柱廊：在柱列背后开洞，柱子才不会与块体糊成一片
    for x0, w in PORTICOES:
        out.append((x0, H(L_ENTAB_TOP), w, (L_ENTAB_TOP - L_PORT_FLOOR) * FT))
    # 中央门廊基座：大台阶的踏步线。不划这几道线，柱廊底下就是整块黑。
    x0c, wc = PORTICOES[1]
    for k in range(5):
        ft = 8 + k * 6.4
        out.append((x0c + 30 + k * 16, H(ft), wc - 60 - k * 32, 9))
    # 鼓座底部阴影线
    out.append((CX - DRUM_W / 2 + 30, H(L_DRUM_BASE - 1.2), DRUM_W - 60, 14))
    return out


def drum_openings():
    out = []
    n, w = 18, 30.0
    for i in range(n):
        cx = CX - DRUM_W / 2 + (i + 0.5) * (DRUM_W / n)
        out.append((cx - w / 2, H(L_DRUM_TOP) + 60, w, (L_DRUM_TOP - L_DRUM_BASE) * FT - 140))
    return out


# ---------------- 构件 ----------------
def cornice_band(x0, w):
    """檐口 → 束带 → 女儿墙栏杆：翼楼与殿宇共用同一套标高，横向线条才能贯通。

    栏杆的「空格」不用画：上下两条横枋之间留空，背景透出来即成栏杆缝。
    """
    s = [rect(x0 - 10, H(L_WC_TOP), w + 20, 34)]                          # 檐口
    s.append(rect(x0 - 6, H(L_WC_TOP) + 34, w + 12, 12))                  # 束带
    y_top, y_bot = H(L_WBAL_TOP), H(L_WC_TOP)
    s.append(rect(x0 - 8, y_top, w + 16, 18))                             # 上枋
    s.append(rect(x0 - 8, y_bot - 16, w + 16, 16))                        # 下枋
    pitch, bw = 46.0, 18.0
    n = int(w / pitch)
    off = (w - n * pitch) / 2
    for k in range(n):
        s.append(rect(x0 + off + k * pitch + (pitch - bw) / 2, y_top + 18, bw, y_bot - 16 - (y_top + 18)))
    for k in range(4):                                                    # 栏杆墩，读出韵律
        px = x0 + w * (k + 0.5) / 4
        s.append(rect(px - 15, y_top - 6, 30, y_bot - y_top + 6))
    return ''.join(s)


def wing(x0):
    """两殿之间的联系体量：低矮三层 + 檐上栏杆（与殿宇同一条檐线）。"""
    s = [rect(x0, H(L_WA_TOP), WING_W, L_WA_TOP * FT)]
    s.append(rect(x0 - 4, H(0) - 26, WING_W + 8, 26))                     # 勒脚
    s.append(cornice_band(x0, WING_W))
    return ''.join(s)


def rotunda():
    """中央圆厅外墙：一直砌到鼓座座盘之上，鼓座不能悬空。"""
    x0 = CX - DRUM_W / 2
    w = DRUM_W
    top = L_DRUM_BASE + 4
    s = [rect(x0, H(top), w, top * FT)]
    s.append(rect(x0 - 22, H(L_PED_APEX) - 22, w + 44, 22))               # 山花顶处的檐线
    return ''.join(s)


def block_wall(x0, w, top=L_WA_TOP):
    """殿宇背后的实体块：墙身 + 檐口 + 女儿墙（与翼楼同标高，檐线不断）。"""
    s = [rect(x0, H(top), w, top * FT)]
    s.append(rect(x0 - 4, H(0) - 26, w + 8, 26))                          # 勒脚
    s.append(cornice_band(x0, w))
    return ''.join(s)


def portico(x0, w):
    """凸出的柱廊：浅台阶 + 10 柱 + 额枋 + 实心山花。

    山花底在檐口线（86 ft）、顶在 103 ft，三段同一举高——实测国会山花极浅
    （高约宽的 1/8），不能用古典神庙的 25°。山花必须实心：参考照片里三角
    内部是雕塑组，画成空心斜檐会露出背景，读起来像线框图。
    """
    s = []
    for k in range(4):                                                    # 台阶段（只到 10 ft，不遮基座拱洞）
        ww = w + 200 - k * 34
        s.append(rect(x0 + w / 2 - ww / 2, H(0) - (k + 1) * 26, ww, 26))
    n = 10
    for i in range(n):
        cx = x0 + (i + 0.5) * (w / n)
        s.append(rect(cx - 22, H(L_COL_TOP), 44, (L_COL_TOP - L_PORT_FLOOR) * FT))
        s.append(rect(cx - 30, H(L_COL_TOP) - 22, 60, 22))                # 柱头
        s.append(rect(cx - 28, H(L_PORT_FLOOR) - 24, 56, 24))             # 柱础
    s.append(rect(x0 - 52, H(L_ENTAB_TOP), w + 104, (L_ENTAB_TOP - L_COL_TOP) * FT))
    half = w / 2 + 62
    base, apex = H(L_ENTAB_TOP), H(L_PED_APEX)
    cx = x0 + w / 2
    s.append(f'<path d="M{cx-half:.1f},{base+34:.1f} L{cx:.1f},{apex:.1f} '
             f'L{cx+half:.1f},{base+34:.1f} Z"/>')                        # 实心山花，底边压入额枋
    return ''.join(s)


def drum():
    s = []
    x0 = CX - DRUM_W / 2
    s.append(rect(x0 - 30, H(L_DRUM_BASE), DRUM_W + 60, 24))
    s.append(rect(x0, H(L_DRUM_TOP), DRUM_W, (L_DRUM_TOP - L_DRUM_BASE) * FT))
    s.append(rect(x0 - 34, H(L_DRUM_TOP) - 30, DRUM_W + 68, 30))
    s.append(rect(x0 - 18, H(L_DRUM_TOP) - 50, DRUM_W + 36, 20))
    yt, yb = H(L_DOME_BASE) + 22, H(L_DOME_BASE)
    s.append(rect(x0 - 26, yt, DRUM_W + 52, 16))
    s.append(rect(x0 - 26, yb, DRUM_W + 52, 12))
    pitch, bw = 54.0, 22.0
    n = int((DRUM_W + 52) / pitch)
    for k in range(n):
        s.append(rect(x0 - 26 + k * pitch + (pitch - bw) / 2, yt + 16, bw, yb - (yt + 16)))
    return ''.join(s)


def dome():
    s = []
    top, base, hw = H(L_DOME_APEX), H(L_DOME_BASE), 500.0
    s.append(rect(CX - hw - 26, base, hw * 2 + 52, 46))                   # 座环
    b = base + 46
    s.append(f'<path d="M{CX-hw:.1f},{b:.1f} '
             f'C{CX-hw*0.92:.1f},{b-(b-top)*0.60:.1f} {CX-hw*0.55:.1f},{top+30:.1f} {CX:.1f},{top:.1f} '
             f'C{CX+hw*0.55:.1f},{top+30:.1f} {CX+hw*0.92:.1f},{b-(b-top)*0.60:.1f} {CX+hw:.1f},{b:.1f} Z"/>')
    return ''.join(s)


def tholos():
    s = []
    top, base, w = H(L_LANTERN_TOP), H(L_DOME_APEX), 180.0
    s.append(rect(CX - w / 2, base - 4, w, 22))
    for i in range(12):
        cx = CX - w / 2 + (i + 0.5) * (w / 12)
        s.append(rect(cx - 7, top + 70, 14, base - top - 96))
    s.append(rect(CX - w / 2 - 14, top + 46, w + 28, 24))
    s.append(rect(CX - w / 2 - 10, top + 12, w + 20, 36))
    return ''.join(s)


def statue():
    s = []
    top, base = H(L_STATUE_TOP), H(L_LANTERN_TOP)
    s.append(rect(CX - 26, base - 10, 52, 18))
    s.append(f'<path d="M{CX-26:.1f},{base-10:.1f} L{CX-15:.1f},{top+56:.1f} '
             f'Q{CX-13:.1f},{top+24:.1f} {CX:.1f},{top+20:.1f} '
             f'Q{CX+13:.1f},{top+24:.1f} {CX+15:.1f},{top+56:.1f} L{CX+26:.1f},{base-10:.1f} Z"/>')
    s.append(f'<circle cx="{CX:.1f}" cy="{top+14:.1f}" r="13"/>')
    s.append(f'<path d="M{CX+6:.1f},{top+10:.1f} L{CX+26:.1f},{top-6:.1f} L{CX+30:.1f},{top+4:.1f} Z"/>')
    return ''.join(s)


def build():
    facade_holes = ''
    for item in holes():
        if item[0] == 'ARC':
            _, cx, ytop, r = item
            facade_holes += f'<path d="M{cx-r:.1f},{ytop:.1f} A{r:.1f},{r:.1f} 0 0 1 {cx+r:.1f},{ytop:.1f} Z"/>'
        else:
            facade_holes += rect(*item)
    drum_holes = ''.join(rect(x, y, w, h) for x, y, w, h in drum_openings())
    W = int(W_TOTAL + 120)
    Hh = 2900

    s = []
    s.append(f'<svg viewBox="0 {int(H(L_STATUE_TOP) - 60)} {W} '
             f'{int(GY - (H(L_STATUE_TOP) - 60))}" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">')
    s.append('<defs>')
    s.append(f'<mask id="cap-fac" maskUnits="userSpaceOnUse" x="-60" y="0" width="{W}" height="{Hh}">'
             f'<rect x="-60" y="0" width="{W}" height="{Hh}" fill="#fff"/>'
             f'<g fill="#000">{facade_holes}</g></mask>')
    s.append(f'<mask id="cap-drum" maskUnits="userSpaceOnUse" x="-60" y="0" width="{W}" height="{Hh}">'
             f'<rect x="-60" y="0" width="{W}" height="{Hh}" fill="#fff"/>'
             f'<g fill="#000">{drum_holes}</g></mask>')
    s.append('</defs>')
    s.append('<g fill="#000" mask="url(#cap-fac)">')
    s.append(rotunda())
    s.append(block_wall(X_PAV_L0, PAV_W))
    s.append(block_wall(X_PAV_R0, PAV_W))
    s.append(wing(X_WL0)); s.append(wing(X_WR0))
    s.append('</g>')
    s.append('<g fill="#000" mask="url(#cap-drum)">')
    s.append(drum())
    s.append('</g>')
    s.append('<g fill="#000">')
    for px, pw in PORTICOES:
        s.append(portico(px, pw))
    s.append(dome()); s.append(tholos()); s.append(statue())
    s.append('</g></svg>')
    return '\n'.join(s)


def render():
    svg = build()
    svg = re.sub(r'(\d+\.\d)\d+', r'\1', svg)
    svg = re.sub(r'(\d)\.0(?=["\'htvMLz ,])', r'\1', svg)
    return svg


def main(argv):
    svg = render()
    if len(argv) > 1:
        try:
            with open(argv[1], 'w', encoding='utf-8') as f:
                f.write(svg)
        except OSError as exc:
            print('write failed: %s' % exc, file=sys.stderr)
            return 2
        print('written %s (%d bytes)' % (argv[1], len(svg)))
    else:
        print(svg)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
