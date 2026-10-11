"""Read-only dataset endpoints; crawling runs separately from web requests."""
import json
from decimal import Decimal
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix='/data', tags=['collected-data'])
DATA = Path(__file__).resolve().parents[1] / 'data'


def latest_folder():
    pointer = DATA / 'latest.json'
    if not pointer.exists():
        raise HTTPException(503, 'No collected dataset. Run python -m crawler.collect first.')
    return DATA / json.loads(pointer.read_text(encoding='utf-8'))['path']


@router.get('/status')
def status():
    return json.loads((latest_folder() / 'report.json').read_text(encoding='utf-8'))


@router.get('/prices')
def prices(category: str | None = None, q: str | None = None,
           max_price: Decimal | None = Query(None, gt=0), in_stock: bool = False,
           sort: str | None = Query(None, pattern='^(price_asc|price_desc)$'),
           offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=500)):
    with (latest_folder() / 'prices.jsonl').open(encoding='utf-8') as stream:
        rows = [json.loads(line) for line in stream if line.strip()]
    # Every word of the search must occur, in any order; "rtx 5070" also finds a listing written "RTX5070".
    plain = lambda text: text.casefold().replace('™', '').replace('®', '')
    words = plain(q or '').split()
    squashed = ''.join(words)

    def named(row):
        name = plain(row['name'])
        return all(word in name for word in words) or squashed in name.replace(' ', '').replace('-', '')

    rows = [r for r in rows if (not category or r['category'] == category)
            and (not words or named(r))
            and (max_price is None or Decimal(r['price']) <= max_price)
            and (not in_stock or r['available'] is True)]
    if sort:
        rows.sort(key=lambda r: Decimal(r['price']), reverse=sort == 'price_desc')
    elif words:          # listings that contain the search as typed come first
        rows.sort(key=lambda r: ' '.join(words) not in plain(r['name']))
    return {'total': len(rows), 'items': rows[offset:offset + limit]}


@router.get('/reviews')
def reviews(category: str | None = None, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=500)):
    with (latest_folder() / 'reviews.jsonl').open(encoding='utf-8') as stream:
        rows = [json.loads(line) for line in stream if line.strip()]
    rows = [r for r in rows if not category or r['category'] == category]
    return {'total': len(rows), 'items': rows[offset:offset + limit]}
