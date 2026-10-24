#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国金门户 · 美国政策文件定时抓取
================================

每周六运行（对应美国时间周五），从官方来源抓取三个子板块的政策文件，
去重合并后回写 index.html 中的 <script type="application/json" id="policy-data"> 数据块。

数据来源（均为美国政府官方接口）：
  白宫政策    —— 联邦公报（Federal Register）总统文件（行政令/备忘录/公告）
                 + 总统行政办公室（EOP）各组成机构（OMB、USTR、OSTP 等，
                 见 WHITEHOUSE_AGENCY_SLUGS）在联邦公报发布的文件
                 + 内阁各部（见 CABINET_DEPT_SLUGS）发布的规则与拟议规则
                 （部委通告类程序文件不收录，控制信噪比）
  国会政策    —— 国会官网 congress.gov 官方 API，参众两院法案/决议最新动态
  独立机构政策 —— 联邦公报按机构过滤（美联储、FCC，见 AGENCY_SLUGS，可自行增删）

选举动态板块按需求不抓取，脚本不涉及。

用法：
  python3 fetch_policy.py              # 正常抓取并回写 index.html
  python3 fetch_policy.py --days 14    # 覆盖回看天数（默认 10）
  python3 fetch_policy.py --dry-run    # 只抓取打印统计，不回写文件

环境变量：
  CONGRESS_API_KEY  国会 API 密钥（优先级：环境变量 > scripts/.congress_api_key
                    文件 > DEMO_KEY；密钥申请 https://api.congress.gov/sign-up/）

日志：logs/fetch.log（自动轮转，1MB×3），同时输出到控制台。
退出码：0 全部成功；1 部分成功（已回写成功板块）；2 全部失败（不回写，保留旧数据）。
"""

import argparse
import json
import logging
import logging.handlers
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------- 配置

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_HTML = os.path.join(BASE_DIR, 'index.html')
LOG_FILE = os.path.join(BASE_DIR, 'logs', 'fetch.log')

LOOKBACK_DAYS = 10          # 回看天数；每周运行的话 7 天足够，10 天留缓冲
MAX_ITEMS_PER_BOARD = 60    # 每个板块在页面上保留的最大条目数
# 分板块上限覆盖：白宫板块含总统文件 + EOP 机构 + 内阁各部规则，条数多，上限放宽
MAX_OVERRIDES = {'whitehouse': 150}
HTTP_TIMEOUT = 20           # 单次请求超时（秒）
HTTP_RETRIES = 3            # 单源请求最大重试次数
USER_AGENT = ('sinolink-portal-policy-fetcher/1.0 '
              '(+https://github.com/liulijia123456/sinolink-portal)')

def _load_congress_key():
    """优先级：环境变量 CONGRESS_API_KEY > scripts/.congress_api_key 文件 > DEMO_KEY。"""
    env = os.environ.get('CONGRESS_API_KEY')
    if env:
        return env.strip()
    key_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.congress_api_key')
    try:
        with open(key_file, encoding='utf-8') as f:
            k = f.read().strip()
            if k:
                return k
    except OSError:
        pass
    return 'DEMO_KEY'

CONGRESS_API_KEY = _load_congress_key()

# 独立机构板块收录的联邦机构（Federal Register agency slug → 中文标签）
# 范围：美联储 + 联邦通信委员会（FCC）；不含 CFPB 等其他机构。
AGENCY_SLUGS = {
    'federal-reserve-system': '美联储（Federal Reserve）',
    'federal-communications-commission': '联邦通信委员会 FCC',
}

FR_API = 'https://www.federalregister.gov/api/v1/documents.json'
CONGRESS_API = 'https://api.congress.gov/v3/bill'

# 白宫板块收录的总统行政办公室（EOP）组成机构（Federal Register agency slug → 中文标签）
# USTR 之外涵盖 OMB、OSTP、CEQ、ONDCP、NSC、NEC；CEA 在联邦公报无发文记录，不收。
WHITEHOUSE_AGENCY_SLUGS = {
    'trade-representative-office-of-united-states': '美国贸易代表办公室 USTR',
    'management-and-budget-office': '管理与预算办公室 OMB',
    'science-and-technology-policy-office': '科技政策办公室 OSTP',
    'council-on-environmental-quality': '环境质量委员会 CEQ',
    'office-of-national-drug-control-policy': '国家药物管制政策办公室 ONDCP',
    'national-security-council': '国家安全委员会 NSC',
    'national-economic-council': '国家经济委员会 NEC',
}

# 白宫板块收录的内阁各部（Federal Register agency slug → 中文标签）
# 部委发文量大（10 天约 440 条），仅收录规则（Rule）与拟议规则（Proposed Rule），
# 不收录通告（Notice）等程序性文件，以保证板块信噪比。
CABINET_DEPT_SLUGS = {
    'state-department': '国务院',
    'treasury-department': '财政部',
    'defense-department': '国防部',
    'justice-department': '司法部',
    'interior-department': '内政部',
    'agriculture-department': '农业部',
    'commerce-department': '商务部',
    'labor-department': '劳工部',
    'health-and-human-services-department': '卫生与公众服务部',
    'housing-and-urban-development-department': '住房与城市发展部',
    'transportation-department': '交通部',
    'energy-department': '能源部',
    'education-department': '教育部',
    'veterans-affairs-department': '退伍军人事务部',
    'homeland-security-department': '国土安全部',
}

# 内阁各部的默认口径是「只收规则与拟议规则」（见 fetch_whitehouse 第 3 段），
# 但个别部门几乎不发规则，政策信号主要体现为通告，按默认口径会长期空置
# （实测国防部近 30 天 84 条全部为 Notice，板块内因此 0 条）。
# 这里为这些部门单独放宽到通告，同时用关键词剔除纯程序性、批量重复的条目，
# 并限制条数，避免它们挤占板块。
DEPT_NOTICE_EXTRA = {
    'defense-department': {
        'allow': ('NOTICE',),
        # 剔除：军售通报（每天多条、标题完全相同）、OMB 报送与信息收集征求意见、
        # 《隐私法》比对项目与系统记录——均为程序性文件，无政策含量
        'exclude_kw': (
            'arms sales notification',
            'submission for omb review',
            'proposed collection',
            'privacy act of 1974',
        ),
        'max': 8,   # 该部门每次抓取最多保留的通告条数
    },
}

# 联邦公报文档类型 → 中文标签（API 返回为 Title Case）
FR_KIND = {
    'Rule': '规则',
    'Proposed Rule': '拟议规则',
    'Notice': '通告',
    'Presidential Document': '总统文件',
    'Sunshine Act Document': '阳光法文件',
}
# 总统文件子类型 → 中文标签（按 presidential_document_type 分次查询精确标注）
PRESDOCU_KIND = {
    'executive_order': '行政令',
    'memorandum': '总统备忘录',
    'proclamation': '公告',
    'determination': '总统决定',
    'notice': '总统通告',
}
# 国会法案类型 → 中文标签
CONGRESS_KIND = {
    'S': '参议院法案', 'HR': '众议院法案',
    'SRES': '参议院决议', 'HRES': '众议院决议',
    'SJRES': '参院联合决议', 'HJRES': '众院联合决议',
    'SCONRES': '参院共同决议', 'HCONRES': '众院共同决议',
}
CONGRESS_CHAMBER_URL = {'S': 'senate', 'HR': 'house', 'SRES': 'senate',
                        'HRES': 'house', 'SJRES': 'senate', 'HJRES': 'house',
                        'SCONRES': 'senate', 'HCONRES': 'house'}
# 国会法案类型 → URL 中的路径段。注意：congress.gov 对法案与三类决议使用不同的
# 路径段（…-bill / …-resolution / …-joint-resolution / …-concurrent-resolution）。
# 早期版本统一写成 '-bill'，导致决议的编号被当作同号法案解析（如 S.Res.951 错指向
# S.951「Stop Comstock Act」），既跳错原文也抓错素材。此处按类型分别映射。
CONGRESS_URL_SLUG = {
    'S': 'senate-bill', 'HR': 'house-bill',
    'SRES': 'senate-resolution', 'HRES': 'house-resolution',
    'SJRES': 'senate-joint-resolution', 'HJRES': 'house-joint-resolution',
    'SCONRES': 'senate-concurrent-resolution',
    'HCONRES': 'house-concurrent-resolution',
}

DATA_BLOCK_RE = re.compile(
    r'(<script type="application/json" id="policy-data"[^>]*>).*?(</script>)',
    re.S)

# ---------------------------------------------------------------- 日志

def setup_logger():
    logger = logging.getLogger('policy_fetch')
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S')
    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=1024 * 1024, backupCount=3, encoding='utf-8')
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger

log = setup_logger()

# ---------------------------------------------------------------- HTTP

def http_get_json(url, params, source_name):
    """带重试的 GET JSON。返回解析后的 dict；失败抛出最后一次异常。"""
    qs = urllib.parse.urlencode(params, doseq=True)
    full = url + '?' + qs
    last_err = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            req = urllib.request.Request(full, headers={'User-Agent': USER_AGENT})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                raw = resp.read().decode('utf-8', errors='replace')
            return json.loads(raw)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                # 限流：等待后重试（DEMO_KEY 每小时配额，等一轮即可恢复）
                last_err = e
                log.warning('[%s] HTTP 429 限流（第 %d/%d 次），等待 70s 后重试',
                            source_name, attempt, HTTP_RETRIES)
                if attempt < HTTP_RETRIES:
                    time.sleep(70)
                continue
            # 其他 4xx 客户端错误重试无意义，直接失败
            if 400 <= e.code < 500:
                log.error('[%s] HTTP %s（客户端错误，不重试）: %s', source_name, e.code, full)
                raise
            last_err = e
            log.warning('[%s] HTTP %s（第 %d/%d 次），将重试', source_name, e.code, attempt, HTTP_RETRIES)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            last_err = e
            log.warning('[%s] 请求失败（第 %d/%d 次）: %s', source_name, attempt, HTTP_RETRIES, e)
        if attempt < HTTP_RETRIES:
            time.sleep(2 ** attempt)  # 2s, 4s 指数退避
    raise RuntimeError('%s 连续 %d 次请求失败: %s' % (source_name, HTTP_RETRIES, last_err))

# ---------------------------------------------------------------- 抓取源

def _fr_params(extra):
    params = {
        'per_page': 100,
        'order': 'newest',
        'fields[]': ['title', 'publication_date', 'html_url', 'type',
                     'document_number', 'executive_order_number', 'agencies'],
    }
    params.update(extra)
    return params


def _fr_agency_names(doc):
    names = [a.get('name', '') for a in doc.get('agencies', []) if isinstance(a, dict)]
    return ' / '.join(n for n in names if n) or '联邦公报'


def _fr_item(board, doc, kind, agency=None):
    return {
        'board': board,
        'date': doc.get('publication_date', ''),
        'title': (doc.get('title') or '').strip(),
        'url': doc.get('html_url', ''),
        'source': '联邦公报',
        'kind': kind,
        'agency': agency or _fr_agency_names(doc),
    }


def fetch_whitehouse(since):
    """白宫政策：联邦公报总统文件 + USTR 文件。"""
    items = []
    # 1) 总统文件：按子类型分次查询，精确标注 行政令/总统备忘录/公告
    for ptype in ('executive_order', 'memorandum', 'proclamation'):
        params = _fr_params({
            'conditions[type][]': ['PRESDOCU'],
            'conditions[presidential_document_type][]': [ptype],
            'conditions[publication_date][gte]': since,
        })
        data = http_get_json(FR_API, params, '白宫-' + PRESDOCU_KIND[ptype])
        n = 0
        for doc in data.get('results', []):
            items.append(_fr_item('whitehouse', doc, PRESDOCU_KIND[ptype],
                                  agency='美国总统行政办公室'))
            n += 1
        log.info('[白宫-%s] 抓到 %d 条（%s 以来）', PRESDOCU_KIND[ptype], n, since)
        time.sleep(1)  # 礼貌限速

    # 2) 总统行政办公室（EOP）各组成机构在联邦公报的文件（逐机构独立容错：
    #    单个机构失败不影响总统文件与其他机构的结果）
    for slug, label in WHITEHOUSE_AGENCY_SLUGS.items():
        agency_params = _fr_params({
            'conditions[agencies][]': [slug],
            'conditions[publication_date][gte]': since,
        })
        try:
            agency_data = http_get_json(FR_API, agency_params, '白宫-' + label)
            n_ag = 0
            for doc in agency_data.get('results', []):
                kind = FR_KIND.get(doc.get('type'), doc.get('type') or '文件')
                items.append(_fr_item('whitehouse', doc, kind, agency=label))
                n_ag += 1
            log.info('[白宫-%s] 抓到 %d 条', label, n_ag)
        except Exception as e:
            log.error('[白宫-%s] 抓取失败: %s（仅该机构子源失败，其他结果保留）', label, e)
        time.sleep(1)  # 礼貌限速

    # 3) 内阁各部：默认仅收录规则与拟议规则（通告类程序文件不收，控制信噪比）；
    #    对 DEPT_NOTICE_EXTRA 中列明的部门额外收录通告（剔除程序性噪声后限量）
    for slug, label in CABINET_DEPT_SLUGS.items():
        extra = DEPT_NOTICE_EXTRA.get(slug)
        types = ['RULE', 'PRORULE']
        if extra:
            types = types + list(extra.get('allow', ()))
        dept_params = _fr_params({
            'conditions[agencies][]': [slug],
            'conditions[publication_date][gte]': since,
            'conditions[type][]': types,
        })
        try:
            dept_data = http_get_json(FR_API, dept_params, '白宫-' + label)
            n_dept = 0
            n_extra = 0
            for doc in dept_data.get('results', []):
                if extra and doc.get('type') == 'Notice':
                    title_l = (doc.get('title') or '').lower()
                    if any(kw in title_l for kw in extra.get('exclude_kw', ())):
                        continue
                    if n_extra >= int(extra.get('max', 0)):
                        continue
                    n_extra += 1
                kind = FR_KIND.get(doc.get('type'), doc.get('type') or '文件')
                items.append(_fr_item('whitehouse', doc, kind, agency=label))
                n_dept += 1
            log.info('[白宫-内阁:%s] 抓到 %d 条', label, n_dept)
        except Exception as e:
            log.error('[白宫-内阁:%s] 抓取失败: %s（仅该部子源失败，其他结果保留）', label, e)
        time.sleep(1)  # 礼貌限速
    return items


def fetch_congress(since):
    """国会政策：congress.gov API 最新法案/决议动态。"""
    items = []
    offset = 0
    # 最多翻 3 页（150 条），按 updateDate 本地过滤；DEMO_KEY 限流，控制请求数
    for page in range(3):
        params = {
            'api_key': CONGRESS_API_KEY,
            'format': 'json',
            'sort': 'updateDate desc',
            'limit': 50,
            'offset': offset,
        }
        data = http_get_json(CONGRESS_API, params, '国会')
        bills = data.get('bills', [])
        if not bills:
            break
        reached_old = False
        for b in bills:
            upd = (b.get('updateDate') or '')[:10]
            if upd and upd < since:
                reached_old = True
                continue
            btype = (b.get('type') or '').upper()
            chamber = CONGRESS_CHAMBER_URL.get(btype, 'house')
            kind = CONGRESS_KIND.get(btype, btype or '法案')
            slug = CONGRESS_URL_SLUG.get(btype, chamber + '-bill')
            url = 'https://www.congress.gov/bill/%sth-congress/%s/%s' % (
                b.get('congress'), slug, b.get('number'))
            latest = (b.get('latestAction') or {}).get('text', '')
            items.append({
                'board': 'congress',
                'date': upd or (b.get('introducedDate') or ''),
                'title': (b.get('title') or '').strip(),
                'url': url,
                'source': '国会官网',
                'kind': kind,
                'agency': ('参议院' if chamber == 'senate' else '众议院')
                          + (' · ' + latest[:60] if latest else ''),
            })
        if reached_old or 'next' not in data.get('pagination', {}):
            break
        offset += 50
        time.sleep(1)  # 礼貌限速
    log.info('[国会] 抓到 %d 条（%s 以来）', len(items), since)
    return items


def fetch_agencies(since):
    """独立机构政策：联邦公报按机构（美联储、FCC）。"""
    items = []
    for slug, label in AGENCY_SLUGS.items():
        params = _fr_params({
            'conditions[agencies][]': [slug],
            'conditions[publication_date][gte]': since,
        })
        data = http_get_json(FR_API, params, '独立机构-' + label)
        n = 0
        for doc in data.get('results', []):
            kind = FR_KIND.get(doc.get('type'), doc.get('type') or '文件')
            items.append(_fr_item('agencies', doc, kind, agency=label))
            n += 1
        log.info('[独立机构-%s] 抓到 %d 条', label, n)
        time.sleep(1)  # 礼貌限速
    return items

# ---------------------------------------------------------------- 合并与回写

def read_index():
    with open(INDEX_HTML, 'r', encoding='utf-8') as f:
        return f.read()


def extract_payload(html):
    m = DATA_BLOCK_RE.search(html)
    if not m:
        raise RuntimeError('index.html 中未找到 id="policy-data" 数据块，请先检查页面结构')
    try:
        payload = json.loads(m.group(0)[len(m.group(1)):-len(m.group(2))])
    except json.JSONDecodeError:
        log.warning('旧数据块 JSON 解析失败，按空数据处理')
        payload = {}
    payload.setdefault('fetched_at', None)
    payload.setdefault('boards', {})
    for key in ('whitehouse', 'congress', 'agencies'):
        payload['boards'].setdefault(key, {'updated_at': None, 'items': []})
    return payload


def merge_items(old_items, new_items, max_n):
    """按 url 去重合并，新条目覆盖旧条目（保留旧条目已生成的简报/翻译/领域标签）；按日期倒序截断。"""
    # 人工生成字段：新抓条目不含这些字段，须从旧条目沿用
    CARRY_FIELDS = ('brief', 'title_cn', 'domain')
    old_by_url = {it.get('url'): it for it in old_items if it.get('url')}
    merged = {}
    for it in old_items + new_items:
        if not it.get('url'):
            continue
        if it['url'] in old_by_url and it is not old_by_url[it['url']]:
            # 新抓的同一条目：沿用已生成的简报、中文翻译与领域标签
            old = old_by_url[it['url']]
            carried = {f: old[f] for f in CARRY_FIELDS
                       if old.get(f) and not it.get(f)}
            if carried:
                it = dict(it)
                it.update(carried)
        merged[it['url']] = it
    out = sorted(merged.values(), key=lambda x: (x.get('date') or ''), reverse=True)
    return out[:max_n]


def write_payload(payload):
    html = read_index()
    block = json.dumps(payload, ensure_ascii=False, indent=2)
    new_html, n = DATA_BLOCK_RE.subn(
        lambda m: m.group(1) + '\n' + block + '\n' + m.group(2), html)
    if n != 1:
        raise RuntimeError('数据块替换失败（匹配 %d 处，应为 1）' % n)
    with open(INDEX_HTML, 'w', encoding='utf-8') as f:
        f.write(new_html)

# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description='国金门户 · 美国政策文件定时抓取')
    ap.add_argument('--days', type=int, default=LOOKBACK_DAYS, help='回看天数（默认 %(default)s）')
    ap.add_argument('--dry-run', action='store_true', help='只抓取打印统计，不回写 index.html')
    args = ap.parse_args()

    tz8 = timezone(timedelta(hours=8))
    now = datetime.now(tz8)
    since = (now - timedelta(days=args.days)).strftime('%Y-%m-%d')
    stamp = now.strftime('%Y-%m-%d %H:%M')

    log.info('========== 抓取开始（回看 %d 天，since %s） ==========', args.days, since)

    sources = [
        ('whitehouse', '白宫政策', fetch_whitehouse),
        ('congress', '国会政策', fetch_congress),
        ('agencies', '独立机构政策', fetch_agencies),
    ]

    payload = extract_payload(read_index())
    ok, fail = [], []

    for key, name, fn in sources:
        try:
            new_items = fn(since)
            board = payload['boards'][key]
            old_n = len(board.get('items', []))
            board['items'] = merge_items(board.get('items', []), new_items,
                                         MAX_OVERRIDES.get(key, MAX_ITEMS_PER_BOARD))
            board['updated_at'] = stamp
            log.info('[%s] 合并完成：旧 %d 条 + 新 %d 条 → 共 %d 条',
                     name, old_n, len(new_items), len(board['items']))
            ok.append((name, len(new_items), len(board['items'])))
        except Exception as e:
            log.error('[%s] 抓取失败: %s（保留旧数据，本板块不更新）', name, e)
            fail.append((name, str(e)))

    if not ok:
        log.error('========== 全部来源失败，不回写文件（旧数据完整保留） ==========')
        return 2

    payload['fetched_at'] = stamp

    if args.dry_run:
        log.info('dry-run 模式：不回写 index.html')
        for name, new_n, total_n in ok:
            log.info('  %s：新增 %d 条，累计 %d 条', name, new_n, total_n)
        return 0

    try:
        write_payload(payload)
    except Exception as e:
        log.error('回写 index.html 失败: %s（本次抓取结果未保存，下次将重新抓取）', e)
        return 2

    for name, new_n, total_n in ok:
        log.info('  %s：新增 %d 条，累计 %d 条', name, new_n, total_n)
    if fail:
        for name, err in fail:
            log.warning('  %s：失败 —— %s', name, err)
        log.warning('========== 部分成功（%d 成功 / %d 失败），已回写成功板块 ==========',
                    len(ok), len(fail))
        return 1

    log.info('========== 抓取完成，index.html 已更新 ==========')
    return 0


if __name__ == '__main__':
    sys.exit(main())
