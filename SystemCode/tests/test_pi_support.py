"""Server-side support for the Pi runtime's repair loop: budget swaps and filter checks."""
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import create_router
from backend.orchestrator import RecommendationEngine
from backend.planning import Chooser, budget_repair, make_item, minor, plan_desktop
from backend.retrieval import LocalCorpus
from backend.settings import Settings
from backend.store import StateStore
from backend.validation import validate_option

DATA = Path(__file__).resolve().parents[1] / "data"
TOKEN = {"X-Internal-Token": "secret-token"}


def requirements(budget_minor: int) -> dict:
    return {"device_type": "desktop", "budget": {"currency": "SGD", "maximum_minor": budget_minor, "is_hard_limit": True},
            "workloads": ["SolidWorks", "MATLAB"], "preferences": [], "hard_constraints": {},
            "locked_product_ids": [], "locked_items": [], "owned_components": []}


class BudgetRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = LocalCorpus(DATA)
        cls.option = plan_desktop(cls.corpus.candidates, cls.corpus.products, requirements(200000), Chooser())
        cls.total = validate_option(cls.option, requirements(10 ** 9))["total_minor"]

    def repair(self, option: dict, req: dict) -> dict | None:
        return budget_repair(option, validate_option(option, req), self.corpus.candidates, req, validate_option)

    def swapped(self, category: str, row: dict) -> dict:
        return {**self.option, "items": [make_item(row) if i["category"] == category else i for i in self.option["items"]]}

    def test_no_advice_when_the_build_is_within_budget(self):
        self.assertIsNone(self.repair(self.option, requirements(self.total)))

    def test_small_overspend_lists_single_swaps_that_really_fix_it(self):
        req = requirements(self.total - 1600)            # S$16 over, as in the reported run
        repair = self.repair(self.option, req)
        self.assertEqual(repair["over_minor"], 1600)
        self.assertTrue(repair["reachable"])
        self.assertGreater(len(repair["single_swaps"]), 1)     # not only the most expensive part
        savings = [entry["saving_minor"] for entry in repair["single_swaps"]]
        self.assertEqual(savings, sorted(savings))            # smallest downgrade first
        for entry in repair["single_swaps"]:
            for row in entry["alternatives"]:
                self.assertLessEqual(minor(row), entry["current_minor"] - 1600)
                self.assertEqual(validate_option(self.swapped(entry["category"], row), req)["failed_codes"], [])

    def test_locked_parts_are_never_offered_for_replacement(self):
        req = requirements(self.total - 1600)
        free = {entry["category"] for entry in self.repair(self.option, req)["single_swaps"]}
        locked = {**self.option, "items": [{**i, "locked": i["category"] in free} for i in self.option["items"]]}
        repair = self.repair(locked, req)
        self.assertFalse(free & {entry["category"] for entry in repair["single_swaps"] + repair["partial_savings"]})

    def test_hopeless_budget_is_reported_as_unreachable(self):
        repair = self.repair(self.option, requirements(self.total // 4))
        self.assertEqual(repair["single_swaps"], [])
        self.assertFalse(repair["reachable"])


class CandidateFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        settings = Settings(data_dir=DATA, state_db=Path(cls.temp.name) / "s.db", model_name=None, model_provider=None,
                            embedding_provider="hash", embedding_model=None, embedding_dimensions=256,
                            retrieval_backend="local", pi_runtime_url=None, internal_api_token="secret-token")
        store, cls.corpus = StateStore(settings.state_db), LocalCorpus(DATA)
        app = FastAPI()
        app.include_router(create_router(store, cls.corpus, RecommendationEngine(store, cls.corpus, settings)))
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def candidates(self, **body) -> dict:
        response = self.client.post("/api/v1/internal/candidates", headers=TOKEN, json={"run_id": "r", **body})
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_filter_on_a_spec_the_category_lacks_is_rejected_with_the_valid_names(self):
        result = self.candidates(category="gpu", require={"socket": "PCIe 3.0"}, minimum_specs={"wattage_w": 300})
        self.assertEqual(result["items"], [])
        self.assertEqual(result["invalid_filters"]["unknown_keys"], ["socket", "wattage_w"])
        self.assertIn("vram_gb", result["invalid_filters"]["valid_keys"])

    def test_unknown_category_is_rejected(self):
        result = self.candidates(category="graphics card")
        self.assertEqual(result["invalid_filters"]["unknown_category"], "graphics card")
        self.assertIn("gpu", result["invalid_filters"]["valid_categories"])

    def test_valid_filters_still_return_offers(self):
        result = self.candidates(category="motherboard", require={"socket": "AM5"})
        self.assertTrue(result["items"])
        self.assertNotIn("invalid_filters", result)

    def test_empty_price_range_reports_where_matching_offers_start(self):
        result = self.candidates(category="gpu", maximum_minor=100)
        self.assertEqual(result["items"], [])
        self.assertGreater(result["cheapest_match_sgd"], 1)

    def test_assemble_attaches_budget_swaps_to_an_overspend(self):
        option = plan_desktop(self.corpus.candidates, self.corpus.products, requirements(200000), Chooser())
        total = validate_option(option, requirements(10 ** 9))["total_minor"]
        body = {"run_id": "r", "device_type": "desktop", "offer_ids": [i["offer_id"] for i in option["items"]]}
        over = self.client.post("/api/v1/internal/options/assemble", headers=TOKEN,
                                json={**body, "requirements": requirements(total - 1600)}).json()
        self.assertEqual(over["budget_repair"]["over_minor"], 1600)
        swap = over["budget_repair"]["single_swaps"][0]["alternatives"][0]
        self.assertTrue({"offer_id", "name", "price_sgd"} <= set(swap))
        within = self.client.post("/api/v1/internal/options/assemble", headers=TOKEN,
                                  json={**body, "requirements": requirements(total)}).json()
        self.assertNotIn("budget_repair", within)


if __name__ == "__main__":
    unittest.main()
