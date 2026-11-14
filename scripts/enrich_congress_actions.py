#!/usr/bin/env python3
"""Backfill selected Congress.gov actions into congress-items.json.

Run daily after fetch_policy.py; existing records are skipped unless --refresh is set.
Only milestone actions, plus the first and latest official actions, are retained.
"""
import argparse
import html
import json
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ITEMS = ROOT / 'data/policy/congress-items.json'
KEY = ROOT / 'scripts/.congress_api_key'
URL_RE = re.compile(r'/bill/(\d+)th-congress/([a-z-]+)/(\d+)')
TYPE = {'house-bill': 'hr', 'senate-bill': 's', 'house-resolution': 'hres',
        'senate-resolution': 'sres', 'house-joint-resolution': 'hjres',
        'senate-joint-resolution': 'sjres', 'house-concurrent-resolution': 'hconres',
        'senate-concurrent-resolution': 'sconres'}
MILESTONE = re.compile(
    r'introduced|submitted|referred to|reported by|ordered to be reported|'
    r'placed on .*calendar|motion to discharge|discharge petition|'
    r'passed/agreed|passed (house|senate)|on (agreeing|passage|motion to discharge)|'
    r'received in (the )?(house|senate)|presented to (the )?president|'
    r'signed by (the )?president|became (public|private) law|vetoed|'
    r'conference report|amendment agreed to', re.I)
NOISE = re.compile(r'^(DEBATE|POSTPONED PROCEEDINGS|NOTIFICATION OF INTENT)', re.I)
TAG = {'IntroReferral': '提出/转交', 'Committee': '委员会', 'Floor': '院会',
       'Calendars': '议事日程', 'President': '总统', 'ResolvingDifferences': '两院协商',
       'Discharge': '撤销审议', 'Veto': '否决', 'NotUsed': '其他'}
_gate = threading.Lock()
_next_request = [0.0]


def rate_gate():
    with _gate:
        now = time.monotonic()
        if now < _next_request[0]:
            time.sleep(_next_request[0] - now)
        _next_request[0] = max(now, _next_request[0]) + .75


def load_actions(item, key):
    match = URL_RE.search(item.get('url', ''))
    if not match or match.group(2) not in TYPE:
        return None
    congress, slug, number = match.groups()
    endpoint = 'https://api.congress.gov/v3/bill/%s/%s/%s/actions' % (
        congress, TYPE[slug], number)
    all_actions = []
    for offset in range(0, 5000, 250):
        url = '%s?api_key=%s&format=json&limit=250&offset=%s' % (endpoint, key, offset)
        for attempt in range(3):
            rate_gate()
            result = subprocess.run(['curl', '-fsS', '--max-time', '30', url],
                                    capture_output=True)
            if result.returncode == 0:
                break
            time.sleep(2 * (attempt + 1))
        else:
            raise RuntimeError('Congress.gov request failed for %s' % item.get('official_no'))
        page = json.loads(result.stdout)
        batch = page.get('actions') or []
        all_actions.extend(batch)
        if len(batch) < 250 or not page.get('pagination', {}).get('next'):
            break
    return select_actions(all_actions)


def select_actions(actions):
    clean = []
    seen = set()
    for action in actions:
        date = (action.get('actionDate') or action.get('date') or '')[:10]
        text = html.unescape(re.sub(r'<[^>]+>', '', action.get('text') or '')).strip()
        if not date or not text or (date, text) in seen:
            continue
        seen.add((date, text))
        raw_tag = action.get('type') or action.get('tag') or ''
        clean.append({'date': date, 'text': text[:500], 'tag': TAG.get(raw_tag, raw_tag)})
    if not clean:
        return []
    chosen = []
    per_date = {}
    for index, action in enumerate(clean):
        if index not in (0, len(clean) - 1) and (NOISE.search(action['text']) or not MILESTONE.search(action['text'])):
            continue
        if any(other['date'] == action['date'] and other['text'] != action['text']
               and action['text'] in other['text'] and other['text'].startswith('Passed/agreed to')
               for other in clean):
            continue
        date = action['date']
        if index not in (0, len(clean) - 1) and per_date.get(date, 0) >= 3:
            continue
        chosen.append(action)
        per_date[date] = per_date.get(date, 0) + 1
    if clean[-1] not in chosen:
        chosen.append(clean[-1])
    return chosen


def save(doc):
    temp = ITEMS.with_suffix('.json.tmp')
    temp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(ITEMS)


def apply_actions(item, actions):
    if not actions:
        return False
    item['actions'] = actions
    latest = actions[0]
    if latest['date'] > (item.get('stage_date') or ''):
        prefix = (item.get('agency') or '众议院').split('·', 1)[0].strip()
        item['stage_date'] = latest['date']
        item['agency'] = prefix + ' · ' + latest['text'][:180]
        if item.get('brief', {}).get('body'):
            item['brief']['stale'] = True
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int)
    parser.add_argument('--refresh', action='store_true')
    args = parser.parse_args()
    key = KEY.read_text(encoding='utf-8').strip()
    doc = json.loads(ITEMS.read_text(encoding='utf-8'))
    todo = [item for item in doc['items'] if args.refresh or not item.get('actions')]
    if args.limit:
        todo = todo[:args.limit]
    print('Items to enrich:', len(todo), flush=True)
    done = 0
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = pool.map(lambda item: load_actions(item, key), todo)
        for index, item in enumerate(todo, 1):
            try:
                actions = next(results)
                if apply_actions(item, actions):
                    done += 1
            except Exception as error:
                print('%s: %s' % (item.get('official_no'), error), flush=True)
            if index % 25 == 0 or index == len(todo):
                save(doc)
                print('Saved %s / %s' % (done, index), flush=True)


if __name__ == '__main__':
    main()
