"""The catalogue endpoint behind the Components page: search, sorting and paging."""
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from crawler.api import router


class CatalogueSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def prices(self, **params) -> dict:
        response = self.client.get("/data/prices", params={"in_stock": True, **params})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_search_ignores_spacing_and_word_order(self):
        spaced, joined = self.prices(q="rtx 5070", category="gpu"), self.prices(q="rtx5070", category="gpu")
        self.assertGreater(spaced["total"], 1)
        self.assertEqual(spaced["total"], joined["total"])
        self.assertTrue(any("RTX5070" in item["name"].upper().replace(" ", "") for item in spaced["items"]))
        self.assertEqual(self.prices(q="ryzen 9800x3d")["total"], self.prices(q="9800x3d ryzen")["total"])
        self.assertEqual(self.prices(q="   ")["total"], self.prices()["total"])

    def test_listing_named_as_typed_comes_first(self):
        first = self.prices(q="ryzen 7 9800x3d")["items"][0]
        self.assertIn("ryzen 7 9800x3d", first["name"].casefold())

    def test_sort_and_paging(self):
        cheap = [float(i["price"]) for i in self.prices(category="gpu", sort="price_asc")["items"]]
        dear = [float(i["price"]) for i in self.prices(category="gpu", sort="price_desc")["items"]]
        self.assertEqual(cheap, sorted(cheap))
        self.assertEqual(dear, sorted(dear, reverse=True))
        page_one, page_two = self.prices(limit=10)["items"], self.prices(limit=10, offset=10)["items"]
        self.assertFalse({i["id"] for i in page_one} & {i["id"] for i in page_two})
        self.assertEqual(self.client.get("/data/prices", params={"sort": "name"}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
