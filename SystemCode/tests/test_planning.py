import unittest
from pathlib import Path

from backend.planning import Chooser, Picks, make_item, plan_desktop, plan_laptops, revise
from backend.retrieval import LocalCorpus
from backend.validation import validate_option

DATA = Path(__file__).resolve().parents[1] / "data"


class PickFirstOfferIdModel:
    """Fake model that always proposes an offer id that is NOT in the shortlist."""
    enabled = True

    def structured(self, system, payload, schema):
        return Picks(picks=[{"category": c, "offer_id": "invented-offer", "reason": "made up"} for c in payload["shortlists"]])


class PlanningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = LocalCorpus(DATA)
        cls.req = {"device_type": "desktop", "budget": {"currency": "SGD", "maximum_minor": 350000},
                   "workloads": ["gaming"], "preferences": [], "hard_constraints": {}, "locked_product_ids": [],
                   "owned_components": []}

    def specs(self, option, category, field):
        return next(i for i in option["items"] if i["category"] == category)["specs"].get(field)

    def test_desktop_parts_follow_compatibility_chain(self):
        option = plan_desktop(self.corpus.candidates, self.corpus.products, self.req, Chooser())
        self.assertEqual([i["category"] for i in option["items"]],
                         ["cpu", "motherboard", "ram", "gpu", "psu", "case", "ssd", "cooler"])
        self.assertEqual(self.specs(option, "cpu", "socket"), self.specs(option, "motherboard", "socket"))
        board_memory = self.specs(option, "motherboard", "memory_type")
        if board_memory:
            self.assertEqual(self.specs(option, "ram", "memory_type"), board_memory)
        validation = validate_option(option, self.req)
        self.assertNotIn("cpu_motherboard_socket", validation["failed_codes"])
        self.assertNotIn("motherboard_memory_type", validation["failed_codes"])
        self.assertNotIn("budget_limit", validation["failed_codes"])
        self.assertGreater(validation["total_minor"], 350000 * 0.6)   # uses the budget instead of the cheapest parts

    def test_no_bundle_listings_are_selected(self):
        option = plan_desktop(self.corpus.candidates, self.corpus.products, self.req, Chooser())
        self.assertFalse(any(i["flags"].get("bundle_suspect") for i in option["items"]))

    def test_model_pick_outside_shortlist_is_ignored(self):
        chooser = Chooser(PickFirstOfferIdModel())
        option = plan_desktop(self.corpus.candidates, self.corpus.products, self.req, chooser)
        self.assertTrue(all(i["selected_by"] == "rules" for i in option["items"]))
        self.assertTrue(any(e["event"] == "rejected_model_pick" for e in chooser.log))

    def test_locked_product_is_kept_and_reserved_from_budget(self):
        gpu = self.corpus.candidates("gpu", limit=1)[0]
        req = {**self.req, "locked_product_ids": [str(gpu.get("product_id") or gpu["id"])]}
        option = plan_desktop(self.corpus.candidates, self.corpus.products, req, Chooser())
        chosen_gpu = next(i for i in option["items"] if i["category"] == "gpu")
        self.assertEqual(chosen_gpu["offer_id"], gpu["id"])
        self.assertTrue(chosen_gpu["locked"])

    def test_owned_component_costs_nothing(self):
        req = {**self.req, "owned_components": [{"mention": "RTX 4070", "category": "gpu", "specs": {"tdp_w": 200}}]}
        option = plan_desktop(self.corpus.candidates, self.corpus.products, req, Chooser())
        gpu = next(i for i in option["items"] if i["category"] == "gpu")
        self.assertTrue(gpu["owned_by_user"])
        self.assertEqual(validate_option(option, req)["total_minor"],
                         sum(int(float(i["price"]) * 100) for i in option["items"] if not i.get("owned_by_user")))

    def test_revise_changes_only_the_affected_category(self):
        option = plan_desktop(self.corpus.candidates, self.corpus.products, self.req, Chooser())
        board_socket = self.specs(option, "motherboard", "socket")
        other = next(r for r in self.corpus.candidates("cpu", limit=60)
                     if r["specs"].get("socket") not in (None, board_socket))
        broken = {**option, "items": [make_item(other) if i["category"] == "cpu" else i for i in option["items"]]}
        validation = validate_option(broken, self.req)
        self.assertIn("cpu_motherboard_socket", validation["failed_codes"])
        fixed, changes = revise(broken, validation["affected_categories"], validation, self.corpus.candidates, self.req, Chooser())
        self.assertEqual([c["category"] for c in changes], ["motherboard"])
        before = {i["category"]: i["offer_id"] for i in broken["items"]}
        after = {i["category"]: i["offer_id"] for i in fixed["items"]}
        self.assertEqual({c for c in before if before[c] != after[c]}, {"motherboard"})
        self.assertNotIn("cpu_motherboard_socket", validate_option(fixed, self.req)["failed_codes"])

    def test_revise_never_changes_locked_parts_and_excludes_tried_offers(self):
        option = plan_desktop(self.corpus.candidates, self.corpus.products, self.req, Chooser())
        for item in option["items"]:
            item["locked"] = item["category"] == "motherboard"
        validation = {"failed_codes": ["cpu_motherboard_socket"], "total_minor": 0}
        same, changes = revise(option, ["motherboard"], validation, self.corpus.candidates, self.req, Chooser())
        self.assertEqual(changes, [])
        old_psu = next(i for i in option["items"] if i["category"] == "psu")["offer_id"]
        new, _ = revise(option, ["psu"], {"failed_codes": ["psu_headroom"], "total_minor": 0},
                        self.corpus.candidates, self.req, Chooser())
        self.assertIn(old_psu, new["tried_offer_ids"]["psu"])
        self.assertNotEqual(next(i for i in new["items"] if i["category"] == "psu")["offer_id"], old_psu)

    def test_laptops_respect_budget_and_memory(self):
        req = {"device_type": "laptop", "budget": {"maximum_minor": 200000}, "workloads": ["programming"],
               "preferences": [], "hard_constraints": {"minimum_memory_gb": 16}}
        options = plan_laptops(self.corpus.candidates, req, Chooser(), 3)
        self.assertTrue(options)
        for option in options:
            laptop = option["items"][0]
            self.assertLessEqual(float(laptop["price"]), 2000)
            if laptop["specs"].get("ram_gb") is not None:
                self.assertGreaterEqual(laptop["specs"]["ram_gb"], 16)


if __name__ == "__main__":
    unittest.main()
