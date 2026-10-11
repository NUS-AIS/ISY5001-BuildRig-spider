import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from backend.api import create_router
from backend.orchestrator import RecommendationEngine
from backend.requirements_parser import parse_requirements
from backend.retrieval import LocalCorpus
from backend.production_retrieval import DimensionCheckedEmbeddings
from backend.settings import Settings
from backend.store import StateStore
from fastapi import FastAPI
from langchain_core.embeddings import Embeddings


class StubEmbeddings(Embeddings):
    def embed_documents(self, texts):
        return [[0.0, 1.0] for _ in texts]

    def embed_query(self, text):
        return [0.0, 1.0]


class BackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(__file__).resolve().parents[1]
        cls.settings = Settings(
            data_dir=root / "data",
            state_db=Path(cls.temp.name) / "state.db",
            model_name=None,
            model_provider=None,
            embedding_provider="hash",
            embedding_model=None,
            embedding_dimensions=256,
        )
        cls.store = StateStore(cls.settings.state_db)
        cls.corpus = LocalCorpus(cls.settings.data_dir)
        cls.engine = RecommendationEngine(cls.store, cls.corpus, cls.settings)
        app = FastAPI()
        app.include_router(create_router(cls.store, cls.corpus, cls.engine))
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_parser_does_not_treat_ram_as_budget(self):
        req, questions = parse_requirements("需要32GB内存的台式机", {})
        self.assertNotIn("budget", req)
        self.assertEqual(req["hard_constraints"]["minimum_memory_gb"], 32)
        self.assertEqual(questions[0]["question_id"], "q_budget")

    def test_parser_accepts_comma_budget_and_uses_english_questions(self):
        req, questions = parse_requirements("A laptop under S$1,800 for programming", {})
        self.assertEqual(req["budget"]["maximum_minor"], 180000)
        self.assertEqual(req["device_type"], "laptop")
        self.assertEqual(questions, [])
        _, missing = parse_requirements("I need a computer for university", {})
        self.assertTrue(all(question["text"].isascii() for question in missing))

    def test_session_to_recommendation(self):
        session = self.client.post("/api/v1/sessions", json={"long_term_memory": True}).json()
        response = self.client.post(f"/api/v1/sessions/{session['id']}/messages", json={
            "client_message_id": "client-1", "expected_requirements_version": 0,
            "text": "我要一台预算2000新币的笔记本，主要用于MATLAB"
        })
        self.assertEqual(response.status_code, 200)
        parsed = response.json()
        self.assertTrue(parsed["can_generate"])
        run_response = self.client.post(f"/api/v1/sessions/{session['id']}/runs", json={
            "requirements_version": parsed["requirements_version"], "orchestration_mode": "dag", "maximum_options": 2
        })
        self.assertEqual(run_response.status_code, 202)
        run_id = run_response.json()["id"]
        result_response = self.client.get(f"/api/v1/runs/{run_id}/result")
        self.assertEqual(result_response.status_code, 200)
        result = result_response.json()
        self.assertEqual(result["outcome"], "recommendations_available")
        self.assertLessEqual(result["options"][0]["validation"]["total_minor"], 200000)
        self.assertIn("evidence", result["options"][0])
        self.assertEqual(result["plan"]["branches"], ["laptop_selector"])
        self.assertNotIn("desktop_planner", result["plan"]["active_agents"])
        self.assertIn("evidence_agent", [x["agent_role"] for x in result["tool_calls"]])
        execution = self.client.get(f"/api/v1/runs/{run_id}/execution").json()
        self.assertGreaterEqual(execution["operation_state"]["version"], 3)
        self.assertTrue(all(x["status"] == "completed" for x in execution["tool_calls"]))

    def test_session_owner_isolation(self):
        session = self.client.post("/api/v1/sessions", headers={"X-User-ID": "alice"}, json={}).json()
        self.assertEqual(self.client.get(f"/api/v1/sessions/{session['id']}", headers={"X-User-ID": "alice"}).status_code, 200)
        self.assertEqual(self.client.get(f"/api/v1/sessions/{session['id']}", headers={"X-User-ID": "bob"}).status_code, 404)

    def test_desktop_multi_agent_never_claims_unknown_compatibility_passed(self):
        session = self.client.post("/api/v1/sessions", json={}).json()
        parsed = self.client.post(f"/api/v1/sessions/{session['id']}/messages", json={
            "client_message_id": "desktop-message", "expected_requirements_version": 0,
            "text": "预算3000新币的组装台式机，用于游戏"
        }).json()
        run = self.client.post(f"/api/v1/sessions/{session['id']}/runs", json={
            "requirements_version": parsed["requirements_version"], "orchestration_mode": "dag", "maximum_options": 1
        }).json()
        result = self.client.get(f"/api/v1/runs/{run['id']}/result").json()
        for option in result["options"]:
            for check in option["validation"]["checks"]:
                if check["status"] == "passed" and check["inputs"]:
                    # A compatibility rule may only pass when every spec it compared is known.
                    self.assertNotIn(None, check["inputs"].values(), check["code"])
        self.assertIn("desktop_planner", result["plan"]["active_agents"])

    def test_memory_opt_in_and_version_check(self):
        session = self.client.post("/api/v1/sessions", json={"long_term_memory": True}).json()
        created = self.client.post(f"/api/v1/sessions/{session['id']}/memories",
                                   params={"kind": "preference", "value": "quiet", "confirmed": True})
        self.assertEqual(created.status_code, 201)
        memory = created.json()
        conflict = self.client.patch(f"/api/v1/memories/{memory['id']}", json={
            "expected_version": 2, "value": "very quiet", "confirmed": True})
        self.assertEqual(conflict.status_code, 409)

    def test_memory_follows_the_person_into_a_new_conversation_and_reaches_the_run(self):
        user = {"X-User-ID": "memory-owner"}
        first = self.client.post("/api/v1/sessions", json={"long_term_memory": True}, headers=user).json()["id"]
        for _ in range(2):          # saving the same thing twice keeps one
            self.client.post(f"/api/v1/sessions/{first}/memories", headers=user,
                             params={"kind": "preference", "value": "Prefer quiet systems", "confirmed": True})
        self.client.post(f"/api/v1/sessions/{first}/memories", headers=user,
                         params={"kind": "workload", "value": "video editing", "confirmed": True})
        second = self.client.post("/api/v1/sessions", json={"long_term_memory": True}, headers=user).json()["id"]
        listed = self.client.get(f"/api/v1/sessions/{second}/memories", headers=user).json()["items"]
        self.assertEqual(sorted(m["value"] for m in listed), ["Prefer quiet systems", "video editing"])
        other = self.client.post("/api/v1/sessions", json={"long_term_memory": True}, headers={"X-User-ID": "someone-else"}).json()["id"]
        self.assertEqual(self.client.get(f"/api/v1/sessions/{other}/memories", headers={"X-User-ID": "someone-else"}).json()["items"], [])
        parsed = self.client.post(f"/api/v1/sessions/{second}/messages", headers=user, json={
            "client_message_id": "m", "expected_requirements_version": 0, "text": "Gaming desktop, budget S$2,500"}).json()
        run = self.client.post(f"/api/v1/sessions/{second}/runs", headers=user,
                               json={"requirements_version": parsed["requirements_version"]}).json()["id"]
        result = self.client.get(f"/api/v1/runs/{run}/result", headers=user).json()
        # The preference is applied; the saved workload is not, because this request names its own.
        self.assertEqual(result["memory_context"]["applied"], ["quiet"])
        self.assertIn("Applied from your saved memory: quiet.", result["assistant_message"])
        self.assertTrue(any("You asked for quiet" in t for o in result["options"] for t in o["trade_offs"]))

    def test_run_creation_is_idempotent(self):
        session = self.client.post("/api/v1/sessions", json={}).json()
        parsed = self.client.post(f"/api/v1/sessions/{session['id']}/messages", json={
            "client_message_id": "idempotent-message", "expected_requirements_version": 0,
            "text": "预算1500新币的笔记本"
        }).json()
        headers = {"Idempotency-Key": "same-request"}
        body = {"requirements_version": parsed["requirements_version"], "orchestration_mode": "dag", "maximum_options": 1}
        first = self.client.post(f"/api/v1/sessions/{session['id']}/runs", headers=headers, json=body).json()
        second = self.client.post(f"/api/v1/sessions/{session['id']}/runs", headers=headers, json=body).json()
        self.assertEqual(first["id"], second["id"])

    def test_embedding_dimension_mismatch_fails_before_milvus_write(self):
        embeddings = DimensionCheckedEmbeddings(StubEmbeddings(), expected_dimensions=3)
        with self.assertRaisesRegex(RuntimeError, "expected 3, got 2"):
            embeddings.embed_documents(["sample"])


if __name__ == "__main__":
    unittest.main()
