#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国金门户 · 官方编号回填
=======================

为 data/policy/*items.json 的每一条目补写 official_no 字段：

  whitehouse / agencies  联邦公报文档号（URL 日期段之后的路径段）
                         例：2026-22386、C1-2026-20019
  congress               法案/决议编号
                         例：H.R. 8707、S. 5476、H.Res. 310、S.J.Res. 32
  judicial               裁判文书无案号字段，留空（页面不显示）

用法：
    python3 scripts/backfill_official_no.py --dry-run   # 只统计，不写盘
    python3 scripts/backfill_official_no.py             # 写回 data/policy/*.json

退出码：0 全部可解析；1 存在无法解析的条目（已保留原值，需人工核）。
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import portal_store as store          # noqa: E402

BOARDS = ('whitehouse', 'congress', 'agencies', 'judicial')

# 联邦公报文档 URL：/documents/YYYY/MM/DD/<文档号>/...
# 文档号形如 2026-22386，更正文件形如 C1-2026-20019
FR_DOC_RE = re.compile(r'federalregister\.gov/documents/\d{4}/\d{2}/\d{2}/([^/?#]+)')

# 国会法案 URL：/bill/<届次>th-congress/<类型段>/<编号>
CG_BILL_RE = re.compile(r'congress\.gov/bill/(\d+)th-congress/([a-z-]+)/([A-Za-z0-9]+)')

# 类型段 → 正式编号前缀
CG_DESIG = {
    'house-bill': 'H.R.',
    'senate-bill': 'S.',
    'house-resolution': 'H.Res.',
    'senate-resolution': 'S.Res.',
    'house-joint-resolution': 'H.J.Res.',
    'senate-joint-resolution': 'S.J.Res.',
    'house-concurrent-resolution': 'H.Con.Res.',
    'senate-concurrent-resolution': 'S.Con.Res.',
}


def derive(board, item):
    """返回该条目的官方编号；无法解析返回 None。"""
    url = item.get('url') or ''
    if board in ('whitehouse', 'agencies'):
        m = FR_DOC_RE.search(url)
        return m.group(1) if m else None
    if board == 'congress':
        m = CG_BILL_RE.search(url)
        if not m:
            return None
        prefix = CG_DESIG.get(m.group(2))
        if not prefix:
            return None
        return '%s %s' % (prefix, m.group(3))
    if board == 'judicial':
        # 数据源只有意见书链接，没有正式案号（docket number），留空不臆造
        return ''
    return None


def main():
    ap = argparse.ArgumentParser(description='回填政策条目的官方编号')
    ap.add_argument('--dry-run', action='store_true', help='只统计，不写盘')
    args = ap.parse_args()

    payload = store.load_policy()
    total = filled = miss = 0
    problems = []
    for board in BOARDS:
        items = (payload.get('boards', {}).get(board) or {}).get('items') or []
        b_total = b_filled = b_miss = 0
        samples = []
        for it in items:
            total += 1
            b_total += 1
            no = derive(board, it)
            if no is None:
                b_miss += 1
                miss += 1
                if len(problems) < 12:
                    problems.append((board, it.get('url') or '', it.get('title') or ''))
                continue
            b_filled += 1
            filled += 1
            if len(samples) < 3 and no:
                samples.append(no)
            if not args.dry_run:
                it['official_no'] = no
        print('%-11s %4d 条 · 可写 %4d · 无法解析 %d %s'
              % (board, b_total, b_filled, b_miss,
                 ('· 样例：' + '、'.join(samples)) if samples else ''))

    print('\n合计 %d 条 · 可写 %d · 无法解析 %d' % (total, filled, miss))
    if problems:
        print('\n无法解析（已保留原值，需人工核）：')
        for board, url, title in problems:
            print('  [%s] %s  %s' % (board, title[:40], url[:100]))

    if args.dry_run:
        print('\n[dry-run] 未写盘。')
        return 1 if miss else 0

    store.save_policy(payload)
    print('\n已写回 data/policy/*.json（official_no）。')
    return 1 if miss else 0


if __name__ == '__main__':
    sys.exit(main())
