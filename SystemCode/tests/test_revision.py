import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import create_router
from backend.orchestrator import RecommendationEngine
from backend.retrieval import LocalCorpus
from backend.revision import carry_over, requirement_changes, reuse_decision
from backend.settings import Settings
from backend.store import StateStore

DATA = Path(__file__).resolve().parents[1] / "data"


def req(budget, **kw):
    return {"device_type": "desktop", "budget": {"maximum_minor": budget * 100}, "workloads": ["gaming"],
            "preferences": [], "hard_constraints": {}, "locked_product_ids": [], "owned_components": [], **kw}


class DecisionTests(unittest.TestCase):
    def test_changes_are_listed_per_field(self):
        changes = requirement_changes(req(3500), req(3000, hard_constraints={"minimum_memory_gb": 32}))
        self.assertEqual({c["field"] for c in changes}, {"budget", "hard_constraints"})

    def test_reuse_rules(self):
        self.assertTrue(reuse_decision(req(3500), req(3000), requirement_changes(req(3500), req(3000)))[0])
        self.assertFalse(reuse_decision(req(2000), req(3000), requirement_changes(req(2000), req(3000)))[0])
        laptop = {**req(2000), "device_type": "laptop"}
        self.assertFalse(reuse_decision(req(2000), laptop, requirement_changes(req(2000), laptop))[0])

    def test_carry_over_inserts_new_locked_part_and_owned_part(self):
        corpus = LocalCorpus(DATA)
        gpu_a, gpu_b = corpus.candidates("gpu", limit=2)
        cpu = corpus.candidates("cpu", limit=1)[0]
        base = [{"option_id": "o1", "device_type": "desktop", "items": [
            {"category": "gpu", "offer_id": gpu_a["id"], "product_id": str(gpu_a["product_id"]), "name": gpu_a["name"], "price": gpu_a["price"]},
            {"category": "cpu", "offer_id": cpu["id"], "product_id": str(cpu["product_id"]), "name": cpu["name"], "price": cpu["price"]}],
            "validation": {}, "evidence": []}]
        locked = carry_over(base, req(3000, locked_product_ids=[str(gpu_b["product_id"])]), corpus.products)[0]
        gpu = next(i for i in locked["items"] if i["category"] == "gpu")
        self.assertEqual(gpu["offer_id"], gpu_b["id"])
        self.assertTrue(gpu["locked"])
        self.assertTrue(next(i for i in locked["items"] if i["category"] == "cpu")["carried_over"])
        self.assertNotIn("validation", locked)
        owned = carry_over(base, req(3000, owned_components=[{"mention": "RTX 4070", "category": "gpu", "specs": {"tdp_w": 200}}]),
                           corpus.products)[0]
        self.assertTrue(next(i for i in owned["items"] if i["category"] == "gpu")["owned_by_user"])


class FollowUpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        settings = Settings(data_dir=DATA, state_db=Path(cls.temp.name) / "s.db", model_name=None, model_provider=None,
                            embedding_provider="hash", embedding_model=None, embedding_dimensions=256,
                            retrieval_backend="local", pi_runtime_url=None)
        store, corpus = StateStore(settings.state_db), LocalCorpus(DATA)
        app = FastAPI()
        app.include_router(create_router(store, corpus, RecommendationEngine(store, corpus, settings)))
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def say(self, session, text, version):
        return self.client.post(f"/api/v1/sessions/{session}/messages", json={
            "client_message_id": text[:20] + str(version), "text": text, "expected_requirements_version": version}).json()

    def run_and_get(self, session, version, base=None):
        body = {"requirements_version": version, "maximum_options": 1}
        if base:
            body["base_run_id"] = base
        run = self.client.post(f"/api/v1/sessions/{session}/runs", json=body)
        self.assertEqual(run.status_code, 202, run.text)
        return run.json()["id"], self.client.get(f"/api/v1/runs/{run.json()['id']}/result").json()

    def test_lower_budget_keeps_unaffected_parts_and_other_constraints(self):
        session = self.client.post("/api/v1/sessions", json={}).json()["id"]
        first = self.say(session, "gaming desktop with 32GB RAM, budget S$3500", 0)
        run1, result1 = self.run_and_get(session, first["requirements_version"])
        self.assertEqual(result1["outcome"], "recommendations_available")
        second = self.say(session, "Actually my budget is S$3000", first["requirements_version"])
        self.assertEqual(second["requirements"]["hard_constraints"]["minimum_memory_gb"], 32)
        _, result2 = self.run_and_get(session, second["requirements_version"], base=run1)
        self.assertTrue(result2["follow_up"]["reused"])
        self.assertEqual([c["field"] for c in result2["follow_up"]["changes"]], ["budget"])
        option = result2["options"][0]
        self.assertLessEqual(option["validation"]["total_minor"], 300000)
        before = {i["offer_id"] for i in result1["options"][0]["items"]}
        after = {i["offer_id"] for i in option["items"]}
        self.assertGreater(len(before & after), len(after) // 2)          # most parts were kept
        self.assertTrue(option["follow_up"]["kept_parts"])
        ram = next(i for i in option["items"] if i["category"] == "ram")
        self.assertGreaterEqual(ram["specs"]["capacity_gb"], 32)          # untouched constraint still holds

    def test_base_run_from_another_session_is_rejected(self):
        a = self.client.post("/api/v1/sessions", json={}).json()["id"]
        b = self.client.post("/api/v1/sessions", json={}).json()["id"]
        msg_a = self.say(a, "gaming desktop, budget S$2500", 0)
        run_a, _ = self.run_and_get(a, msg_a["requirements_version"])
        msg_b = self.say(b, "gaming desktop, budget S$2500", 0)
        response = self.client.post(f"/api/v1/sessions/{b}/runs", json={
            "requirements_version": msg_b["requirements_version"], "base_run_id": run_a})
        self.assertEqual(response.status_code, 409)


if __name__ == "__main__":
    unittest.main()
