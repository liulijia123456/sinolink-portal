#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国金门户 · 部门新闻稿（press releases）定时抓取
================================================

从白宫及内阁各部、独立机构的新闻发布页抓取条目，写入
data/policy/press-{whitehouse,agencies}.json，供美国板块「政策来源 → 新闻动态」渲染。

**与政策条目的分工**：政策条目取自联邦公报（Rules / Proposed Rules / Notices），
本脚本只收各机构主动对外发布的 press release / statement / remarks，
两条流水线互不写入对方的数据文件。

数据源两类（见 SOURCES）：
  rss  —— 官方 RSS/Atom，结构化、最稳，优先
  html —— 列表页按链接模式提取，需站点提供 <time datetime>；站点改版时只调 link_re

收录口径：
  · 只收官方域名的发布页条目，按 url 去重，纯增量累积，不截断历史
  · 日期取 <time datetime> 或 RSS pubDate；取不到日期的条目丢弃（避免无时序噪声）
  · 源站返回 403 / 超时的机构在配置里 enabled=False 并写明原因，页面侧显示「源站限制」，
    等有可用出口再打开，无需改代码

用法：
  python3 scripts/fetch_press.py --board all
  python3 scripts/fetch_press.py --board whitehouse --days 30
  python3 scripts/fetch_press.py --board agencies --dry-run
退出码：0 正常；1 全部来源失败；2 参数错误
"""

import argparse
import email.utils
import html as htmllib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urljoin
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, 'data', 'policy')
BEIJING = timezone(timedelta(hours=8))
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36')

# 默认直连，不继承系统代理：urllib 走 _scproxy 会握手超时。
# 需要换出口时用 --proxy 或环境变量 PRESS_PROXY 指定（如 http://127.0.0.1:7890）：
# 部分部门站点对数据中心 / 非美国 IP 做 Akamai 拦截，换到美国住宅出口才能取到。
OPENER = None


def get_opener():
    global OPENER
    if OPENER is None:
        OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return OPENER


def set_proxy(url):
    global OPENER
    if url:
        OPENER = urllib.request.build_opener(
            urllib.request.ProxyHandler({'http': url, 'https': url}))
        log('出口代理：' + url)

TIMEOUT = 20
MAX_PER_SOURCE = 40          # 单源单轮最多取多少条
RETENTION_DAYS = 540         # 保留历史窗口（仅用于提示，不做截断）

# ---------------------------------------------------------------------------
# 源清单
#   type=rss   ：官方 feed
#   type=html  ：列表页；link_re 限定条目路径，避免抓到导航与标签页
#   enabled=False 的源仍保留在页面侧栏，抓取时跳过并在 sources 中记为 skipped
# ---------------------------------------------------------------------------
SOURCES = {
    'whitehouse': [
        {'agency': '白宫', 'name': 'White House', 'type': 'html',
         'url': 'https://www.whitehouse.gov/news/',
         'link_re': r'/(briefings-statements|presidential-actions|fact-sheets|remarks)/',
         'require_re': r'/20\d{2}/\d{2}/'},
        {'agency': '国务院', 'name': 'State Department', 'type': 'rss',
         'url': 'https://www.state.gov/rss-feed/press-releases/feed/'},
        {'agency': '财政部', 'name': 'Treasury', 'type': 'html',
         'url': 'https://home.treasury.gov/news/press-releases',
         'link_re': r'/news/press-releases/',
         'require_re': r'/news/press-releases/[a-z]{2}\d'},
        {'agency': '内政部', 'name': 'Interior', 'type': 'html',
         'url': 'https://www.doi.gov/news',
         'link_re': r'/pressreleases/',
         'require_re': r'/pressreleases/[a-z]'},
        {'agency': '能源部', 'name': 'Energy', 'type': 'html',
         'url': 'https://www.energy.gov/newsroom',
         'link_re': r'/articles/',
         'require_re': r'/articles/[a-z]'},
        {'agency': '退伍军人事务部', 'name': 'Veterans Affairs', 'type': 'html',
         'url': 'https://www.va.gov/opa/pressrel/',
         'link_re': r'(/pressrel/pressrelease\.cfm|news\.va\.gov/)'},
        {'agency': '国土安全部', 'name': 'Homeland Security', 'type': 'html',
         'url': 'https://www.dhs.gov/news-releases/press-releases',
         'link_re': r'/news/20\d{2}/\d{2}/\d{2}/'},
        # 以下源站对数据中心 IP 做 IP 级拦截（Akamai），直连 / 系统代理 / 无头浏览器均 403
        {'agency': '国防部', 'name': 'Defense', 'type': 'html', 'enabled': False,
         'url': 'https://www.defense.gov/News/Releases/',
         'link_re': r'defense\.gov/News/Releases/Release/',
         'blocked': 'Akamai 403'},
        {'agency': '司法部', 'name': 'Justice', 'type': 'rss', 'enabled': False,
         'url': 'https://www.justice.gov/feeds/opa/justice-news.xml',
         'blocked': '连接超时'},
        {'agency': '农业部', 'name': 'Agriculture', 'type': 'html', 'enabled': False,
         'url': 'https://www.usda.gov/media/press-releases',
         'link_re': r'usda\.gov/media/press-releases/',
         'blocked': 'Akamai 403'},
        {'agency': '商务部', 'name': 'Commerce', 'type': 'html', 'enabled': False,
         'url': 'https://www.commerce.gov/news/press-releases',
         'link_re': r'commerce\.gov/news/press-releases/',
         'blocked': 'Akamai 403'},
        {'agency': '劳工部', 'name': 'Labor', 'type': 'html', 'enabled': False,
         'url': 'https://www.dol.gov/newsroom/releases',
         'link_re': r'dol\.gov/newsroom/releases/',
         'blocked': 'Akamai 403'},
        {'agency': '卫生与公众服务部', 'name': 'HHS', 'type': 'html', 'enabled': False,
         'url': 'https://www.hhs.gov/news/press-announcements/index.html',
         'link_re': r'hhs\.gov/news/',
         'blocked': 'Akamai 403'},
        {'agency': '住房与城市发展部', 'name': 'HUD', 'type': 'html', 'enabled': False,
         'url': 'https://www.hud.gov/press/press_releases_media_advisories',
         'link_re': r'hud\.gov/press/',
         'blocked': 'Akamai 403'},
        {'agency': '交通部', 'name': 'Transportation', 'type': 'html', 'enabled': False,
         'url': 'https://www.transportation.gov/newsroom',
         'link_re': r'transportation\.gov/newsroom/',
         'blocked': 'Akamai 403'},
        {'agency': '教育部', 'name': 'Education', 'type': 'html', 'enabled': False,
         'url': 'https://www.ed.gov/news/press-releases',
         'link_re': r'ed\.gov/news/press-releases/',
         'blocked': 'Akamai 403'},
    ],
    'agencies': [
        {'agency': '美联储（Federal Reserve）', 'name': 'Federal Reserve', 'type': 'rss',
         'url': 'https://www.federalreserve.gov/feeds/press_all.xml'},
        {'agency': '外国投资委员会 CFIUS', 'name': 'CFIUS', 'type': 'html',
         'url': 'https://home.treasury.gov/policy-issues/international/'
                'the-committee-on-foreign-investment-in-the-united-states-cfius',
         'link_re': r'/news/press-releases/',
         'require_re': r'/news/press-releases/[a-z]{2}\d'},
        {'agency': '联邦通信委员会 FCC', 'name': 'FCC', 'type': 'html', 'enabled': False,
         'url': 'https://www.fcc.gov/news-events/headlines',
         'link_re': r'fcc\.gov/document/',
         'blocked': 'Akamai 403'},
        {'agency': '国际贸易委员会 USITC', 'name': 'USITC', 'type': 'html', 'enabled': False,
         'url': 'https://www.usitc.gov/press_room/news_release',
         'link_re': r'usitc\.gov/press_room/news_release/',
         'blocked': 'Akamai 403'},
        {'agency': '国际开发署 USAID', 'name': 'USAID', 'type': 'html', 'enabled': False,
         'url': 'https://www.usaid.gov/news-information/press-releases',
         'link_re': r'usaid\.gov/news-information/press-releases/',
         'blocked': '机构已停摆 / 站点 403'},
    ],
}

BOARD_LABEL = {'whitehouse': '白宫及内阁各部', 'agencies': '独立机构'}
MONTHS = {m: i for i, m in enumerate(
    ['January', 'February', 'March', 'April', 'May', 'June', 'July',
     'August', 'September', 'October', 'November', 'December'], 1)}
TAG_RE = re.compile(r'<[^>]+>')
WS_RE = re.compile(r'\s+')


def log(msg):
    print('[press] ' + msg, flush=True)


def clean_text(s):
    s = htmllib.unescape(TAG_RE.sub(' ', s or ''))
    return WS_RE.sub(' ', s).strip()


def fetch(url):
    req = urllib.request.Request(url, headers={
        'User-Agent': UA,
        'Accept': 'application/rss+xml,application/xml,text/xml,text/html;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Cache-Control': 'no-cache',
    })
    with get_opener().open(req, timeout=TIMEOUT) as r:
        raw = r.read()
        if r.headers.get('content-encoding') == 'gzip':
            import gzip
            raw = gzip.decompress(raw)
        return raw.decode(r.headers.get_content_charset() or 'utf-8', 'replace')


def norm_date(value):
    """把 RSS pubDate / ISO datetime / 常见英文日期统一成 YYYY-MM-DD。"""
    if not value:
        return ''
    v = value.strip()
    # ISO 8601：2026-11-01T10:24:46-05:00
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', v)
    if m:
        return '%s-%s-%s' % m.groups()
    # RSS：Fri, 31 Oct 2026 12:00:00 +0000
    try:
        dt = email.utils.parsedate_to_datetime(v)
        if dt:
            return dt.date().isoformat()
    except Exception:
        pass
    # 英文长日期：November 1, 2026
    m = re.match(r'([A-Z][a-z]+)\s+(\d{1,2}),?\s+(\d{4})', v)
    if m and m.group(1) in MONTHS:
        return '%s-%02d-%02d' % (m.group(3), MONTHS[m.group(1)], int(m.group(2)))
    return ''


def kind_of(title, category=''):
    t = (title or '').lower()
    c = (category or '').lower()
    if 'statement' in t or 'statement' in c:
        return '声明'
    if 'remarks' in t or 'speech' in t or 'remarks' in c:
        return '讲话'
    if 'executive order' in t or 'proclamation' in t or 'memorandum' in t:
        return '行政行动'
    if 'fact sheet' in t or 'fact-sheets' in c:
        return '事实清单'
    return '新闻稿'


def parse_rss(xml_text):
    """RSS 2.0 / Atom 通用解析，返回 [{date,title,url,summary}]。"""
    out = []
    try:
        root = ET.fromstring(xml_text.encode('utf-8'))
    except ET.ParseError:
        # 个别 feed 带 BOM 或前置空白
        root = ET.fromstring(xml_text.lstrip('\ufeff \r\n\t').encode('utf-8'))
    ns = {'atom': 'http://www.w3.org/2005/Atom',
          'dc': 'http://purl.org/dc/elements/1.1/',
          'content': 'http://purl.org/rss/1.0/modules/content/'}
    nodes = root.findall('.//item') or root.findall('.//atom:entry', ns)
    for it in nodes:
        def pick(tag, atom_tag=None):
            el = it.find(tag)
            if el is None and atom_tag:
                el = it.find('atom:' + atom_tag, ns)
            return el
        title = pick('title', 'title')
        link = pick('link')
        url = ''
        if link is not None:
            url = (link.text or '').strip() or link.get('href', '')
        if not url:
            # Atom 的 link 在属性里
            for l in it.findall('atom:link', ns):
                if l.get('rel', 'alternate') == 'alternate':
                    url = l.get('href', '')
                    break
        date = ''
        for tag, atom_tag in (('pubDate', None), ('{http://purl.org/dc/elements/1.1/}date', None),
                              ('published', 'published'), ('updated', 'updated')):
            el = pick(tag, atom_tag)
            if el is not None and (el.text or '').strip():
                date = norm_date(el.text)
                if date:
                    break
        desc = ''
        for tag, atom_tag in (('description', None), ('{http://purl.org/rss/1.0/modules/content/}encoded', None),
                              ('summary', 'summary')):
            el = pick(tag, atom_tag)
            if el is not None and (el.text or '').strip():
                desc = clean_text(el.text)
                if desc:
                    break
        if url and title is not None and (title.text or '').strip():
            out.append({'date': date, 'title': clean_text(title.text), 'url': url.strip(),
                        'summary': desc[:400]})
    return out


def parse_html_list(html_text, source):
    """列表页提取：先按链接模式筛候选，再在同一卡片内找最近的日期锚点。

    各站卡片结构不一（白宫 / DHS 是标题在前、日期在后；能源部相反），
    因此对每个候选链接分别在前后各 1200 字符内找 <time datetime> 或英文长日期，
    取距离最近的一个，避免跨卡片错配。取不到日期的条目直接丢弃。
    """
    link_re = re.compile(source.get('link_re', r''), re.I)
    require_re = re.compile(source.get('require_re', r''), re.I) if source.get('require_re') else None
    time_re = re.compile(r'<time[^>]*datetime="([^"]+)"', re.I)
    date_re = re.compile(r'((?:January|February|March|April|May|June|July|August|'
                         r'September|October|November|December)\s+\d{1,2},\s+20\d{2})')
    out = []
    seen = set()

    for m in re.finditer(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html_text, re.S | re.I):
        url = urljoin(source['url'], htmllib.unescape(m.group(1)).strip())
        if not url or url.startswith(('#', 'mailto:', 'javascript:')):
            continue
        if not link_re.search(url):
            continue
        if require_re and not require_re.search(url):
            continue
        title = clean_text(m.group(2))
        if len(title) < 12 or len(title) > 240:
            continue
        if url in seen:
            continue
        # 同卡片内找最近日期：前 1200 字符取最后一个，后 1200 字符取第一个
        before = html_text[max(0, m.start() - 1200):m.start()]
        after = html_text[m.end():m.end() + 1200]
        best = None
        cands = []
        for mm in time_re.finditer(before):
            cands.append((m.start() - (max(0, m.start() - 1200) + mm.start()), mm.group(1)))
        for mm in time_re.finditer(after):
            cands.append((mm.start(), mm.group(1)))
        for mm in date_re.finditer(before):
            cands.append((m.start() - (max(0, m.start() - 1200) + mm.start()), mm.group(1)))
        for mm in date_re.finditer(after):
            cands.append((mm.start(), mm.group(1)))
        for dist, raw in sorted(cands, key=lambda x: abs(x[0])):
            d = norm_date(raw)
            if d:
                best = d
                break
        if not best:
            continue
        seen.add(url)
        out.append({'date': best, 'title': title, 'url': url, 'summary': ''})
        if len(out) >= MAX_PER_SOURCE:
            break
    return out


def load_existing(path):
    if not os.path.exists(path):
        return {'board': '', 'updated_at': '', 'sources': [], 'items': []}
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def save_atomic(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write('\n')
    os.replace(tmp, path)


def run_board(board, days, dry_run):
    sources = SOURCES[board]
    path = os.path.join(DATA_DIR, 'press-%s.json' % board)
    existing = load_existing(path)
    by_url = {}
    for it in existing.get('items', []):
        if it.get('url'):
            by_url[it['url']] = it

    cutoff = (datetime.now(BEIJING) - timedelta(days=days)).date().isoformat()
    stats = []
    added_total = 0
    ok_count = 0

    for src in sources:
        agency = src['agency']
        row = {'agency': agency, 'name': src.get('name', ''),
               'type': src['type'], 'url': src['url'],
               'status': 'skipped', 'fetched': 0, 'added': 0, 'error': ''}
        if not src.get('enabled', True):
            row['error'] = src.get('blocked', '未启用')
            stats.append(row)
            continue
        try:
            body = fetch(src['url'])
            items = parse_rss(body) if src['type'] == 'rss' else parse_html_list(body, src)
            row['fetched'] = len(items)
            if not items:
                row['status'] = 'empty'
            else:
                row['status'] = 'ok'
                ok_count += 1
            for it in items:
                if not it.get('date') or it['date'] < cutoff:
                    continue
                url = it['url']
                rec = {
                    'date': it['date'],
                    'title': it['title'],
                    'title_cn': by_url.get(url, {}).get('title_cn', ''),
                    'agency': agency,
                    'kind': kind_of(it['title'], it.get('category', '')),
                    'url': url,
                    'source': src.get('name', ''),
                    'summary': it.get('summary', ''),
                }
                if url in by_url:
                    # 已有条目只补空缺，不覆盖人工加工过的中文标题与摘要
                    old = by_url[url]
                    for k in ('title_cn', 'summary', 'kind'):
                        if not old.get(k) and rec.get(k):
                            old[k] = rec[k]
                else:
                    by_url[url] = rec
                    added_total += 1
                    row['added'] += 1
        except Exception as e:
            row['status'] = 'error'
            row['error'] = '%s: %s' % (type(e).__name__, str(e)[:120])
        stats.append(row)
        time.sleep(0.6)

    items = sorted(by_url.values(), key=lambda x: (x.get('date', ''), x.get('url', '')), reverse=True)
    out = {
        'board': board,
        'updated_at': datetime.now(BEIJING).strftime('%Y-%m-%dT%H:%M:%S+08:00'),
        'sources': stats,
        'items': items,
    }
    log('%s：源 %d 个（成功 %d），累计 %d 条，本轮新增 %d 条'
        % (BOARD_LABEL[board], len(sources), ok_count, len(items), added_total))
    for r in stats:
        flag = {'ok': '✓', 'empty': '·', 'skipped': '–', 'error': '✗'}[r['status']]
        log('  %s %-14s %-7s 抓 %2d 增 %2d %s'
            % (flag, r['agency'], r['status'], r['fetched'], r['added'], r['error']))
    if dry_run:
        log('（dry-run，未写盘）')
    else:
        save_atomic(path, out)
        log('写入 %s（%.1f KB）' % (os.path.relpath(path, ROOT),
                                   os.path.getsize(path) / 1024.0))
    return ok_count, len(items), added_total


def main():
    ap = argparse.ArgumentParser(description='部门新闻稿抓取')
    ap.add_argument('--board', choices=['whitehouse', 'agencies', 'all'], default='all')
    ap.add_argument('--days', type=int, default=45, help='入库日期窗口，默认 45 天')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--proxy', default=os.environ.get('PRESS_PROXY', ''),
                    help='HTTP 代理，如 http://127.0.0.1:7890；默认直连')
    ap.add_argument('--only', default='', help='只抓指定机构（填 agency 名，用于单源验证）')
    args = ap.parse_args()
    if args.days < 1 or args.days > 3650:
        print('--days 应在 1–3650 之间', file=sys.stderr)
        return 2
    set_proxy(args.proxy.strip())
    if args.only:
        # 单源验证：目标源强制启用，其余全部跳过
        for b in SOURCES:
            for src in SOURCES[b]:
                src['enabled'] = (src['agency'] == args.only)
    boards = ['whitehouse', 'agencies'] if args.board == 'all' else [args.board]
    total_ok = 0
    for b in boards:
        ok, _, _ = run_board(b, args.days, args.dry_run)
        total_ok += ok
    return 0 if total_ok else 1


if __name__ == '__main__':
    sys.exit(main())
