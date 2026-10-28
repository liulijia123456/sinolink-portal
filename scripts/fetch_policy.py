#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国金门户 · 美国政策文件定时抓取
================================

每日运行，从官方来源抓取四个子板块的政策文件，
去重合并后写入 data/policy/{board}-items.json（并同步 manifest.json 的计数与时间戳）。
若 data/policy/manifest.json 不存在，则退回旧形态：回写 index.html 中的
<script type="application/json" id="policy-data"> 数据块。

**留置策略：纯增量累积，无留置上限。** 合并只按 url 去重与覆盖，绝不截断，
历史条目永久保留。政策文本是研究底稿，过期不等于无价值；截断会连带丢掉
已写好的简报、译名与领域标签（已付出的加工成本）。传输体积由页面端
fetch + gzip 控制，不在存储端删数据。

数据来源：
  白宫政策    —— 联邦公报（Federal Register）总统文件（行政令/备忘录/公告）
                 + 总统行政办公室（EOP）各组成机构（OMB、USTR、OSTP 等，
                 见 WHITEHOUSE_AGENCY_SLUGS）在联邦公报发布的文件
                 + 内阁各部（见 CABINET_DEPT_SLUGS）发布的规则与拟议规则
                 （部委通告类程序文件不收录，控制信噪比）
  国会政策    —— 国会官网 congress.gov 官方 API，参众两院法案/决议最新动态
  独立机构政策 —— 联邦公报按机构过滤（美联储、FCC、USITC、CFIUS、USAID，
                 见 AGENCY_SLUGS，可自行增删）
  司法裁判    —— CourtListener（Free Law Project）检索 RSS，分三路抓取：
                 最高法院全部裁判、13 个巡回上诉法院与重点地区法院的
                 政策相关裁判。该站镜像 PACER/法院意见书并提供原文链接；
                 最高法院官网与 GovInfo 对自动抓取分别有 Akamai 拦截与
                 限流，故作日更主通道，官方站点用于人工复核（见 README §5.1.1）。

选举动态板块按需求不抓取，脚本不涉及。

用法：
  python3 fetch_policy.py              # 正常抓取并写入 data/policy/*.json
  python3 fetch_policy.py --days 14    # 覆盖回看天数（默认 10）
  python3 fetch_policy.py --dry-run    # 只抓取打印统计，不回写文件

环境变量：
  CONGRESS_API_KEY  国会 API 密钥（优先级：环境变量 > scripts/.congress_api_key
                    文件 > DEMO_KEY；密钥申请 https://api.congress.gov/sign-up/）

日志：logs/fetch.log（自动轮转，1MB×3），同时输出到控制台。
退出码：0 全部成功；1 部分成功（已回写成功板块）；2 全部失败（不回写，保留旧数据）。
"""

import argparse
import html
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
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import portal_store as store          # noqa: E402  统一数据读写（JSON / 内嵌双形态）

# ---------------------------------------------------------------- 配置

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_HTML = os.path.join(BASE_DIR, 'index.html')
LOG_FILE = os.path.join(BASE_DIR, 'logs', 'fetch.log')

LOOKBACK_DAYS = 10          # 回看天数；每日运行 7 天足够，10 天留缓冲
# 内阁各部单独用更长的回看窗口：规则类文件发布频次低，10 天窗口下国务院 / 教育部 /
# 住房与城市发展部 / 退伍军人事务部等部门常常连续多日为 0，等于名义覆盖、实际空置。
# 21 天覆盖多数部门的月度节奏；实测 10 天 55 条、21 天 167 条、30 天 272 条。
DEPT_LOOKBACK_DAYS = 21
# 单部门单轮入库上限（按发布日从新到旧取），用于把首次扩大窗口后的回补分摊到数轮，
# 避免一轮涌入上百条而写不完简报。超出的条目不丢弃，下一轮继续抓（接口按日期倒序）。
DEPT_MAX_PER_RUN = 6
# 留置策略：纯增量累积，不设留置上限。
# 历史条目永久保留——政策文本是研究底稿，过期不等于无价值，截断会静默丢失
# 已写好的简报与译名（那是已付出的加工成本）。
# 代价是 JSON 体积随时间线性增长，页面侧靠 fetch + gzip 传输控制开销。
# 如需为传输体积设限，应在页面端做「只渲染前 N 条」，而不是在存储端删数据。
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
# 范围：美联储、FCC、USITC、CFIUS、USAID。
# 注：CFIUS 在联邦公报无独立机构条目，其文件由财政部「投资安全办公室」
#     （investment-security-office）署名发布——该办公室同时主管外国投资审查
#     （CFIUS）与对外投资安全计划，故按 CFIUS 归集。此类文件同时带有
#     treasury-department 标签，白宫板块的内阁各部查询（Rule / Proposed Rule）
#     也会收录同一条，两个板块各按自己的口径展示，属预期重叠。
# 注：USAID 自 2025 年重组后职能并入国务院，联邦公报发文近乎停摆
#     （近 180 天 1 条），保留该 slug 以接住后续可能出现的文件。
AGENCY_SLUGS = {
    'federal-reserve-system': '美联储（Federal Reserve）',
    'federal-communications-commission': '联邦通信委员会 FCC',
    'international-trade-commission': '国际贸易委员会 USITC',
    'investment-security-office': '外国投资委员会 CFIUS',
    'agency-for-international-development': '国际开发署 USAID',
}

# 独立机构的通告噪声词表（按机构逐个配置，标题命中即剔除）。
# 独立机构板块不设类型过滤——USITC 的贸易救济裁决、CFIUS 的审查规则
# 大多以通告（Notice）形式发布，按类型卡会把实质条目一并挡掉。
# 因此这里按标题剔除纯行政事务类通告：人事任命、会议、信息公开、人事制度。
AGENCY_NOISE_KW = {
    'international-trade-commission': (
        'performance review board',     # 绩效评审委员会任命（纯人事）
        'sunshine act',                 # 会议公告
        'information collection',       # 信息采集征求意见
        'senior executive service',     # 高管任命与评级
    ),
}

# ---------------------------------------------------------------- 司法裁判（judicial）
#
# 来源口径：
#   CourtListener（Free Law Project）检索 RSS —— 镜像 PACER 与各法院公布的裁判文书，
#   提供案名、法院、裁判日期、原文链接与命中的原文片段。最高法院官网对本机网络
#   返回 403（Akamai），GovInfo 的 api.data.gov 配额对 DEMO_KEY 极低，故以该站
#   作为日更主通道；两者留作人工复核与后续接正（见 README §5.1.1）。
#
# 抓取分三路，各自限量，避免上诉法院与地区法院的高频刑事案件淹没板块：
#   1) 最高法院：全部裁判（`court=scotus`），不看关键词；
#   2) 巡回上诉法院：13 个联邦巡回法院，政策关键词全文检索；
#   3) 地区法院：21 个政策诉讼高发地，同一组关键词检索。
# 三路结果按「法院层级 + 案名 + 日期」二次去重，再按层级配额取最新。
JUDICIAL_SCOTUS_LOOKBACK_DAYS = 180  # 最高法院：覆盖整个开庭期与夏季命令
JUDICIAL_CAPS = {'supreme': 3, 'circuit': 7, 'district': 4}
JUDICIAL_MAX_PER_RUN = 14

CL_FEED = 'https://www.courtlistener.com/feed/search/'
# 政策相关争点词表。只取能落到「政府行为合法性」的表述：
# 行政程序、规章效力、行政令、关税与制裁、禁令救济、权限争议。
# 刻意不收 "due process" "constitutional" 一类词——刑事案件几乎逐案命中。
JUDICIAL_POLICY_QUERY = (
    '("Administrative Procedure Act" OR "arbitrary and capricious" '
    'OR "major questions doctrine" OR "final rule" OR "agency action" '
    'OR "executive order" OR tariff OR "Section 301" OR sanctions '
    'OR "export control" OR "preliminary injunction" OR "stay pending appeal" '
    'OR preemption OR "ultra vires" OR "notice and comment")'
)
CIRCUIT_COURT_IDS = ('ca1', 'ca2', 'ca3', 'ca4', 'ca5', 'ca6', 'ca7',
                     'ca8', 'ca9', 'ca10', 'ca11', 'cadc', 'cafc')
# 政策诉讼高发的地区法院（PACER/CL 法院代码）：
# 哥伦比亚特区（联邦机构所在地）、纽约两区、加州三区、得州四区、
# 伊利诺伊北区、马萨诸塞、弗吉尼亚东区、佛州南区、新泽西、马里兰、
# 亚利桑那、科罗拉多、华盛顿西区、密歇根东区、宾州东区、俄亥俄北区。
DISTRICT_COURT_IDS = ('dcd', 'nysd', 'nyed', 'cand', 'cacd', 'casd',
                      'txsd', 'txnd', 'txwd', 'ilnd', 'mad', 'vaed',
                      'flsd', 'njd', 'mdd', 'azd', 'cod', 'wawd',
                      'mied', 'paed', 'ohnd')

# 无实质信息的占位案名（CourtListener 对封存/未公开案件的占位写法）
JUDICIAL_BAD_TITLE = re.compile(
    r'(^case name in .*unavailable$|^sealed\b|^sealed case$|^order$|^judgment$)',
    re.I)

# 刑事程序与缠讼类噪声：这些案件会因 "sanctions" "stay" "injunction" 等词
# 命中政策检索，但争点是程序或个案量刑，不属政策动态，入库即污染板块。
JUDICIAL_NOISE_RE = re.compile(
    r'\b(vexatious litigator|motion to suppress|habeas corpus|'
    r'sentencing guidelines|plea agreement|jury convicted|indictment|'
    r'2255|2241)\b', re.I)

# 「United States v. 某人」在巡回法院绝大多数是刑事上诉，不是政策诉讼；
# 只有当被告为政府、公司、机构时才保留（反垄断、关税、民事执法等）。
JUDICIAL_CIVIL_PARTY_RE = re.compile(
    r'\b(State of|Commonwealth of|City of|County of|District of|Secretary|'
    r'Department|Agency|Administration|Commission|Board|Bureau|Office|'
    r'Corporation|Corp\.|Inc\.|LLC|Ltd\.|Company|University|Association|'
    r'League|Coalition|Center|Foundation|Bank|Fund)\b', re.I)

CIRCUIT_CN = {
    'First': '第一', 'Second': '第二', 'Third': '第三', 'Fourth': '第四',
    'Fifth': '第五', 'Sixth': '第六', 'Seventh': '第七', 'Eighth': '第八',
    'Ninth': '第九', 'Tenth': '第十', 'Eleventh': '第十一',
}
DISTRICT_DIR_CN = {
    'Northern': '北区', 'Southern': '南区', 'Eastern': '东区',
    'Western': '西区', 'Central': '中区', 'Middle': '中区',
}
STATE_CN = {
    'Alabama': '阿拉巴马', 'Alaska': '阿拉斯加', 'Arizona': '亚利桑那',
    'Arkansas': '阿肯色', 'California': '加州', 'Colorado': '科罗拉多',
    'Connecticut': '康涅狄格', 'Delaware': '特拉华', 'Florida': '佛罗里达',
    'Georgia': '佐治亚', 'Hawaii': '夏威夷', 'Idaho': '爱达荷',
    'Illinois': '伊利诺伊', 'Indiana': '印第安纳', 'Iowa': '艾奥瓦',
    'Kansas': '堪萨斯', 'Kentucky': '肯塔基', 'Louisiana': '路易斯安那',
    'Maine': '缅因', 'Maryland': '马里兰', 'Massachusetts': '马萨诸塞',
    'Michigan': '密歇根', 'Minnesota': '明尼苏达', 'Mississippi': '密西西比',
    'Missouri': '密苏里', 'Montana': '蒙大拿', 'Nebraska': '内布拉斯加',
    'Nevada': '内华达', 'New Hampshire': '新罕布什尔', 'New Jersey': '新泽西',
    'New Mexico': '新墨西哥', 'New York': '纽约', 'North Carolina': '北卡罗来纳',
    'North Dakota': '北达科他', 'Ohio': '俄亥俄', 'Oklahoma': '俄克拉何马',
    'Oregon': '俄勒冈', 'Pennsylvania': '宾夕法尼亚', 'Rhode Island': '罗得岛',
    'South Carolina': '南卡罗来纳', 'South Dakota': '南达科他',
    'Tennessee': '田纳西', 'Texas': '得州', 'Utah': '犹他', 'Vermont': '佛蒙特',
    'Virginia': '弗吉尼亚', 'Washington': '华盛顿', 'West Virginia': '西弗吉尼亚',
    'Wisconsin': '威斯康星', 'Wyoming': '怀俄明', 'Puerto Rico': '波多黎各',
    'Guam': '关岛', 'Virgin Islands': '维尔京群岛',
    'Northern Mariana Islands': '北马里亚纳群岛',
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
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
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

# 代理策略：优先显式环境变量代理（http_proxy / https_proxy），否则直连。
# 不采用 macOS「系统设置 → 代理」里的代理（urllib 经 _scproxy 自动读取）：本机常驻
# 127.0.0.1:7890 一类的本地代理，代理进程未运行时 urlopen 会卡在 TLS 握手直至超时
# （报 _ssl.c 握手超时），而 curl 等不读系统代理的工具可正常直连。政府站点直连可达。
# 注：显式设置 http_proxy / https_proxy 时仍按环境变量走代理，不改变用户显式配置。
_ENV_PROXY = {k: v for k, v in urllib.request.getproxies_environment().items() if v}
OPENER = (urllib.request.build_opener() if _ENV_PROXY
          else urllib.request.build_opener(urllib.request.ProxyHandler({})))


def http_get_json(url, params, source_name):
    """带重试的 GET JSON。返回解析后的 dict；失败抛出最后一次异常。"""
    qs = urllib.parse.urlencode(params, doseq=True)
    full = url + '?' + qs
    last_err = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            req = urllib.request.Request(full, headers={'User-Agent': USER_AGENT})
            with OPENER.open(req, timeout=HTTP_TIMEOUT) as resp:
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


def http_get_text(url, source_name):
    """带重试的 GET 文本（用于 CourtListener RSS，返回 Atom XML）。"""
    last_err = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT,
                                                       'Accept': 'application/atom+xml,application/xml,text/xml'})
            with OPENER.open(req, timeout=HTTP_TIMEOUT) as resp:
                return resp.read().decode('utf-8', errors='replace')
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500 and e.code != 429:
                log.error('[%s] HTTP %s（客户端错误，不重试）: %s', source_name, e.code, url)
                raise
            last_err = e
            log.warning('[%s] HTTP %s（第 %d/%d 次），将重试', source_name, e.code, attempt, HTTP_RETRIES)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
            log.warning('[%s] 请求失败（第 %d/%d 次）: %s', source_name, attempt, HTTP_RETRIES, e)
        if attempt < HTTP_RETRIES:
            time.sleep(2 ** attempt)
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
    dept_since = (datetime.now(timezone(timedelta(hours=8)))
                  - timedelta(days=DEPT_LOOKBACK_DAYS)).strftime('%Y-%m-%d')
    for slug, label in CABINET_DEPT_SLUGS.items():
        extra = DEPT_NOTICE_EXTRA.get(slug)
        types = ['RULE', 'PRORULE']
        if extra:
            types = types + list(extra.get('allow', ()))
        dept_params = _fr_params({
            'conditions[agencies][]': [slug],
            'conditions[publication_date][gte]': dept_since,
            'conditions[type][]': types,
        })
        try:
            dept_data = http_get_json(FR_API, dept_params, '白宫-' + label)
            docs = sorted(dept_data.get('results', []),
                          key=lambda d: d.get('publication_date') or '', reverse=True)
            n_dept = 0
            n_extra = 0
            n_defer = 0
            for doc in docs:
                if extra and doc.get('type') == 'Notice':
                    title_l = (doc.get('title') or '').lower()
                    if any(kw in title_l for kw in extra.get('exclude_kw', ())):
                        continue
                    if n_extra >= int(extra.get('max', 0)):
                        continue
                    n_extra += 1
                if n_dept >= DEPT_MAX_PER_RUN:
                    n_defer += 1
                    continue
                kind = FR_KIND.get(doc.get('type'), doc.get('type') or '文件')
                items.append(_fr_item('whitehouse', doc, kind, agency=label))
                n_dept += 1
            log.info('[白宫-内阁:%s] 抓到 %d 条%s', label, n_dept,
                     ('（另有 %d 条顺延至下轮）' % n_defer) if n_defer else '')
        except Exception as e:
            log.error('[白宫-内阁:%s] 抓取失败: %s（仅该部子源失败，其他结果保留）', label, e)
        time.sleep(1)  # 礼貌限速
    return items


def current_congress(year=None):
    """按年份推算当前国会届次（第 1 届为 1789—1791，每届两年）。

    用于过滤往届法案：congress.gov 的 updateDate 会因元数据回填而变动，
    只按 updateDate 过滤会把往届旧案当成当日新条目收进来（实测混入第 102—118 届
    共 35 条，最早可追溯到 1991 年）。
    """
    y = year or datetime.now(timezone(timedelta(hours=8))).year
    return (y - 1789) // 2 + 1


def fetch_congress(since):
    """国会政策：congress.gov API 最新法案/决议动态（仅当前届次）。"""
    items = []
    offset = 0
    cur_cg = current_congress()
    skipped_old_cg = 0
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
            # 只收当前届次：往届法案的 updateDate 也可能落在窗口内，
            # 但那是元数据回填，不是本届议程，收进来会污染板块与研判。
            if b.get('congress') and int(b['congress']) != cur_cg:
                skipped_old_cg += 1
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
                # 最新行动文本保留足够长度：截断过短会把 "ordered to be reported"
                # 一类关键进展切掉，导致 brief.py 的层级判定失效（实测漏判）。
                'agency': ('参议院' if chamber == 'senate' else '众议院')
                          + (' · ' + latest[:180] if latest else ''),
            })
        if reached_old or 'next' not in data.get('pagination', {}):
            break
        offset += 50
        time.sleep(1)  # 礼貌限速
    log.info('[国会] 抓到 %d 条（%s 以来，第 %d 届）%s',
             len(items), since, cur_cg,
             ('，跳过往届 %d 条' % skipped_old_cg) if skipped_old_cg else '')
    return items


def fetch_agencies(since):
    """独立机构政策：联邦公报按机构（美联储、FCC、USITC、CFIUS、USAID）。

    逐机构独立容错：单个机构接口失败不影响其他机构的结果（与 EOP 各机构同口径）。
    噪声剔除见 AGENCY_NOISE_KW——只有该机构显式配置时才按标题过滤。
    """
    items = []
    for slug, label in AGENCY_SLUGS.items():
        params = _fr_params({
            'conditions[agencies][]': [slug],
            'conditions[publication_date][gte]': since,
        })
        try:
            data = http_get_json(FR_API, params, '独立机构-' + label)
        except Exception as e:
            log.error('[独立机构-%s] 抓取失败: %s（仅该机构子源失败，其他结果保留）', label, e)
            time.sleep(1)
            continue
        noise = AGENCY_NOISE_KW.get(slug, ())
        n = 0
        n_noise = 0
        for doc in data.get('results', []):
            if noise:
                title_l = (doc.get('title') or '').lower()
                if any(kw in title_l for kw in noise):
                    n_noise += 1
                    continue
            kind = FR_KIND.get(doc.get('type'), doc.get('type') or '文件')
            items.append(_fr_item('agencies', doc, kind, agency=label))
            n += 1
        log.info('[独立机构-%s] 抓到 %d 条%s', label, n,
                 ('（剔除程序性通告 %d 条）' % n_noise) if n_noise else '')
        time.sleep(1)  # 礼貌限速
    return items

# ---------------------------------------------------------------- 司法裁判抓取
#
# CourtListener 的检索 RSS 是 Atom，条目字段：
#   <title> 案名 / <link href> 意见书页 / <published> 裁判日期
#   <author><name> 法院全称 / <summary> 命中关键词的原文片段
# 该站不提供摘要接口，片段即撰写简报时能拿到的最可靠素材，故一并入库
# （字段 summary），brief.py 直接取用，不再回抓网页（意见书页对脚本返回 202）。

def _strip_tags(s):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', s or '')).strip()


def _court_cn(name):
    """法院英文全称 → 中文名（用于「法院」筛选与列表展示）。"""
    n = (name or '').strip()
    if n.startswith('Supreme Court'):
        return '美国最高法院'
    m = re.match(r'Court of Appeals for the (.+?) Circuit$', n)
    if m:
        c = m.group(1)
        if c in ('District of Columbia', 'D.C.'):
            return '哥伦比亚特区巡回上诉法院'
        if c == 'Federal':
            return '联邦巡回上诉法院'
        return (CIRCUIT_CN.get(c, c) or c) + '巡回上诉法院'
    m = re.match(r'District Court, District of (.+)$', n)
    if m:
        state = m.group(1)
        if state == 'Columbia':
            return '哥伦比亚特区联邦地区法院'
        return STATE_CN.get(state, state) + '联邦地区法院'
    m = re.match(r'District Court, (.+?) District of (.+)$', n)
    if m:
        direction, state = m.group(1), m.group(2)
        return (STATE_CN.get(state, state) + DISTRICT_DIR_CN.get(direction, direction)
                + '联邦地区法院')
    if 'Court of International Trade' in n:
        return '国际贸易法院'
    return ''


def _court_type(name):
    """法院全称 → 层级代码；非联邦 Art. III 法院（含州法院、破产法院）返回 None。"""
    n = (name or '')
    if 'Supreme Court' in n:
        return 'supreme'
    if 'Court of Appeals' in n:
        return 'circuit'
    if 'Court of International Trade' in n:
        return 'trade'
    if 'Bankruptcy' in n:
        return None
    if 'District Court' in n:
        return 'district'
    return None


def _judicial_kind(ctype, summary):
    """裁判类型：判决（实体裁判）/ 裁定（程序性命令、中止申请等）。"""
    head = (summary or '')[:500].lower()
    if ctype == 'supreme':
        if 'on application for stay' in head or 'on application to vacate' in head:
            return '裁定'
        if 'on petition for writ of certiorari' in head and 'certiorari to the' not in head:
            return '裁定'
    if head.startswith('order') or re.search(r'\bit is (hereby )?ordered\b', head):
        return '裁定'
    # 片段常从句子中间开始，需按「请求 + 准驳」句式判定临时禁令、中止等程序性裁定
    if re.search(r'\b(motion|application|request|petition)\b[^.]{0,120}'
                 r'\b(is|are|was|were)\s+(hereby\s+)?(granted|denied)\b', head):
        return '裁定'
    return '判决'


def _parse_cl_feed(xml_text):
    """解析 CourtListener Atom feed，返回 [{title, url, date, court, summary}]。"""
    ns = {'a': 'http://www.w3.org/2005/Atom'}
    root = ET.fromstring(xml_text)
    out = []
    for e in root.findall('a:entry', ns):
        link_el = e.find('a:link', ns)
        summary = _strip_tags(html.unescape(e.findtext('a:summary', default='', namespaces=ns) or ''))
        summary = re.sub(r'Original document\s*$', '', summary).strip()
        out.append({
            'title': (e.findtext('a:title', default='', namespaces=ns) or '').strip(),
            'url': link_el.get('href', '') if link_el is not None else '',
            'date': (e.findtext('a:published', default='', namespaces=ns) or '')[:10],
            'court': (e.findtext('a:author/a:name', default='', namespaces=ns) or '').strip(),
            'summary': summary[:1200],
        })
    return out


def _cl_search(courts, query, since, label):
    """调用 CourtListener 检索 RSS。courts 为空表示不限法院。"""
    params = {'q': query, 'type': 'o', 'order_by': 'dateFiled desc',
              'filed_after': since}
    if courts:
        params['court'] = ','.join(courts)
    url = CL_FEED + '?' + urllib.parse.urlencode(params)
    xml_text = http_get_text(url, label)
    entries = _parse_cl_feed(xml_text)
    if len(entries) >= 20 and entries[-1]['date'] > since:
        log.warning('[%s] 返回 20 条上限，最早仅到 %s（窗口自 %s 起），更早条目可能未覆盖',
                    label, entries[-1]['date'], since)
    log.info('[%s] 抓到 %d 条（filed_after %s）', label, len(entries), since)
    return entries


def fetch_judicial(since):
    """司法裁判：最高法院 + 巡回上诉法院 + 重点地区法院（CourtListener）。"""
    tz8 = timezone(timedelta(hours=8))
    now = datetime.now(tz8)
    today = now.strftime('%Y-%m-%d')
    scotus_since = (now - timedelta(days=JUDICIAL_SCOTUS_LOOKBACK_DAYS)).strftime('%Y-%m-%d')

    routes = [
        ('supreme', '司法-最高法院', ('scotus',), '*', scotus_since),
        ('circuit', '司法-巡回上诉法院', CIRCUIT_COURT_IDS, JUDICIAL_POLICY_QUERY, since),
        ('district', '司法-地区法院', DISTRICT_COURT_IDS, JUDICIAL_POLICY_QUERY, since),
    ]
    items, seen = [], set()
    for ctype, label, courts, query, win_start in routes:
        try:
            entries = _cl_search(courts, query, win_start, label)
        except Exception as e:
            # 单路失败不影响其余两路（与 EOP/独立机构逐机构容错同口径）
            log.error('[%s] 抓取失败: %s（其余来源保留）', label, e)
            time.sleep(1)
            continue
        cap = JUDICIAL_CAPS.get(ctype, 0)
        kept = skipped_old = skipped_bad = skipped_dup = 0
        for en in entries:
            if kept >= cap:
                break
            d = en['date']
            if not re.match(r'^\d{4}-\d{2}-\d{2}$', d) or d < win_start or d > today:
                skipped_old += 1
                continue
            title = en['title']
            if not title or JUDICIAL_BAD_TITLE.search(title):
                skipped_bad += 1
                continue
            if JUDICIAL_NOISE_RE.search(en['summary'] or ''):
                skipped_bad += 1
                continue
            if (title.lower().startswith('united states v.')
                    and not JUDICIAL_CIVIL_PARTY_RE.search(title)):
                skipped_bad += 1
                continue
            cn = _court_cn(en['court'])
            if not cn:
                skipped_bad += 1
                continue
            # 二次去重：同一案子在多路查询中重复出现时，按「法院 + 案名 + 日期」只留一条
            key = (cn, re.sub(r'[^a-z0-9]+', '', title.lower()), d)
            if key in seen:
                skipped_dup += 1
                continue
            seen.add(key)
            items.append({
                'board': 'judicial',
                'date': d,
                'title': title,
                'url': en['url'],
                'source': 'CourtListener',
                'kind': _judicial_kind(ctype, en['summary']),
                'agency': cn,
                'summary': en['summary'],
            })
            kept += 1
        log.info('[%s] 入库 %d 条（配额 %d；跳过 超窗 %d / 无效 %d / 重复 %d）',
                 label, kept, cap, skipped_old, skipped_bad, skipped_dup)
        time.sleep(1)  # 礼貌限速

    items.sort(key=lambda x: x['date'], reverse=True)
    if len(items) > JUDICIAL_MAX_PER_RUN:
        items = items[:JUDICIAL_MAX_PER_RUN]
    log.info('[司法裁判] 合计 %d 条（上限 %d）', len(items), JUDICIAL_MAX_PER_RUN)
    return items


# ---------------------------------------------------------------- 合并与回写

def read_index():
    """兼容旧调用：JSON 存储形态下不再需要读取 index.html。"""
    try:
        with open(INDEX_HTML, 'r', encoding='utf-8') as f:
            return f.read()
    except OSError:
        return ''


def extract_payload(html=None):
    """统一走 portal_store：data/policy/manifest.json 存在则读 JSON，否则回落 HTML 数据块。"""
    return store.load_policy()


def merge_items(old_items, new_items):
    """按 url 去重合并，新条目覆盖旧条目（保留旧条目已生成的简报/翻译/领域标签）。

    纯增量累积：**不做任何截断**，历史条目永久保留。
    仅当 url 缺失时丢弃该条（无 url 无法去重，也无溯源价值）。
    """
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
    # 按日期倒序，页面排序不受影响
    return sorted(merged.values(), key=lambda x: (x.get('date') or ''), reverse=True)


def write_payload(payload, boards=None):
    """统一走 portal_store：JSON 形态写 data/policy/*.json（原子写），否则回写 HTML 数据块。
    boards 给定时只写这些板块（拆分定时任务时用，避免覆盖其他板块的抓取结果）。"""
    store.save_policy(payload, boards)
    if not store.use_json():
        log.info('已回写 index.html 数据块（未启用 data/policy JSON 形态）')

# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description='国金门户 · 美国政策文件定时抓取')
    ap.add_argument('--days', type=int, default=LOOKBACK_DAYS, help='回看天数（默认 %(default)s）')
    ap.add_argument('--dry-run', action='store_true', help='只抓取打印统计，不回写数据')
    ap.add_argument('--board', choices=['whitehouse', 'congress', 'agencies', 'judicial'],
                    help='只抓取指定板块（拆分定时任务用）；不填则四板块全抓')
    args = ap.parse_args()

    tz8 = timezone(timedelta(hours=8))
    now = datetime.now(tz8)
    since = (now - timedelta(days=args.days)).strftime('%Y-%m-%d')
    stamp = now.strftime('%Y-%m-%d %H:%M')

    log.info('========== 抓取开始（%s，回看 %d 天，since %s） ==========',
             args.board or '四板块', args.days, since)

    sources = [
        ('whitehouse', '白宫及内阁各部政策', fetch_whitehouse),
        ('congress', '国会法案', fetch_congress),
        ('agencies', '独立机构政策', fetch_agencies),
        ('judicial', '司法裁判', fetch_judicial),
    ]
    if args.board:
        sources = [x for x in sources if x[0] == args.board]

    payload = extract_payload(read_index())
    ok, fail = [], []

    for key, name, fn in sources:
        try:
            new_items = fn(since)
            board = payload['boards'][key]
            old_n = len(board.get('items', []))
            board['items'] = merge_items(board.get('items', []), new_items)
            board['updated_at'] = stamp
            log.info('[%s] 合并完成：旧 %d 条 + 抓取 %d 条 → 共 %d 条（净增 %d）',
                     name, old_n, len(new_items), len(board['items']),
                     len(board['items']) - old_n)
            ok.append((name, len(new_items), len(board['items']) - old_n,
                       len(board['items'])))
        except Exception as e:
            log.error('[%s] 抓取失败: %s（保留旧数据，本板块不更新）', name, e)
            fail.append((name, str(e)))

    if not ok:
        log.error('========== 全部来源失败，不回写文件（旧数据完整保留） ==========')
        return 2

    payload['fetched_at'] = stamp

    target_desc = 'data/policy/*.json' if store.use_json() else 'index.html 数据块'

    if args.dry_run:
        log.info('dry-run 模式：不回写 %s', target_desc)
        for name, new_n, net_n, total_n in ok:
            log.info('  %s：抓取 %d 条，净增 %d 条，累计 %d 条', name, new_n, net_n, total_n)
        return 0

    try:
        write_payload(payload, [args.board] if args.board else None)
    except Exception as e:
        log.error('回写 %s 失败: %s（本次抓取结果未保存，下次将重新抓取）', target_desc, e)
        return 2

    for name, new_n, net_n, total_n in ok:
        log.info('  %s：抓取 %d 条，净增 %d 条，累计 %d 条', name, new_n, net_n, total_n)
    if fail:
        for name, err in fail:
            log.warning('  %s：失败 —— %s', name, err)
        log.warning('========== 部分成功（%d 成功 / %d 失败），已回写成功板块到 %s ==========',
                    len(ok), len(fail), target_desc)
        return 1

    log.info('========== 抓取完成，已写入 %s ==========', target_desc)
    return 0


if __name__ == '__main__':
    sys.exit(main())
