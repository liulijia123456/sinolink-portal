#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国金门户 · 大事日历 · 半自动采集
================================

每天跑一次，从结构化官方源拉「可能该进日历」的条目，产出**候选收件箱**，
不直接改数据——由日更任务或人工挑选后再走 cal_update.py merge。

为什么只做半自动：官方源给的是英文标题与生效日，日历要的是中文标题 + 分类 +
体例化的 note，这一步必须过一遍判断。脚本负责「别漏」，人负责「写对」。

数据源
  1. federalregister.gov API —— 最终规则（RULE）的生效日，JSON、免 key，最可靠
  2. OPM holidays.ics —— 联邦假日，ICS 标准格式（文件含 2021 起全部年份，必须按窗口截）
  3. 美联储 FOMC 会议日历 —— HTML，按年份 panel 里的 month/date 对解析

已知盲区（脚本抓不到，仍靠日更人工补）
  · 纽交所假日与 OPM 联邦假日不完全重合（耶稣受难日只休市、不是联邦假日）
  · 财政部 OFAC/拍卖、BLS/BEA/Census 发布日历、国会表决——均为网页或 PDF，无稳定接口
  · 会议纪要发布日（决议日 +21 天）由 cal_audit 推得，不在此抓

产出
  scripts/cal_inbox.json  候选条目（含去重标记 is_new）
  logs/cal.log            每次抓取的时间、源、条数、耗时——**这是事后复盘的唯一依据**

用法
    python3 scripts/cal_fetch.py fetch [--days 30] [--horizon 150] [--sources fr,opm,fed]
    python3 scripts/cal_fetch.py report          # 只读 cal_inbox.json 打印摘要
退出码：0 正常 / 1 全部源失败
"""

import argparse
import collections
import datetime
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PORTAL = os.path.join(ROOT, 'index.html')
INBOX = os.path.join(HERE, 'cal_inbox.json')
CAL_LOG = os.path.join(ROOT, 'logs', 'cal.log')
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36'

# 只收与本门户相关的机构；其余（海岸警卫队安全区、渔业配额等）一律不进
AGENCY_KEEP = (
    'treasury', 'commerce', 'trade representative', 'securities and exchange',
    'commodity futures', 'federal reserve', 'environmental protection', 'federal communications',
    'nuclear regulatory', 'medicare', 'internal revenue', 'homeland security', 'state department',
    'personnel management', 'food and drug', 'health and human services', 'labor department',
    'agriculture department', 'transportation department', 'energy department', 'federal energy',
    'small business', 'citizenship', 'comptroller of the currency', 'federal deposit',
    'credit union', 'maritime', 'national highway', 'pipeline', 'foreign assets',
)
# 整机构丢弃：纯内部人事、信用社、小企业、矿安——规则多但与政策研究无关
AGENCY_DROP = (
    'personnel management', 'credit union', 'small business', 'mine safety',
)
# 例行公事类规则：数量大、与政策研究无关，直接丢弃
TITLE_DROP = re.compile(
    r'safety zone|security zone|special local regulation|drawbridge|air quality state implementation|'
    r'air plan approval|fisheries|fishery|atlantic highly migratory|pacific island|'
    r'airworthiness directive|class [a-e] airspace|standard instrument approach|'
    r'temporary deviation|anchorage|regulated navigation area|'
    # 州级环保计划（占 EPA 规则大头，逐州逐污染物重复）
    r'implementation plans?|air plan|state plans? for designated|codification and incorporation|'
    r'coal combustion residuals permit|approval of state|promulgation of state|'
    # 其余高频例行：食品着色剂、儿童抚养、学前教育、农房贷款、场地租约、营养补助
    r'color additive petition|child support enforcement|head start|single family housing|'
    r'administrative site lease|special supplemental nutrition|incorporation by reference|'
    # 与政策—市场—地缘三条主线无关的纯程序/内部管理类
    r'\bfoia\b|administrative hearings|practice and procedure|capital stock|inspection programs|'
    r'significant new use|apprenticeship|organ procurement|nondiscrimination|'
    r'electronic payments|surety bond|advertising and notice|voluntary consensus standards|'
    r'adjudications?|closed captioning', re.I)
# 高相关主题：命中即收，压过下面的例行与机构过滤
TITLE_KEEP = re.compile(
    r'tariff|export control|export administration|entity list|sanction|ofac|'
    r'semiconductor|microelectronic|artificial intelligence|quantum|'
    r'stablecoin|digital asset|crypto|tokeniz|payment stablecoin|'
    r'antitrust|merger|data broker|consumer privacy|'
    r'medicare|medicaid|drug price|prescription drug|340b|'
    r'greenhouse gas|vehicle emission|tailpipe|clean air act|clean water act|climate|'
    r'spectrum|broadband|submarine cable|telecommunications carrier|'
    r'critical mineral|critical infrastructure|electric grid|nuclear|'
    r'outbound investment|cfius|foreign investment|'
    r'immigration|visa|h-1b|asylum|'
    r'income tax|internal revenue|'
    r'covid|public health emergency', re.I)


def keep_rule(title, agency):
    """三道闸：主题词优先收 → 例行模式弃 → 机构白/黑名单。返回 (留?, 原因)。"""
    if TITLE_KEEP.search(title):
        return True, 'topic'
    if TITLE_DROP.search(title):
        return False, 'routine'
    low = agency.lower()
    if any(k in low for k in AGENCY_DROP):
        return False, 'agency'
    if any(k in low for k in AGENCY_KEEP):
        return True, 'agency'
    return False, 'agency'



def log(msg):
    os.makedirs(os.path.dirname(CAL_LOG), exist_ok=True)
    with open(CAL_LOG, 'a', encoding='utf-8') as f:
        f.write('[%s] %s\n' % (datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg))


def get(url, timeout=30):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', 'replace')


# ---------------------------------------------------------------- 数据源

def fetch_fr(days_back, horizon):
    """联邦公报：近 N 天发布的最终规则，取其中生效日落在未来 horizon 天内的。"""
    today = datetime.date.today()
    since = today - datetime.timedelta(days=days_back)
    until = today + datetime.timedelta(days=horizon)
    out, page, seen = [], 1, 0
    drop = collections.Counter()
    while page <= 6:
        q = {
            'conditions[type][]': 'RULE',
            'conditions[publication_date][gte]': since.isoformat(),
            'conditions[publication_date][lte]': today.isoformat(),
            'per_page': '100',
            'page': str(page),
            'order': 'newest',
        }
        # fields 用重复键，手工拼查询串
        fields = ['title', 'effective_on', 'publication_date', 'document_number', 'html_url', 'agencies', 'type']
        url = ('https://www.federalregister.gov/api/v1/documents.json?'
               + urllib.parse.urlencode(q)
               + ''.join('&fields[]=' + f for f in fields))
        data = json.loads(get(url))
        results = data.get('results') or []
        if not results:
            break
        seen += len(results)
        for r in results:
            eff = r.get('effective_on') or ''
            if not eff or eff < today.isoformat() or eff > until.isoformat():
                drop['窗口外'] += 1
                continue
            title = (r.get('title') or '').strip()
            ags = ' / '.join(a.get('name', '') for a in (r.get('agencies') or [])[:2])
            ok, why = keep_rule(title, ags)
            if not ok:
                drop[why] += 1
                continue
            out.append({
                'd': eff,
                'title_en': title,
                'agency': ags,
                'doc': r.get('document_number'),
                'url': r.get('html_url'),
                'pub': r.get('publication_date'),
                'kind': 'rule',
            })
        if len(results) < 100:
            break
        page += 1
        time.sleep(0.4)          # 对公共 API 客气一点
    log('fr: 取回 %d 条，保留 %d，弃 %s'
        % (seen, len(out), ', '.join('%s=%d' % kv for kv in drop.most_common()) or '0'))
    return out


def fetch_opm(_days_back, horizon):
    """OPM 联邦假日 ICS：解析 DTSTART 与 SUMMARY，只留 [今天, 今天+horizon] 内的。

    OPM 这份 ics 是**滚动全量**的（当前含 2021—2030 共 112 条），不过滤就会把
    往年假日全倒进收件箱，所以必须按日期窗口截断。
    """
    txt = get('https://www.opm.gov/policy-data-oversight/pay-leave/federal-holidays/holidays.ics')
    today = datetime.date.today()
    until = today + datetime.timedelta(days=horizon)
    out, seen = [], set()
    for blk in re.findall(r'BEGIN:VEVENT(.*?)END:VEVENT', txt, re.S):
        d = re.search(r'DTSTART(?:;VALUE=DATE)?[^:]*:(\d{8})', blk)
        s = re.search(r'SUMMARY:(.+)', blk)
        if not d or not s:
            continue
        raw = d.group(1)
        try:
            day = datetime.date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
        except ValueError:
            continue
        if not (today <= day <= until) or day.isoformat() in seen:
            continue
        seen.add(day.isoformat())
        out.append({'d': day.isoformat(), 'title_en': s.group(1).strip(), 'agency': 'US OPM',
                    'doc': None, 'url': 'https://www.opm.gov/policy-data-oversight/pay-leave/federal-holidays/',
                    'pub': None, 'kind': 'holiday'})
    return out


_MON = {m: i + 1 for i, m in enumerate(
    ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'))}


def _fomc_span(month_raw, date_raw):
    """把「Apr/May 30-1」这类写法折成 (起, 止)；解析不了就返回 None。

    页面上的取值形态：`27-28` / `17-18*`（星号＝附经济预测摘要）/
    `Apr/May 30-1`（跨月会期）/ `22 (notation vote)`（非议息会议，丢弃）。
    """
    date_raw = date_raw.replace('\xa0', ' ').strip()
    if 'notation' in date_raw.lower():
        return None
    sep = '*' in date_raw
    date_raw = date_raw.replace('*', '').strip()
    mon_toks = [t.strip() for t in month_raw.replace('\xa0', ' ').split('/') if t.strip()]
    nums = re.match(r'^(\d{1,2})(?:\s*[-–—]\s*(\d{1,2}))?', date_raw)
    if not mon_toks or not nums:
        return None
    ms = []
    for t in mon_toks[:2]:
        k = _MON.get(t[:3].lower())
        if not k:
            return None
        ms.append(k)
    d1 = int(nums.group(1))
    d2 = int(nums.group(2)) if nums.group(2) else d1
    return ms, d1, d2, sep


def fetch_fed(_days_back, horizon):
    """美联储 FOMC 议息日历。

    日期口径跟随库内既有 21 条议息条目：`d` 记**会期首日**（如 2027-01-26 对应
    Jan 26-27），决议与声明落在 `e`（末日）。改口径须全量迁移，不能只改脚本。
    """
    html = get('https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm')
    today = datetime.date.today()
    until = today + datetime.timedelta(days=horizon)
    heads = [(m.start(), int(m.group(1)))
             for m in re.finditer(r'(\d{4})\s+FOMC\s+Meetings', html)]
    out = []
    for i, (pos, yr) in enumerate(heads):
        end_pos = heads[i + 1][0] if i + 1 < len(heads) else len(html)
        body = html[pos:end_pos]
        for mm in re.finditer(
                r'fomc-meeting__month[^>]*>(.*?)</div>.*?fomc-meeting__date[^>]*>(.*?)</div>',
                body, re.S):
            month_raw = re.sub(r'<[^>]+>', ' ', mm.group(1)).strip()
            date_raw = re.sub(r'<[^>]+>', ' ', mm.group(2)).strip()
            parsed = _fomc_span(month_raw, date_raw)
            if not parsed:
                continue
            ms, d1, d2, sep = parsed
            try:
                start = datetime.date(yr, ms[0], d1)
                end = datetime.date(yr, ms[-1], d2)
            except ValueError:                     # 日期越界（如 2 月 30 日）
                continue
            if end < start or not (today <= start <= until):
                continue
            if start.month == end.month:
                span = '%s %d' % (month_raw.split('/')[0].strip()[:3], start.day)
                if start.day != end.day:
                    span = '%s %d-%d' % (month_raw.split('/')[0].strip()[:3], start.day, end.day)
            else:
                span = '%s %d-%s %d' % (month_raw.split('/')[0].strip()[:3], start.day,
                                        month_raw.split('/')[-1].strip()[:3], end.day)
            out.append({'d': start.isoformat(), 'e': end.isoformat(),
                        'title_en': 'FOMC meeting %s%s' % (span, ' (SEP)' if sep else ''),
                        'agency': 'Federal Reserve', 'doc': None,
                        'url': 'https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm',
                        'pub': None, 'kind': 'fed', 'sep': sep})
    return out



SOURCES = {'fr': fetch_fr, 'opm': fetch_opm, 'fed': fetch_fed}


# ---------------------------------------------------------------- 去重

def load_existing():
    """已收录情况：规则按文件号认，议息/假日按日期认。"""
    with open(PORTAL, encoding='utf-8') as f:
        html = f.read()
    ctx = {'docs': set(), 'keys': set(), 'fed': set(), 'hol': set()}
    m = re.search(r'<script type="application/json" id="us-cal-data"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return ctx
    data = json.loads(m.group(1), object_pairs_hook=collections.OrderedDict)
    for e in data['events']:
        ctx['keys'].add((e.get('d'), e.get('title')))
        title = e.get('title') or ''
        if 'FOMC' in title or '议息' in title:
            ctx['fed'].add(e.get('d'))
        if '假日' in title or '休市' in title:
            ctx['hol'].add(e.get('d'))
        for l in e.get('links') or []:
            mm = re.search(r'federalregister\.gov/d/(\S+)$', l.get('u') or '')
            if mm:
                ctx['docs'].add(mm.group(1))
    return ctx


def is_new(it, ctx):
    """新条目判定：各源用自己最可靠的键，避免中文标题与英文标题对不上导致全判新。"""
    kind = it.get('kind')
    if kind == 'rule' and it.get('doc'):
        return it['doc'] not in ctx['docs']
    if kind == 'fed':
        return it['d'] not in ctx['fed']
    if kind == 'holiday':
        return it['d'] not in ctx['hol']
    return (it['d'], it['title_en']) not in ctx['keys']


def cmd_fetch(args):
    ctx = load_existing()
    srcs = [s.strip() for s in (args.sources or 'fr,opm,fed').split(',') if s.strip()]
    all_items, ok = [], 0
    for name in srcs:
        fn = SOURCES.get(name)
        if not fn:
            continue
        t0 = time.time()
        try:
            items = fn(args.days, args.horizon)
            ok += 1
            log('fetch %s ok: %d 条，%.1fs' % (name, len(items), time.time() - t0))
        except Exception as e:                     # 单源失败不影响其他源
            items = []
            log('fetch %s FAILED: %s' % (name, e))
            print('[失败] %s：%s' % (name, e))
        for it in items:
            it['is_new'] = is_new(it, ctx)
            it['src'] = name
            all_items.append(it)

    all_items.sort(key=lambda x: (x['d'], x['src'], x['agency']))
    with open(INBOX, 'w', encoding='utf-8') as f:
        json.dump({'fetched_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                   'window': {'days_back': args.days, 'horizon': args.horizon},
                   'items': all_items}, f, ensure_ascii=False, indent=1)
    new = [x for x in all_items if x['is_new']]
    by_src = collections.Counter(x['src'] for x in all_items)
    log('fetch done: 共 %d 条（%s），其中新条目 %d'
        % (len(all_items), ', '.join('%s=%d' % kv for kv in sorted(by_src.items())), len(new)))
    print('抓取完成：%d 条，新条目 %d 条 → %s'
          % (len(all_items), len(new), os.path.relpath(INBOX, ROOT)))
    print('  分源：' + ' · '.join('%s=%d' % kv for kv in sorted(by_src.items())))
    for x in new[:15]:
        print('  + %s  [%s]  %s' % (x['d'], x['agency'][:28], x['title_en'][:70]))
    if len(new) > 15:
        print('  …… 其余 %d 条见 cal_inbox.json' % (len(new) - 15))
    return 0 if ok else 1


def cmd_report(args):
    if not os.path.exists(INBOX):
        sys.exit('尚无收件箱，先跑 fetch')
    data = json.load(open(INBOX, encoding='utf-8'))
    items = data['items']
    new = [x for x in items if x['is_new']]
    print('收件箱：%s（%d 条，新 %d 条）' % (data['fetched_at'], len(items), len(new)))
    by_src = collections.Counter(x['src'] for x in items)
    print('分源：' + ' · '.join('%s=%d' % kv for kv in sorted(by_src.items())))
    by_date = collections.Counter(x['d'][:7] for x in new)
    print('新增按月份：' + (' · '.join('%s=%d' % kv for kv in sorted(by_date.items())) or '无'))
    for x in new:
        print('  %s  [%s]  %s' % (x['d'], x['agency'][:24], x['title_en'][:80]))
    return 0


def main():
    ap = argparse.ArgumentParser(description='大事日历半自动采集')
    sub = ap.add_subparsers(dest='cmd', required=True)
    f = sub.add_parser('fetch', help='抓取候选条目')
    f.add_argument('--days', type=int, default=30, help='回看天数（发布日窗口）')
    f.add_argument('--horizon', type=int, default=150, help='向前看天数（生效日窗口）')
    f.add_argument('--sources', default='fr,opm,fed')
    f.set_defaults(func=cmd_fetch)
    r = sub.add_parser('report', help='打印收件箱摘要')
    r.set_defaults(func=cmd_report)
    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == '__main__':
    main()
