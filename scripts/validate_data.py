#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国金门户 · 数据体检
===================

校验 data/policy/*.json 的结构与内容质量，供定时任务在**发布前**做机器把关：
校验不通过就不提交，避免把坏数据推上线。

用法：
    python3 scripts/validate_data.py                # 常规体检（缺中文标题/领域/简报只告警）
    python3 scripts/validate_data.py --strict       # 严格模式：缺中文标题、领域或简报即失败
    python3 scripts/validate_data.py --dir data/policy

退出码：
    0 通过（可能含告警）
    1 结构错误（文件缺失、JSON 坏、url 重复、count 不符、日期乱序等）
    2 严格模式下的内容缺失（缺 title_cn、domain 或 brief）
      注：简报为**全覆盖**要求——每一条目都必须有简报（长或短），
      层级只决定详略。缺简报在严格模式下拦截发布。
"""

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime

BOARDS = ('whitehouse', 'congress', 'agencies', 'judicial')
SCHEMA = 1
# 留置策略：纯增量累积，不设留置上限（与 fetch_policy.py 的合并逻辑一致）。
# 此处不做条数上限校验——存储层不截断，校验层就不该报「超过上限」的告警。
# 若需控制页面传输体积，应在页面端只渲染前 N 条，而不是限制存储。

# 固定领域词表（与定时任务 prompt 中的约定保持一致）
DOMAINS = {
    '科技/AI', '贸易/关税', '能源/环境', '医疗/医药', '国防/安全', '财政/税收',
    '金融/保险', '电信/媒体', '农业/食品', '教育/劳动', '住房', '交通',
    '司法/执法', '社会保障', '产业/企业', '政府运作', '其他',
}
BRIEF_HEADERS = ('【政策背景】', '【核心内容】', '【要点提炼】', '对金融市场的预期影响')
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')

errors, warns, blocks = [], [], []


def err(msg):
    errors.append(msg)


def warn(msg):
    warns.append(msg)


def block(msg):
    """内容缺失：严格模式下会拦截发布的项。"""
    blocks.append(msg)


def check_board(path, board):
    try:
        with open(path, encoding='utf-8') as f:
            d = json.load(f)
    except Exception as e:
        return err('%s：JSON 解析失败 —— %s' % (os.path.basename(path), e))

    if d.get('schema') != SCHEMA:
        err('%s：schema 应为 %d，实际 %r' % (board, SCHEMA, d.get('schema')))
    if d.get('board') != board:
        err('%s：board 字段与文件名不符（%r）' % (board, d.get('board')))
    if not isinstance(d.get('updated_at'), str):
        err('%s：缺少 updated_at' % board)

    items = d.get('items') or []
    if not items:
        err('%s：条目数为 0' % board)
        return
    if d.get('count') != len(items):
        err('%s：count=%r 与实际 %d 不符' % (board, d.get('count'), len(items)))

    urls, dates, no_cn, no_dom, no_brief, bad_dom, brief_issues = set(), [], 0, 0, 0, [], 0
    for i, it in enumerate(items):
        u = (it.get('url') or '').strip()
        if not u:
            err('%s[%d]：缺少 url' % (board, i))
        elif u in urls:
            err('%s[%d]：url 重复 %s' % (board, i, u[:70]))
        urls.add(u)

        dt = it.get('date') or ''
        if not DATE_RE.match(dt):
            err('%s[%d]：date 非法 %r' % (board, i, dt))
        dates.append(dt)

        if not (it.get('title') or '').strip():
            err('%s[%d]：缺少 title' % (board, i))
        if not (it.get('title_cn') or '').strip():
            no_cn += 1
        dom = (it.get('domain') or '').strip()
        if not dom:
            no_dom += 1
        elif dom not in DOMAINS:
            bad_dom.append(dom)

        b = it.get('brief') or {}
        body = b.get('body') or ''
        if not body:
            no_brief += 1
        else:
            # 分层校验：full 需四段结构；note 只求短而实，不要求段头
            tier = b.get('tier') or ('full' if len(body) >= 400 else 'note')
            if tier == 'note':
                if not (100 <= len(body) <= 900):
                    warn('%s[%d]：要点速览长度 %d 字，参考区间 100–900'
                         % (board, i, len(body)))
            else:
                miss = [h for h in BRIEF_HEADERS if h not in body]
                if miss:
                    brief_issues += 1
                    warn('%s[%d]：长简报缺少段头 %s（%s）'
                         % (board, i, '、'.join(miss), (it.get('title') or '')[:40]))
                elif not (400 <= len(body) <= 4000):
                    warn('%s[%d]：长简报长度 %d 字，参考区间 400–4000'
                         % (board, i, len(body)))

    if dates != sorted(dates, reverse=True):
        err('%s：条目未按日期倒序排列' % board)
    if bad_dom:
        err('%s：出现词表外的领域标签 %s' % (board, '、'.join(sorted(set(bad_dom)))))

    n_brief = sum(1 for it in items if (it.get('brief') or {}).get('body'))
    n_note = sum(1 for it in items
                 if (it.get('brief') or {}).get('body')
                 and (((it.get('brief') or {}).get('tier') == 'note')
                      or (not (it.get('brief') or {}).get('tier')
                          and len((it.get('brief') or {}).get('body') or '') < 400)))
    cov = (100.0 * n_brief / len(items)) if items else 100.0
    print('[%s] %d 条 · 中文标题 %d/%d · 领域 %d/%d · 简报 %d（长 %d / 要点 %d，覆盖 %.1f%%）· 最近 %s'
          % (board, len(items), len(items) - no_cn, len(items),
             len(items) - no_dom, len(items), n_brief, n_brief - n_note, n_note,
             cov, max(dates) if dates else '-'))
    if no_cn:
        block('%s：%d 条缺中文标题（title_cn）' % (board, no_cn))
    if no_dom:
        block('%s：%d 条缺领域标签（domain）' % (board, no_dom))
    if no_brief:
        block('%s：%d 条缺简报（brief），按全覆盖策略每一条目都必须有简报' % (board, no_brief))
    return None


def main():
    ap = argparse.ArgumentParser(description='校验 data/policy 数据文件')
    ap.add_argument('--dir', default='data/policy', help='数据目录（默认 data/policy）')
    ap.add_argument('--strict', action='store_true', help='新增条目缺中文标题/领域即失败')
    ap.add_argument('--board', choices=['whitehouse', 'congress', 'agencies', 'judicial'],
                    help='只校验指定板块（拆分定时任务用）；不填则四板块全验')
    args = ap.parse_args()

    if not os.path.isdir(args.dir):
        print('[错误] 目录不存在：%s' % args.dir, file=sys.stderr)
        return 1

    targets = [args.board] if args.board else list(BOARDS)
    for board in targets:
        p = os.path.join(args.dir, '%s-items.json' % board)
        if not os.path.exists(p):
            err('缺少文件 %s-items.json' % board)
        else:
            check_board(p, board)
        ov = os.path.join(args.dir, '%s-overview.json' % board)
        if not os.path.exists(ov):
            err('缺少文件 %s-overview.json' % board)

    mf = os.path.join(args.dir, 'manifest.json')
    if os.path.exists(mf):
        try:
            m = json.load(open(mf, encoding='utf-8'))
            for board in targets:
                p = os.path.join(args.dir, '%s-items.json' % board)
                if os.path.exists(p):
                    actual = len(json.load(open(p, encoding='utf-8')).get('items') or [])
                    expect = (m.get('boards', {}).get(board) or {}).get('count')
                    if expect != actual:
                        err('manifest：%s count=%r 与实际 %d 不符' % (board, expect, actual))
        except Exception as e:
            err('manifest.json 解析失败：%s' % e)
    else:
        err('缺少 manifest.json')

    print()
    for w in warns:
        print('[告警] %s' % w)
    for b in blocks:
        print('[待补] %s' % b)
    if errors:
        for e in errors:
            print('[错误] %s' % e, file=sys.stderr)
        print('\n[失败] %d 项结构错误，不要发布' % len(errors))
        return 1
    if args.strict and blocks:
        print('\n[失败·严格模式] %d 项内容缺失，请先补齐再发布' % len(blocks))
        return 2
    print('\n[通过] 结构完整%s'
          % ('，内容尚有告警/待补项' if (warns or blocks) else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main())
