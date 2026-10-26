#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国金门户 · 政策简报工具
======================

配合 scripts/fetch_policy.py 使用，为三个政策子板块的条目生成、管理「政策简报」，
写入数据文件（data/policy/*items.json）中条目的 brief 字段。原条目（标题、
官方原文链接等源文件信息）完整保留。

简报分两层（详见下方「简报分层」说明），**但覆盖率必须是 100%**——
层级只决定详略，不决定有无：

  full  长简报 800–2000 字，四段结构
  note  要点速览 150–300 字，3–5 条要点

三个子命令：

1) 体检覆盖率（发布前必看）：
     python3 brief.py coverage [--board whitehouse]
   按板块报告总条数 / 有简报 / 缺简报，并列出缺失条目的日期与标题。
   退出码：0 全覆盖；1 存在缺失。

2) 生成简报队列（供撰写）：
     python3 brief.py queue [--board whitehouse] [--limit 8] [--fresh-ratio 0.5] [--all]
   输出 scripts/brief_queue.md：按「两头推进」列出尚无简报的条目（最新+最旧），
   并附上从官方接口抓取的摘要/文本节选（联邦公报 abstract、国会官网 bill summary），
   同时标注每条该写 full 还是 note。
   指定 --all 时列出**全部**待写条目（不等量抽样），每日任务用它做全覆盖。
   素材节选长度按层级自适应：full 取 1800 字，note 取 600 字（够写 150–300 字概述）。

3) 合并已写好的简报：
     python3 brief.py merge scripts/briefs.json [--force]
   briefs.json 格式：{ "<条目url>": { "at": "2026-10-12", "tier": "full|note", "body": "…" }, ... }
   已有长简报不会被要点速览覆盖（除非 --force）。

环境变量：CONGRESS_API_KEY（优先级：环境变量 > scripts/.congress_api_key 文件 > DEMO_KEY）
日志：logs/brief.log（自动轮转）
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import portal_store as store          # noqa: E402  统一数据读写（JSON / 内嵌双形态）

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_HTML = os.path.join(BASE_DIR, 'index.html')
LOG_FILE = os.path.join(BASE_DIR, 'logs', 'brief.log')
QUEUE_FILE = os.path.join(BASE_DIR, 'scripts', 'brief_queue.md')

# ---------------------------------------------------------------- 简报分层
#
# **全覆盖策略：每一条目都必须有简报，不允许空简报。**
# 层级只决定篇幅深浅，不决定有无——这是 2026-10-25 从"抽样配额"改过来的核心变化：
# 旧策略下每轮只写 6–8 份长简报、覆盖率约 3%，缺口单调扩大，条目是研究底稿，
# 没有简报的条目对读者等于不存在。
#
#   full  长简报 800–2000 字，四段结构（【政策背景】【核心内容】【要点提炼】【对金融市场的预期影响】）
#   note  要点速览 150–300 字，3–5 条要点，不要求段头
#
# full 判据（任一成立）：
#   - 白宫直接行为：行政令 / 总统备忘录 / 公告
#   - 最终规则（kind=规则）且属高相关领域
#   - 法案已进入实质程序（委员会审议、列入日历、通过等）且属高相关领域
# 其余（拟议规则、程序性通告、决议、低相关领域规则、仅提出未审议的法案）一律 note。
# 层级由脚本判定，不自行改判；判错只影响详略，不影响覆盖率。

TIER_FULL = 'full'
TIER_NOTE = 'note'

# 高市场相关领域：金融资产定价能直接映射到的监管部门/议题
HIGH_VALUE_DOMAINS = {
    '金融/保险', '财政/税收', '贸易/关税', '能源/环境',
    '科技/AI', '医疗/医药', '电信/媒体', '国防/安全',
}

# 白宫直接行为，一律长简报
WH_DIRECT_KINDS = {'行政令', '总统备忘录', '公告'}

# 法案进入实质程序的行动特征（agency 字段在国会板块存的是"最新行动"文本）
# 注意：只收已越过委员会门槛或已通过的行动。"Referred to the Committee on X"
# 只是提出后交付委员会，绝大多数法案止步于此，不算实质进展。
SUBSTANTIVE_ACTIONS = (
    'placed on senate legislative calendar',
    'placed on the union calendar',
    'ordered to be reported',
    'reported by',
    'reported to senate', 'reported to house',
    'passed senate', 'passed house', 'passed/agreed to in',
    'engrossed', 'enrolled', 'signed by president',
    'became public law',
)

# 礼仪/纪念/表彰类：无法律效力，一律要点
CEREMONIAL_RE = re.compile(
    r'^(Expressing|Recognizing|Honoring|Congratulat|Commending|Celebrating|'
    r'Designating|Acknowledging|Welcoming|Mourning)', re.I)

# 例行程序性/区域性文件：技术性或属地性安排，无市场定价含义，一律要点。
# 这类文件常落在"国防/安全""交通"等高价值领域，若只按领域判定会被误升为长简报
# （实测把海岸警卫队安全区、空域设立、渔业休渔、适航指令更正都判成了长简报）。
ROUTINE_RE = re.compile(
    r'(Safety Zone|Security Zone|Security Zones|Special Local Regulation|'
    r'Anchorage Grounds|Drawbridge Operation|'
    r'Class [BCDE] Airspace|Airspace;|Airworthiness Directives|'
    r'Fisheries of the Exclusive Economic Zone|Fisheries Off West Coast|'
    r'Fisheries of the Atlantic|Pacific Halibut|'
    r'Correction$|; Correction)', re.I)


# 往届法案：congress.gov 的 updateDate 会因元数据回填而落在近期窗口，
# 导致第 102—118 届旧案被当作当日新条目收录（实测 35 条）。这类条目不是本届议程，
# 一律只写要点，且简报须注明所属届次，不得当作当届立法进展叙述。
CONGRESS_IN_URL_RE = re.compile(r'/(\d+)th-congress/')


def _current_congress():
    """按年份推算当前国会届次（第 1 届为 1789—1791，每届两年）。"""
    y = datetime.now(timezone(timedelta(hours=8))).year
    return (y - 1789) // 2 + 1


def classify_tier(item):
    """判断条目该写长简报(full)还是要点速览(note)。

    只决定**详略**，不决定有无——每一条目都必须有简报。
    """
    kind = (item.get('kind') or '').strip()
    domain = (item.get('domain') or '').strip()
    title = (item.get('title') or '').strip()

    # 往届法案一律要点（详见上方说明）
    m = CONGRESS_IN_URL_RE.search(item.get('url') or '')
    if m and int(m.group(1)) != _current_congress():
        return TIER_NOTE

    if CEREMONIAL_RE.match(title):
        return TIER_NOTE
    if ROUTINE_RE.search(title):             # 例行程序性/区域性文件
        return TIER_NOTE
    if kind in WH_DIRECT_KINDS:
        return TIER_FULL
    if '决议' in kind:                      # 决议无法律效力
        return TIER_NOTE
    if kind == '规则':                       # 最终规则，有约束力
        return TIER_FULL if domain in HIGH_VALUE_DOMAINS else TIER_NOTE
    if '法案' in kind:
        action = (item.get('agency') or '').lower()
        substantive = any(k in action for k in SUBSTANTIVE_ACTIONS)
        if domain in HIGH_VALUE_DOMAINS and substantive:
            return TIER_FULL
        return TIER_NOTE
    # 拟议规则、通告等程序性文件
    return TIER_NOTE


TIER_DESC = {
    TIER_FULL: '长简报 800–2000 字，四段结构',
    TIER_NOTE: '要点速览 150–300 字，3–5 条要点，不写段头',
}

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
HTTP_TIMEOUT = 25
USER_AGENT = 'sinolink-portal-policy-fetcher/1.0'

FR_DOC_RE = re.compile(r'/documents/\d{4}/\d{2}/\d{2}/(\d{4}-\d+)/')
# 注意：congress.gov 对法案与三类决议使用不同路径段（senate-bill / senate-resolution /
# senate-joint-resolution / senate-concurrent-resolution …），正则须全部覆盖，
# 否则决议类条目抓取不到素材（早期版本只匹配 -bill，导致决议素材为空）。
CG_BILL_RE = re.compile(r'/bill/(\d+)th-congress/([a-z-]+?)-(?:bill|resolution|joint-resolution|concurrent-resolution)/([A-Za-z0-9]+)')
# URL 路径段 → API 的 bill type
CG_SLUG_TYPE = {
    'senate-bill': 's', 'house-bill': 'hr',
    'senate-resolution': 'sres', 'house-resolution': 'hres',
    'senate-joint-resolution': 'sjres', 'house-joint-resolution': 'hjres',
    'senate-concurrent-resolution': 'sconres',
    'house-concurrent-resolution': 'hconres',
}

BOARD_CN = {'whitehouse': '白宫政策', 'congress': '国会政策', 'agencies': '其他部门政策'}


def setup_logger():
    logger = logging.getLogger('policy_brief')
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1024 * 1024, backupCount=3, encoding='utf-8')
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


log = setup_logger()


def http_get_json(url, retries=3):
    last = None
    for i in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                return json.loads(r.read().decode('utf-8', errors='replace'))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                log.warning('HTTP 429 限流，等待 70s 重试（%d/%d）', i, retries)
                time.sleep(70)
                last = e
                continue
            raise
        except Exception as e:
            last = e
            log.warning('请求失败（%d/%d）：%s', i, retries, e)
            time.sleep(2)
    raise RuntimeError('连续 %d 次请求失败：%s' % (retries, last))


def strip_html(html_text):
    text = re.sub(r'<[^>]+>', ' ', html_text or '')
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


# ---------------------------------------------------------------- 数据块读写

def load_payload():
    """统一走 portal_store；返回值保留 3 元组以兼容旧调用（前两项已不再使用）。"""
    return None, None, store.load_policy()


def save_payload(payload):
    store.save_policy(payload)


# ---------------------------------------------------------------- 摘要抓取

def fr_abstract(doc_number):
    """联邦公报：取文档 abstract（含官方摘要）。"""
    url = ('https://www.federalregister.gov/api/v1/documents/%s.json'
           '?fields%%5B%%5D=abstract&fields%%5B%%5D=title&fields%%5B%%5D=body_html_url'
           % doc_number)
    d = http_get_json(url)
    ab = d.get('abstract') or ''
    ab = strip_html(ab) if '<' in ab else ab
    return ab


def congress_summary(congress, chamber_slug, number):
    """国会官网：取法案 summary（官方摘要）；无 summary 时降级取法案详情。"""
    api_type = {'senate': 's', 'house': 'hr'}.get(chamber_slug, chamber_slug)
    try:
        # 注意：官方端点是复数 /summaries（返回列表）；旧写法 /summary 恒 404，
        # 导致所有法案都取不到 CRS 官方摘要、只能降级为法案元数据。
        url = ('https://api.congress.gov/v3/bill/%s/%s/%s/summaries?api_key=%s&format=json'
               % (congress, api_type, number, CONGRESS_API_KEY))
        d = http_get_json(url)
        summaries = d.get('summaries') or []
        if summaries:
            summaries = sorted(summaries, key=lambda x: (x.get('version') or ''))
            text = (summaries[-1].get('text') or '')
            if text:
                return strip_html(text) if '<' in text else text
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    # 降级：法案详情（标题/最新行动/政策领域/委员会）
    url = ('https://api.congress.gov/v3/bill/%s/%s/%s?api_key=%s&format=json'
           % (congress, api_type, number, CONGRESS_API_KEY))
    d = http_get_json(url)
    b = d.get('bill') or {}
    parts = []
    if b.get('title'):
        parts.append('法案标题：%s' % b['title'])
    la = b.get('latestAction') or {}
    if la.get('text'):
        parts.append('最新行动（%s）：%s' % (la.get('actionDate', ''), la['text']))
    pa = b.get('policyArea') or {}
    if pa.get('name'):
        parts.append('政策领域：%s' % pa['name'])
    sas = [s.get('name') for s in (b.get('subjects') or {}).get('legislativeSubjects', []) if s.get('name')]
    if sas:
        parts.append('立法主题：%s' % '；'.join(sas[:12]))
    cms = [c.get('name') for c in (b.get('committees') or {}).get('items', []) if c.get('name')]
    if cms:
        parts.append('主管委员会：%s' % '；'.join(cms[:4]))
    if b.get('introducedDate'):
        parts.append('提出日期：%s（第 %s 届国会）' % (b['introducedDate'], b.get('congress')))
    sponsors = (b.get('sponsors') or [])
    if sponsors:
        sp = sponsors[0]
        parts.append('首席提案人：%s（%s）' % (sp.get('fullName', ''), (sp.get('party') or '') + ''))
    return '\n'.join(parts)


def fetch_material(item):
    """返回撰写素材文本（官方摘要），失败返回空串。"""
    url = item.get('url', '')
    try:
        m = FR_DOC_RE.search(url)
        if m:
            return fr_abstract(m.group(1))
        m = CG_BILL_RE.search(url)
        if m:
            # m.group(2) 是完整路径段（如 senate-resolution），需映射为 API 的 bill type
            full_slug = m.group(2) + '-' + (url.split('/')[5] if len(url.split('/')) > 5 else 'bill')
            api_type = CG_SLUG_TYPE.get(full_slug)
            if not api_type:
                # 回退：从完整 URL 里直接取路径段
                seg = re.search(r'/bill/\d+th-congress/([a-z-]+)/', url)
                api_type = CG_SLUG_TYPE.get(seg.group(1) if seg else '', m.group(2))
            return congress_summary(m.group(1), api_type, m.group(3))
    except Exception as e:
        log.warning('素材抓取失败 %s: %s', url[:70], e)
    return ''


# ---------------------------------------------------------------- 队列模式

def _select_pending(items, limit, fresh_ratio):
    """两头推进选取：一部分取最新（保时效），一部分取最旧（清欠账）。

    items 已按日期倒序。若只取头部，新条目会持续把未写的旧条目压到队尾，
    造成存量永久饥饿——实测缺口在时间上呈"最新一段全空白"即由此而来。
    limit <= 0 时返回全部待写条目（``--all`` 全覆盖模式）。
    """
    pending = [it for it in items if not (it.get('brief') or {}).get('body')]
    if limit <= 0 or len(pending) <= limit:
        return pending
    n_fresh = max(1, int(round(limit * fresh_ratio))) if limit > 1 else limit
    n_fresh = min(n_fresh, limit)
    n_stale = limit - n_fresh
    picked = pending[:n_fresh]
    if n_stale:
        tail = pending[len(pending) - n_stale:]
        seen = {id(x) for x in picked}
        picked += [x for x in tail if id(x) not in seen]
    picked.sort(key=lambda x: (x.get('date') or ''), reverse=True)
    return picked


# note 只需 150–300 字概述，取 600 字素材足够；full 需要完整背景，取 1800 字。
MATERIAL_CHARS = {TIER_FULL: 1800, TIER_NOTE: 600}


def cmd_coverage(args):
    """体检简报覆盖率：每一条目都必须有简报。"""
    _, _, payload = load_payload()
    boards = [args.board] if args.board else ['whitehouse', 'congress', 'agencies']
    total_all = miss_all = 0
    for board in boards:
        items = (payload['boards'].get(board) or {}).get('items', [])
        miss = [it for it in items if not (it.get('brief') or {}).get('body')]
        # 层级口径与 validate_data.py 一致：无 tier 字段的旧简报按长度推断，
        # 否则同一份数据在两个脚本里会报出不同的「长/要点」计数。
        n_full = sum(1 for it in items
                     if (it.get('brief') or {}).get('body')
                     and ((it['brief'].get('tier') or _infer_tier(it['brief']['body']))
                          == TIER_FULL))
        n_note = sum(1 for it in items
                     if (it.get('brief') or {}).get('body')) - n_full
        total_all += len(items)
        miss_all += len(miss)
        pct = (100.0 * (len(items) - len(miss)) / len(items)) if items else 100.0
        print('%-11s %4d 条 · 已写 %4d（长 %d / 要点 %d）· 缺 %3d · 覆盖 %.1f%%'
              % (board, len(items), len(items) - len(miss), n_full, n_note,
                 len(miss), pct))
        if miss:
            dates = sorted({it.get('date') or '' for it in miss})
            print('            缺失日期：%s' % '、'.join(dates[:12])
                  + ('…' if len(dates) > 12 else ''))
            for it in miss[:8]:
                print('            · %s %s' % (it.get('date'),
                                              (it.get('title') or '')[:56]))
            if len(miss) > 8:
                print('            · …… 其余 %d 条' % (len(miss) - 8))
    pct_all = (100.0 * (total_all - miss_all) / total_all) if total_all else 100.0
    print('\n合计 %d 条 · 缺简报 %d 条 · 覆盖 %.1f%%' % (total_all, miss_all, pct_all))
    if miss_all:
        print('[未通过] 存在 %d 条无简报条目，按全覆盖策略不可发布。'
              '运行 brief.py queue --all 生成撰写队列。' % miss_all)
        return 1
    print('[通过] 简报全覆盖。')
    return 0


def cmd_queue(args):
    _, _, payload = load_payload()
    limit = 0 if getattr(args, 'all', False) else args.limit
    mode = ('全量覆盖模式（列出全部待写条目）' if limit <= 0
            else '两头推进抽样：最新 %d%% + 最旧 %d%%'
                 % (round(args.fresh_ratio * 100), round((1 - args.fresh_ratio) * 100)))
    lines = ['# 简报撰写队列', '',
             '> 全覆盖策略：**每一条目都必须有简报**，层级只决定详略、不决定有无。',
             '> 本队列%s。每条标注待写层级：' % mode,
             '>   full = 长简报 800–2000 字，四段结构【政策背景】【核心内容】【要点提炼】【对金融市场的预期影响】',
             '>   note = 要点速览 150–300 字，3–5 条要点，无需段头',
             '> 成果写入 scripts/briefs.json（键为条目 url，含 tier 字段）后运行 merge 合并。',
             '> 合并后运行 brief.py coverage 复核覆盖率。', '']
    total = 0
    boards = [args.board] if args.board else ['whitehouse', 'congress', 'agencies']
    for board in boards:
        items = (payload['boards'].get(board) or {}).get('items', [])
        all_pending = [it for it in items if not (it.get('brief') or {}).get('body')]
        pending = _select_pending(items, limit, args.fresh_ratio)
        if not pending:
            continue
        n_full = sum(1 for it in pending if classify_tier(it) == TIER_FULL)
        lines.append('## %s（%s）待写 %d 条，本次列 %d 条（长简报 %d / 要点 %d）'
                     % (BOARD_CN.get(board, board), board, len(all_pending),
                        len(pending), n_full, len(pending) - n_full))
        lines.append('')
        for it in pending:
            total += 1
            tier = classify_tier(it)
            cap = MATERIAL_CHARS[tier]
            lines.append('### %s · %s · %s' % (it.get('date'), it.get('kind'), it.get('title')))
            lines.append('')
            lines.append('- url: `%s`' % it.get('url'))
            lines.append('- agency: %s' % it.get('agency', ''))
            lines.append('- 待写层级：**%s**（%s）' % (tier, TIER_DESC[tier]))
            lines.append('')
            mat = fetch_material(it)
            if mat:
                shown = mat[:cap]
                lines.append('**官方摘要素材**：')
                lines.append('')
                lines.append(shown + ('…' if len(mat) > cap else ''))
            else:
                lines.append('**官方摘要素材**：（抓取失败，请依据标题与知识谨慎撰写，不确定处须注明）')
            lines.append('')
            time.sleep(1)  # 礼貌限速
    with open(QUEUE_FILE, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    log.info('队列已生成：%s（共 %d 条待写）', QUEUE_FILE, total)
    return 0


# ---------------------------------------------------------------- 合并模式

def _infer_tier(body):
    """未标 tier 时按长度推断：400 字以下视为要点速览（兼容存量旧简报）。"""
    return TIER_FULL if len(body or '') >= 400 else TIER_NOTE


def cmd_merge(args):
    briefs = json.load(open(args.input, encoding='utf-8'))
    _, _, payload = load_payload()
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d')
    added = updated = 0
    downgrade_skipped = 0
    for board in payload['boards'].values():
        for it in board.get('items', []):
            b = briefs.get(it.get('url'))
            if not b or not b.get('body'):
                continue
            body = b['body'].strip()
            new_tier = b.get('tier') or _infer_tier(body)
            old = it.get('brief') or {}
            if old.get('body'):
                old_tier = old.get('tier') or _infer_tier(old.get('body'))
                # 已有长简报不被要点速览覆盖（除非显式 --force）
                if old_tier == TIER_FULL and new_tier == TIER_NOTE and not args.force:
                    downgrade_skipped += 1
                    continue
                updated += 1
            else:
                added += 1
            it['brief'] = {'at': b.get('at') or stamp, 'body': body, 'tier': new_tier}
    save_payload(payload)
    target = 'data/policy/*.json' if store.use_json() else 'index.html 数据块'
    log.info('合并完成：新增简报 %d 份，更新 %d 份，跳过降级 %d 份（已写入 %s）',
             added, updated, downgrade_skipped, target)
    return 0


def main():
    ap = argparse.ArgumentParser(description='国金门户 · 政策简报工具')
    sub = ap.add_subparsers(dest='cmd', required=True)
    q = sub.add_parser('queue', help='生成待写简报队列（含官方摘要素材）')
    q.add_argument('--board', choices=['whitehouse', 'congress', 'agencies'])
    q.add_argument('--limit', type=int, default=8, help='每个板块最多列出条数（默认 8）')
    q.add_argument('--all', action='store_true',
                   help='列出全部待写条目（全覆盖模式，忽略 --limit；每日任务应使用）')
    q.add_argument('--fresh-ratio', type=float, default=0.5,
                   help='其中最新条目占比（默认 0.5，即一半最新一半最旧，两头推进清欠账）')
    c = sub.add_parser('coverage', help='体检简报覆盖率（每一条目都必须有简报）')
    c.add_argument('--board', choices=['whitehouse', 'congress', 'agencies'])
    m = sub.add_parser('merge', help='合并简报 JSON 进数据文件')
    m.add_argument('input', help='简报 JSON 文件路径（键为条目 url）')
    m.add_argument('--force', action='store_true',
                   help='允许用要点速览覆盖已有长简报（默认不允许降级）')
    args = ap.parse_args()
    if args.cmd == 'queue':
        return cmd_queue(args)
    if args.cmd == 'coverage':
        return cmd_coverage(args)
    return cmd_merge(args)


if __name__ == '__main__':
    sys.exit(main())
