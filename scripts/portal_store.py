#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国金门户 · 政策数据存储层
=========================

统一 fetch_policy.py / brief.py 的数据读写入口，屏蔽两种存储形态：

1) **JSON 形态（当前）**：data/policy/{board}-items.json + {board}-overview.json + manifest.json
2) **内嵌形态（历史）**：index.html 里的 <script id="policy-data"> 数据块

判定规则：`data/policy/manifest.json` 存在即走 JSON；否则回落到 HTML 数据块。
因此同一份脚本在新旧两种目录结构下都能跑，迁移期不会中断。

对外只需两个函数：

    payload = load_policy()      # {'fetched_at': str, 'boards': {key: {updated_at, items, overview}}}
    save_policy(payload)         # 原子写回（临时文件 + os.replace）

payload 结构与页面里原来的 data.boards 完全一致，调用方无需改动。
"""

import json
import os
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POLICY_DIR = os.path.join(BASE_DIR, 'data', 'policy')
INDEX_HTML = os.path.join(BASE_DIR, 'index.html')
MANIFEST = os.path.join(POLICY_DIR, 'manifest.json')

BOARDS = ('whitehouse', 'congress', 'agencies', 'judicial', 'parties')
SCHEMA = 1
TZ8 = timezone(timedelta(hours=8))


def _now():
    return datetime.now(TZ8)


def use_json():
    """当前是否使用 JSON 存储形态。"""
    return os.path.exists(MANIFEST)


# ---------------------------------------------------------------- JSON 形态

def _item_path(board):
    return os.path.join(POLICY_DIR, '%s-items.json' % board)


def _overview_path(board):
    return os.path.join(POLICY_DIR, '%s-overview.json' % board)


def _write_json(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write('\n')
    with open(tmp, 'r', encoding='utf-8') as f:
        json.load(f)                     # 写后自校验，避免半截文件
    os.replace(tmp, path)


def _load_json():
    payload = {'fetched_at': '', 'boards': {}}
    if os.path.exists(MANIFEST):
        try:
            with open(MANIFEST, encoding='utf-8') as f:
                payload['fetched_at'] = json.load(f).get('source_fetched_at') or ''
        except Exception:
            pass
    for board in BOARDS:
        items, updated_at, overview = [], '', {}
        try:
            with open(_item_path(board), encoding='utf-8') as f:
                d = json.load(f)
            items = d.get('items') or []
            updated_at = d.get('updated_at') or ''
        except FileNotFoundError:
            pass
        try:
            with open(_overview_path(board), encoding='utf-8') as f:
                o = json.load(f)
            overview = {k: o[k] for k in ('weekly', 'monthly') if o.get(k)}
        except FileNotFoundError:
            pass
        payload['boards'][board] = {
            'updated_at': updated_at,
            'items': items,
            'overview': overview,
        }
    return payload


def _save_json(payload, boards=None):
    os.makedirs(POLICY_DIR, exist_ok=True)
    stamp = _now().strftime('%Y-%m-%d %H:%M')
    targets = [b for b in BOARDS if (not boards or b in boards)]
    # 只写部分板块时，其余板块的 manifest 计数沿用磁盘现值，避免用可能过期的内存值覆盖
    manifest_boards = {}
    if boards and os.path.exists(MANIFEST):
        try:
            old = json.load(open(MANIFEST, encoding='utf-8'))
            for b, v in (old.get('boards') or {}).items():
                if b not in targets:
                    manifest_boards[b] = v
        except Exception:
            pass
    for board in targets:
        b = payload.get('boards', {}).get(board) or {}
        items = b.get('items') or []
        updated_at = b.get('updated_at') or stamp
        _write_json(_item_path(board), {
            'schema': SCHEMA,
            'board': board,
            'updated_at': updated_at,
            'count': len(items),
            'items': items,
        })
        ov = b.get('overview') or {}
        _write_json(_overview_path(board), dict(
            {'schema': SCHEMA, 'board': board},
            **{k: ov[k] for k in ('weekly', 'monthly') if ov.get(k)}
        ))
        manifest_boards[board] = {'count': len(items), 'updated_at': updated_at}
    _write_json(MANIFEST, {
        'schema': SCHEMA,
        'built_at': _now().strftime('%Y-%m-%dT%H:%M:%S+08:00'),
        'source_fetched_at': payload.get('fetched_at') or stamp,
        'boards': manifest_boards,
    })


# ---------------------------------------------------------------- 内嵌形态（兜底）

def _load_html():
    import re
    payload = {'fetched_at': None, 'boards': {}}
    try:
        with open(INDEX_HTML, encoding='utf-8') as f:
            html = f.read()
        m = re.search(r'<script type="application/json" id="policy-data"[^>]*>(.*?)</script>',
                      html, re.S)
        if m:
            payload = json.loads(m.group(1))
    except Exception:
        pass
    payload.setdefault('fetched_at', None)
    payload.setdefault('boards', {})
    for key in BOARDS:
        payload['boards'].setdefault(key, {'updated_at': None, 'items': []})
    return payload


def _save_html(payload):
    import re
    with open(INDEX_HTML, encoding='utf-8') as f:
        html = f.read()
    block = json.dumps(payload, ensure_ascii=False, indent=2)
    pat = re.compile(r'(<script type="application/json" id="policy-data"[^>]*>).*?(</script>)', re.S)
    new_html, n = pat.subn(lambda m: m.group(1) + '\n' + block + '\n' + m.group(2), html)
    if n != 1:
        raise RuntimeError('数据块替换失败（匹配 %d 处，应为 1）' % n)
    with open(INDEX_HTML, 'w', encoding='utf-8') as f:
        f.write(new_html)


# ---------------------------------------------------------------- 对外接口

def load_policy():
    return _load_json() if use_json() else _load_html()


def save_policy(payload, boards=None):
    """写回策略数据。boards 为 None 时写全部板块；给定列表时只写这些板块，
    便于「一个板块一个定时任务」的拆分场景——不覆盖其他板块的文件与 manifest 计数。"""
    return _save_json(payload, boards) if use_json() else _save_html(payload)
