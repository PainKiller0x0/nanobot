#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
import urllib.parse
import urllib.request

BASE_URL_CANDIDATES = [
    os.environ.get('WECHAT_RSS_BASE_URL', '').strip(),
    # Host-side scripts should be fast; container-side scripts can use Podman DNS.
    'http://127.0.0.1:8091',
    'http://wechat-rss-sidecar:8091',
]


def request(path: str, method: str = 'GET', payload: dict | None = None, expect_json: bool = True):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'

    last_error: Exception | None = None
    for base_url in BASE_URL_CANDIDATES:
        if not base_url:
            continue
        req = urllib.request.Request(base_url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = resp.read().decode('utf-8')
                return json.loads(body) if expect_json else body
        except Exception as exc:
            last_error = exc
            continue

    if last_error is not None:
        raise last_error
    raise RuntimeError('No sidecar base URL configured')


def request_json(path: str, method: str = 'GET', payload: dict | None = None) -> dict:
    return request(path, method=method, payload=payload, expect_json=True)


def request_text(path: str, method: str = 'GET') -> str:
    return request(path, method=method, expect_json=False)


def print_json(data: dict) -> int:
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def parse_iso_to_utc(value: str | None) -> datetime:
    text = (value or '').strip()
    if not text:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fetch_timeline_items(days: int, limit: int, subscription_id: int | None = None) -> list[dict]:
    params = {'days': days, 'limit': limit}
    if subscription_id:
        params['subscription_id'] = subscription_id
    payload = request_json('/api/timeline?' + urllib.parse.urlencode(params))
    items = payload.get('items') or []
    if not isinstance(items, list):
        return []
    items.sort(
        key=lambda row: (
            parse_iso_to_utc(row.get('published_at')),
            parse_iso_to_utc(row.get('inserted_at')),
            int(row.get('id') or 0),
        ),
        reverse=True,
    )
    return items


def build_article_payload(entry_id: int, raw: bool = False) -> dict:
    raw_item = request_json(f'/api/articles/{entry_id}')
    if raw:
        return raw_item

    item = raw_item.get('item') or {}
    markdown = request_text(f'/api/articles/{entry_id}/markdown').strip()
    if not markdown:
        markdown = (
            item.get('article_markdown')
            or item.get('content_markdown')
            or item.get('summary')
            or ''
        )

    return {
        'entry_id': item.get('id') or entry_id,
        'title': item.get('title') or '',
        'subscription_name': item.get('subscription_name') or '',
        'published_at': item.get('published_at') or '',
        'published_at_local': item.get('published_at_local') or '',
        'link': item.get('link') or '',
        'article_markdown': markdown,
    }


def build_latest_article_payload(
    days: int = 7,
    limit: int = 50,
    subscription_id: int | None = None,
    refresh: bool = False,
    sample_fetches: int = 3,
    sample_interval: float = 0.6,
) -> dict:
    if refresh:
        refresh_payload = {
            'days': days,
            'sample_fetches': sample_fetches,
            'sample_interval': sample_interval,
        }
        if subscription_id:
            request_json(
                f'/api/subscriptions/{subscription_id}/refresh',
                method='POST',
                payload=refresh_payload,
            )
        else:
            request_json('/api/refresh-all', method='POST', payload=refresh_payload)
    items = fetch_timeline_items(days=days, limit=max(10, limit), subscription_id=subscription_id)
    if not items:
        return {
            'status': 'empty',
            'reason': 'NO_ITEMS_IN_TIMELINE',
            'days': days,
            'subscription_id': subscription_id or 0,
        }
    top = items[0]
    entry_id = int(top.get('id') or 0)
    if entry_id <= 0:
        return {
            'status': 'error',
            'reason': 'INVALID_ENTRY_ID',
            'days': days,
            'subscription_id': subscription_id or 0,
        }
    article = build_article_payload(entry_id, raw=False)
    article['status'] = 'ok'
    article['selection'] = {
        'picked_entry_id': entry_id,
        'picked_published_at': top.get('published_at') or '',
        'picked_inserted_at': top.get('inserted_at') or '',
        'days': days,
        'limit': max(10, limit),
        'subscription_id': subscription_id or 0,
        'refresh': bool(refresh),
    }
    return article


def extract_question_tokens(question: str) -> list[str]:
    tokens = re.findall(r'[\u4e00-\u9fff]{2,}|[A-Za-z0-9_]{3,}', question or '')
    stop = {
        'weixin', 'wechat', 'article', 'latest', 'question',
        'what', 'which', 'about', 'tell', 'please', 'content',
        'this', 'that', 'does', 'did', 'is', 'are', 'was', 'were',
        'say', 'says', 'mention', 'mentioned', 'have', 'has', 'had',
        'there', 'their', 'from', 'into', 'with', 'without',
        'http', 'https', 'com', 'scene', 'biz', 'mid', 'idx', 'sn',
    }
    uniq: list[str] = []
    seen = set()
    for token in tokens:
        low = token.lower()
        if low in stop:
            continue
        if low in seen:
            continue
        seen.add(low)
        uniq.append(token)
    return uniq[:12]


def extractive_answer_from_markdown(markdown: str, question: str, max_lines: int = 8) -> dict:
    lines = [line.strip() for line in (markdown or '').splitlines() if line.strip()]
    body_lines = lines[1:] if lines and lines[0].startswith('# ') else list(lines)
    body_lines = [
        line for line in body_lines
        if not re.match(r'^-\s*(Account|Biz|Published|Inserted|Original)\b', line, flags=re.IGNORECASE)
        and '/ Account:' not in line
        and '/ Published:' not in line
        and '/ Inserted:' not in line
        and '/ Original:' not in line
        and not line.startswith('- Biz:')
    ]
    if not body_lines:
        return {'status': 'not_found', 'answer': 'NOT_FOUND_IN_ARTICLE', 'evidence': [], 'tokens': []}
    tokens = extract_question_tokens(question)
    if not tokens:
        evidence = body_lines[: min(3, len(body_lines))]
        return {
            'status': 'ok' if evidence else 'not_found',
            'answer': '\n'.join(evidence) if evidence else 'NOT_FOUND_IN_ARTICLE',
            'evidence': evidence,
            'tokens': [],
        }

    scored: list[tuple[int, int, str]] = []
    for line in body_lines:
        line_low = line.lower()
        score = 0
        for token in tokens:
            if token.lower() in line_low:
                score += 2
        if score > 0 and '](' in line:
            score += 1
        if score > 0:
            scored.append((score, len(line), line))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    evidence: list[str] = []
    for _, _, line in scored:
        if line in evidence:
            continue
        evidence.append(line)
        if len(evidence) >= max_lines:
            break
    if not evidence:
        return {'status': 'not_found', 'answer': 'NOT_FOUND_IN_ARTICLE', 'evidence': [], 'tokens': tokens}
    return {'status': 'ok', 'answer': '\n'.join(evidence), 'evidence': evidence, 'tokens': tokens}


def build_ask_payload(
    question: str,
    entry_id: int | None = None,
    days: int = 7,
    limit: int = 50,
    subscription_id: int | None = None,
    refresh: bool = False,
    sample_fetches: int = 3,
    sample_interval: float = 0.6,
) -> dict:
    if entry_id is not None and entry_id > 0:
        article = build_article_payload(entry_id, raw=False)
    else:
        article = build_latest_article_payload(
            days=days,
            limit=limit,
            subscription_id=subscription_id,
            refresh=refresh,
            sample_fetches=sample_fetches,
            sample_interval=sample_interval,
        )
        if article.get('status') not in (None, 'ok'):
            return {
                'status': 'not_found',
                'question': question,
                'answer': 'NOT_FOUND_IN_ARTICLE',
                'reason': article.get('reason') or 'LATEST_NOT_AVAILABLE',
                'evidence': [],
            }

    result = extractive_answer_from_markdown(article.get('article_markdown') or '', question)
    return {
        'status': result['status'],
        'mode': 'extractive',
        'question': question,
        'entry_id': article.get('entry_id') or entry_id or 0,
        'published_at': article.get('published_at') or '',
        'published_at_local': article.get('published_at_local') or '',
        'title': article.get('title') or '',
        'link': article.get('link') or '',
        'tokens': result.get('tokens') or [],
        'answer': result['answer'],
        'evidence': result.get('evidence') or [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='Thin client for the WeChat RSS Sidecar')
    subparsers = parser.add_subparsers(dest='command', required=True)
    subparsers.add_parser('subscriptions')

    timeline_parser = subparsers.add_parser('timeline')
    timeline_parser.add_argument('--days', type=int, default=7)
    timeline_parser.add_argument('--limit', type=int, default=20)
    timeline_parser.add_argument('--subscription-id', type=int)

    new_parser = subparsers.add_parser('new-items')
    new_parser.add_argument('--hours', type=int, default=24)
    new_parser.add_argument('--limit', type=int, default=20)

    article_parser = subparsers.add_parser('article')
    article_parser.add_argument('--entry-id', type=int, required=True)
    article_parser.add_argument('--raw', action='store_true', help='Return raw /api/articles payload')

    latest_parser = subparsers.add_parser('latest')
    latest_parser.add_argument('--days', type=int, default=7)
    latest_parser.add_argument('--limit', type=int, default=50)
    latest_parser.add_argument('--subscription-id', type=int)
    latest_parser.add_argument('--refresh', action='store_true')
    latest_parser.add_argument('--sample-fetches', type=int, default=3)
    latest_parser.add_argument('--sample-interval', type=float, default=0.6)

    ask_parser = subparsers.add_parser('ask')
    ask_parser.add_argument('--question', required=True)
    ask_parser.add_argument('--entry-id', type=int)
    ask_parser.add_argument('--days', type=int, default=7)
    ask_parser.add_argument('--limit', type=int, default=50)
    ask_parser.add_argument('--subscription-id', type=int)
    ask_parser.add_argument('--refresh', action='store_true')
    ask_parser.add_argument('--sample-fetches', type=int, default=3)
    ask_parser.add_argument('--sample-interval', type=float, default=0.6)

    llm_parser = subparsers.add_parser('llm-settings')
    llm_parser.add_argument('--api-base')
    llm_parser.add_argument('--api-key')
    llm_parser.add_argument('--model')

    refresh_parser = subparsers.add_parser('refresh')
    refresh_parser.add_argument('--subscription-id', type=int)
    refresh_parser.add_argument('--all', action='store_true')
    refresh_parser.add_argument('--days', type=int, default=7)
    refresh_parser.add_argument('--sample-fetches', type=int, default=5)
    refresh_parser.add_argument('--sample-interval', type=float, default=1.0)

    args = parser.parse_args()

    if args.command == 'subscriptions':
        return print_json(request_json('/api/subscriptions'))

    if args.command == 'timeline':
        return print_json({'items': fetch_timeline_items(days=args.days, limit=args.limit, subscription_id=args.subscription_id)})

    if args.command == 'new-items':
        params = {'hours': args.hours, 'limit': args.limit}
        return print_json(request_json('/api/new-items?' + urllib.parse.urlencode(params)))

    if args.command == 'article':
        return print_json(build_article_payload(args.entry_id, raw=args.raw))

    if args.command == 'latest':
        return print_json(
            build_latest_article_payload(
                days=args.days,
                limit=args.limit,
                subscription_id=args.subscription_id,
                refresh=args.refresh,
                sample_fetches=args.sample_fetches,
                sample_interval=args.sample_interval,
            )
        )

    if args.command == 'ask':
        return print_json(
            build_ask_payload(
                question=args.question,
                entry_id=args.entry_id,
                days=args.days,
                limit=args.limit,
                subscription_id=args.subscription_id,
                refresh=args.refresh,
                sample_fetches=args.sample_fetches,
                sample_interval=args.sample_interval,
            )
        )

    if args.command == 'llm-settings':
        if args.api_base is None and args.api_key is None and args.model is None:
            return print_json(request_json('/api/settings/llm'))
        payload = {'api_base': args.api_base or '', 'api_key': args.api_key or '', 'model': args.model or ''}
        return print_json(request_json('/api/settings/llm', method='POST', payload=payload))

    if args.command == 'refresh':
        payload = {'days': args.days, 'sample_fetches': args.sample_fetches, 'sample_interval': args.sample_interval}
        if args.all:
            return print_json(request_json('/api/refresh-all', method='POST', payload=payload))
        if not args.subscription_id:
            print('refresh requires --subscription-id or --all', file=sys.stderr)
            return 2
        return print_json(request_json(f'/api/subscriptions/{args.subscription_id}/refresh', method='POST', payload=payload))

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
