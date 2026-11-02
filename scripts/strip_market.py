#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国金门户 · 清除简报中的市场影响分析
====================================

政策/法案/裁决速览只保留事实、程序与法律依据，不再包含市场影响、投资含义
或定价判断。本脚本对存量 data/policy/*items.json 做一次性清洗：

  1. 删除【对金融市场的预期影响】整段（至下一个【…】段头或文末）；
  2. 删除以「对市场意味着什么：」「总体判断：」「市场影响：」等开头的行
     （含 · / • / - / 1. 等项目符号）；
  3. 压缩多余空行。

用法：
    python3 scripts/strip_market.py --dry-run   # 只统计并抽样，不写盘
    python3 scripts/strip_market.py             # 写回 data/policy/*.json

退出码：0 清洗完成且无残留；1 有残留或清洗后过短，需人工复核。
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import portal_store as store          # noqa: E402

BOARDS = ('whitehouse', 'congress', 'agencies', 'judicial')

# 段头（可带项目符号）
MARKET_HEAD_RE = re.compile(
    r'^\s*(?:[·•\-*]\s*)?【\s*对金融市场的预期影响\s*】')
# 市场结论行（可带项目符号或序号）
# 注意：「市场影响」等词后不一定紧跟冒号（如「市场影响取决于支付方归属：…」），
# 凡以此类词开头的整行均属市场分析，一律删除。
MARKET_LINE_RE = re.compile(
    r'^\s*(?:[·•\-*]\s*|\d+[.、]\s*)?'
    r'(?:对市场意味着什么|总体判断|市场影响|市场含义|对金融市场的影响)')
ANY_HEAD_RE = re.compile(r'^\s*【[^】]+】')

# 清洗后用于自检的残留词
RESIDUAL_RE = re.compile(
    r'对市场意味着什么|对金融市场的预期影响|总体判断|'
    r'受益方向|承压方向|关键前提|信号节点|风险提示|定价影响|市场影响')


def strip_body(body):
    """返回 (清洗后正文, 删除的段数, 删除的行数)。"""
    lines = (body or '').replace('\r\n', '\n').replace('\r', '\n').split('\n')
    out = []
    removed_sections = removed_lines = 0
    i = 0
    while i < len(lines):
        ln = lines[i]
        if MARKET_HEAD_RE.match(ln):
            removed_sections += 1
            i += 1
            # 跳过该段正文，直到下一个【…】段头或文末
            while i < len(lines) and not ANY_HEAD_RE.match(lines[i]):
                i += 1
            continue
        if MARKET_LINE_RE.match(ln):
            removed_lines += 1
            i += 1
            continue
        out.append(ln)
        i += 1
    text = '\n'.join(out)
    text = re.sub(r'[ \t]+\n', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text, removed_sections, removed_lines


def main():
    ap = argparse.ArgumentParser(description='清除速览中的市场影响分析')
    ap.add_argument('--dry-run', action='store_true', help='只统计并抽样，不写盘')
    args = ap.parse_args()

    payload = store.load_policy()
    changed = removed_sec = removed_ln = 0
    emptied = short = residual = 0
    samples = []
    issues = []

    for board in BOARDS:
        items = (payload.get('boards', {}).get(board) or {}).get('items') or []
        b_changed = 0
        for it in items:
            brief = it.get('brief') or {}
            body = brief.get('body') or ''
            if not body:
                continue
            new_body, ns, nl = strip_body(body)
            if ns or nl:
                changed += 1
                b_changed += 1
                removed_sec += ns
                removed_ln += nl
                if len(samples) < 6:
                    samples.append((board, it.get('title') or '', len(body), len(new_body)))
                if not new_body:
                    emptied += 1
                    issues.append(('清洗后为空', board, it.get('title') or '', it.get('url') or ''))
                elif len(new_body) < 60:
                    short += 1
                    issues.append(('清洗后过短(%d字)' % len(new_body), board,
                                   it.get('title') or '', it.get('url') or ''))
                if RESIDUAL_RE.search(new_body):
                    residual += 1
                    issues.append(('仍有市场表述', board, it.get('title') or '',
                                   it.get('url') or ''))
                if not args.dry_run:
                    brief['body'] = new_body
                    it['brief'] = brief
        if b_changed:
            print('%-11s 清洗 %3d 条' % (board, b_changed))

    print('\n合计清洗 %d 条 · 删除市场段 %d 处 · 删除市场结论行 %d 行'
          % (changed, removed_sec, removed_ln))
    print('清洗后为空 %d 条 · 少于 60 字 %d 条 · 仍有市场表述 %d 条'
          % (emptied, short, residual))

    if samples:
        print('\n抽样（清洗前 → 清洗后字数）：')
        for board, title, before, after in samples:
            print('  [%s] %s  %d → %d' % (board, title[:36], before, after))
    if issues:
        print('\n需人工复核：')
        for why, board, title, url in issues[:20]:
            print('  [%s] %s  %s' % (why, title[:40], url[:90]))
        if len(issues) > 20:
            print('  …… 其余 %d 条' % (len(issues) - 20))

    if args.dry_run:
        print('\n[dry-run] 未写盘。')
        return 1 if issues else 0

    store.save_policy(payload)
    print('\n已写回 data/policy/*.json（brief.body）。')
    return 1 if issues else 0


if __name__ == '__main__':
    sys.exit(main())
