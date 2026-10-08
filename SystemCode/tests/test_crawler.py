import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from crawler.collect import money, parse_shopify, parse_reviews, category_for


class ParsingTests(unittest.TestCase):
    def test_price_is_not_installment_or_nan(self):
        self.assertEqual(money('1,299.00'), '1299.00')
        for value in ('NaN', 'Infinity', '-10', '0', None, 'Call for price'):
            self.assertIsNone(money(value))

    def test_variants_do_not_collapse_and_currency_is_explicit(self):
        source = {'base_url': 'https://example.com', 'currency': 'SGD', 'name': 'SG store'}
        data = {'products': [{'id': 1, 'handle': 'laptop', 'title': 'Laptop', 'variants': [
            {'id': 2, 'title': '16GB', 'price': '1299', 'available': False},
            {'id': 3, 'title': '32GB', 'price': '1599', 'available': True}]}]}
        rows = parse_shopify(data, source, 'laptop')
        self.assertNotEqual(rows[0]['id'], rows[1]['id'])
        self.assertFalse(rows[0]['available'])
        self.assertEqual(rows[1]['currency'], 'SGD')
        self.assertEqual(rows[1]['price'], '1599.00')

    def test_store_ratings_are_not_product_reviews(self):
        p = {'source_url': 'https://example.com/p', 'name': 'CPU', 'category': 'cpu'}
        html = '''<script type="application/ld+json">[{"@type":"LocalBusiness","review":{"reviewBody":"Nice staff"}},
        {"@type":"Product","aggregateRating":{"ratingValue":5},"review":[{"reviewBody":"Runs cool","reviewRating":{"ratingValue":4,"bestRating":5}}]}]</script>'''
        rows = parse_reviews(html, p)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['text'], 'Runs cool')
        self.assertIsNone(rows[0]['verified_purchase'])

    def test_bundle_and_gpu_accessory(self):
        self.assertEqual(category_for('CPU + Motherboard', 'cpu'), 'bundle')
        self.assertEqual(category_for('Graphics Card Holder', 'gpu'), 'accessory')
        self.assertEqual(category_for('Portable External HDD', 'hdd'), 'external_storage')
        self.assertEqual(category_for('32GB SO-DIMM Notebook Memory', 'ram'), 'laptop_ram')

    def test_unicode_line_separators_in_real_product_descriptions(self):
        from crawler.api import prices
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            row = {'name': 'SSD', 'description': 'Fast\u2028storage', 'category': 'ssd', 'price': '80.00', 'available': True}
            (folder / 'prices.jsonl').write_text(json.dumps(row, ensure_ascii=False) + '\n', encoding='utf-8')
            with patch('crawler.api.latest_folder', return_value=folder):
                result = prices(max_price=None, offset=0, limit=50)
            self.assertEqual(result['total'], 1)


if __name__ == '__main__':
    unittest.main()
