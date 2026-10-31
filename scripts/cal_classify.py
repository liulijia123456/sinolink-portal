#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国金门户 · 大事日历分类与口径规范化
====================================

给 us-cal-data 的每条事件补四个分类指标，并把同一类条目的写法收敛到统一口径。

四个指标（详细定义见 docs/日历分类指标.md）：

  cat     一级范畴，决定格子色条与图例，5 值：stat / monetary / pol / dip / mkt
  topic   议题域，用于检索与聚合，11 值：price / labor / growth / trade / money /
          fiscal / tech / energy / security / election / calendar
  actor   主导主体，7 值：exec / agency / congress / fed / court / stats / intl / none
  impact  市场传导强度，3 值：high / mid / low

为什么要有这套东西：原方案只有 econ/pol/dip/hol 四类，结果「美国对外投资安全计划
拟议规则意见期截止」这类行政规则制定被塞进 econ，「政治」只剩 12 条选举与国会程序；
同一类条目（如联邦假日）标题后缀、备注句式、来源写法各写各的，无法横向比较。

用法：
    python3 scripts/cal_classify.py check             # 体检：覆盖、取值合法性、跨类冲突
    python3 scripts/cal_classify.py report            # 打印分类结果（按范畴分组）
    python3 scripts/cal_classify.py report --csv out.csv
    python3 scripts/cal_classify.py apply             # 写入 index.html（幂等）
    python3 scripts/cal_classify.py apply --fix-holiday   # 同时统一假日标题与备注口径

退出码：check 0 合规 / 1 有问题；其余 0 完成
"""

import argparse
import collections
import csv
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_PORTAL = os.path.join(ROOT, 'index.html')
BLOCK_RE = re.compile(
    r'(<script type="application/json" id="us-cal-data"[^>]*>)(.*?)(</script>)', re.S)

# ---------------------------------------------------------------- 指标定义

CAT_DEF = collections.OrderedDict([
    ('stat', '统计发布'),
    ('monetary', '货币政策'),
    ('pol', '政治与治理'),
    ('dip', '外交与安全'),
    ('mkt', '市场作息'),
])

TOPIC_DEF = collections.OrderedDict([
    ('price', '物价'),
    ('labor', '就业与薪酬'),
    ('growth', '增长与消费'),
    ('trade', '贸易与外部平衡'),
    ('money', '货币与金融监管'),
    ('fiscal', '财政与住房'),
    ('tech', '科技与投资安全'),
    ('energy', '能源与环境'),
    ('security', '安全与地缘'),
    ('election', '选举与政治程序'),
    ('calendar', '作息与假期'),
])

ACTOR_DEF = collections.OrderedDict([
    ('exec', '行政部门'),
    ('agency', '独立监管机构'),
    ('congress', '国会'),
    ('fed', '美联储'),
    ('court', '司法'),
    ('stats', '统计机构'),
    ('intl', '外国政府或多边组织'),
    ('none', '无主导主体'),
])

IMPACT_DEF = collections.OrderedDict([
    ('high', '高'),
    ('mid', '中'),
    ('low', '低'),
])

# ---------------------------------------------------------------- 分类规则

# 统计发布：按发布机构与指标族识别，顺序即优先级
STAT_RULES = [
    (r'非农就业报告', 'labor'),
    (r'CPI', 'price'),
    (r'PPI', 'price'),
    (r'PCE', 'price'),
    (r'GDP', 'growth'),
    (r'零售电商', 'growth'),
    (r'零售销售', 'growth'),
    (r'国际交易与对外投资头寸', 'growth'),
    (r'就业成本指数', 'labor'),
    (r'薪酬成本调查', 'labor'),
    (r'贸易差额', 'trade'),
    (r'进出口价格', 'trade'),
]

# 外交与安全：多边机制与双边磋商节点。议题按机制的**主要议程**定，不一律归到「安全」
DIP_RULES = [
    (r'Quad|四方安全对话', 'security', 'intl'),
    (r'慕尼黑安全会议', 'security', 'intl'),
    (r'东盟', 'security', 'intl'),
    (r'G20.*外长', 'security', 'intl'),
    (r'G20', 'money', 'intl'),                 # 领导人峰会、财长会与 Sherpa 会以宏观经济议程为主
    (r'IMF|世界银行', 'money', 'intl'),
    (r'APEC', 'trade', 'intl'),
    (r'达沃斯', 'money', 'intl'),              # 世界经济论坛，议题以宏观与商业为主
    (r'COP\d', 'energy', 'intl'),
    (r'关税休战', 'trade', 'exec'),
    (r'以色列', 'election', 'intl'),
]

# 政治与治理：选举与国会程序、行政规则制定、贸易救济措施
POL_RULES = [
    (r'中期选举日|大选日', 'election', 'exec'),
    (r'就职典礼', 'election', 'exec'),
    (r'国会开幕|国会联席会议|计点选举人票', 'election', 'congress'),
    (r'选举人', 'election', 'exec'),
    (r'拨款截止', 'fiscal', 'congress'),
    (r'USMCA', 'trade', 'exec'),
    (r'301 条款|关税暂停', 'trade', 'exec'),
    (r'对外投资安全计划', 'tech', 'exec'),
    (r'压力测试|资本计划规则|资本缓冲', 'money', 'fed'),
    (r'稳定币|加密资产', 'money', 'agency'),
    (r'EPA|可再生燃料|三氯乙烯', 'energy', 'agency'),
    (r'住房法|机构购房', 'fiscal', 'exec'),
    (r'意见期|规则生效|新规', 'money', 'agency'),
]

# 个别条目的手工归类：规则判错或需要专门口径时在此覆盖，键为 (日期, 标题)
OVERRIDES = {
    ('2026-12-07', '美国对外投资安全计划拟议规则公众意见期截止'):
        {'cat': 'pol', 'topic': 'tech', 'actor': 'exec', 'impact': 'mid'},
    ('2026-11-02', '美联储压力测试透明度新规生效'):
        {'cat': 'pol', 'topic': 'money', 'actor': 'fed', 'impact': 'low'},
    ('2026-12-01', '美联储资本计划规则与压力资本缓冲修订生效'):
        {'cat': 'pol', 'topic': 'money', 'actor': 'fed', 'impact': 'mid'},
    ('2027-01-18', 'OCC 稳定币发行实施规则生效'):
        {'cat': 'pol', 'topic': 'money', 'actor': 'agency', 'impact': 'mid'},
    ('2026-10-20', 'SEC 加密资产拟议规则公众意见期截止'):
        {'cat': 'pol', 'topic': 'money', 'actor': 'agency', 'impact': 'mid'},
    ('2026-11-13', 'EPA 三氯乙烯合规日期延长意见期截止'):
        {'cat': 'pol', 'topic': 'energy', 'actor': 'agency', 'impact': 'low'},
    ('2026-12-04', 'EPA 可再生燃料标准豁免再分配意见期截止'):
        {'cat': 'pol', 'topic': 'energy', 'actor': 'agency', 'impact': 'low'},
    ('2026-11-23', '财政部 ROAD 住房法机构购房禁令意见期截止'):
        {'cat': 'pol', 'topic': 'fiscal', 'actor': 'exec', 'impact': 'low'},
    ('2027-01-09', '301 条款造船等行业关税暂停拟延至 1 月 9 日'):
        {'cat': 'pol', 'topic': 'trade', 'actor': 'exec', 'impact': 'mid'},
    ('2027-01-12', 'USMCA 2027 年度联合审查公众意见截止'):
        {'cat': 'pol', 'topic': 'trade', 'actor': 'exec', 'impact': 'low'},
    ('2027-07-01', 'USMCA 2027 年度联合审查'):
        {'cat': 'pol', 'topic': 'trade', 'actor': 'exec', 'impact': 'mid'},
    ('2026-12-11', '联邦政府拨款截止日'):
        {'cat': 'pol', 'topic': 'fiscal', 'actor': 'congress', 'impact': 'high'},
    ('2026-11-03', '2026 年中期选举日'):
        {'cat': 'pol', 'topic': 'election', 'actor': 'exec', 'impact': 'high'},
    ('2028-11-07', '2028 年美国总统与国会大选日'):
        {'cat': 'pol', 'topic': 'election', 'actor': 'exec', 'impact': 'high'},
    ('2029-01-20', '美国总统就职典礼'):
        {'cat': 'pol', 'topic': 'election', 'actor': 'exec', 'impact': 'mid'},
    ('2027-01-10', '中美关税休战延期期满'):
        {'cat': 'dip', 'topic': 'trade', 'actor': 'exec', 'impact': 'high'},
}

# 统计发布的市场传导强度：同一指标族内部按「初值 > 修正 > 终值」「总量 > 分项」区分
IMPACT_BY_FAMILY = [
    (r'非农就业报告|CPI|PCE 物价指数|FOMC 议息会议', 'high'),
    (r'GDP 初值', 'high'),
    (r'GDP', 'mid'),
    (r'PPI|零售销售|贸易差额|就业成本指数|薪酬成本调查|进出口价格|零售电商|国际交易', 'mid'),
]


def classify(ev):
    """按规则给出四指标；返回 dict（含 _from 说明依据，便于复核）。"""
    title = ev.get('title') or ''
    date = ev.get('d') or ''
    key = (date, title)
    if key in OVERRIDES:
        out = dict(OVERRIDES[key])
        out['_from'] = 'override'
        return out

    # ① 市场作息：联邦假日与时制切换
    if ev.get('cat') in ('hol', 'mkt') or '联邦假日' in title:
        return {'cat': 'mkt', 'topic': 'calendar', 'actor': 'none', 'impact': 'low',
                '_from': 'holiday'}
    if '冬令时' in title or '夏令时' in title:
        return {'cat': 'mkt', 'topic': 'calendar', 'actor': 'none', 'impact': 'low',
                '_from': 'dst'}

    # ② 货币政策
    if 'FOMC' in title:
        imp = 'high' if '纪要' not in title else 'mid'
        return {'cat': 'monetary', 'topic': 'money', 'actor': 'fed', 'impact': imp,
                '_from': 'fomc'}

    # ③ 统计发布
    for pat, topic in STAT_RULES:
        if re.search(pat, title):
            imp = 'mid'
            for ipat, ilevel in IMPACT_BY_FAMILY:
                if re.search(ipat, title):
                    imp = ilevel
                    break
            actor = 'stats'
            if re.search(r'贸易差额', title):
                actor = 'stats'          # 普查局与 BEA 联合发布，统一记统计机构
            return {'cat': 'stat', 'topic': topic, 'actor': actor, 'impact': imp,
                    '_from': 'stat:' + pat}

    # ④ 外交与安全
    for pat, topic, actor in DIP_RULES:
        if re.search(pat, title):
            imp = 'mid' if re.search(r'领导人峰会|领导人非正式会议|外长|峰会', title) else 'low'
            return {'cat': 'dip', 'topic': topic, 'actor': actor, 'impact': imp,
                    '_from': 'dip:' + pat}

    # ⑤ 政治与治理
    for pat, topic, actor in POL_RULES:
        if re.search(pat, title):
            imp = 'mid' if re.search(r'意见期|生效|审查|选举|就职|国会', title) else 'low'
            return {'cat': 'pol', 'topic': topic, 'actor': actor, 'impact': imp,
                    '_from': 'pol:' + pat}

    return {'cat': '', 'topic': '', 'actor': '', 'impact': '', '_from': 'none'}


# ---------------------------------------------------------------- 假日口径

# 美股全天休市的假日族（NYSE/Nasdaq）。哥伦布日、退伍军人节照常交易，不在表内。
MARKET_CLOSED = ('元旦', '马丁·路德·金', '总统日', '华盛顿诞辰', '耶稣受难',
                 '阵亡将士', '六月节', '独立日', '劳工节', '感恩节', '圣诞')

# 归并标题时会丢掉的法定名称，补进备注（标题保持统一格式，事实不丢）
NAME_ALIAS = {'总统日': "法定名称为华盛顿诞辰日（Washington's Birthday）"}


def holiday_is_closed(ev):
    """按 note 的事实陈述判定该年该日的休市状态，判断不了才回落到假日族表。

    个别年份有例外：2027-12-31 是 2028 年元旦的联邦观察日，纽交所当日照常交易。
    """
    note = ev.get('note') or ''
    pos_closed = note.find('美股休市')
    pos_open = note.find('美股正常交易')
    if pos_closed >= 0 and (pos_open < 0 or pos_closed < pos_open):
        return True
    if pos_open >= 0:
        return False
    name = re.sub(r'（[^）]*）', '', ev.get('title') or '')
    return any(k in name for k in MARKET_CLOSED)


def fix_holiday(ev):
    """统一假日条目的 title 与 note 口径，返回改动 dict（可能为空）。

    标题统一为「节日名（联邦假日）」/「节日名（联邦假日，美股休市）」；
    备注去掉与标题重复的「联邦假日」段、统一「周一」写法与来源全称。
    """
    if ev.get('cat') not in ('hol', 'mkt') and '联邦假日' not in (ev.get('title') or ''):
        return {}
    closed = holiday_is_closed(ev)
    raw_title = ev.get('title') or ''
    name = re.sub(r'（[^）]*）', '', raw_title).strip()
    name = name.replace('华盛顿诞辰日', '总统日').replace('美国独立日', '独立日')
    new_title = '%s（联邦假日%s）' % (name, '，美股休市' if closed else '')

    segs = [x.strip() for x in re.split(r'\s*·\s*', ev.get('note') or '') if x.strip()]
    body, src = [], ''
    for s in segs:
        m = re.match(r'^来源\s*[:：]\s*(.+)$', s)
        if m:
            src = m.group(1).strip()
            continue
        if re.fullmatch(r'联邦假日(（[^）]*）)?', s):
            continue
        s = re.sub(r'^联邦假日[，,]\s*', '', s)
        s = re.sub(r'星期([一二三四五六日天])', r'周\1', s)
        if s:
            body.append(s)
    if not body:
        body = ['美股休市' if closed else '美股正常交易']
    alias = NAME_ALIAS.get(name)
    if alias and not any('法定名称' in x for x in body):
        body.append(alias)
    src = re.sub(r'^美国人事管理局联邦假日安排', '美国人事管理局（OPM）联邦假日安排', src)
    new_note = ' · '.join(body + (['来源：' + src] if src else []))

    diff = {}
    if new_title != raw_title:
        diff['title'] = new_title
    if new_note != (ev.get('note') or ''):
        diff['note'] = new_note
    return diff


# ---------------------------------------------------------------- 读写

def load(path):
    with open(path, encoding='utf-8') as f:
        html = f.read()
    m = BLOCK_RE.search(html)
    if not m:
        sys.exit('错误：%s 中没有 id="us-cal-data" 数据块' % path)
    data = json.loads(m.group(2), object_pairs_hook=collections.OrderedDict)
    data.setdefault('events', [])
    return html, m, data


def cmd_check(args):
    _h, _m, data = load(args.portal)
    evs = data['events']
    bad = []
    for e in evs:
        got = classify(e)
        key = (e.get('d'), e.get('title'))
        if not got.get('cat'):
            bad.append((key, '无法归类', got['_from']))
            continue
        if got['cat'] not in CAT_DEF:
            bad.append((key, '非法 cat=%r' % got['cat'], got['_from']))
        if got['topic'] not in TOPIC_DEF:
            bad.append((key, '非法 topic=%r' % got['topic'], got['_from']))
        if got['actor'] not in ACTOR_DEF:
            bad.append((key, '非法 actor=%r' % got['actor'], got['_from']))
        if got['impact'] not in IMPACT_DEF:
            bad.append((key, '非法 impact=%r' % got['impact'], got['_from']))
        # 已写入的数据与规则结果不一致时提示（改过规则没重跑 apply）
        for f in ('cat', 'topic', 'actor', 'impact'):
            if e.get(f) and e.get(f) != got[f]:
                bad.append((key, '%s 数据=%r 规则=%r' % (f, e.get(f), got[f]), got['_from']))

    cur = collections.Counter(e.get('cat') for e in evs)
    print('条目 %d 条，规则可归类 %d 条' % (len(evs), len(evs) - len([b for b in bad if b[1] == '无法归类'])))
    print('当前分布：' + ' · '.join('%s=%d' % (CAT_DEF.get(k, k), cur[k])
                                   for k in CAT_DEF if cur.get(k)))
    if bad:
        print('\n[未通过] %d 项问题：' % len(bad))
        for (d, t), msg, frm in bad[:30]:
            print('  %s %-40s %s（规则：%s）' % (d, (t or '')[:38], msg, frm))
        return 1
    print('\n[通过] 四指标均合法且与规则一致。')
    return 0


def cmd_report(args):
    _h, _m, data = load(args.portal)
    evs = data['events']
    rows = []
    for e in evs:
        got = classify(e)
        rows.append(collections.OrderedDict([
            ('日期', e.get('d')),
            ('结束日', e.get('d2', '')),
            ('范畴', CAT_DEF.get(got['cat'], got['cat'])),
            ('议题', TOPIC_DEF.get(got['topic'], got['topic'])),
            ('主体', ACTOR_DEF.get(got['actor'], got['actor'])),
            ('传导', IMPACT_DEF.get(got['impact'], got['impact'])),
            ('标题', e.get('title')),
            ('短标签', e.get('s', '')),
            ('备注', e.get('note', '')),
        ]))
    if args.csv:
        with open(args.csv, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print('已写出 %s（%d 行）' % (args.csv, len(rows)))
        return 0

    by = collections.OrderedDict((k, []) for k in CAT_DEF)
    for r, e in zip(rows, evs):
        by[classify(e)['cat']].append(r)
    for cat, items in by.items():
        if not items:
            continue
        print('\n【%s】%s · %d 条' % (cat, CAT_DEF[cat], len(items)))
        for r in items[:8]:
            print('  %s  %-22s %s' % (r['日期'], r['标题'][:22], r['议题']))
        if len(items) > 8:
            print('  …… 其余 %d 条' % (len(items) - 8))
    return 0


def cmd_apply(args):
    html, m, data = load(args.portal)
    evs = data['events']
    n_cat = n_hol = 0
    changes = []
    for e in evs:
        got = classify(e)
        if not got.get('cat'):
            sys.exit('错误：无法归类 %s %s' % (e.get('d'), e.get('title')))
        touched = False
        for f in ('cat', 'topic', 'actor', 'impact'):
            if e.get(f) != got[f]:
                e[f] = got[f]
                touched = True
        if touched:
            n_cat += 1
            changes.append(('指标', e.get('d'), e.get('title'),
                            '%s/%s/%s/%s' % (got['cat'], got['topic'], got['actor'], got['impact'])))
        if args.fix_holiday:
            d = fix_holiday(e)
            if d:
                n_hol += 1
                e.update(d)

    print('分类指标：%d 条中 %d 条更新' % (len(evs), n_cat))
    if args.fix_holiday:
        print('假日口径：%d 条更新' % n_hol)
    if args.verbose:
        for kind, d, t, info in changes:
            print('  %s %s %-38s %s' % (kind, d, (t or '')[:36], info))

    if args.dry_run:
        print('\n--dry-run：未写入 %s' % args.portal)
        return 0

    block = '\n' + json.dumps(data, ensure_ascii=False, indent=2) + '\n'
    with open(args.portal, 'w', encoding='utf-8') as f:
        f.write(html[:m.start(2)] + block + html[m.end(2):])
    print('已写入 %s' % args.portal)
    return 0


def main():
    ap = argparse.ArgumentParser(description='大事日历分类与口径规范化')
    ap.add_argument('--portal', default=DEFAULT_PORTAL)
    sub = ap.add_subparsers(dest='cmd', required=True)

    c = sub.add_parser('check', help='体检：覆盖、取值、与规则一致性')
    c.set_defaults(func=cmd_check)

    r = sub.add_parser('report', help='按范畴打印分类结果，或导出 CSV')
    r.add_argument('--csv', help='导出 CSV 路径')
    r.set_defaults(func=cmd_report)

    a = sub.add_parser('apply', help='写入分类指标（幂等）')
    a.add_argument('--dry-run', action='store_true')
    a.add_argument('--fix-holiday', action='store_true', help='同时统一假日 title/note 口径')
    a.add_argument('--verbose', action='store_true')
    a.set_defaults(func=cmd_apply)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == '__main__':
    main()
