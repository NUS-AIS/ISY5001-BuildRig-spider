"""A budget below the cheapest compatible build is answered at once, with a floor priced from the snapshot."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import create_router
from backend.harness import ToolHarness
from backend.orchestrator import RecommendationEngine
from backend.planning import cheapest_desktop_option, feasibility_floor
from backend.requirements_parser import parse_requirements
from backend.retrieval import LocalCorpus
from backend.settings import Settings
from backend.store import StateStore
from backend.validation import validate_option

DATA = Path(__file__).resolve().parents[1] / "data"


def ids(questions):
    return [q["question_id"] for q in questions]


class FloorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = LocalCorpus(DATA)

    def floor(self, text: str) -> dict:
        return feasibility_floor(self.corpus, parse_requirements(text, {})[0])

    def test_cheapest_builds_are_complete_and_pass_validation(self):
        req, _ = parse_requirements("Gaming desktop, budget S$9,000", {})
        floor = feasibility_floor(self.corpus, req)
        self.assertEqual(floor["floor_minor"], min(b["total_minor"] for b in floor["desktop_builds"]))
        for build in floor["desktop_builds"]:
            option = {"device_type": "desktop", "items": build["items"]}
            validation = validate_option(option, req)
            self.assertEqual(validation["failed_codes"], [])
            self.assertEqual(validation["total_minor"], build["total_minor"])
            self.assertEqual({"cpu", "motherboard", "ram", "psu", "case", "ssd", "cooler"} - {i["category"] for i in build["items"]}, set())

    def test_hard_requirements_raise_the_floor(self):
        base = self.floor("Office desktop, budget S$5,000")["floor_minor"]
        self.assertGreater(self.floor("Office desktop with 32GB RAM, budget S$5,000")["floor_minor"], base)
        self.assertGreater(self.floor("Office desktop with a 2TB SSD, budget S$5,000")["floor_minor"], base)

    def test_graphics_card_threshold_only_for_workloads_that_want_one(self):
        self.assertIsNone(self.floor("Office desktop, budget S$5,000")["graphics_card_floor_minor"])
        gaming = self.floor("Gaming desktop, budget S$5,000")
        self.assertGreater(gaming["graphics_card_floor_minor"], gaming["floor_minor"])

    def test_laptop_floor_respects_a_memory_minimum(self):
        self.assertGreater(self.floor("Laptop with 64GB RAM under S$800")["floor_minor"],
                           self.floor("Laptop under S$800")["floor_minor"])

    def test_cheapest_option_prefers_a_card_when_the_workload_wants_one_and_it_fits(self):
        req, _ = parse_requirements("Gaming desktop, budget S$9,000", {})
        self.assertFalse(cheapest_desktop_option(self.corpus, req)["integrated_graphics_build"])
        req["budget"]["maximum_minor"] = feasibility_floor(self.corpus, req)["floor_minor"]
        tight = cheapest_desktop_option(self.corpus, req)
        self.assertTrue(tight["integrated_graphics_build"])
        self.assertTrue(tight["planning_notes"])           # the user is told why there is no card
        office, _ = parse_requirements("Office desktop, budget S$9,000", {})
        self.assertTrue(cheapest_desktop_option(self.corpus, office)["integrated_graphics_build"])

    def test_question_states_the_priced_floor_and_offers_budgets_that_work(self):
        req, questions = parse_requirements("Gaming desktop, budget S$300", {}, catalogue=self.corpus)
        self.assertEqual(ids(questions), ["q_budget_low"])
        floor = feasibility_floor(self.corpus, req)
        question = questions[0]
        self.assertIn(f"S${floor['floor_minor'] / 100:,.0f}", question["text"])
        self.assertNotIn("Keep my budget", [o["label"] for o in question["options"]])
        self.assertEqual(len(question["options"]), 2)       # the floor, and a build with a graphics card
        for option in question["options"]:
            raised, again = parse_requirements(option["value"], req, catalogue=self.corpus)
            self.assertGreaterEqual(raised["budget"]["maximum_minor"], floor["floor_minor"])
            self.assertEqual(again, [])

    def test_budget_above_the_old_fixed_figure_but_below_the_floor_is_questioned(self):
        _, questions = parse_requirements("Office desktop, budget S$700", {}, catalogue=self.corpus)
        self.assertEqual(ids(questions), ["q_budget_low"])
        _, questions = parse_requirements("Desktop with 128GB RAM, budget S$1,000", {}, catalogue=self.corpus)
        self.assertEqual(ids(questions), ["q_budget_low"])

    def test_acknowledgement_applies_to_one_budget_only(self):
        req, _ = parse_requirements("Gaming desktop, budget S$300", {}, catalogue=self.corpus)
        req, questions = parse_requirements("Keep my budget and show what is possible.", req, catalogue=self.corpus)
        self.assertEqual(questions, [])
        _, questions = parse_requirements("Make the budget S$400", req, catalogue=self.corpus)
        self.assertEqual(ids(questions), ["q_budget_low"])


class RunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        settings = Settings(data_dir=DATA, state_db=Path(cls.temp.name) / "s.db", model_name=None, model_provider=None,
                            embedding_provider="hash", embedding_model=None, embedding_dimensions=256,
                            retrieval_backend="local", pi_runtime_url=None, internal_api_token="t")
        store, cls.corpus = StateStore(settings.state_db), LocalCorpus(DATA)
        app = FastAPI()
        app.include_router(create_router(store, cls.corpus, RecommendationEngine(store, cls.corpus, settings)))
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def converse(self, *turns: str) -> tuple[str, dict]:
        session = self.client.post("/api/v1/sessions", json={}).json()["id"]
        reply = {"requirements_version": 0}
        for n, text in enumerate(turns):
            reply = self.client.post(f"/api/v1/sessions/{session}/messages", json={
                "client_message_id": f"m{n}", "text": text, "expected_requirements_version": reply["requirements_version"]}).json()
        return session, reply

    def run_result(self, session: str, version: int, mode: str = "dag") -> dict:
        run = self.client.post(f"/api/v1/sessions/{session}/runs",
                               json={"requirements_version": version, "orchestration_mode": mode})
        self.assertEqual(run.status_code, 202)
        return self.client.get(f"/api/v1/runs/{run.json()['id']}/result").json()

    def test_run_is_refused_while_the_budget_question_is_open(self):
        session, reply = self.converse("Office desktop, budget S$700")
        self.assertFalse(reply["can_generate"])
        run = self.client.post(f"/api/v1/sessions/{session}/runs", json={"requirements_version": reply["requirements_version"]})
        self.assertEqual(run.status_code, 409)

    def test_every_offered_budget_produces_a_recommendation(self):
        for request in ("Office desktop, budget S$300", "Gaming desktop, budget S$300",
                        "Gaming desktop with 32GB RAM, budget S$300"):
            _, reply = self.converse(request)
            for option in reply["reply_options"]:
                with self.subTest(request=request, raise_to=option["label"]):
                    session, raised = self.converse(request, option["value"])
                    self.assertTrue(raised["can_generate"])
                    result = self.run_result(session, raised["requirements_version"])
                    self.assertEqual(result["outcome"], "recommendations_available")
                    budget = raised["requirements"]["budget"]["maximum_minor"]
                    self.assertTrue(all(o["validation"]["total_minor"] <= budget for o in result["options"]))
                    if "graphics card" in option["label"]:
                        self.assertTrue(any(i["category"] == "gpu" for o in result["options"] for i in o["items"]))

    def test_run_survives_replanning_using_up_its_tool_allowance(self):
        _, reply = self.converse("Gaming desktop with 32GB RAM, budget S$300")
        session, raised = self.converse("Gaming desktop with 32GB RAM, budget S$300", reply["reply_options"][0]["value"])

        def tight(store, tools, max_calls):          # enough to plan and review once, not to replan
            return ToolHarness(store, tools, max_calls=60)

        with patch("backend.orchestrator.ToolHarness", tight):
            result = self.run_result(session, raised["requirements_version"])
        self.assertEqual(result["outcome"], "recommendations_available")
        self.assertTrue(any("tool allowance" in note for note in result["limitations"]))
        self.assertIn("cheapest compatible build", str(result["options"][0]["revision_history"][-1]["reason"]))

    def test_insisting_on_an_impossible_budget_is_answered_without_planning(self):
        session, reply = self.converse("Gaming desktop, budget S$300", "Keep my budget and show what is possible.")
        self.assertTrue(reply["can_generate"])
        for mode in ("dag", "pi"):                       # no Pi runtime is configured: the check runs first
            result = self.run_result(session, reply["requirements_version"], mode)
            self.assertEqual(result["outcome"], "no_feasible_option")
            self.assertEqual(result["tool_calls"], [])
            self.assertEqual(result["feasibility"]["budget_minor"], 30000)
            self.assertIn(f"S${result['feasibility']['floor_minor'] / 100:,.0f}", result["assistant_message"])


if __name__ == "__main__":
    unittest.main()
