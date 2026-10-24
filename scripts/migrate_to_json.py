#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国金门户 · 一次性迁移：把 index.html 内嵌的 policy-data 拆成独立 JSON
=====================================================================

用途：
    把页面里 <script type="application/json" id="policy-data"> 的三个板块
    （whitehouse / congress / agencies）拆成 6 个数据文件 + 1 个清单，
    页面改造后不再从 HTML 读数据。

    这是一个**迁移 / 回灌**工具，正常日更不需要运行它。
    日更由 scripts/update_policy.py（或 fetch_policy.py 的 JSON 模式）负责。

用法：
    python3 scripts/migrate_to_json.py --source index.html --out data/policy
    python3 scripts/migrate_to_json.py --source index.html --out data/policy --dry-run
    # 回灌历史：把另一个 HTML 里存在、主源没有的条目按 url 补进来
    python3 scripts/migrate_to_json.py --source A.html --merge-source B.html --out data/policy

输出：
    data/policy/whitehouse-items.json      {"schema":1,"board":...,"updated_at":...,"items":[...]}
    data/policy/whitehouse-overview.json   {"schema":1,"board":...,"weekly":{...},"monthly":{...}}
    data/policy/{congress,agencies}-*.json
    data/policy/manifest.json              {"schema":1,"built_at":...,"boards":{key:{count,updated_at}}}

退出码：0 成功；1 源文件缺少数据块；2 校验不通过（条数为 0 / 字段缺失）。
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

SCHEMA = 1
BOARDS = ('whitehouse', 'congress', 'agencies')
BOARD_LABEL = {'whitehouse': '白宫政策', 'congress': '国会政策', 'agencies': '独立机构政策'}

# 条目字段顺序固定，便于 diff；brief 放最后（长文本）
ITEM_ORDER = ('date', 'title', 'title_cn', 'url', 'source', 'kind', 'agency', 'domain', 'brief')

DATA_BLOCK_RE = re.compile(
    r'<script type="application/json" id="policy-data"[^>]*>(.*?)</script>', re.S)

TZ8 = timezone(timedelta(hours=8))


def read_block(path):
    with open(path, 'r', encoding='utf-8') as f:
        html = f.read()
    m = DATA_BLOCK_RE.search(html)
    if not m:
        raise SystemExit('[错误] %s 中未找到 id="policy-data" 数据块' % path)
    return json.loads(m.group(1)), html


def normalize_item(it):
    """按固定字段顺序重排，丢掉运行时注入的临时字段（__oi 等）。"""
    out = {}
    for k in ITEM_ORDER:
        v = it.get(k)
        if k == 'brief':
            if isinstance(v, dict) and (v.get('body') or '').strip():
                out['brief'] = {'at': v.get('at', ''), 'body': v.get('body', '')}
            continue
        out[k] = '' if v is None else v
    # 保留其余自定义字段（如 id），放在 brief 之前无意义，这里直接丢弃非白名单键
    return out


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write('\n')
    json.load(open(tmp, encoding='utf-8'))      # 自校验
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description='把 index.html 的 policy-data 拆成独立 JSON')
    ap.add_argument('--source', default='index.html', help='源 HTML（默认 index.html）')
    ap.add_argument('--out', default='data/policy', help='输出目录（默认 data/policy）')
    ap.add_argument('--merge-source', default='', help='历史回灌：仅补齐主源缺失的条目（按 url 去重）')
    ap.add_argument('--max-items', type=int, default=150, help='单板块上限（默认 150，按日期倒序截断）')
    ap.add_argument('--dry-run', action='store_true', help='只统计不写文件')
    args = ap.parse_args()

    payload, html = read_block(args.source)
    boards = payload.get('boards') or {}
    missing = [b for b in BOARDS if b not in boards]
    if missing:
        print('[错误] 数据块缺少板块：%s' % '、'.join(missing), file=sys.stderr)
        return 1

    old_boards = {}
    if args.merge_source:
        try:
            old_payload, _ = read_block(args.merge_source)
            old_boards = old_payload.get('boards') or {}
            print('[回灌] 读取历史源 %s' % args.merge_source)
        except Exception as e:
            print('[警告] 回灌源读取失败，忽略：%s' % e, file=sys.stderr)

    stamp = datetime.now(TZ8).strftime('%Y-%m-%d %H:%M')
    manifest = {
        'schema': SCHEMA,
        'built_at': datetime.now(TZ8).strftime('%Y-%m-%dT%H:%M:%S+08:00'),
        'source_fetched_at': payload.get('fetched_at') or '',
        'source_html_md5': hashlib.md5(html.encode('utf-8')).hexdigest(),
        'boards': {},
    }

    fail = []
    for key in BOARDS:
        raw_items = (boards[key] or {}).get('items') or []
        items = [normalize_item(it) for it in raw_items]
        items = [it for it in items if it.get('url') or it.get('title')]

        # 历史回灌：主源没有的条目补进来（主源优先，不做字段覆盖）
        # 只接受已加工的条目（title_cn 非空），避免把旧客户端 updates.json 灌进来的
        # 纯英文骨架条目混进来（那批条目无中文、无简报、无领域，属噪声）
        added = 0
        if old_boards.get(key):
            seen = set(it['url'] for it in items if it['url'])
            for it in (old_boards[key].get('items') or []):
                o = normalize_item(it)
                if not (o.get('url') or o.get('title')):
                    continue
                if not o.get('title_cn'):
                    continue
                if o['url'] and o['url'] in seen:
                    continue
                seen.add(o['url'])
                items.append(o)
                added += 1
        # 按日期倒序，同日期保持原顺序；再按上限截断
        items.sort(key=lambda x: x.get('date') or '', reverse=True)
        truncated = max(0, len(items) - args.max_items)
        items = items[:args.max_items]

        # 完整性校验：url 唯一、日期非空
        urls = [it['url'] for it in items if it['url']]
        if len(urls) != len(set(urls)):
            fail.append('%s：url 存在重复' % key)
        if not items:
            fail.append('%s：条目数为 0' % key)
        no_date = sum(1 for it in items if not it['date'])
        no_cn = sum(1 for it in items if not it['title_cn'])
        no_brief = sum(1 for it in items if 'brief' not in it)

        ov = (boards[key] or {}).get('overview') or {}
        if not ov and old_boards.get(key):
            ov = (old_boards[key] or {}).get('overview') or {}
        overview = {'schema': SCHEMA, 'board': key}
        for k in ('weekly', 'monthly'):
            if ov.get(k):
                overview[k] = ov[k]

        print('[%s] %d 条（回灌 +%d / 截断 -%d / 无日期 %d / 无中文标题 %d / 无简报 %d）overview=%s'
              % (key, len(items), added, truncated, no_date, no_cn, no_brief,
                 '、'.join(k for k in ('weekly', 'monthly') if k in overview)))

        if not args.dry_run:
            write_json(os.path.join(args.out, '%s-items.json' % key), {
                'schema': SCHEMA,
                'board': key,
                'board_label': BOARD_LABEL[key],
                'updated_at': (boards[key] or {}).get('updated_at') or stamp,
                'count': len(items),
                'items': items,
            })
            write_json(os.path.join(args.out, '%s-overview.json' % key), overview)

        manifest['boards'][key] = {
            'count': len(items),
            'updated_at': (boards[key] or {}).get('updated_at') or stamp,
        }

    if fail:
        print('[失败] %s' % '；'.join(fail), file=sys.stderr)
        return 2

    if not args.dry_run:
        write_json(os.path.join(args.out, 'manifest.json'), manifest)
        total = sum(v['count'] for v in manifest['boards'].values())
        print('[完成] 共 %d 条 → %s' % (total, os.path.abspath(args.out)))
    else:
        print('[dry-run] 未写文件')
    return 0


if __name__ == '__main__':
    sys.exit(main())
