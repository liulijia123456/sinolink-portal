#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国金门户 · 条目字段补写（翻译 / 领域标注）
==========================================

定时任务只负责「判断」，不负责「改写文件」：模型把结果写成一份 patch JSON，
由本脚本按字段归属规则受控写入 data/policy/{board}-items.json。
这样能避免模型直接改 JSON 时出现的编码错乱、字段被覆盖、count 与 manifest 脱节。

用法：
    python3 scripts/edit_items.py patch.json              # 只补空字段（默认）
    python3 scripts/edit_items.py patch.json --overwrite  # 覆盖已有值
    python3 scripts/edit_items.py patch.json --dry-run    # 只看会改什么

patch.json 格式（键为条目 url，值为要补的字段）：
    {
      "https://www.federalregister.gov/documents/2026/10/24/2026-22187/...": {
        "title_cn": "修订《联邦水污染控制法》第404条湿地许可范围",
        "domain": "能源/环境"
      },
      ...
    }

字段归属规则（与项目约定一致）：
    title_cn  仅当原值为空时写入（除非 --overwrite）
    domain    仅当原值为空时写入（除非 --overwrite），且必须在固定词表内
    brief     本脚本不处理，简报统一走 brief.py merge

退出码：
    0 正常（可能有个别 url 未命中，属正常：该 url 不在当前数据中）
    1 出错（patch 文件坏、领域不在词表、写入失败）
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import portal_store as store

# 固定领域词表，须与 validate_data.py、定时任务 prompt 完全一致
DOMAINS = {
    '科技/AI', '贸易/关税', '能源/环境', '医疗/医药', '国防/安全', '财政/税收',
    '金融/保险', '电信/媒体', '农业/食品', '教育/劳动', '住房', '交通',
    '司法/执法', '社会保障', '产业/企业', '政府运作', '其他',
}

FIELDS = ('title_cn', 'domain')


def main():
    ap = argparse.ArgumentParser(description='按 patch 补写条目 title_cn / domain')
    ap.add_argument('patch', help='patch JSON 文件路径（键为条目 url）')
    ap.add_argument('--overwrite', action='store_true', help='覆盖已有值')
    ap.add_argument('--dry-run', action='store_true', help='只打印，不写文件')
    args = ap.parse_args()

    try:
        with open(args.patch, encoding='utf-8') as f:
            patch = json.load(f)
    except Exception as e:
        print('[错误] patch 文件无法解析：%s' % e, file=sys.stderr)
        return 1
    if not isinstance(patch, dict) or not patch:
        print('[错误] patch 应为非空对象（键为 url）', file=sys.stderr)
        return 1

    # 领域词表先整体校验，避免写一半才发现错
    bad = []
    for url, val in patch.items():
        if not isinstance(val, dict):
            bad.append('%s：值应为对象' % url[:60])
            continue
        dom = (val.get('domain') or '').strip()
        if dom and dom not in DOMAINS:
            bad.append('%s：领域 %r 不在词表内' % (url[:60], dom))
    if bad:
        for b in bad[:20]:
            print('[错误] %s' % b, file=sys.stderr)
        print('[错误] %d 处不合法，未写入任何文件' % len(bad), file=sys.stderr)
        return 1

    payload = store.load_policy()
    index = {}
    for board, b in payload.get('boards', {}).items():
        for it in b.get('items') or []:
            u = (it.get('url') or '').strip()
            if u:
                index[u] = (board, it)

    filled, skipped, missing = [], [], []
    for url, val in patch.items():
        hit = index.get(url.strip())
        if not hit:
            missing.append(url)
            continue
        board, it = hit
        for field in FIELDS:
            new = (val.get(field) or '').strip()
            if not new:
                continue
            old = (it.get(field) or '').strip()
            if old and not args.overwrite:
                skipped.append('%s/%s 已有值，跳过' % (board, field))
                continue
            if old == new:
                continue
            if not args.dry_run:
                it[field] = new
            filled.append('%s · %s ← %s' % (board, field, new[:40]))

    print('patch %d 条 · 命中 %d · 未命中 %d'
          % (len(patch), len(patch) - len(missing), len(missing)))
    print('写入 %d 项 · 跳过 %d 项%s'
          % (len(filled), len(skipped), '（dry-run，未落盘）' if args.dry_run else ''))
    for line in filled[:30]:
        print('  + %s' % line)
    if len(filled) > 30:
        print('  …… 其余 %d 项省略' % (len(filled) - 30))
    if missing:
        print('未命中 url（该 url 不在当前数据中）：')
        for u in missing[:10]:
            print('  ? %s' % u[:90])
        if len(missing) > 10:
            print('  …… 其余 %d 条省略' % (len(missing) - 10))

    if args.dry_run or not filled:
        return 0
    try:
        store.save_policy(payload)
    except Exception as e:
        print('[错误] 写入失败：%s' % e, file=sys.stderr)
        return 1
    print('已写入 data/policy/*.json（count 与 manifest 已同步）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
