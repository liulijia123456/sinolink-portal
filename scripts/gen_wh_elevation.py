#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成白宫北立面正投影剪影（SVG），供 index.html 条目列表抬头使用。

用途
    输出可直接粘贴进 index.html `<div class="wh-art">` 的整段 <svg>。参数化建模，
    改比例只改下面「尺度常量」与各构件函数，不要手改已生成的 d / x 属性。

形制与依据
    白宫北立面（North Front）：11 开间、每间 15.27 ft、全宽 168 ft；中央三间为
    1830 年建成的四柱爱奥尼门廊（prostyle portico），上承额枋与山花；
    两层可窗（主层 18.5 ft、二层 15 ft），底层被马车坡道与女儿墙遮挡；
    檐部带栏杆女儿墙；屋脊四座烟囱。窗楣「弧形—三角」交替排布，
    在纯黑剪影中不可见，故未刻出。
    参考：Wikimedia Commons《North Façade White House.JPG》实测绘比。

用法
    python3 scripts/gen_wh_elevation.py                 # 打印到标准输出
    python3 scripts/gen_wh_elevation.py out.svg         # 写入文件

退出码
    0 成功；2 写盘失败。
"""
import re
import sys

BAY = 1680 / 11.0
CX = 840.0
GY = 760.0

def Yft(ft): return GY - ft * 10

BASE_TOP    = Yft(8)      # 基座（底层被遮挡，仅余露明段）顶
STATE_TOP   = Yft(27.5)   # 主层顶
SECOND_TOP  = Yft(42.5)   # 二层顶
FRIEZE_TOP  = Yft(44.0)   # 檐部额枋顶
CORNICE_TOP = Yft(46.0)   # 檐口顶 / 栏杆底
BALUST_TOP  = Yft(50.0)   # 栏杆顶

def bayc(i): return (i + 0.5) * BAY
def bayline(i): return i * BAY

def rect(x, y, w, h, r=0):
    if r:
        return f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{r}"/>'
    return f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}"/>'

ST_W, ST_H = 50.0, 105.0     # 主层窗
ND_W, ND_H = 50.0, 85.0      # 二层窗
PORCH_X0, PORCH_X1 = bayline(4) - 33, bayline(7) + 33

def window_rects():
    out = []
    for i in range(11):
        if 4 <= i <= 6:
            continue
        out.append((bayc(i) - ST_W / 2, BASE_TOP - 25 - ST_H, ST_W, ST_H))
        out.append((bayc(i) - ND_W / 2, STATE_TOP - 22 - ND_H, ND_W, ND_H))
    return out

HOLES = window_rects()
DOOR_W, DOOR_TOP, DOOR_BOT = 54.0, GY - 70, GY - 2
FAN_R = DOOR_W / 2

# ---------------- 构件 ----------------
def mass():
    s = []
    s.append(rect(0, CORNICE_TOP, 1680, GY - CORNICE_TOP))
    s.append(rect(-10, CORNICE_TOP, 1700, 14))              # 出挑檐口
    s.append(rect(-6, CORNICE_TOP + 14, 1692, 8))           # 檐下束带
    s.append(rect(-8, GY - 10, 1696, 10))                   # 勒脚
    return ''.join(s)

def balustrade(x0, x1, top=BALUST_TOP):
    s = []
    s.append(rect(x0, top, x1 - x0, 10))                    # 上枋
    s.append(rect(x0, CORNICE_TOP - 10, x1 - x0, 10))       # 下枋
    y0, y1 = top + 10, CORNICE_TOP - 10
    pitch, bw = 11.0, 6.5
    n = int((x1 - x0) / pitch)
    off = ((x1 - x0) - n * pitch) / 2
    for k in range(n):
        s.append(rect(x0 + off + k * pitch + (pitch - bw) / 2, y0, bw, y1 - y0))
    return ''.join(s)

def column(cx):
    s = []
    s.append(rect(cx - 34, GY - 12, 68, 12))                # 柱础座
    s.append(rect(cx - 28, GY - 22, 56, 10, 2))             # 柱础线脚
    bot_w, top_w = 50.0, 42.0
    yb, yt = GY - 22, 320.0
    s.append(f'<path d="M{cx-bot_w/2:.1f},{yb:.1f} L{cx-top_w/2:.1f},{yt:.1f} '
             f'L{cx+top_w/2:.1f},{yt:.1f} L{cx+bot_w/2:.1f},{yb:.1f} Z"/>')
    # 爱奥尼柱头：涡卷 + 顶板
    s.append(f'<path d="M{cx-26:.1f},320 L{cx-26:.1f},308 Q{cx-26:.1f},298 {cx-17:.1f},298 '
             f'L{cx+17:.1f},298 Q{cx+26:.1f},298 {cx+26:.1f},308 L{cx+26:.1f},320 Z"/>')
    s.append(rect(cx - 30, 291, 60, 7))                     # 顶板
    return ''.join(s)

def pediment():
    s = []
    x0, x1 = PORCH_X0, PORCH_X1
    s.append(rect(x0, CORNICE_TOP, x1 - x0, FRIEZE_TOP - CORNICE_TOP))   # 门廊额枋
    s.append(rect(x0 - 7, CORNICE_TOP - 13, x1 - x0 + 14, 13))          # 门廊檐口
    base = CORNICE_TOP - 13
    apex_y = 188.0
    thick = 15
    s.append(f'<path d="M{x0:.1f},{base:.1f} L{CX:.1f},{apex_y+thick:.1f} L{x1:.1f},{base:.1f} Z"/>')
    s.append(f'<path d="M{x0-11:.1f},{base+thick:.1f} L{CX:.1f},{apex_y:.1f} L{x1+11:.1f},{base+thick:.1f} '
             f'L{x1+11:.1f},{base+thick+11:.1f} L{x0-11:.1f},{base+thick+11:.1f} Z"/>')
    return ''.join(s)

def dentils(x0, y, n, pitch=13.0, w=7.0, h=8.0):
    return ''.join(rect(x0 + k * pitch, y, w, h) for k in range(n))

def chimney(cx, top, w=28, h=54):
    s = []
    s.append(rect(cx - w / 2, top + 9, w, h))
    s.append(rect(cx - w / 2 - 5, top, w + 10, 10))
    return ''.join(s)

def pediment_heads():
    out = []
    for i in range(11):
        if 4 <= i <= 6:
            continue
        x = bayc(i); y = BASE_TOP - 25 - ST_H          # 窗顶
        w = ST_W / 2
        if i in (0, 2, 7, 9):                          # 弧形窗楣
            out.append(f'<path d="M{x-w-5:.1f},{y-2:.1f} A{w+5:.1f},{w*0.42:.1f} 0 0 1 {x+w+5:.1f},{y-2:.1f}" '
                       f'fill="none" stroke="#000" stroke-width="4"/>')
        else:                                          # 三角窗楣
            ay = y - (w * 0.34 + 6)
            out.append(f'<path d="M{x-w-6:.1f},{y-2:.1f} L{x:.1f},{ay:.1f} L{x+w+6:.1f},{y-2:.1f}" '
                       f'fill="none" stroke="#000" stroke-width="4"/>')
    return ''.join(out)


def build(op=1.0):
    holes = ''.join(rect(x, y, w, h) for x, y, w, h in HOLES)
    # 檐下齿饰（镂空）
    for k in range(int(1680 / 13)):
        holes += rect(6 + k * 13, CORNICE_TOP + 4, 6, 7)
    holes += rect(PORCH_X0 + 13, FRIEZE_TOP, PORCH_X1 - PORCH_X0 - 26, GY - 12 - FRIEZE_TOP)

    s = []
    s.append('<svg class="whp-svg" viewBox="0 150 1680 610" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">')
    s.append('<defs><mask id="whp-h" maskUnits="userSpaceOnUse" x="-40" y="0" width="1760" height="800">'
             '<rect x="-40" y="0" width="1760" height="800" fill="#fff"/>'
             f'<g fill="#000">{holes}</g></mask></defs>')
    s.append(f'<g mask="url(#whp-h)" fill="#000" opacity="{op}">')
    s.append(mass())
    s.append(balustrade(0, PORCH_X0))
    s.append(balustrade(PORCH_X1, 1680))
    s.append(chimney(430, 218))
    s.append(chimney(566, 236))
    s.append(chimney(1114, 236))
    s.append(chimney(1250, 218))
    s.append('</g>')
    s.append(f'<g fill="#000" opacity="{op}">')
    s.append(pediment())
    for c in (bayline(4), bayline(5), bayline(6), bayline(7)):
        s.append(column(c))
    s.append(rect(PORCH_X0 - 12, GY - 12, PORCH_X1 - PORCH_X0 + 24, 12))   # 台基
    s.append(rect(PORCH_X0 - 26, GY - 5, PORCH_X1 - PORCH_X0 + 52, 5))     # 踏步
    s.append(rect(CX - DOOR_W / 2, DOOR_TOP, DOOR_W, DOOR_BOT - DOOR_TOP))
    s.append(f'<path d="M{CX-FAN_R:.1f},{DOOR_TOP:.1f} A{FAN_R:.1f},{FAN_R:.1f} 0 0 1 {CX+FAN_R:.1f},{DOOR_TOP:.1f} Z"/>')
    s.append('</g></svg>')
    return '\n'.join(s)



def render():
    """输出带唯一 mask id、坐标保留一位小数的最终标记。"""
    svg = build()
    svg = svg.replace('id="whp-h"', 'id="wh-board-mask"').replace('url(#whp-h)', 'url(#wh-board-mask)')
    svg = svg.replace('class="whp-svg" ', '')
    svg = re.sub(r'(\d+\.\d)\d+', r'\1', svg)
    svg = re.sub(r'(\d)\.0(?=["\'htvMLz ,])', r'\1', svg)
    return svg


def main(argv):
    svg = render()
    if len(argv) > 1:
        try:
            with open(argv[1], 'w', encoding='utf-8') as f:
                f.write(svg)
        except OSError as exc:                     # 写盘失败
            print('write failed: %s' % exc, file=sys.stderr)
            return 2
        print('written %s (%d bytes)' % (argv[1], len(svg)))
    else:
        print(svg)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
