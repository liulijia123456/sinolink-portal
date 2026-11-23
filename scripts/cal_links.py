#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国金门户 · 大事日历来源链接
==========================

给 us-cal-data 的每条事件补 `links` 数组（最多 3 条），供展开卡渲染成可点击的来源。

链接来源优先级（高 → 低）：
  1. 联邦公报文件号 / 引证号——正文里已写明 `2026-06956` 或 `91 FR 18183` 的，
     直接拼 federalregister.gov 的稳定地址，最精确、最可核验；
  2. 正文里已写出的域名（`bea.gov/news/schedule`、`g20.org/events`）——原样转 https；
  3. 发布机构关键词——回落到该机构的官方日程页 / 新闻室，稳定不易失效；
  4. 全无来源的条目——按事项类型补一条「官方日程」（如假日→OPM、CPI→BLS）。

用法：
    python3 scripts/cal_links.py stats        # 覆盖情况与唯一 URL 清单
    python3 scripts/cal_links.py apply        # 写入 links（幂等）
    python3 scripts/cal_links.py check        # 校验：每条 1—3 个链接、协议合法

退出码：check 0 通过 / 1 有问题
"""

import argparse
import collections
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_PORTAL = os.path.join(ROOT, 'index.html')
BLOCK_RE = re.compile(
    r'(<script type="application/json" id="us-cal-data"[^>]*>)(.*?)(</script>)', re.S)
MAX_LINKS = 3

# ---------------------------------------------------------------- 机构 → 官方页

CR = 'https://www.federalregister.gov'
PUBLISHERS = [
    # 统计与货币
    (r'BLS|劳工统计局', 'BLS 发布日程', 'https://www.bls.gov/schedule/{year}/home.htm'),
    (r'BEA|经济分析局', 'BEA 发布日程', 'https://www.bea.gov/news/schedule'),
    (r'普查局零售|零售销售发布', '普查局零售日程', 'https://www.census.gov/retail/index.html'),
    (r'贸易差额|外贸统计|Census.*贸易', '普查局外贸数据', 'https://www.census.gov/foreign-trade/index.html'),
    (r'FOMC|美联储|Federal Reserve', '美联储日程', 'https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm'),
    # 作息
    (r'OPM|人事管理局|holidays\.ics', 'OPM 联邦假日', 'https://www.opm.gov/policy-data-oversight/pay-leave/federal-holidays/'),
    (r'NYSE|纽交所|纽约证券交易所', 'NYSE 交易日历', 'https://www.nyse.com/markets/hours-calendars'),
    # 多边机制
    (r'IMF', 'IMF 会议', 'https://www.imf.org/en/meetings'),
    (r'世界银行|World Bank', '世行活动', 'https://www.worldbank.org/en/events'),
    (r'G20', 'G20 官网', 'https://g20.org/'),
    (r'APEC', 'APEC 官网', 'https://www.apec.org/'),
    (r'ASEAN|东盟', '东盟官网', 'https://asean.org/'),
    (r'UNFCCC|COP\d|气候公约', 'UNFCCC 官网', 'https://unfccc.int/'),
    (r'WEF|世界经济论坛|达沃斯', 'WEF 活动', 'https://www.weforum.org/events'),
    (r'慕尼黑安全会议|MSC', '慕安会官网', 'https://securityconference.org/en/'),
    # 美国政府
    (r'白宫|White House', '白宫简报室', 'https://www.whitehouse.gov/briefing-room/'),
    (r'国务院|State Department|State\.gov', '国务院', 'https://www.state.gov/'),
    (r'USTR|贸易代表', 'USTR', 'https://ustr.gov/'),
    (r'财政部|Treasury', '财政部', 'https://home.treasury.gov/'),
    (r'商务部|Commerce', '商务部', 'https://www.commerce.gov/'),
    (r'国会|congress', 'Congress.gov', 'https://www.congress.gov/'),
    (r'FEC|联邦选举委员会', 'FEC', 'https://www.fec.gov/'),
    (r'Ballotpedia', 'Ballotpedia', 'https://ballotpedia.org/'),
    # 独立监管机构
    (r'SEC|证券交易委员会', 'SEC 新闻室', 'https://www.sec.gov/newsroom'),
    (r'CFTC|商品期货', 'CFTC 新闻室', 'https://www.cftc.gov/PressRoom'),
    (r'FinCEN', 'FinCEN 新闻', 'https://www.fincen.gov/news'),
    (r'FDIC', 'FDIC 新闻', 'https://www.fdic.gov/news'),
    (r'OCC|货币监理', 'OCC 新闻', 'https://www.occ.treas.gov/news-issuances/news-releases.html'),
    (r'NCUA', 'NCUA 新闻', 'https://ncua.gov/newsroom'),
    (r'(?<!UN)FCC|联邦通信委员会', 'FCC 新闻', 'https://www.fcc.gov/news-events'),
    (r'NRC|核管理委员会', 'NRC 新闻', 'https://www.nrc.gov/reading-rm/doc-collections/news/'),
    (r'EPA|环保署', 'EPA 新闻', 'https://www.epa.gov/newsreleases'),
    (r'FDA|食品药品', 'FDA 新闻', 'https://www.fda.gov/news-events'),
    (r'CMS|医保服务中心', 'CMS 新闻室', 'https://www.cms.gov/newsroom'),
    (r'IRS|税务局', 'IRS 新闻室', 'https://www.irs.gov/newsroom'),
    (r'HHS|卫生部', 'HHS 新闻', 'https://www.hhs.gov/about/news/index.html'),
    (r'劳工部|Department of Labor', '劳工部新闻', 'https://www.dol.gov/newsroom'),
    (r'NHTSA', 'NHTSA 新闻', 'https://www.nhtsa.gov/press-releases'),
    (r'PHMSA', 'PHMSA 新闻', 'https://www.phmsa.dot.gov/newsroom'),
    (r'FERC', 'FERC 新闻', 'https://www.ferc.gov/news-events'),
    (r'SBA|小企业管理局', 'SBA', 'https://www.sba.gov/'),
    (r'USCIS', 'USCIS 新闻', 'https://www.uscis.gov/newsroom'),
    (r'DHS|国土安全', 'DHS 新闻', 'https://www.dhs.gov/news-releases'),
    (r'APHIS|动植物卫生', 'APHIS', 'https://www.aphis.usda.gov/aphis/newsroom'),
    (r'MARAD|海事管理局', 'MARAD', 'https://www.maritime.dot.gov/'),
    (r'ITAR|军品清单', '国务院 DDTC', 'https://www.pmddtc.state.gov/'),
    # 外国与媒体
    (r'中国商务部|商务部.*磋商|MOFCOM', '中国商务部', 'http://www.mofcom.gov.cn/'),
    (r'新华社|新华网', '新华网', 'http://www.news.cn/'),
    (r'央视|中央广播电视总台|CCTV', '央视新闻', 'https://news.cctv.com/'),
    (r'央广网|中国之声', '央广网', 'https://www.cnr.cn/'),
    (r'人民网', '人民网', 'http://www.people.com.cn/'),
    (r'中国外交部|外交部', '中国外交部', 'https://www.mfa.gov.cn/'),
    (r'印度外交部|MEA', '印度外交部', 'https://www.mea.gov.in/'),
    (r'菲律宾|PCO|DFA', '菲总统府', 'https://pco.gov.ph/'),
    (r'美联社|AP\b', '美联社', 'https://apnews.com/'),
]

# 没有来源、也匹配不到机构时，按事项类型兜底
FALLBACK_BY_TITLE = [
    (r'CPI|PPI|非农|进出口价格|就业成本|薪酬成本|ECI', 'BLS 发布日程', 'https://www.bls.gov/schedule/{year}/home.htm'),
    (r'PCE|GDP|国际收支|国际交易', 'BEA 发布日程', 'https://www.bea.gov/news/schedule'),
    (r'零售', '普查局零售日程', 'https://www.census.gov/retail/index.html'),
    (r'贸易差额', '普查局外贸数据', 'https://www.census.gov/foreign-trade/index.html'),
    (r'FOMC|美联储', '美联储日程', 'https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm'),
    (r'联邦假日|冬令时|夏令时', 'OPM 联邦假日', 'https://www.opm.gov/policy-data-oversight/pay-leave/federal-holidays/'),
    (r'中期选举|大选|选举人|国会|就职', 'Congress.gov', 'https://www.congress.gov/'),
    (r'拨款', '国会拨款法案', 'https://www.congress.gov/'),
    (r'G20', 'G20 官网', 'https://g20.org/'),
    (r'APEC', 'APEC 官网', 'https://www.apec.org/'),
    (r'东盟', '东盟官网', 'https://asean.org/'),
    (r'IMF|世界银行', 'IMF 会议', 'https://www.imf.org/en/meetings'),
    (r'COP\d', 'UNFCCC 官网', 'https://unfccc.int/'),
    (r'慕尼黑', '慕安会官网', 'https://securityconference.org/en/'),
    (r'达沃斯', 'WEF 活动', 'https://www.weforum.org/events'),
    (r'以色列', '以色列政府', 'https://www.gov.il/en'),
    (r'俄乌|乌克兰', '国务院', 'https://www.state.gov/'),
    (r'万斯|副总统', '白宫简报室', 'https://www.whitehouse.gov/briefing-room/'),
    (r'NDAA|国防授权', 'Congress.gov', 'https://www.congress.gov/'),
]


def year_of(ev):
    return (ev.get('d') or '2026')[:4]


def note_source(ev):
    """取出 note 尾部的来源串（没有则空串）。"""
    m = re.search(r'来源[:：]\s*(.+)$', ev.get('note') or '')
    return m.group(1).strip() if m else ''


def fr_links(text):
    """联邦公报：文件号优先，其次引证号。"""
    out = []
    for doc in re.findall(r'\b(20\d{2}-\d{4,5})\b', text)[:3]:
        out.append(('联邦公报 ' + doc, '%s/d/%s' % (CR, doc)))
    if not out:
        for vol, page in re.findall(r'(\d{2,3})\s*FR\s*(\d+)', text)[:3]:
            out.append(('联邦公报 %s FR %s' % (vol, page), '%s/citation/%s-FR-%s' % (CR, vol, page)))
    return out


CANON_HOST = {
    'bea.gov': 'www.bea.gov', 'census.gov': 'www.census.gov', 'bls.gov': 'www.bls.gov',
    'federalreserve.gov': 'www.federalreserve.gov', 'opm.gov': 'www.opm.gov',
    'nyse.com': 'www.nyse.com', 'federalregister.gov': 'www.federalregister.gov',
}


def explicit_domain(text):
    """正文里已写出域名的，转成链接；无路径或明显坏路径的丢弃，交给机构表回落。"""
    out = []
    for m in re.findall(r'\b((?:[a-z0-9-]+\.)+(?:gov|org|com|net|int)(?:/[^\s，、）)]*)?)', text):
        if m.startswith('www.'):
            m = m[4:]
        host, _, path = m.partition('/')
        host = CANON_HOST.get(host, host)
        if not path:                    # 只有域名，没有具体页面
            continue
        if path in ('retail',):
            path = 'retail/index.html'
        if 'bls.gov/schedule/' in host + '/' + path and not path.endswith('.htm'):
            continue                     # BLS 月份页必须以 .htm 结尾
        out.append((host[4:] if host.startswith('www.') else host, 'https://' + host + '/' + path))
    return out[:MAX_LINKS]


# 人工回填的精确链接：按 (日期, 标题) 命中后直接采用，规则不再覆盖。
# 用于「已找到原文、且原文不是机构首页」的条目——回填时把 URL 与出处日期写进标签。
OVERRIDES = {
    ('2026-11-10', '美方斡旋的俄乌工作层级会谈（阿布扎比）'): [
        ('新华网 11-11', 'https://app.xinhuanet.com/news/article.html?articleId=20261111fb7fddb9800143049a87e50522c9b217'),
        ('央广网 11-11', 'https://www.cnr.cn/china/news/20261111/t20261111_527868594.shtml'),
        ('央视新闻 11-10', 'https://ysxw.cctv.cn/article.html?item_id=7107679433075922306'),
    ],
    ('2026-11-19', 'BIS 远程访问先进半导体规则生效'): [
        ('联邦公报 2026-23529', 'https://www.federalregister.gov/d/2026-23529'),
        ('BIS 新闻稿 11-16', 'https://www.bis.gov/press-release/bis-issues-rule-addressing-remote-access-american-super-intelligence-technology'),
        ('规则原文（RIN 0694-AK50）', 'https://www.bis.gov/media/documents/rin-0694-ak50.pdf'),
    ],
    ('2027-01-19', 'HHS HIPAA 隐私规则修订生效'): [
        ('联邦公报 2026-23429', 'https://www.federalregister.gov/d/2026-23429'),
        ('HHS 事实清单 11-16', 'https://www.hhs.gov/hipaa/for-professionals/privacy/changes-support-coordinated-care-individual-engagement-reduce-regulatory-burdens-final-rule.html'),
        ('HHS 新闻稿 11-16', 'https://www.hhs.gov/press-room/hhs-issues-new-final-rule-modernize-hipaa-privacy-rule-support-coordinated-care-individual-engagement-reduce-regulatory-burdens.html'),
    ],
    ('2027-01-19', 'IRS 退休计划自动加入规则生效'): [
        ('联邦公报 2026-23530', 'https://www.federalregister.gov/d/2026-23530'),
    ],
    ('2026-11-13', '中美贸易理事会首次会议'): [
        ('商务部新闻发言人栏目', 'https://www.mofcom.gov.cn/xwfb/xwfyrth/'),
    ],
    ('2026-12-14', 'G20 领导人峰会'): [
        ('G20 官网（美国主席国）', 'https://g20.org/'),
        ('政治报 11-18（多拉尔筹备通报）', 'https://www.politico.com/news/2026/11/18/florida-g20-doral-prep-traffic-protests-trump-01173712'),
        ('阿纳多卢通讯社 11-22（领导人出席表态）', 'https://aa.com.tr/en/americas/trump-says-all-the-top-20-leaders-will-attend-next-months-g20-summit-in-florida/4132632'),
    ],
    ('2026-11-29', '西班牙议会选举'): [
        ('西班牙国家公报 皇家法令 806/2026', 'https://www.boe.es/diario_boe/txt.php?id=BOE-A-2026-20742'),
        ('新华社 11-12（选举与罢工背景）', 'https://world.people.com.cn/n1/2026/1112/c1002-40832555.html'),
    ],
    ('2026-12-01', '参议院民主党领袖选举'): [
        ('The Hill 11-21（逐人表态统计 15:5、34 人未表态）', 'https://thehill.com/homenews/senate/6235600-van-hollen-schumer-senate-race'),
        ('ABC News 刊美联社电 11-21（选情与 12-01 投票）', 'https://abcnews.com/Politics/wireStory/democrats-swept-senate-promised-change-leadership-vote-testing-138342385'),
        ('路透社 11-22（55 人投票与 28 票门槛，经 Internazionale 转载）', 'https://www.internazionale.it/ultime-notizie-reuters/2026/11/22/schumer-led-democrats-to-big-senate-win-now-some-say-they-need-a-new-leader'),
    ],
    ('2026-12-11', '联邦政府拨款截止日'): [
        ('公法 119-103 全文（govinfo）', 'https://www.govinfo.gov/content/pkg/PLAW-119publ103/pdf/PLAW-119publ103.pdf'),
        ('H.R.6500 立法进程（congress.gov）', 'https://www.congress.gov/bill/119th-congress/house-bill/6500'),
        ('华盛顿邮报 11-23（参院共和党对切断伊朗战事经费松口）', 'https://washingtonpost.com/business/2026/11/23/senate-gop-leaders-signal-openness-cutting-off-iran-war-funding'),
    ],
    ('2026-11-19', '中美元首双边会晤（深圳）'): [
        ('中国外交部通稿 11-19', 'https://www.mfa.gov.cn/zyxw/202611/t20261119_12062924.shtml'),
        ('中国外交部英文通稿 11-19', 'https://www.mfa.gov.cn/eng/xw/zyxw/202611/t20261119_12063139.html'),
        ('人民网转新华社 11-19', 'https://cpc.people.com.cn/n1/2026/1119/c64094-40837158.html'),
    ],
    ('2026-11-19', '美中俄三方领导人会晤（深圳）'): [
        ('中国外交部通稿 11-19', 'https://www.mfa.gov.cn/eng/xw/zyxw/202611/t20261119_12063266.html'),
        ('新华社《和平宣言》全文 11-19', 'https://www.news.cn/politics/leaders/20261119/52ee3a20593f4b3db7642db2a7ece1a7/c.html'),
        ('王毅介绍会晤情况 11-19', 'https://www.mfa.gov.cn/wjbzhd/202611/t20261120_12063432.shtml'),
    ],
    ('2027-01-03', '第 120 届国会开幕'): [
        ('《宪法》第二十修正案（Constitution Annotated）', 'https://constitution.congress.gov/browse/amendment-20/'),
    ],
    ('2027-03-17', 'FISA 第 702 条既有监控授权届满'): [
        ('众议院书记官表决记录第 142 号（2026-04-29）', 'https://clerk.house.gov/Votes/2026142'),
        ('S. 1318 立法进程（congress.gov）', 'https://www.congress.gov/bill/119th-congress/senate-bill/1318'),
        ('布伦南司法中心 11-16（3 月 17 日前完成续期即不断档）', 'https://www.brennancenter.org/our-work/research-reports/section-702-surveillance-will-not-lapse-if-reauthorized-march-17-2027'),
    ],
    ('2027-01-10', '中美关税休战延期期满'): [
        ('联邦公报 91 FR 72608（造船 301 暂停延至 2027-01-09）', 'https://www.federalregister.gov/citation/91-FR-72608'),
        ('联邦公报 91 FR 72610（178 项豁免延至 2027-01-09）', 'https://www.federalregister.gov/citation/91-FR-72610'),
    ],
}


# 来源等级：p = 一手（政府/国际组织/主办方），w = 通讯社与公共广播，m = 媒体/聚合。
# 排序时 p > w > m，最多取前 3 条——「能点到原文时不要给转述」。
WIRE_HOSTS = ('xinhuanet.com', 'news.cn', 'cnr.cn', 'cctv.cn', 'cctv.com', 'news.cctv.com',
              'people.com.cn', 'apnews.com', 'reuters.com', 'chinanews.com.cn', 'ce.cn',
              'aa.com.tr')
OFFICIAL_TLDS = ('.gov', '.gov.', '.mil', '.int', '.europa.eu', 'un.org', 'imf.org',
                 'worldbank.org', 'oecd.org', 'wto.org', 'g20.org', 'apec.org', 'asean.org',
                 'unfccc.int', 'wam.ae', 'gov.uk', 'gov.cn', 'boe.es',
                 'nyse.com', 'uscode.house.gov')


def link_tier(url):
    host = url.split('/')[2].lower() if '://' in url else ''
    if host in WIRE_HOSTS or any(host.endswith('.' + h) or host == h for h in WIRE_HOSTS):
        return 'w'
    # 域名边界匹配：'.int' 这类后缀须落在「主域标签结束」处，
    # 否则 internazionale.it 之类主机名会被误判为国际组织（一手）。
    if any(host.endswith(t) or re.search(re.escape(t) + r'(?=[./]|$)', host)
           for t in OFFICIAL_TLDS):
        return 'p'
    return 'm'


def sort_by_tier(links):
    order = {'p': 0, 'w': 1, 'm': 2}
    return sorted(links, key=lambda x: order.get(x[2], 3))


def classify_links(ev):
    """返回 [(label, url, tier)]，按来源等级排序后取前 3 条。"""
    ov = OVERRIDES.get((ev.get('d') or '', ev.get('title') or ''))
    if ov:
        cand = [(t, u) for t, u in ov]
        return sort_by_tier([(t, u, link_tier(u)) for t, u in cand])[:MAX_LINKS]

    text = note_source(ev)
    year = year_of(ev)
    cand, seen = [], set()

    def add(label, url):
        url = url.replace('{year}', year)
        if url in seen:
            return
        seen.add(url)
        cand.append((label, url, link_tier(url)))

    # 先收集全部候选，再按 p > w > m 排序取前三条——
    # 若边收边截，排在后面的机构官网会被前面的媒体挤掉。
    for label, url in fr_links(text):
        add(label, url)
    for label, url in explicit_domain(text):
        add(label, url)
    for pat, label, url in PUBLISHERS:
        if re.search(pat, text):
            add(label, url)
    if not cand:
        for pat, label, url in FALLBACK_BY_TITLE:
            if re.search(pat, ev.get('title') or ''):
                add(label, url)
                break
    return sort_by_tier(cand)[:MAX_LINKS]


# ---------------------------------------------------------------- 读写

def load(path):
    with open(path, encoding='utf-8') as f:
        html = f.read()
    m = BLOCK_RE.search(html)
    if not m:
        sys.exit('错误：%s 中没有 id="us-cal-data" 数据块' % path)
    return html, m, json.loads(m.group(2), object_pairs_hook=collections.OrderedDict)


def cmd_stats(args):
    _h, _m, data = load(args.portal)
    evs = data['events']
    urls = collections.Counter()
    none, total = [], 0
    for e in evs:
        links = classify_links(e)
        if not links:
            none.append((e.get('d'), e.get('title')))
        for _l, u, _k in links:
            urls[u] += 1
        total += len(links)
    print('事件 %d 条，链接 %d 条，唯一 URL %d 个' % (len(evs), total, len(urls)))
    print('无链接条目 %d 条' % len(none))
    for d, t in none[:10]:
        print('  -', d, t)
    if args.urls:
        print('\n=== 唯一 URL（按使用次数）===')
        for u, c in urls.most_common():
            print('%4d  %s' % (c, u))
    return 0


def cmd_apply(args):
    html, m, data = load(args.portal)
    n = 0
    for e in data['events']:
        links = classify_links(e)
        arr = [collections.OrderedDict([('t', t), ('u', u), ('k', k)]) for t, u, k in links]
        if e.get('links') != arr:
            e['links'] = arr
            n += 1
    # 待定事项同样补链接
    np = 0
    for p in data.get('pending', []):
        links = classify_links({'d': p.get('from', ''), 'title': p.get('title', ''),
                                'note': '来源：' + (p.get('src') or '')})
        arr = [collections.OrderedDict([('t', t), ('u', u), ('k', k)]) for t, u, k in links]
        if p.get('links') != arr:
            p['links'] = arr
            np += 1
    print('事件补链接 %d 条，待定 %d 条' % (n, np))
    if args.dry_run:
        print('--dry-run：未写入')
        return 0
    block = '\n' + json.dumps(data, ensure_ascii=False, indent=2) + '\n'
    with open(args.portal, 'w', encoding='utf-8') as f:
        f.write(html[:m.start(2)] + block + html[m.end(2):])
    print('已写入 %s' % args.portal)
    return 0


def cmd_check(args):
    _h, _m, data = load(args.portal)
    bad = []
    for e in data['events'] + data.get('pending', []):
        links = e.get('links') or []
        k = '%s %s' % (e.get('d') or e.get('from'), e.get('title'))
        if not links:
            bad.append((k, '没有来源链接'))
        if len(links) > MAX_LINKS:
            bad.append((k, '链接超过 %d 条' % MAX_LINKS))
        for l in links:
            u = l.get('u') or ''
            if not u.startswith('https://') and not u.startswith('http://'):
                bad.append((k, '链接协议不合法：%s' % u))
            if not l.get('t'):
                bad.append((k, '链接缺少标签：%s' % u))
    print('条目 %d 条' % (len(data['events']) + len(data.get('pending', []))))
    if bad:
        print('[未通过] %d 项：' % len(bad))
        for k, msg in bad[:20]:
            print('  %s  ← %s' % (k, msg))
        return 1
    print('[通过] 每条均有 1—%d 个来源链接，协议合法。' % MAX_LINKS)
    return 0


def main():
    ap = argparse.ArgumentParser(description='大事日历来源链接')
    ap.add_argument('--portal', default=DEFAULT_PORTAL)
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('stats', help='覆盖情况，--urls 列出唯一 URL')
    s.add_argument('--urls', action='store_true')
    s.set_defaults(func=cmd_stats)
    a = sub.add_parser('apply', help='写入 links（幂等）')
    a.add_argument('--dry-run', action='store_true')
    a.set_defaults(func=cmd_apply)
    c = sub.add_parser('check', help='校验链接数量与协议')
    c.set_defaults(func=cmd_check)
    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == '__main__':
    main()
