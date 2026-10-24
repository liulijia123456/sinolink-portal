#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国金门户 · 政策简报工具
======================

配合 scripts/fetch_policy.py 使用，为三个政策子板块的条目生成、管理「政策简报」
（800–2000 字：提炼重点 + 对金融市场的预期影响），写入 index.html 数据块中
条目的 brief 字段。原条目（标题、官方原文链接等源文件信息）完整保留。

两个子命令：

1) 生成简报队列（供撰写）：
     python3 brief.py --queue [--board whitehouse] [--limit 8]
   输出 scripts/brief_queue.md：列出尚无简报的条目，并附上从官方接口抓取的
   摘要/文本节选（联邦公报 abstract、国会官网 bill summary），作为撰写素材。

2) 合并已写好的简报：
     python3 brief.py --merge scripts/briefs.json
   briefs.json 格式：{ "<条目url>": { "at": "2026-10-12", "body": "【要点】\\n…\\n\\n【对金融市场的预期影响】\\n…" }, ... }
   合并后回写 index.html 的 policy-data 数据块（仅新增/更新 brief 字段，其余不动）。

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
        url = ('https://api.congress.gov/v3/bill/%s/%s/%s/summary?api_key=%s&format=json'
               % (congress, api_type, number, CONGRESS_API_KEY))
        d = http_get_json(url)
        s = (d.get('summary') or {})
        text = s.get('text') or ''
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

def cmd_queue(args):
    _, _, payload = load_payload()
    lines = ['# 简报撰写队列', '',
             '> 以下条目尚无简报。素材来自官方摘要；请据此撰写 800–2000 字简报，',
             '> 结构：【政策背景】【核心内容】【要点提炼】【对金融市场的预期影响】。',
             '> 成果写入 scripts/briefs.json（键为条目 url）后运行 --merge 合并。', '']
    total = 0
    boards = [args.board] if args.board else ['whitehouse', 'congress', 'agencies']
    for board in boards:
        items = (payload['boards'].get(board) or {}).get('items', [])
        pending = [it for it in items if not (it.get('brief') or {}).get('body')]
        pending = pending[:args.limit]
        if not pending:
            continue
        lines.append('## %s（%s）待写 %d 条' % (BOARD_CN.get(board, board), board, len(pending)))
        lines.append('')
        for it in pending:
            total += 1
            lines.append('### %s · %s · %s' % (it.get('date'), it.get('kind'), it.get('title')))
            lines.append('')
            lines.append('- url: `%s`' % it.get('url'))
            lines.append('- agency: %s' % it.get('agency', ''))
            lines.append('')
            mat = fetch_material(it)
            if mat:
                shown = mat[:1800]
                lines.append('**官方摘要素材**：')
                lines.append('')
                lines.append(shown + ('…' if len(mat) > 1800 else ''))
            else:
                lines.append('**官方摘要素材**：（抓取失败，请依据标题与知识谨慎撰写，不确定处须注明）')
            lines.append('')
            time.sleep(1)  # 礼貌限速
    with open(QUEUE_FILE, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    log.info('队列已生成：%s（共 %d 条待写）', QUEUE_FILE, total)
    return 0


# ---------------------------------------------------------------- 合并模式

def cmd_merge(args):
    briefs = json.load(open(args.input, encoding='utf-8'))
    _, _, payload = load_payload()
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d')
    added = updated = 0
    for board in payload['boards'].values():
        for it in board.get('items', []):
            b = briefs.get(it.get('url'))
            if not b or not b.get('body'):
                continue
            if (it.get('brief') or {}).get('body'):
                updated += 1
            else:
                added += 1
            it['brief'] = {'at': b.get('at') or stamp, 'body': b['body'].strip()}
    save_payload(payload)
    log.info('合并完成：新增简报 %d 份，更新 %d 份（index.html 已回写）', added, updated)
    return 0


def main():
    ap = argparse.ArgumentParser(description='国金门户 · 政策简报工具')
    sub = ap.add_subparsers(dest='cmd', required=True)
    q = sub.add_parser('queue', help='生成待写简报队列（含官方摘要素材）')
    q.add_argument('--board', choices=['whitehouse', 'congress', 'agencies'])
    q.add_argument('--limit', type=int, default=8, help='每个板块最多列出条数（默认 8）')
    m = sub.add_parser('merge', help='合并简报 JSON 进 index.html')
    m.add_argument('input', help='简报 JSON 文件路径（键为条目 url）')
    args = ap.parse_args()
    if args.cmd == 'queue':
        return cmd_queue(args)
    return cmd_merge(args)


if __name__ == '__main__':
    sys.exit(main())
