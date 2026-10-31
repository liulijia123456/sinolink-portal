#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国金门户 · 大事日历事实审计
===========================

对 index.html 中 id="us-cal-data" 的 events 做**规则化事实核查**，
拦住「日期不可能」「与美股休市规则矛盾」「重复」「会期长度离谱」「远期臆测」等错误，
并提供受控删除能力（用于修正历史错误条目）。

背景：2026-10-30 实测发现日历被写入若干与事实不符的条目，例如
  · 2027-01-20「美国总统就职典礼」（下一届就职是 2029-01-20，2026 是中期选举年）
  · 2026-12 / 2027-01 出现「选举人会议投票」「国会计票」（中期选举年无选举人团）
  · 2027-10-11「哥伦布日（联邦假日，美股休市）」（美股当日照常交易）
  · 2027 年东盟系列会议会期 8—11 天（实际峰会会期 2—3 天）
这些错误靠人工复核难以及时发现，故固化为机器规则。

用法：
    python3 scripts/cal_audit.py check                 # 全量审计（退出码 0=无错 1=有错）
    python3 scripts/cal_audit.py check --json          # 输出机器可读结果
    python3 scripts/cal_audit.py prune del.json --dry-run
    python3 scripts/cal_audit.py prune del.json        # 受控删除（按 (d, title) 精确匹配）

prune 文件格式：{"keys": [{"d": "2027-01-20", "title": "美国总统就职典礼"}, ...]}
删除会逐条打印被删内容与原因，不触碰其他条目。

退出码：check 0 通过 / 1 发现问题；prune 0 完成 / 1 有键未命中
"""

import argparse
import collections
import json
import os
import re
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PORTAL = os.path.join(os.path.dirname(HERE), 'index.html')
BLOCK_RE = re.compile(
    r'(<script type="application/json" id="us-cal-data"[^>]*>)(.*?)(</script>)', re.S)
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')

# ---------------------------------------------------------------- 事实常量

# 美股全天休市的 10 个假日（NYSE/Nasdaq 规则）。其余联邦假日美股照常交易，
# 尤其是**哥伦布日与退伍军人节**——把它们标成「休市」是常见错误。
MARKET_CLOSED_HOLIDAYS = (
    '元旦', '马丁·路德·金', '华盛顿诞辰', '总统日', '耶稣受难日',
    '阵亡将士', '六月节', '独立日', '劳工节', '感恩节', '圣诞节',
)
# 美股照常交易的联邦假日
MARKET_OPEN_HOLIDAYS = ('哥伦布日', '退伍军人节')

# 已从官方日程核实的 FOMC 会议起始日（联邦储备委员会 fomccalendars）
FOMC_KNOWN = {
    '2026-01-27', '2026-03-17', '2026-04-28', '2026-06-16',
    '2026-07-28', '2026-09-15', '2026-10-27', '2026-12-08',
    '2027-01-26', '2027-03-16', '2027-04-27', '2027-06-08',
    '2027-07-27', '2027-09-14', '2027-10-26', '2027-12-07',
}
# 官方日程已公布到的最远年份；超出即「尚未公布」，不应写入具体日期
FOMC_PUBLISHED_YEARS = (2026, 2027)

# 一级范畴取值（与 scripts/cal_classify.py、docs/日历分类指标.md 保持一致）
CATS = ('econ', 'pol', 'dip', 'mkt')

# dip 类会期上限：峰会/部长会通常 2—4 天，超过 7 天基本可判定为录入错误
DIP_MAX_SPAN = 8
# 远期阈值：超过该月数的 econ/pol 条目，官方通常尚未公布，须复核
FAR_FUTURE_MONTHS = 18


def load_block(path):
    with open(path, encoding='utf-8') as f:
        html = f.read()
    m = BLOCK_RE.search(html)
    if not m:
        sys.exit('错误：在 %s 中找不到 id="us-cal-data" 数据块' % path)
    data = json.loads(m.group(2), object_pairs_hook=collections.OrderedDict)
    data.setdefault('events', [])
    return html, m, data


def _ym_add(ym, delta):
    y, m = int(ym[:4]), int(ym[5:7])
    m += delta
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return '%04d-%02d' % (y, m)


# ---------------------------------------------------------------- 规则

def audit(events, today=None):
    """返回 (errors, warns)。errors 为确定的事实错误，warns 为需人工复核项。"""
    today = today or date.today().isoformat()
    errors, warns = [], []

    def err(msg):
        errors.append(msg)

    def warn(msg):
        warns.append(msg)

    # R0 基础字段
    for e in events:
        d = e.get('d') or ''
        if not DATE_RE.match(d):
            err('起始日非法：%r（%s）' % (d, e.get('title')))
        d2 = e.get('d2') or ''
        if d2 and (not DATE_RE.match(d2) or d2 < d):
            err('结束日非法或早于起始日：%s~%s（%s）' % (d, d2, e.get('title')))
        if e.get('cat') not in CATS:
            err('分类非法：%r（%s）' % (e.get('cat'), e.get('title')))
        if not (e.get('title') or '').strip():
            err('缺少标题（d=%s）' % d)

    # R1 重复：同日同标题（含完全相同 title 的多条）
    seen = collections.Counter((e.get('d'), e.get('title')) for e in events)
    for (d, t), n in seen.items():
        if n > 1:
            err('重复条目 %s ×%d：%s' % (d, n, t))

    # R2 同名事件在同一年内跨不同日期并存。
    # 项目约定：改期走 replaces_title，旧条标 stale 保留痕迹、不删除。
    # 因此「新条 + 若干 stale 旧条」是**合规**的，不能判重；
    # 只有当多条都不是 stale（即真的重复录入）时才告警。
    # 注意：同一标题在不同年份重复出现是正常的（年度定期数据），不参与判重。
    by_title_year = collections.defaultdict(list)
    for e in events:
        t = (e.get('title') or '').strip()
        d = e.get('d') or ''
        if t and len(d) >= 4:
            by_title_year[(t, d[:4])].append(e)
    for (t, y), lst in by_title_year.items():
        if len(lst) < 2:
            continue
        live = [x for x in lst if not x.get('stale')]
        if len(live) > 1:
            ds = sorted({(x.get('d') or '') + ('~' + x['d2'] if x.get('d2') else '')
                         for x in live})
            if len(ds) > 1:
                warn('%s 年内同名事件存在多个未标 stale 的日期：%s → %s'
                     '（若非改期，应删重复；若为改期，旧条应标 stale）'
                     % (y, t, ' / '.join(ds)))

    # R3 总统就职：只在「选举年 + 1」的 1 月 20 日（年份 % 4 == 1）
    for e in events:
        t = e.get('title') or ''
        if '总统就职' in t or '就职典礼' in t:
            d = e.get('d') or ''
            try:
                y, m, dd = int(d[:4]), int(d[5:7]), int(d[8:10])
            except Exception:
                continue
            if y % 4 != 1 or m != 1 or dd != 20:
                err('总统就职日期不成立：%s（%s）——就职日仅在大选次年 1 月 20 日，'
                    '即年份 ≡1 (mod 4)' % (d, t))

    # R4 选举人团链条：仅存在于总统大选年
    #    证书/选举人投票 → 大选年 12 月；国会计票 → 次年 1 月 6 日
    for e in events:
        t = e.get('title') or ''
        d = e.get('d') or ''
        try:
            y, m = int(d[:4]), int(d[5:7])
        except Exception:
            continue
        if ('选举人' in t and ('证书' in t or '投票' in t)) or '选举人会议' in t:
            if y % 4 != 0 or m != 12:
                err('选举人团程序存在于中期选举年：%s（%s）——'
                    '仅总统大选年（年份能被 4 整除）12 月才有选举人会议与任命证书' % (d, t))
        if '计点选举人票' in t or ('国会' in t and '计票' in t):
            if y % 4 != 1 or m != 1:
                err('国会计票日期不成立：%s（%s）——仅在总统大选次年 1 月 6 日' % (d, t))

    # R5 美股休市标注：哥伦布日 / 退伍军人节当日美股照常交易。
    # 只看标题里的「休市」（页面就显示这个），不看 note——note 常写「不因…休市」
    # 「美股正常交易」，按子串匹配会把正确条误判成错误。
    for e in events:
        t = e.get('title') or ''
        if '休市' not in t:
            continue
        if any(h in t for h in MARKET_OPEN_HOLIDAYS):
            err('休市标注错误：%s（%s）——哥伦布日与退伍军人节美股照常交易，'
                '标题不应出现「休市」' % (e.get('d'), t))

    # R6 dip 类会期长度。COP 气候大会按惯例约两周，单独放宽。
    for e in events:
        if e.get('cat') != 'dip':
            continue
        d, d2 = e.get('d') or '', e.get('d2') or ''
        if not d2:
            continue
        try:
            span = (date.fromisoformat(d2) - date.fromisoformat(d)).days + 1
        except Exception:
            continue
        t = e.get('title') or ''
        cap = 16 if ('气候大会' in t or 'COP' in t) else DIP_MAX_SPAN
        if span > cap:
            err('会期长度可疑：%s~%s 共 %d 天（%s）——峰会/部长会通常 2—4 天，'
                '超过 %d 天应核实或拆分' % (d, d2, span, t, cap))

    # R7 FOMC 会议日期必须落在官方已公布日程内（会议纪要不算会议，单独放行）
    for e in events:
        t = e.get('title') or ''
        if 'FOMC' not in t or '纪要' in t:
            continue
        d = e.get('d') or ''
        try:
            y = int(d[:4])
        except Exception:
            continue
        if y not in FOMC_PUBLISHED_YEARS:
            err('FOMC 会议年份超出自公布日程：%s（%s）——美联储仅公布到 %d 年，'
                '不得写入更远年份的具体会期' % (d, t, max(FOMC_PUBLISHED_YEARS)))
        elif d not in FOMC_KNOWN:
            err('FOMC 会期与官方日程不符：%s（%s）——不在已核实的会议列表中' % (d, t))

    # R8 远期 econ/pol 条目：官方一般尚未公布，提示复核。
    # 法定/公式可推算的事件不算臆测（大选日、就职日、国会开幕、计票、选举人团、
    # 夏令时切换、联邦假日），一律豁免，否则每年都会刷一堆无意义告警。
    STATUTORY_RE = re.compile(
        r'(大选日|中期选举|就职典礼|国会开幕|计票|选举人|夏令时|冬令时|联邦假日)')
    limit = _ym_add(today[:7], FAR_FUTURE_MONTHS)
    for e in events:
        d = e.get('d') or ''
        t = e.get('title') or ''
        if e.get('cat') == 'mkt' or STATUTORY_RE.search(t):
            continue
        if e.get('cat') in ('econ', 'pol') and d[:7] > limit:
            warn('远期条目（>%d 个月）须核实是否有官方公布：%s（%s）'
                 % (FAR_FUTURE_MONTHS, d, t))

    # R9 相关性：美国非参与方的多边机制不应出现在美国板块
    NOT_US_RELEVANT = ('英联邦', 'CHOGM', '法语国家组织', '非盟峰会', '海湾合作委员会')
    for e in events:
        t = e.get('title') or ''
        if any(k in t for k in NOT_US_RELEVANT):
            err('相关性不符：%s（%s）——美国非该机制成员或参与方，'
                '不应收录进美国政治日历' % (e.get('d'), t))

    return errors, warns


# ---------------------------------------------------------------- 命令

def cmd_check(args):
    _html, _m, data = load_block(args.portal)
    events = data['events']
    errors, warns = audit(events)
    if args.json:
        print(json.dumps({'total': len(events), 'errors': errors, 'warns': warns},
                         ensure_ascii=False, indent=2))
        return 1 if errors else 0
    print('日历审计：%d 条，updated_at=%s' % (len(events), data.get('updated_at')))
    if errors:
        print('\n[错误] %d 项（确定的事实错误，必须修正后才能发布）' % len(errors))
        for x in errors:
            print('  ✗ %s' % x)
    if warns:
        print('\n[待核] %d 项（需人工复核）' % len(warns))
        for x in warns:
            print('  ! %s' % x)
    if not errors and not warns:
        print('\n[通过] 未发现事实或结构问题。')
    elif not errors:
        print('\n[通过] 无确定错误，但有待核项。')
    else:
        print('\n[未通过] 修正错误后重跑本审计。')
    return 1 if errors else 0


def cmd_prune(args):
    html, m, data = load_block(args.portal)
    events = data['events']
    with open(args.keys, encoding='utf-8') as f:
        raw = json.load(f)
    keys = raw.get('keys') if isinstance(raw, dict) else raw
    if not isinstance(keys, list) or not keys:
        sys.exit('错误：prune 文件应为 {"keys": [...]} 且非空')

    keep, removed, missed = [], [], []
    want = {(k.get('d', ''), (k.get('title') or '').strip()): k for k in keys}
    used = set()
    for e in events:
        k = (e.get('d', ''), (e.get('title') or '').strip())
        if k in want and k not in used:
            used.add(k)
            removed.append((e, want[k].get('reason', '')))
        else:
            keep.append(e)
    for for_k in want:
        if for_k not in used:
            missed.append(for_k)

    print('计划删除 %d 条，保留 %d 条：' % (len(removed), len(keep)))
    for e, reason in removed:
        print('  - %s %s %s%s'
              % (e.get('d'), ('~' + e['d2']) if e.get('d2') else '',
                 e.get('title'), ('  ← ' + reason) if reason else ''))
    if missed:
        print('\n[警告] %d 个键未命中（可能已删除或标题不符）：' % len(missed))
        for d, t in missed:
            print('  ? %s %s' % (d, t))

    if args.dry_run:
        print('\n--dry-run：未写入 %s' % args.portal)
        return 0 if not missed else 1
    if not removed:
        print('\n无条目被删除，未写入。')
        return 1

    data['events'] = keep
    block = '\n' + json.dumps(data, ensure_ascii=False, indent=2) + '\n'
    new_html = html[:m.start(2)] + block + html[m.end(2):]
    with open(args.portal, 'w', encoding='utf-8') as f:
        f.write(new_html)
    print('\n已写入 %s（删除 %d 条）' % (args.portal, len(removed)))
    return 0 if not missed else 1


def main():
    ap = argparse.ArgumentParser(description='大事日历事实审计与受控删除')
    ap.add_argument('--portal', default=DEFAULT_PORTAL, help='index.html 路径')
    sub = ap.add_subparsers(dest='cmd')

    c = sub.add_parser('check', help='全量事实审计（默认）')
    c.add_argument('--json', action='store_true', help='输出 JSON')
    c.set_defaults(func=cmd_check)

    p = sub.add_parser('prune', help='受控删除（按 (d,title) 精确匹配）')
    p.add_argument('keys', help='删除清单 JSON')
    p.add_argument('--dry-run', action='store_true', help='只打印，不写入')
    p.set_defaults(func=cmd_prune)

    args = ap.parse_args()
    if not getattr(args, 'func', None):
        args.func = cmd_check
        args.json = False
    sys.exit(args.func(args))


if __name__ == '__main__':
    main()
