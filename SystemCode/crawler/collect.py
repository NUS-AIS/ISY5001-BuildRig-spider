"""Run: python -m crawler.collect --max-pages 5 --review-products 20."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from pathlib import Path
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from protego import Protego
import requests

ROOT = Path(__file__).resolve().parents[1]
AGENT = "SGComputerResearchBot/0.1"


def now():
    return datetime.now(timezone.utc).isoformat()


def key(value):
    return hashlib.sha256(value.encode()).hexdigest()[:20]


def money(value):
    try:
        amount = Decimal(str(value).replace(',', '').replace('$', '').strip())
        return str(amount.quantize(Decimal('0.01'))) if amount.is_finite() and amount > 0 else None
    except (InvalidOperation, ValueError):
        return None


class Client:
    def __init__(self, folder, delay=1.5):
        self.folder, self.delay = folder, delay
        self.session = requests.Session()
        self.session.headers['User-Agent'] = AGENT
        self.rules, self.last, self.events = {}, {}, []

    def request(self, url):
        origin = '{0.scheme}://{0.netloc}'.format(urlsplit(url))
        wait = self.delay - (time.monotonic() - self.last.get(origin, 0))
        if wait > 0:
            time.sleep(wait)
        attempts = 5
        for attempt in range(attempts):
            self.last[origin] = time.monotonic()
            response = self.session.get(url, timeout=(10, 35), allow_redirects=False)
            if response.status_code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                # Honour the server's Retry-After (Shopify sends 60 s on 429) instead of giving up.
                try:
                    pause = min(120, max(self.delay, float(response.headers.get('Retry-After', 2 ** (attempt + 2)))))
                except ValueError:
                    pause = 10
                print(f'  HTTP {response.status_code} for {url}; waiting {pause:.0f}s', flush=True)
                time.sleep(pause)
                continue
            return response

    def get(self, url, redirects=0):
        origin = '{0.scheme}://{0.netloc}'.format(urlsplit(url))
        if origin not in self.rules:
            robot = self.request(origin + '/robots.txt')
            if robot.status_code == 404:
                self.rules[origin] = Protego.parse('User-agent: *\nAllow: /')
            else:
                robot.raise_for_status()
                if robot.is_redirect or '<html' in robot.text.lower():
                    raise RuntimeError('robots.txt unavailable or redirected: ' + origin)
                self.rules[origin] = Protego.parse(robot.text)
            (self.folder / (key(origin) + '.robots.txt')).write_text(robot.text, encoding='utf-8')
        if not self.rules[origin].can_fetch(url, AGENT):
            raise RuntimeError('robots.txt disallows ' + url)
        crawl_delay = self.rules[origin].crawl_delay(AGENT)
        if crawl_delay:
            time.sleep(max(0, float(crawl_delay) - (time.monotonic() - self.last.get(origin, 0))))
        r = self.request(url)
        if r.is_redirect:
            if redirects >= 4:
                raise RuntimeError('Too many redirects')
            return self.get(urljoin(url, r.headers['Location']), redirects + 1)
        r.raise_for_status()
        encoding = r.encoding if r.encoding and r.encoding.lower() != 'iso-8859-1' else 'utf-8'
        content = r.content.decode(encoding, errors='replace')
        filename = key(url) + ('.json' if 'json' in r.headers.get('Content-Type', '') else '.html')
        (self.folder / filename).write_text(content, encoding='utf-8')
        self.events.append({'url': url, 'fetched_at': now(), 'raw_file': filename, 'status': r.status_code})
        return content


def category_for(title, category):
    text = title.lower()
    if category in ('ssd', 'hdd') and any(x in text for x in ('external', 'portable', 'one touch')):
        return 'external_storage'
    if category == 'ram' and re.search(r'so[ -]?dimm|notebook|laptop', text):
        return 'laptop_ram'
    if ' + ' in text or 'bundle' in text:
        return 'bundle'
    if category == 'gpu' and any(x in text for x in ('holder', 'bracket', 'riser')):
        return 'accessory'
    return category


def parse_shopify(payload, source, category):
    result = []
    for product in payload['products']:
        for variant in product.get('variants', []):
            price = money(variant.get('price'))
            if not price:
                continue
            url = source['base_url'] + '/products/' + product['handle']
            result.append({
                'id': key(url + ':' + str(variant['id'])),
                'product_id': str(product['id']), 'variant_id': str(variant['id']),
                'name': product['title'], 'variant': variant.get('title'),
                'brand': product.get('vendor'), 'sku': variant.get('sku'),
                'category': category_for(product['title'], category), 'source_category': category,
                'price': price, 'currency': source['currency'],
                'compare_at_price': money(variant.get('compare_at_price')),
                'available': variant.get('available'), 'market': 'SG',
                'store': source['name'], 'source_url': url,
                'collected_at': now(), 'source_updated_at': product.get('updated_at'),
                'description': BeautifulSoup(product.get('body_html') or '', 'html.parser').get_text(' ', strip=True),
                'tax_included': None, 'shipping_included': None,
                'price_scope': 'variant', 'extraction': 'shopify_public_catalog',
            })
    return result


def walk(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def parse_reviews(html, product):
    soup = BeautifulSoup(html, 'html.parser')
    rows = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            nodes = list(walk(json.loads(script.get_text())))
        except (ValueError, TypeError):
            continue
        for node in nodes:
            types = node.get('@type', [])
            types = [types] if isinstance(types, str) else types
            if 'Product' not in types:
                continue
            reviews = node.get('review', [])
            if isinstance(reviews, dict):
                reviews = [reviews]
            for review in reviews:
                if not isinstance(review, dict) or not review.get('reviewBody'):
                    continue
                body = BeautifulSoup(review['reviewBody'], 'html.parser').get_text(' ', strip=True)
                rating = review.get('reviewRating') or {}
                rows.append({
                    'id': key(product['source_url'] + body),
                    'product_name': node.get('name') or product['name'],
                    'product_url': product['source_url'],
                    'category': product['category'], 'kind': 'user_review',
                    'text': body, 'rating': rating.get('ratingValue'),
                    'rating_scale': rating.get('bestRating'),
                    'published_at': review.get('datePublished'),
                    'source_url': product['source_url'], 'collected_at': now(),
                    'region': 'unknown', 'verified_purchase': None,
                    'extraction': 'product_json_ld',
                })
    return rows


def save_rows(folder, name, rows):
    with (folder / (name + '.jsonl')).open('w', encoding='utf-8') as out:
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False) + '\n')
    with (folder / (name + '.txt')).open('w', encoding='utf-8') as out:
        for row in rows:
            out.write('\n'.join(f'{k}: {v}' for k, v in row.items() if v is not None) + '\n\n' + '=' * 72 + '\n\n')


def run(args):
    config = json.loads(Path(args.config).read_text(encoding='utf-8'))
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    folder = Path(args.output) / 'runs' / stamp
    raw = folder / 'raw'
    raw.mkdir(parents=True, exist_ok=True)
    client = Client(raw, args.delay)
    prices, reviews, issues, coverage = {}, {}, [], []
    for source in config['stores']:
        for category, collection in source['collections'].items():
            seen, count, status = set(), 0, 'page_limit_reached'
            for page in range(1, args.max_pages + 1):
                url = source['base_url'] + '/collections/' + collection + '/products.json'
                if page > 1:
                    url += f'?page={page}'
                try:
                    payload = json.loads(client.get(url))
                    items = parse_shopify(payload, source, category)
                    if not payload['products']:
                        status = 'exhausted'
                        break
                    fresh = [r for r in items if r['id'] not in seen]
                    if not fresh:
                        status = 'repeated_page'
                        break
                    for row in fresh:
                        row['catalog_url'] = url
                        seen.add(row['id'])
                        prices[row['id']] = row
                    count += len(fresh)
                except Exception as exc:
                    issues.append({'url': url, 'stage': 'prices', 'error': str(exc)})
                    status = 'incomplete'
                    break
            coverage.append({'store': source['name'], 'category': category, 'variants': count, 'status': status})
            print(f'{source["name"]} {category}: {count} ({status})', flush=True)
    # Round-robin categories so the review sample is not exclusively CPUs.
    groups = {}
    for row in prices.values():
        groups.setdefault(row['category'], {})[row['source_url']] = row
    buckets = [list(group.values()) for group in groups.values()]
    candidates = [bucket[i] for i in range(max((len(b) for b in buckets), default=0)) for bucket in buckets if i < len(bucket)]
    for product in candidates[:args.review_products]:
        try:
            found = parse_reviews(client.get(product['source_url']), product)
            reviews.update({r['id']: r for r in found})
            if not found:
                issues.append({'url': product['source_url'], 'stage': 'reviews', 'error': 'No public Product JSON-LD review bodies; ratings alone are not user reviews.'})
        except Exception as exc:
            issues.append({'url': product['source_url'], 'stage': 'reviews', 'error': str(exc)})
    for seed in config.get('review_pages', []):
        try:
            html = client.get(seed['url'])
            soup = BeautifulSoup(html, 'html.parser')
            posts = soup.select('shreddit-post [slot="text-body"], .expando .usertext-body')
            if not posts:
                raise RuntimeError('No public community post body; no login or challenge bypass attempted')
            for post in posts:
                body = post.get_text(' ', strip=True)
                if body:
                    row = {**seed, 'id': key(seed['url'] + body), 'text': body,
                           'source_url': seed['url'], 'collected_at': now(), 'verified_purchase': None,
                           'published_at': None, 'extraction': 'public_html_post'}
                    reviews[row['id']] = row
        except Exception as exc:
            issues.append({'url': seed['url'], 'stage': 'community', 'error': str(exc)})
    imported_count = 0
    if args.review_import:
        for line in Path(args.review_import).read_text(encoding='utf-8').split('\n'):
            if not line.strip():
                continue
            row = json.loads(line)
            if not all(row.get(k) for k in ('id', 'text', 'source_url', 'collected_at', 'category', 'extraction')):
                raise ValueError('Imported reviews require id/text/source_url/collected_at/category/extraction')
            if row['id'] not in reviews:
                reviews[row['id']] = row
                imported_count += 1
    save_rows(folder, 'prices', list(prices.values()))
    save_rows(folder, 'reviews', list(reviews.values()))
    report = {'run_id': stamp, 'finished_at': now(), 'price_count': len(prices),
              'review_count': len(reviews), 'imported_review_count': imported_count,
              'crawled_review_count': len(reviews) - imported_count,
              'categories': dict(Counter(r['category'] for r in prices.values())),
              'coverage': coverage, 'issues': issues, 'requests': client.events,
              'limitations': ['Not an exhaustive Singapore market inventory.',
                             'Region and verified purchase of reviews are unknown unless explicitly supplied.',
                             'Compatibility specifications are not inferred from product names.',
                             'GST and delivery inclusion are unknown; recheck checkout prices.']}
    (folder / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (folder / 'report.txt').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    if prices:
        latest = Path(args.output) / 'latest.json'
        temp = latest.with_suffix('.tmp')
        temp.write_text(json.dumps({'run_id': stamp, 'path': 'runs/' + stamp}), encoding='utf-8')
        temp.replace(latest)
    print(json.dumps({'folder': str(folder), 'prices': len(prices), 'reviews': len(reviews), 'issues': len(issues)}), flush=True)
    return 0 if prices else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(ROOT / 'crawler/sources.json'))
    parser.add_argument('--output', default=str(ROOT / 'data'))
    parser.add_argument('--max-pages', type=int, default=5)
    parser.add_argument('--review-products', type=int, default=22)
    parser.add_argument('--delay', type=float, default=1.5)
    parser.add_argument('--review-import', help='Optional previously sourced review JSONL; original provenance is retained')
    args = parser.parse_args()
    if args.max_pages < 1 or args.review_products < 0 or args.delay < 0:
        parser.error('max-pages >= 1, review-products >= 0, delay >= 0 required')
    raise SystemExit(run(args))


if __name__ == '__main__':
    main()
