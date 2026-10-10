"""Conversation state a page can come back to, one run per session, and budgets that are asked about, not guessed."""
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import create_router
from backend.orchestrator import RecommendationEngine
from backend.requirements_parser import RequirementExtraction, ambiguous_amounts, parse_requirements
from backend.retrieval import LocalCorpus
from backend.settings import Settings
from backend.store import StateStore

DATA = Path(__file__).resolve().parents[1] / "data"
TOKEN = {"X-Internal-Token": "secret-token"}


def ids(questions):
    return [q["question_id"] for q in questions]


class BudgetSlipTests(unittest.TestCase):
    def test_only_broken_grouping_is_ambiguous(self):
        self.assertEqual(ambiguous_amounts("budget S$3,00"), {300: 3000})
        self.assertEqual(ambiguous_amounts("budget S$12,50 please"), {1250: 12500})
        for text in ("budget S$3,000", "S$1,070.", "budget 2000, 16GB RAM", "RTX 4070,16GB RAM", "S$12,500 or S$300"):
            self.assertEqual(ambiguous_amounts(text), {}, text)

    def test_slip_is_asked_about_and_no_budget_is_assumed(self):
        req, questions = parse_requirements("Gaming desktop with 32GB RAM, budget S$3,00. Prefer the white one", {})
        self.assertEqual(ids(questions), ["q_budget_confirm"])          # not also "what is your budget?"
        self.assertNotIn("budget", req)
        self.assertEqual([o["label"] for o in questions[0]["options"]], ["S$300", "S$3,000"])
        req, questions = parse_requirements(questions[0]["options"][1]["value"], req)
        self.assertEqual(req["budget"]["maximum_minor"], 300000)
        self.assertEqual(questions, [])

    def test_slip_does_not_replace_an_earlier_budget(self):
        first, _ = parse_requirements("Gaming desktop, budget S$2,500", {})
        second, questions = parse_requirements("Actually make it S$3,00", first)
        self.assertEqual(second["budget"]["maximum_minor"], 250000)
        self.assertEqual(ids(questions), ["q_budget_confirm"])

    def test_model_reading_the_slip_as_a_budget_is_not_accepted(self):
        class Model:
            enabled = True

            def structured(self, system, payload, schema):
                return RequirementExtraction(device_type="desktop", budget_sgd=300)

        req, questions = parse_requirements("Gaming desktop, budget S$3,00", {}, Model())
        self.assertNotIn("budget", req)
        self.assertEqual(ids(questions), ["q_budget_confirm"])


class SessionStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        settings = Settings(data_dir=DATA, state_db=Path(self.temp.name) / "s.db", model_name=None, model_provider=None,
                            embedding_provider="hash", embedding_model=None, embedding_dimensions=256,
                            retrieval_backend="local", pi_runtime_url=None, internal_api_token="secret-token")
        self.store, self.corpus = StateStore(settings.state_db), CORPUS
        app = FastAPI()
        app.include_router(create_router(self.store, self.corpus, RecommendationEngine(self.store, self.corpus, settings)))
        self.client = TestClient(app)

    def say(self, session: str, version: int, text: str) -> dict:
        return self.client.post(f"/api/v1/sessions/{session}/messages", json={
            "client_message_id": f"m{version}", "text": text, "expected_requirements_version": version}).json()

    def new_session(self) -> str:
        return self.client.post("/api/v1/sessions", json={}).json()["id"]

    def transcript(self, session: str) -> dict:
        return self.client.get(f"/api/v1/sessions/{session}/transcript").json()

    def test_transcript_of_a_new_session_is_empty(self):
        saved = self.transcript(self.new_session())
        self.assertEqual((saved["messages"], saved["can_generate"], saved["latest_run"], saved["active_run"]), ([], False, None, None))

    def test_transcript_restores_the_conversation_and_the_open_question(self):
        session = self.new_session()
        reply = self.say(session, 0, "Office desktop, budget S$300")
        saved = self.transcript(session)
        self.assertEqual([m["role"] for m in saved["messages"]], ["user", "assistant"])
        self.assertEqual(saved["messages"][1]["text"], reply["assistant_message"])
        self.assertFalse(saved["can_generate"])
        self.assertEqual(saved["reply_options"], reply["reply_options"])

    def test_transcript_includes_the_last_result(self):
        session = self.new_session()
        reply = self.say(session, 0, "Office desktop, budget S$1,500")
        run = self.client.post(f"/api/v1/sessions/{session}/runs", json={"requirements_version": reply["requirements_version"]}).json()
        saved = self.transcript(session)
        self.assertTrue(saved["can_generate"])
        self.assertEqual(saved["latest_run"], {"id": run["id"], "requirements_version": reply["requirements_version"]})
        self.assertIsNone(saved["active_run"])
        self.assertEqual([m["role"] for m in saved["messages"]], ["user", "assistant", "assistant"])
        self.assertEqual(saved["messages"][-1]["run_id"], run["id"])

    def test_transcript_is_private_to_its_owner(self):
        session = self.client.post("/api/v1/sessions", json={}, headers={"X-User-ID": "alice"}).json()["id"]
        self.assertEqual(self.client.get(f"/api/v1/sessions/{session}/transcript", headers={"X-User-ID": "bob"}).status_code, 404)

    def test_second_request_joins_the_run_in_progress(self):
        session = self.new_session()
        reply = self.say(session, 0, "Office desktop, budget S$1,500")
        active = self.store.create_run(session, reply["requirements_version"], self.corpus.snapshot_id, "dag")   # queued, not executed
        self.assertEqual(self.transcript(session)["active_run"]["id"], active["id"])
        joined = self.client.post(f"/api/v1/sessions/{session}/runs", json={"requirements_version": reply["requirements_version"]})
        self.assertEqual(joined.status_code, 202)
        self.assertEqual(joined.json()["id"], active["id"])
        self.assertEqual(len(self.store.session_runs(session)), 1)
        self.assertEqual(self.store.run(active["id"])["status"], "queued")      # joining did not start it a second time

    def test_runs_cut_off_by_a_restart_are_closed(self):
        session = self.new_session()
        reply = self.say(session, 0, "Office desktop, budget S$1,500")
        run = self.store.create_run(session, reply["requirements_version"], self.corpus.snapshot_id, "dag")
        self.store.reconcile_interrupted_runs()
        closed = self.store.run(run["id"])
        self.assertEqual((closed["status"], closed["error"]["code"]), ("failed", "RUN_INTERRUPTED"))
        self.assertIsNone(self.store.active_run(session))

    def test_progress_reports_update_only_a_run_in_progress(self):
        session = self.new_session()
        reply = self.say(session, 0, "Office desktop, budget S$1,500")
        run = self.store.create_run(session, reply["requirements_version"], self.corpus.snapshot_id, "pi")
        url = f"/api/v1/internal/runs/{run['id']}/progress"
        self.assertEqual(self.client.post(url, json={"stage": "pi_validating_the_build"}).status_code, 403)
        self.assertEqual(self.client.post(url, json={"stage": "Not A Stage!"}, headers=TOKEN).status_code, 422)
        self.assertEqual(self.client.post(url, json={"stage": "pi_validating_the_build", "message": "checking"}, headers=TOKEN).status_code, 204)
        self.assertEqual(self.store.run(run["id"])["stage"], "pi_validating_the_build")
        self.store.set_run(run["id"], "completed", "completed", result={"outcome": "no_feasible_option"})
        self.client.post(url, json={"stage": "pi_searching_gpu"}, headers=TOKEN)      # a late report must not reopen it
        finished = self.store.run(run["id"])
        self.assertEqual((finished["status"], finished["stage"]), ("completed", "completed"))


CORPUS = LocalCorpus(DATA)

if __name__ == "__main__":
    unittest.main()
