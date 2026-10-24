#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国金门户 · 修复国会板块决议类条目的错误 URL
============================================

背景：congress.gov 对法案与三类决议使用不同 URL 路径段。早期抓取脚本统一写成
`-bill`，导致决议条目指向同编号的法案，例如 S.Res.951（指定全国婴儿死亡率关注月）
被写成 senate-bill/951，实际指向 S.951「Stop Comstock Act」。后果是点击「原文」
跳错、简报素材也抓成别的法案。

本脚本逐条用官方 API 核实：按 kind 推断候选类型，查询该法案/决议的官方标题，
与条目现有 title 比对一致后才改写 URL，避免误改。

用法：
  python3 scripts/fix_congress_urls.py            # 检查并修复，直接回写 index.html
  python3 scripts/fix_congress_urls.py --dry-run  # 只报告，不改写

环境变量：CONGRESS_API_KEY（优先级同 fetch_policy.py）
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from difflib import SequenceMatcher

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, 'scripts'))
INDEX_HTML = os.path.join(BASE_DIR, 'index.html')

# kind（中文标签）→ 候选 bill type，按可能性排序
KIND_TO_TYPES = {
    '参议院法案': ['S'], '众议院法案': ['HR'],
    '参议院决议': ['SRES', 'S'], '众议院决议': ['HRES', 'HR'],
    '参院联合决议': ['SJRES', 'S'], '众院联合决议': ['HJRES', 'HR'],
    '参院共同决议': ['SCONRES', 'S'], '众院共同决议': ['HCONRES', 'HR'],
}
# bill type → congress.gov URL 路径段
URL_SLUG = {
    'S': 'senate-bill', 'HR': 'house-bill',
    'SRES': 'senate-resolution', 'HRES': 'house-resolution',
    'SJRES': 'senate-joint-resolution', 'HJRES': 'house-joint-resolution',
    'SCONRES': 'senate-concurrent-resolution',
    'HCONRES': 'house-concurrent-resolution',
}
BILL_URL_RE = re.compile(r'/bill/(\d+)th-congress/([a-z-]+)/([A-Za-z0-9]+)')


def api_key():
    try:
        import fetch_policy as fp
        return fp._load_congress_key() or ''
    except Exception:
        return os.environ.get('CONGRESS_API_KEY', '')


def http_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'sinolink-portal/1.0'})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


def official_title(congress, btype, number, key):
    """查询官方标题；失败返回 None。"""
    url = ('https://api.congress.gov/v3/bill/%s/%s/%s?api_key=%s&format=json'
           % (congress, btype.lower(), number, key))
    try:
        d = http_json(url)
        return ((d.get('bill') or {}).get('title') or '').strip()
    except Exception:
        return None


def norm(s):
    return re.sub(r'[^a-z0-9 ]', '', (s or '').lower()).strip()


def similar(a, b):
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true', help='只报告不改写')
    args = ap.parse_args()

    key = api_key()
    if not key:
        print('[失败] 未取得 CONGRESS_API_KEY，无法核实官方标题', file=sys.stderr)
        return 2

    import fetch_policy as fp
    with open(INDEX_HTML, 'r', encoding='utf-8') as f:
        html = f.read()
    payload = fp.extract_payload(html)
    items = payload['boards']['congress']['items']

    fixed, skipped, unchanged = [], [], 0
    for it in items:
        url = it.get('url', '')
        m = BILL_URL_RE.search(url)
        if not m:
            continue
        congress, slug, number = m.group(1), m.group(2), m.group(3)
        kind = it.get('kind', '')
        cands = KIND_TO_TYPES.get(kind)
        if not cands:
            continue
        # 仅当与「首选类型」的路径段一致才算正确。
        # 注意不能用 any(...)——cands 含法案类型作兜底，会把错误的 senate-bill 判为正确。
        if URL_SLUG[cands[0]] == slug:
            unchanged += 1
            continue

        title = it.get('title', '')
        best = None
        for btype in cands:
            t = official_title(congress, btype, number, key)
            if t:
                score = similar(title, t)
                if best is None or score > best[1]:
                    best = (btype, score, t)
            time.sleep(0.6)

        if not best:
            skipped.append((number, 'API 未返回任何候选标题'))
            continue
        btype, score, off_title = best
        # 相似度阈值 0.6：决议与法案标题通常完全不同，足以区分
        if score < 0.6:
            skipped.append((number, '标题不匹配（%.2f）：%s' % (score, off_title[:60])))
            continue

        new_url = 'https://www.congress.gov/bill/%sth-congress/%s/%s' % (
            congress, URL_SLUG[btype], number)
        fixed.append((number, kind, slug, URL_SLUG[btype], round(score, 2), off_title[:70]))
        it['url'] = new_url
        time.sleep(0.6)

    print('[统计] 国会板块 %d 条：路径段已正确 %d，本次修复 %d，跳过 %d'
          % (len(items), unchanged, len(fixed), len(skipped)))
    for n, kind, old, new, s, t in fixed:
        print('  修复 %s（%s）：%s → %s  相似度 %.2f  %s' % (n, kind, old, new, s, t))
    for n, why in skipped:
        print('  跳过 %s：%s' % (n, why))

    if args.dry_run:
        print('[dry-run] 未改写文件')
        return 0
    if not fixed:
        print('[完成] 无需改写')
        return 0
    fp.write_payload(payload)
    print('[完成] 已回写 index.html，修复 %d 条' % len(fixed))
    return 0


if __name__ == '__main__':
    sys.exit(main())
