#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国金门户 · 多 AI 协作守门工具

用途：多个 AI 平台并行改 index.html 时，防止越界与不可逆破坏。
这是协作协议的执行者——AGENTS.md 定了规矩，本脚本负责查。
不设区块认领：所有平台都可改全站，只拦「不能改的地方」（数据块与脚本区）。

两个子命令：
  progress  看别人改了什么：工作区状态、与远端差异、最近设计提交（数据全部来自 git）
  verify    检查当前改动是否落在允许的设计区间（默认相对 HEAD）

用法：
  python3 scripts/design_check.py progress
  python3 scripts/design_check.py verify
  python3 scripts/design_check.py verify --allow-data     # 定时任务自检时用
  python3 scripts/design_check.py regions                 # 打印区块边界

退出码：
  0  通过
  1  有越界（必须修）
  2  无法检查（文件缺失、非 git 环境等）

无第三方依赖，Python 3.8+。
"""

import os
import re
import subprocess
import sys
import argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORTAL = os.path.join(ROOT, "index.html")

# 令牌表允许的颜色（DESIGN.md §2）。大小写不敏感比对。
ALLOWED_COLORS = {
    # 墨 / 纸 / 卡
    "#0b1724", "#12202f", "#f2eee6", "#ece7dc", "#fbf9f4",
    # 铜金
    "#a8823c", "#c9a86a", "#c9962e", "#e3b95f",
    # 蓝
    "#2f6fb8", "#4a8ad1", "#e7eef8", "#2f5d8a", "#9a4a3c",
    # 中性
    "#ffffff", "#fff", "#000000", "#000",
    "#1f3450", "#2a4a6e", "#4a8ad1", "#0088ff",
}

# 首页区块锚点（顺序敏感）
HOME_ANCHORS = [
    ("columns",  'id="columns"'),
    ("coverage", 'id="coverage"'),
    ("notes",    'id="notes"'),
    ("about",    'id="about"'),
]

VIEW_ANCHORS = {
    "us":       'id="view-us"',
    "eu":       'id="view-eu"',
    "board":    'id="view-board"',
    "election": 'id="view-election"',
    "parties":  'id="view-parties"',
}

STYLE_ANCHORS = {
    "style:base":       None,                      # 第一个无 id 的 <style>
    "style:v2-portal":  'id="v2-portal"',
    "style:v3-us-board": 'id="v3-us-board"',
    "style:v4-parties": 'id="v4-parties"',
}

DATA_ANCHORS = {
    "data:election": 'id="election-data"',
    "data:calendar": 'id="us-cal-data"',
}


def read_lines(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read().split("\n")


def find_line(lines, needle, start=0):
    """返回首个包含 needle 的 1-based 行号，找不到返回 None。"""
    for i in range(start, len(lines)):
        if needle in lines[i]:
            return i + 1
    return None


def find_close(lines, start_line, closer="</script>"):
    """从 start_line（1-based）向后找 closer，返回其 1-based 行号。"""
    for i in range(start_line - 1, len(lines)):
        if closer in lines[i]:
            return i + 1
    return len(lines)


def build_regions(lines):
    """动态计算区块边界，不依赖硬编码行号。返回 [(name, start, end, kind, editable)]"""
    regions = []

    # 1) 样式块
    style_starts = []
    for i, ln in enumerate(lines):
        if "<style" in ln and "</style>" not in ln:
            style_starts.append(i + 1)
    for idx, s in enumerate(style_starts):
        e = find_close(lines, s, "</style>")
        header = lines[s - 1][:200]
        name = "style:base"
        for k, needle in STYLE_ANCHORS.items():
            if needle and needle in header:
                name = k
                break
        regions.append((name, s, e, "style", True))

    # 2) 内嵌 JSON 数据块（最高优先级禁区）
    for name, needle in DATA_ANCHORS.items():
        s = find_line(lines, needle)
        if s:
            e = find_close(lines, s, "</script>")
            regions.append((name, s, e, "data", False))

    # 3) 业务脚本区：第一个非 application/json 的 <script>，到最后一个 </script>
    script_start = None
    for i, ln in enumerate(lines):
        if "<script" in ln and "application/json" not in ln:
            script_start = i + 1
            break
    if script_start:
        last_close = 0
        for i, ln in enumerate(lines):
            if "</script>" in ln:
                last_close = i + 1
        regions.append(("script:app", script_start, last_close, "script", False))

    # 4) 子视图（末端必须止于最早的数据块或脚本区，不能延到文件尾）
    ordered = sorted(
        [(n, find_line(lines, a)) for n, a in VIEW_ANCHORS.items() if find_line(lines, a)],
        key=lambda x: x[1],
    )
    hard_bounds = [s for _n, s, _e, kind, _ed in regions if kind in ("data", "script")]
    dom_ceiling = min(hard_bounds) if hard_bounds else len(lines)
    for idx, (name, s) in enumerate(ordered):
        if idx + 1 < len(ordered):
            e = ordered[idx + 1][1] - 1
        else:
            e = dom_ceiling - 1
        regions.append((("view:" + name), s, e, "dom", True))

    # 5) 首页区块：以 #columns 起，到最早视图前
    home = [(n, find_line(lines, a)) for n, a in HOME_ANCHORS]
    home = [x for x in home if x[1]]
    if home:
        home_ceiling = min([ordered[0][1] if ordered else len(lines)]
                           + hard_bounds)
        for idx, (name, s) in enumerate(home):
            e = home[idx + 1][1] - 1 if idx + 1 < len(home) else home_ceiling - 1
            regions.append((("home:" + name), s, e, "dom", True))
        # mast 页头：<header class="masthead"> 到 #columns 之前
        mast = find_line(lines, 'class="masthead"')
        if mast:
            regions.append(("home:mast", mast, home[0][1] - 1, "dom", True))

    regions.sort(key=lambda r: r[1])
    return regions


def region_of(regions, line):
    """返回命中区块。禁区优先于可编辑区，避免宽区间遮蔽窄区间。"""
    hit = None
    for name, s, e, kind, editable in regions:
        if s <= line <= e:
            if not editable:
                return name, kind, editable
            if hit is None:
                hit = (name, kind, editable)
    return hit if hit else (None, None, True)


def git(args):
    try:
        out = subprocess.run(
            ["git"] + args, cwd=ROOT, capture_output=True, text=True, timeout=120
        )
        return out.returncode, out.stdout, out.stderr
    except Exception as exc:                                  # noqa: BLE001
        return 1, "", str(exc)


def parse_hunks(diff_text):
    """解析 git diff -U0，返回 [(new_start, new_count, added_lines, removed_lines)]"""
    hunks = []
    lines = diff_text.split("\n")
    i = 0
    while i < len(lines):
        m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", lines[i])
        if not m:
            i += 1
            continue
        new_start = int(m.group(1))
        new_count = int(m.group(2)) if m.group(2) is not None else 1
        added, removed = [], []
        i += 1
        while i < len(lines) and not lines[i].startswith("@@"):
            if lines[i].startswith("+") and not lines[i].startswith("+++"):
                added.append(lines[i][1:])
            elif lines[i].startswith("-") and not lines[i].startswith("---"):
                removed.append(lines[i][1:])
            i += 1
        hunks.append((new_start, new_count, added, removed))
    return hunks


COLOR_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")


def cmd_regions(_args):
    lines = read_lines(PORTAL)
    regions = build_regions(lines)
    print(f"{'区块':22s} {'起':>6s} {'止':>6s} {'行数':>6s}  类型      可编辑")
    print("-" * 70)
    for name, s, e, kind, editable in regions:
        flag = "是" if editable else "否（禁区）"
        print(f"{name:22s} {s:6d} {e:6d} {e - s + 1:6d}  {kind:9s} {flag}")
    print(f"\n文件共 {len(lines)} 行")
    return 0


def cmd_verify(args):
    if not os.path.exists(PORTAL):
        print("错误：找不到 index.html", file=sys.stderr)
        return 2

    code, diff_text, err = git(["diff", "-U0", "HEAD", "--", "index.html"])
    if code != 0:
        print(f"错误：git diff 失败：{err.strip()}", file=sys.stderr)
        return 2
    if not diff_text.strip():
        print("index.html 无改动。")
        return 0

    lines = read_lines(PORTAL)
    regions = build_regions(lines)
    hunks = parse_hunks(diff_text)

    violations, warnings = [], []
    touched = {}

    for new_start, new_count, added, removed in hunks:
        # 该 hunk 覆盖的行区间（新增侧）
        span_end = new_start + max(new_count, 1) - 1
        for ln in range(new_start, span_end + 1):
            name, kind, editable = region_of(regions, ln)
            if name:
                touched.setdefault(name, 0)
                touched[name] += 1
            if not editable and not args.allow_data:
                violations.append(
                    f"第 {ln} 行落入禁区 {name or '未知'}"
                    + ("（内嵌数据块）" if kind == "data" else "（业务脚本区）")
                )

        blob_add = "\n".join(added)
        blob_del = "\n".join(removed)

        # 新增元素不得自带 data-page-node-id
        for a in added:
            if "data-page-node-id" in a:
                warnings.append(f"第 {new_start} 行附近新增了 data-page-node-id（新增元素不应携带）")
                break

        # 不得删除既有 data-page-node-id
        if "data-page-node-id" in blob_del:
            violations.append(f"第 {new_start} 行附近删除了既有 data-page-node-id（会破坏上游编辑器绑定）")

        # 颜色必须来自令牌表
        for c in COLOR_RE.findall(blob_add):
            if c.lower() not in ALLOWED_COLORS:
                warnings.append(f"第 {new_start} 行附近引入了令牌表外的颜色 {c}")

        # 禁用的格式化痕迹：整块重排
        if len(removed) > 300 and len(added) > 300 and abs(len(added) - len(removed)) < 10:
            warnings.append(
                f"第 {new_start} 行附近一次改动 {len(added)}/{len(removed)} 行，疑似格式化工具重排"
            )

    print("=" * 70)
    print("改动落点")
    print("=" * 70)
    if touched:
        for name, cnt in sorted(touched.items(), key=lambda x: -x[1]):
            print(f"  {name:24s} {cnt:5d} 行")
    else:
        print("  （未能归属到任何区块）")

    if warnings:
        print("\n" + "=" * 70)
        print(f"警告 {len(warnings)} 项（不阻断提交，但请确认）")
        print("=" * 70)
        for w in warnings:
            print(f"  ! {w}")

    if violations:
        print("\n" + "=" * 70)
        print(f"越界 {len(violations)} 项（必须修）")
        print("=" * 70)
        for v in violations:
            print(f"  x {v}")
        print("\n提示：数据块与脚本区由定时任务与页面逻辑占用，设计改动不应触碰。")
        print("      若确为任务本身写入，用 verify --allow-data 复核。")
        return 1

    print("\n通过：改动均在允许区间内。")
    print("提醒：仍需人工过一遍 375px 与五个视图（见 DESIGN.md §8）。")
    return 0


def cmd_progress(_args):
    """输出「别人改了什么」——进度同步的主入口，数据全部来自 git，不读手工文档。"""
    print("=" * 70)
    print("设计进度")
    print("=" * 70)

    # 1) 工作区
    code, out, _ = git(["status", "--porcelain"])
    dirty = [l for l in out.split("\n") if l.strip()] if code == 0 else []
    html_dirty = [l for l in dirty if l.endswith("index.html") or "index.html" in l]
    print("\n[工作区]")
    if not dirty:
        print("  干净")
    else:
        if html_dirty:
            print(f"  ! index.html 有未提交改动（{len(html_dirty)} 处）→ 先提交或 stash 再拉取")
            code2, stat, _ = git(["diff", "--shortstat", "HEAD", "--", "index.html"])
            if code2 == 0 and stat.strip():
                print(f"    {stat.strip()}")
        others = len(dirty) - len(html_dirty)
        if others:
            print(f"  另有 {others} 个非 index.html 改动（多为数据文件或新文件）")

    # 2) 与远端的关系
    print("\n[与远端]")
    c, ab, _ = git(["rev-list", "--left-right", "--count", "origin/main...HEAD"])
    if c == 0 and ab.strip():
        parts = ab.split()
        if len(parts) == 2:
            behind, ahead = int(parts[0]), int(parts[1])
            print(f"  落后 origin/main {behind} 个提交 / 领先 {ahead} 个")
            if behind:
                print("  → 先 git pull --rebase 再动手")
            if ahead:
                print("  → 有未推送的本地提交")
            if not behind and not ahead:
                print("  与远端一致")
    else:
        print("  无法比较（可能未配置 origin 或尚无 origin/main）")

    # 3) 最近设计提交
    print("\n[最近设计改动]")
    c3, log_out, _ = git([
        "log", "-12", "--grep=^design(",
        "--format=%h|%ad|%s", "--date=format:%m-%d %H:%M", "--shortstat",
    ])
    if c3 == 0 and log_out.strip():
        blocks = [b for b in log_out.split("\n\n") if b.strip()]
        for b in blocks:
            rows = [r for r in b.strip().split("\n") if r.strip()]
            head = rows[0]
            h, when, msg = (head.split("|", 2) + ["", "", ""])[:3]
            delta = ""
            for r in rows[1:]:
                m = re.search(r"(\d+) insertion", r)
                d = re.search(r"(\d+) deletion", r)
                if m or d:
                    delta = f"+{m.group(1) if m else 0}/-{d.group(1) if d else 0}"
            print(f"  {h}  {when}  {msg}  {delta}")
    else:
        print("  （暂无 design(*) 提交）")

    # 4) 同期数据更新任务（非设计，仅作噪声参考）
    c4, cnt, _ = git(["rev-list", "--count", "HEAD", "--grep=日更"])
    if c4 == 0 and cnt.strip().isdigit():
        print(f"\n[同期数据任务] 历史累计 {cnt.strip()} 条日更提交（非设计改动，可忽略）")

    print("\n" + "-" * 70)
    print("线上为准：https://liulijia123456.github.io/sinolink-portal/  （push 后约 40 秒生效）")
    print("判断别人改了什么，先看上面这份列表，不要看文档。")
    return 0


def main():
    ap = argparse.ArgumentParser(description="国金门户 · 多 AI 协作守门工具")
    sub = ap.add_subparsers(dest="cmd")

    p1 = sub.add_parser("verify", help="检查当前改动是否越界")
    p1.add_argument("--allow-data", action="store_true", help="允许改动内嵌数据块（任务自检用）")
    p1.set_defaults(func=cmd_verify)

    p2 = sub.add_parser("progress", help="看别人改了什么（进度同步）")
    p2.set_defaults(func=cmd_progress)

    p3 = sub.add_parser("regions", help="打印区块边界")
    p3.set_defaults(func=cmd_regions)

    args = ap.parse_args()
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
