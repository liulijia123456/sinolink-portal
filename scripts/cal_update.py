#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大事日历数据维护：安全增量合并（只追加、绝不删除历史节点）

用法：
    # 1) 增量合并候选节点（每周六自动更新时调用）
    python3 scripts/cal_update.py merge /tmp/cal_candidates.json
    python3 scripts/cal_update.py merge /tmp/cal_candidates.json --dry-run
    python3 scripts/cal_update.py merge cand.json --updated-at 2026-10-17

    # 2) 体检：条目统计 / 疑似重复 / 字段缺失
    python3 scripts/cal_update.py check

候选文件格式（两种均可）：
    {"events": [ {...}, {...} ]}   或   [ {...}, {...} ]

单个候选节点字段：
    d     必填  YYYY-MM-DD        起始日（美东）
    cat   必填  econ|pol|dip|mkt  经济 / 政治 / 外交 / 交易日历
    title 必填  中文全称
    d2    选填  YYYY-MM-DD        多日会期结束日
    t     选填  HH:MM             美东时刻（定期数据发布、FOMC 决议等）
    s     选填  格内短标题（不填则自动截取 title 前 8 字并提示补齐）
    note  选填  备注（地点 / 机构 / 来源文号 / 补充信息）
    replaces_title  选填  把同名旧条目标记为「已调整」（stale）并在备注前加说明。
                          旧条目只标注、不删除，仍保留在日历中。

合并规则（核心保证）：
    · 键 = (d, d2, title)，命中即跳过，原有条目一字不改；
    · --update-notes：命中时用候选条目更新备注 note / 时刻 t / 结束日 d2（短标题 s 仅在为空时补），
      用于「待定 → 官方确认」等信息精化；不填此参数则命中条目完全不改动；
    · 只追加新条目，绝无删除或覆盖分支；写回前用 (d, title) 键集合比对做兜底断言；
    · 按 (d, d2) 稳定排序后写回；同时把 updated_at 更新为执行日期；
    · 全程只改 index.html 中 id="us-cal-data" 数据块，不触碰其余内容。
"""
import argparse
import collections
import datetime
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PORTAL = os.path.join(os.path.dirname(HERE), 'index.html')
CATS = ('econ', 'pol', 'dip', 'mkt')
BLOCK_RE = re.compile(r'(<script type="application/json" id="us-cal-data"[^>]*>)(.*?)(</script>)', re.S)
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
TIME_RE = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')
# 新条目的字段顺序（与既有数据保持一致）
KEY_ORDER = ('d', 'd2', 't', 'cat', 'title', 's', 'note')


def load_block(path):
    with open(path, encoding='utf-8') as f:
        html = f.read()
    m = BLOCK_RE.search(html)
    if not m:
        sys.exit('错误：在 %s 中找不到 id="us-cal-data" 数据块' % path)
    data = json.loads(m.group(2), object_pairs_hook=collections.OrderedDict)
    if not isinstance(data, collections.OrderedDict):
        data = collections.OrderedDict(data)
    data.setdefault('events', [])
    return html, m, data


def dump_block(data):
    return '\n' + json.dumps(data, ensure_ascii=False, indent=2) + '\n'


def load_candidates(path):
    with open(path, encoding='utf-8') as f:
        raw = json.load(f)
    if isinstance(raw, dict):
        cands = raw.get('events') or raw.get('add') or []
    elif isinstance(raw, list):
        cands = raw
    else:
        sys.exit('错误：候选文件格式应为 {"events": [...]} 或 [...]')
    return list(cands)


def ekey(ev):
    return (ev.get('d', ''), ev.get('d2', '') or '', ev.get('title', ''))


def norm_candidate(c, warn):
    """校验并规范化一个候选节点；返回 OrderedDict 或 None（非法）"""
    d = str(c.get('d', '')).strip()
    cat = str(c.get('cat', '')).strip()
    title = str(c.get('title', '')).strip()
    if not DATE_RE.match(d):
        warn('跳过：起始日非法 %r（title=%r）' % (c.get('d'), title))
        return None
    if cat not in CATS:
        warn('跳过：分类非法 %r（title=%r，应为 %s）' % (cat, title, '/'.join(CATS)))
        return None
    if not title:
        warn('跳过：缺少 title（d=%s）' % d)
        return None
    d2 = str(c.get('d2', '') or '').strip()
    if d2:
        if not DATE_RE.match(d2):
            warn('跳过：结束日非法 %r（title=%r）' % (c.get('d2'), title))
            return None
        if d2 < d:
            warn('跳过：结束日早于起始日（%s > %s，title=%r）' % (d, d2, title))
            return None
    t = str(c.get('t', '') or '').strip()
    if t and not TIME_RE.match(t):
        warn('跳过：时刻非法 %r（title=%r，应为 HH:MM 美东）' % (c.get('t'), title))
        return None
    s = str(c.get('s', '') or '').strip()
    if not s:
        s = title[:8]
        warn('提示：%r 未给格内短标题 s，暂用 %r' % (title, s))
    note = str(c.get('note', '') or '').strip()
    out = collections.OrderedDict()
    out['d'] = d
    if d2:
        out['d2'] = d2
    if t:
        out['t'] = t
    out['cat'] = cat
    out['title'] = title
    out['s'] = s
    if note:
        out['note'] = note
    return out


def cmd_merge(args):
    html, m, data = load_block(args.portal)
    before = list(data['events'])
    title_index = {}
    for i, ev in enumerate(before):
        title_index.setdefault(ev.get('title', ''), []).append(i)

    warned = []
    cands = []
    seen = set()
    dup_in_file = 0
    for c in load_candidates(args.candidates):
        ev = norm_candidate(c, warned.append)
        if ev is None:
            continue
        k = ekey(ev)
        if k in seen:
            dup_in_file += 1
            warned.append('候选文件内重复：%s %s（已忽略后一条）' % (ev['d'], ev['title']))
            continue
        seen.add(k)
        cands.append((c, ev))

    events = list(before)
    added, skipped, revised, nupdated = [], [], [], []
    for c, ev in cands:
        k = ekey(ev)
        hit = next((x for x in events if ekey(x) == k), None)
        if hit is not None:
            skipped.append(ev)
            if args.update_notes:
                changed = []
                for f in ('note', 't', 'd2'):
                    v = str(c.get(f, '') or '').strip()
                    if v and str(hit.get(f, '') or '').strip() != v:
                        hit[f] = v
                        changed.append(f)
                if not str(hit.get('s', '') or '').strip() and str(ev.get('s', '') or '').strip():
                    hit['s'] = ev['s']
                    changed.append('s')
                if changed:
                    nupdated.append('%s %s（更新 %s）' % (ev['d'], ev['title'], ','.join(changed)))
            continue
        # 改期：同名旧条目只标注为 stale，不删除
        rt = str(c.get('replaces_title', '') or '').strip()
        if rt and rt in title_index:
            rd = ev.get('d') + (' ~ ' + ev['d2'] if ev.get('d2') else '')
            for i in title_index[rt]:
                old = events[i]
                if old.get('stale'):
                    continue
                old['stale'] = True
                prefix = '原定 %s%s，已调整至 %s' % (
                    old.get('d', ''), (' ~ ' + old['d2']) if old.get('d2') else '', rd)
                old['note'] = (prefix + ' · ' + old['note']) if old.get('note') else prefix
            revised.append(rt)
        elif rt:
            warned.append('提示：replaces_title=%r 未匹配到既有条目（title=%r）' % (rt, ev['title']))
        events.append(ev)
        added.append(ev)

    # 兜底断言：既有条目一条都不能少（以 d + title 为身份，允许 d2/t/note 精化）
    before_ids = set((e.get('d', ''), e.get('title', '')) for e in before)
    after_ids = set((e.get('d', ''), e.get('title', '')) for e in events)
    if not before_ids.issubset(after_ids):
        sys.exit('错误：合并后检测到既有条目丢失，已中止写入（未改动 index.html）')

    # 稳定排序：按起始日、结束日；保存文件可读性
    events.sort(key=lambda e: (e.get('d', ''), e.get('d2', '') or ''))
    data['events'] = events
    data['updated_at'] = args.updated_at
    if args.source_note:
        data['source_note'] = args.source_note

    today = datetime.date.today().isoformat()
    past = sum(1 for e in events if (e.get('d2') or e.get('d')) < today)

    print('候选 %d 条 → 新增 %d 条，已存在跳过 %d 条（其中更新备注 %d 条），标为已调整 %d 条，字段告警 %d 条'
          % (len(cands), len(added), len(skipped), len(nupdated), len(revised), len(warned)))
    for e in added:
        print('  + %s%s [%s] %s' % (e['d'], ('~' + e['d2']) if e.get('d2') else '', e['cat'], e['title']))
    for e in skipped[:10]:
        print('  = 跳过（已存在）%s %s' % (e['d'], e['title']))
    for t in nupdated:
        print('  * 备注更新：%s' % t)
    for t in revised:
        print('  ~ 已调整标记：%s' % t)
    for w in warned:
        print('  ! %s' % w)
    print('合并后共 %d 条（历史 %d 条 · 未来 %d 条），updated_at=%s'
          % (len(events), past, len(events) - past, data['updated_at']))

    if not added and not revised and not nupdated:
        print('无新增内容，index.html 未写入')
        return 0

    new_html = html[:m.start(2)] + dump_block(data) + html[m.end(2):]
    if args.dry_run:
        print('--dry-run：未写入 index.html')
        return 0
    with open(args.portal, 'w', encoding='utf-8') as f:
        f.write(new_html)
    print('已写入 %s' % args.portal)
    return 0


def cmd_check(args):
    _html, _m, data = load_block(args.portal)
    events = data['events']
    today = datetime.date.today().isoformat()
    cats = collections.Counter(e.get('cat', '?') for e in events)
    past = [e for e in events if (e.get('d2') or e.get('d')) < today]
    print('updated_at：%s' % data.get('updated_at', '—'))
    print('总条数：%d（历史 %d · 未来 %d）' % (len(events), len(past), len(events) - len(past)))
    print('分类分布：' + ' · '.join('%s=%d' % (c, cats.get(c, 0)) for c in CATS))
    print('日期区间：%s ~ %s' % (events[0]['d'] if events else '—',
                              max((e.get('d2') or e['d']) for e in events) if events else '—'))
    ks = collections.Counter(map(ekey, events))
    dups = [k for k, v in ks.items() if v > 1]
    print('重复键：%d %s' % (len(dups), dups[:5] if dups else ''))
    bad = [e for e in events if e.get('cat') not in CATS or not DATE_RE.match(e.get('d', ''))
           or not e.get('s') or not e.get('note')]
    print('字段缺失/异常：%d %s' % (len(bad), [e.get('title', '?') for e in bad[:5]] if bad else ''))
    stale = [e.get('title') for e in events if e.get('stale')]
    print('已调整（stale）条目：%d %s' % (len(stale), stale))
    return 0


def main():
    ap = argparse.ArgumentParser(description='大事日历数据维护（只追加、不删除）')
    ap.add_argument('--portal', default=DEFAULT_PORTAL, help='index.html 路径')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p1 = sub.add_parser('merge', help='增量合并候选节点')
    p1.add_argument('candidates', help='候选节点 JSON 文件')
    p1.add_argument('--updated-at', default=datetime.date.today().isoformat(),
                    help='写回的 updated_at（默认今天）')
    p1.add_argument('--source-note', default='', help='覆盖 source_note（一般不用）')
    p1.add_argument('--update-notes', action='store_true',
                    help='命中既有条目时用候选更新 note/t/d2（用于待定转确认等信息精化）')
    p1.add_argument('--dry-run', action='store_true', help='只打印结果，不写文件')
    p1.set_defaults(func=cmd_merge)

    p2 = sub.add_parser('check', help='体检：统计 / 重复 / 字段缺失')
    p2.set_defaults(func=cmd_check)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == '__main__':
    main()
