#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为国会条目补齐「提出日 / 进入本环节日 / 提出者 / 委员会」四个字段。

背景：抓取脚本原先只存 updateDate（记录写库时间），列表里的日期因此与立法
进程无关。本脚本按条目 url 逐条调用 congress.gov 详情接口，取回
introducedDate、latestAction.actionDate 与 sponsors，写回 items。

可重入：只处理缺 introduced 的条目，每处理 20 条落盘一次，中断后重跑即可。
限速：默认 1.05 秒/条（官方建议 5000 次/小时），838 条约 15 分钟。

用法：
    python3 scripts/enrich_congress.py                # 补齐全部缺失条目
    python3 scripts/enrich_congress.py --limit 60     # 只补 60 条（增量用）
    python3 scripts/enrich_congress.py --dry-run      # 只统计缺多少
"""
import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ITEMS_PATH = os.path.join(ROOT, 'data', 'policy', 'congress-items.json')
KEY_PATH = os.path.join(ROOT, 'scripts', '.congress_api_key')
API = 'https://api.congress.gov/v3/bill'

PARTY_CN = {'R': 'R', 'D': 'D', 'I': 'I', 'ID': 'ID'}

# 全局限速闸门：congress.gov 每 Key 每小时 5000 次，取 0.75 秒/次（约 4800 次/小时）。
# 放在锁里 sleep，保证多线程下的整体速率仍是每 0.75 秒一次。
_gate_lock = threading.Lock()
_next_at = [0.0]


def _rate_gate(interval=0.75):
    with _gate_lock:
        now = time.time()
        if now < _next_at[0]:
            time.sleep(_next_at[0] - now)
        _next_at[0] = max(now, _next_at[0]) + interval


def load_key():
    with open(KEY_PATH, encoding='utf-8') as f:
        return f.read().strip()


def api_get(url, key, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'sinolink-portal/1.0'})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and i < retries - 1:
                time.sleep(3 * (i + 1))
                continue
            raise
        except Exception:
            if i < retries - 1:
                time.sleep(2 * (i + 1))
                continue
            raise
    return None


def parse_committee(text):
    """从最新行动文本里抽委员会名（英文原名，不译）。"""
    if not text:
        return ''
    pats = [
        r'Referred to the (?:House|Senate) Committee on (.+?)(?:, and in addition|,? and in addition|\.|$)',
        r'Referred to the Committee on (.+?)(?:, and in addition|\.|$)',
        r'Read twice and referred to the Committee on (.+?)(?:, and in addition|\.|$)',
        r'Referred to the Subcommittee on (.+?)(?:\.|$)',
        r'Committee on (.+?)\. (?:Ordered to be reported|Hearings held)',
    ]
    for p in pats:
        m = re.search(p, text, re.I)
        if m:
            return m.group(1).strip(' ,.')
    return ''


def parse_sponsor(sp):
    """把 'Rep. Kean, Thomas H. [R-NJ-7]' 拆成姓名与党派州别。"""
    if not sp:
        return '', '', ''
    full = sp.get('fullName') or ''
    party = sp.get('party') or ''
    state = sp.get('state') or ''
    district = sp.get('district')
    name = full
    m = re.match(r'^(?:Rep\.|Sen\.|Del\.|Res\.)\s*(.+?)\s*\[[^\]]*\]\s*$', full)
    if m:
        name = m.group(1)
    if ',' in name:
        last, first = [x.strip() for x in name.split(',', 1)]
        name = (first + ' ' + last).strip()
    tag = ''
    if party and state:
        tag = '%s-%s' % (party, state) + ('-%s' % district if district else '')
    return name, party, tag


def enrich_one(it, key):
    """补齐单条；返回 True 表示成功拿到详情。线程安全：只写各自的 dict。"""
    url = it.get('url') or ''
    m = re.search(r'/bill/(\d+)th-congress/([a-z-]+)/(\d+)', url)
    if not m:
        it['introduced'] = it.get('introduced') or it.get('date') or ''
        it['stage_date'] = it.get('stage_date') or it.get('date') or ''
        return False
    cg, slug, num = m.groups()
    btype = {'house-bill': 'hr', 'senate-bill': 's', 'house-resolution': 'hres',
             'senate-resolution': 'sres', 'house-joint-resolution': 'hjres',
             'senate-joint-resolution': 'sjres', 'house-concurrent-resolution': 'hconres',
             'senate-concurrent-resolution': 'sconres'}.get(slug)
    if not btype:
        it['introduced'] = it.get('introduced') or it.get('date') or ''
        it['stage_date'] = it.get('stage_date') or it.get('date') or ''
        return False
    _rate_gate()
    d = api_get('%s/%s/%s/%s?api_key=%s&format=json' % (API, cg, btype, num, key), key)
    b = (d or {}).get('bill') or {}
    if not b:
        return False
    it['introduced'] = it.get('introduced') or ((b.get('introducedDate') or '')[:10]) or it.get('date', '')
    la = b.get('latestAction') or {}
    it['stage_date'] = it.get('stage_date') or ((la.get('actionDate') or '')[:10]) or it.get('date', '')
    sps = b.get('sponsors') or []
    if sps:
        name, party, tag = parse_sponsor(sps[0])
        it['sponsor'] = name
        it['sponsor_party'] = party
        it['sponsor_tag'] = tag
    it['committee'] = parse_committee(la.get('text') or it.get('agency') or '')
    return True


def enrich(items, key, limit=None, sleep=1.05, dry_run=False, log=None, need='introduced',
           workers=1, batch=60):
    if log is None:
        log = lambda s, *a: print(s % a if a else s, flush=True)
    if need == 'sponsor':
        todo = [it for it in items if not it.get('sponsor') and it.get('url')]
    else:
        todo = [it for it in items if not it.get('introduced')]
    log('待补齐 %d 条（总计 %d）', len(todo), len(items))
    if dry_run:
        return 0
    if limit:
        todo = todo[:limit]
    done = 0
    if workers > 1:
        from concurrent.futures import ThreadPoolExecutor
        for i in range(0, len(todo), batch):
            part = todo[i:i + batch]
            with ThreadPoolExecutor(max_workers=workers) as ex:
                res = list(ex.map(lambda x: _safe_one(x, key), part))
            done += sum(1 for r in res if r)
            log('  已处理 %d / %d', min(i + batch, len(todo)), len(todo))
            time.sleep(sleep)
        return done
    for it in todo:
        try:
            if enrich_one(it, key):
                done += 1
        except Exception as e:
            log('  跳过 %s：%s', it.get('official_no'), e)
        time.sleep(sleep)
    return done


def _safe_one(it, key):
    try:
        return enrich_one(it, key)
    except Exception:
        return False


def save(items):
    """写回 items。保留 schema / board / updated_at 三个外层字段——
    本脚本只补字段、不新增条目，不该改动抓取时间，也不能破坏 portal_store 的封装。"""
    doc = {}
    if os.path.exists(ITEMS_PATH):
        try:
            with open(ITEMS_PATH, encoding='utf-8') as f:
                doc = json.load(f)
        except Exception:
            doc = {}
    doc.setdefault('schema', 1)
    doc.setdefault('board', 'congress')
    doc['items'] = items
    doc['count'] = len(items)
    tmp = ITEMS_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write('\n')
    with open(tmp, encoding='utf-8') as f:
        json.load(f)
    os.replace(tmp, ITEMS_PATH)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=None)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--sleep', type=float, default=1.05)
    ap.add_argument('--workers', type=int, default=1)
    args = ap.parse_args()

    with open(ITEMS_PATH, encoding='utf-8') as f:
        data = json.load(f)
    items = data.get('items') or []
    key = load_key()

    # 每 20 条落盘，中断不丢进度
    todo_n = sum(1 for it in items if not it.get('introduced'))
    if todo_n == 0:
        print('无需补齐，全部条目已有 introduced 字段')
        return 0
    done = 0
    chunk = 20 if not args.limit else min(20, args.limit)
    remaining = args.limit
    while todo_n > 0:
        step = chunk if remaining is None else min(chunk, remaining)
        if step <= 0:
            break
        n = enrich(items, key, limit=step, sleep=args.sleep, dry_run=args.dry_run, workers=args.workers)
        if args.dry_run:
            break
        done += n
        if remaining is not None:
            remaining -= n
        save(items)
        print('已保存，累计完成 %d 条' % done, flush=True)
        todo_n = sum(1 for it in items if not it.get('introduced'))
        if n == 0:
            break
    print('完成：本次补齐 %d 条，仍缺 %d 条' % (done, sum(1 for it in items if not it.get('introduced'))))
    return 0


if __name__ == '__main__':
    sys.exit(main())
