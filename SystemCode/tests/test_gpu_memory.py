"""A minimum of graphics memory is a hard requirement: understood, planned for, validated and kept in follow-ups."""
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import NOTHING_CHANGED, create_router
from backend.orchestrator import RecommendationEngine
from backend.planning import Chooser, feasibility_floor, plan_desktop
from backend.requirements_parser import parse_requirements
from backend.retrieval import LocalCorpus
from backend.settings import Settings
from backend.store import StateStore
from backend.validation import validate_option

DATA = Path(__file__).resolve().parents[1] / "data"
CORPUS = LocalCorpus(DATA)


def constraints(text: str, current: dict | None = None) -> dict:
    return parse_requirements(text, current or {})[0].get("hard_constraints") or {}


def card(option: dict) -> dict | None:
    return next((i for i in option["items"] if i["category"] == "gpu"), None)


class UnderstandingTests(unittest.TestCase):
    def test_graphics_memory_is_not_system_memory(self):
        for text in ("5060 is not enough if I want to play 3A games. so the gpu memory has to been more than 12GB",
                     "But I have told you I want a 12GB memory GPU", "gaming desktop with 12GB VRAM",
                     "a graphics card with at least 12GB", "显存至少12GB的台式机"):
            self.assertEqual(constraints(text), {"minimum_gpu_memory_gb": 12}, text)

    def test_both_memories_in_one_message(self):
        self.assertEqual(constraints("gaming desktop, 32GB RAM and a GPU with 16GB VRAM, budget S$4,000"),
                         {"minimum_memory_gb": 32, "minimum_gpu_memory_gb": 16})
        self.assertEqual(constraints("desktop with a good GPU and 32GB RAM"), {"minimum_memory_gb": 32})
        self.assertEqual(constraints("GPU memory is not important, I need 64GB RAM"), {"minimum_memory_gb": 64})

    def test_follow_up_adds_the_requirement_and_keeps_the_rest(self):
        first, _ = parse_requirements("quiet gaming desktop with 32GB RAM, budget S$3,000", {})
        second, questions = parse_requirements("the gpu memory has to be more than 12GB", first)
        self.assertEqual(second["hard_constraints"], {"minimum_memory_gb": 32, "minimum_gpu_memory_gb": 12})
        self.assertEqual(second["budget"], first["budget"])
        self.assertEqual(questions, [])


class PlanningTests(unittest.TestCase):
    def test_planned_card_meets_the_minimum(self):
        req, _ = parse_requirements("gaming desktop with 32GB RAM and a GPU with at least 12GB, budget S$3,000", {})
        option = plan_desktop(CORPUS.candidates, CORPUS.products, req, Chooser())
        self.assertGreaterEqual(card(option)["specs"]["vram_gb"], 12)

    def test_everyday_desktop_gets_a_card_when_one_is_required(self):
        plain, _ = parse_requirements("office desktop, budget S$2,500", {})
        self.assertIsNone(card(plan_desktop(CORPUS.candidates, CORPUS.products, plain, Chooser())))
        req, _ = parse_requirements("office desktop with a graphics card with at least 12GB, budget S$2,500", {})
        self.assertGreaterEqual(card(plan_desktop(CORPUS.candidates, CORPUS.products, req, Chooser()))["specs"]["vram_gb"], 12)

    def test_floor_counts_the_card(self):
        plain, _ = parse_requirements("gaming desktop, budget S$3,000", {})
        req, _ = parse_requirements("gaming desktop with 12GB VRAM, budget S$3,000", {})
        floor = feasibility_floor(CORPUS, req)
        self.assertGreater(floor["floor_minor"], feasibility_floor(CORPUS, plain)["floor_minor"])
        self.assertIsNone(floor["graphics_card_floor_minor"])            # the floor already includes the card
        self.assertTrue(all(b["has_card"] for b in floor["desktop_builds"]))


class ValidationTests(unittest.TestCase):
    REQ = {"hard_constraints": {"minimum_gpu_memory_gb": 12}}

    def check(self, items: list[dict]) -> dict:
        result = validate_option({"device_type": "desktop", "items": items}, self.REQ)
        return next(c for c in result["checks"] if c["code"] == "minimum_gpu_memory")

    def gpu(self, vram) -> dict:
        return {"category": "gpu", "name": "card", "price": "500", "specs": {"vram_gb": vram}}

    def test_statuses(self):
        self.assertEqual(self.check([self.gpu(16)])["status"], "passed")
        small = self.check([self.gpu(8)])
        self.assertEqual((small["status"], small["affected_categories"]), ("failed", ["gpu"]))
        self.assertEqual(self.check([self.gpu(None)])["status"], "unknown")      # never assumed to be enough
        self.assertEqual(self.check([])["status"], "failed")                    # integrated graphics cannot meet it

    def test_rule_is_silent_without_the_requirement(self):
        result = validate_option({"device_type": "desktop", "items": [self.gpu(8)]}, {})
        self.assertNotIn("minimum_gpu_memory", [c["code"] for c in result["checks"]])


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        settings = Settings(data_dir=DATA, state_db=Path(self.temp.name) / "s.db", model_name=None, model_provider=None,
                            embedding_provider="hash", embedding_model=None, embedding_dimensions=256,
                            retrieval_backend="local", pi_runtime_url=None, internal_api_token="t")
        store = StateStore(settings.state_db)
        app = FastAPI()
        app.include_router(create_router(store, CORPUS, RecommendationEngine(store, CORPUS, settings)))
        self.client = TestClient(app)
        self.session = self.client.post("/api/v1/sessions", json={}).json()["id"]
        self.version = 0

    def say(self, text: str) -> dict:
        reply = self.client.post(f"/api/v1/sessions/{self.session}/messages", json={
            "client_message_id": f"m{self.version}", "text": text, "expected_requirements_version": self.version}).json()
        self.version = reply["requirements_version"]
        return reply

    def generate(self, base: str | None = None) -> tuple[str, dict]:
        body = {"requirements_version": self.version, **({"base_run_id": base} if base else {})}
        run = self.client.post(f"/api/v1/sessions/{self.session}/runs", json=body).json()["id"]
        return run, self.client.get(f"/api/v1/runs/{run}/result").json()

    def test_follow_up_replaces_the_card_within_the_budget(self):
        self.say("quiet gaming desktop with 32GB RAM, budget S$3,000")
        first, _ = self.generate()
        reply = self.say("5060 is not enough if I want to play 3A games. so the gpu memory has to been more than 12GB")
        self.assertTrue(reply["requirements_changed"])
        _, result = self.generate(base=first)
        self.assertEqual(result["outcome"], "recommendations_available")
        for option in result["options"]:
            self.assertGreaterEqual(card(option)["specs"]["vram_gb"], 12)
            self.assertLessEqual(option["validation"]["total_minor"], 300000)
            self.assertIn("minimum_gpu_memory", [c["code"] for c in option["validation"]["checks"] if c["status"] == "passed"])

    def test_message_that_changes_nothing_says_so(self):
        self.say("quiet gaming desktop with 32GB RAM, budget S$3,000")
        reply = self.say("the Gpu is still 5060?")
        self.assertFalse(reply["requirements_changed"])
        self.assertEqual(reply["assistant_message"], NOTHING_CHANGED)
        self.assertTrue(reply["can_generate"])


if __name__ == "__main__":
    unittest.main()
