#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国金门户 · 生成远程更新清单 updates.json
=========================================

把定时抓取到的新条目导出为前端「内置自动更新模块」可消费的 JSON 清单，
放到仓库根目录后由 GitHub Pages 一同部署，页面每 30 分钟自动拉取并渲染。

与 fetch_policy.py 的区别：
  fetch_policy.py      —— 直接回写 index.html 内的数据块（本地/手动路径）
  gen_updates_json.py  —— 产出独立 updates.json（云端/自动路径，页面自取）

用法：
  python3 scripts/gen_updates_json.py                 # 默认回看 10 天，输出 updates.json
  python3 scripts/gen_updates_json.py --days 14
  python3 scripts/gen_updates_json.py --out dist/updates.json

环境变量：
  CONGRESS_API_KEY  国会 API 密钥（可选；缺失时跳过国会板块，不失败）

输出格式（与 index.html 自动更新模块的约定一致）：
  { "version": "...", "updated_at": "...", "source_name": "...",
    "items": [ { "id", "target", "board", "date", "title", "title_cn",
                 "kind", "agency", "url", "summary" } ] }

退出码：0 成功（至少一个板块有数据）；2 全部板块失败（不覆盖已有 updates.json）。
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, 'scripts'))

DEFAULT_OUT = os.path.join(BASE_DIR, 'updates.json')
SOURCE_NAME = '联邦公报 / congress.gov'

# 板块中文名（用于日志与目标标记）
BOARD_LABEL = {'whitehouse': '白宫政策', 'congress': '国会政策', 'agencies': '独立机构政策'}


def since_date(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y-%m-%d')


def load_existing(index_html=None):
    """读取 index.html 存量条目，按 url 建立 {url: {title_cn, brief, domain}} 映射。

    目的：中文标题与简报是本地人工/模型加工字段，云端 CI 无法生成。
    清单里若缺失这些字段，前端会认为「条目内容已变化」而把整批存量误判为变更。
    因此生成清单时把存量已加工的字段补齐，保证同一条目在两处指纹一致。
    """
    path = index_html or os.path.join(BASE_DIR, 'index.html')
    known = {}
    try:
        import fetch_policy as fp
        with open(path, 'r', encoding='utf-8') as f:
            payload = fp.extract_payload(f.read())
        for board in ('whitehouse', 'congress', 'agencies', 'judicial', 'parties'):
            for it in payload.get('boards', {}).get(board, {}).get('items', []):
                u = (it.get('url') or '').strip()
                if not u:
                    continue
                # 只沿用字符串字段：brief 在存量中是 {at, body} 长文结构，不能直接当摘要
                known[u] = {
                    'title_cn': it.get('title_cn', '') if isinstance(it.get('title_cn'), str) else '',
                    'domain': it.get('domain', '') if isinstance(it.get('domain'), str) else '',
                }
    except Exception as e:
        print('[提示] 未能读取存量中文标题（%s），新清单该字段将为空' % e, file=sys.stderr)
    return known


def to_item(board, raw, known=None):
    """把 fetch_policy 抓到的条目转换为前端清单条目。

    - id 取 url：与 index.html 存量条目（有 url 无 id）可互相匹配，去重最稳
    - title_cn / domain 沿用存量已加工内容（否则前端会把整批存量误判为变更）
    - summary 一律用英文原标题：存量 brief 是长文结构不宜作摘要，且原标题不编造内容。
      注意：前端指纹只比对 标题/中文标题/日期/链接 四项，不比对 summary，
      因此摘要变化不会触发「变更」提示——标题或中文标题变化才会。
    """
    known = known or {}
    url = (raw.get('url') or '').strip()
    title = (raw.get('title') or '').strip()
    if not url and not title:
        return None
    old = known.get(url, {})
    return {
        'id': url or title,
        'target': 'policy',
        'board': board,
        'date': raw.get('date', ''),
        'title': title,
        'title_cn': old.get('title_cn') or raw.get('title_cn', ''),
        'kind': raw.get('kind', ''),
        'agency': raw.get('agency', ''),
        'domain': old.get('domain') or raw.get('domain', ''),
        'url': url,
        'summary': title,
    }


def collect(days, known=None):
    """复用 fetch_policy 的抓取函数，收集三个板块的新条目。"""
    known = known or {}
    out = {}
    try:
        import fetch_policy as fp
    except Exception as e:
        print('[错误] 无法导入 fetch_policy：%s' % e, file=sys.stderr)
        return out

    since = since_date(days)
    print('[抓取] 回看起点 %s（%d 天）' % (since, days))

    plan = [('whitehouse', fp.fetch_whitehouse), ('agencies', fp.fetch_agencies)]
    if fp._load_congress_key():
        plan.insert(1, ('congress', fp.fetch_congress))
    else:
        print('[跳过] 未配置 CONGRESS_API_KEY，本轮不抓国会板块')

    for board, fn in plan:
        try:
            raws = fn(since) or []
            items = [x for x in (to_item(board, r, known) for r in raws) if x]
            out[board] = items
            print('[抓取] %s：%d 条' % (BOARD_LABEL[board], len(items)))
        except Exception as e:
            print('[警告] %s 抓取失败：%s（其他板块保留）' % (BOARD_LABEL[board], e), file=sys.stderr)
        time.sleep(1)
    return out


def main():
    ap = argparse.ArgumentParser(description='生成远程更新清单 updates.json')
    ap.add_argument('--days', type=int, default=10, help='回看天数，默认 10')
    ap.add_argument('--out', default=DEFAULT_OUT, help='输出路径，默认仓库根 updates.json')
    args = ap.parse_args()

    known = load_existing()
    print('[存量] 已读取 %d 条历史条目的中文标题/简报，用于补齐清单字段' % len(known))

    boards = collect(args.days, known)
    items = []
    for b in ('whitehouse', 'congress', 'agencies', 'judicial', 'parties'):
        items.extend(boards.get(b, []))

    if not items:
        print('[失败] 三个板块均无数据，保留已有 updates.json 不覆盖', file=sys.stderr)
        return 2

    # 按日期倒序，前端按 unshift 插入后自然为新在前
    items.sort(key=lambda x: x.get('date', ''), reverse=True)

    now = datetime.now(timezone(timedelta(hours=8)))
    manifest = {
        'version': now.strftime('%Y-%m-%dT%H:%M:%S+08:00'),
        'updated_at': now.strftime('%Y-%m-%d %H:%M'),
        'source_name': SOURCE_NAME,
        'items': items,
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or '.', exist_ok=True)
    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print('[完成] %s：共 %d 条（%s）' % (
        args.out, len(items),
        '、'.join('%s %d' % (BOARD_LABEL[b], len(boards.get(b, [])))
                  for b in ('whitehouse', 'congress', 'agencies', 'judicial', 'parties') if boards.get(b))))
    return 0


if __name__ == '__main__':
    sys.exit(main())
