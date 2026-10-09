import unittest

from backend.requirements_parser import RequirementExtraction, parse_requirements


class FakeModel:
    enabled = True

    def __init__(self, **fields):
        self.fields = fields

    def structured(self, system, payload, schema):
        return RequirementExtraction(**self.fields)


class BrokenModel:
    enabled = True

    def structured(self, *args):
        raise ConnectionError("ollama is down")


class Catalogue:
    prices = [
        {"id": "o1", "product_id": "p-4070", "name": "MSI GeForce RTX™ 4070 SUPER 12G VENTUS", "category": "gpu", "price": "899"},
        {"id": "o2", "product_id": "p-9800", "name": "AMD RYZEN 7 9800X3D TRAY PROCESSOR", "category": "cpu", "price": "749"},
        {"id": "o3", "product_id": "p-5070a", "name": "ASUS TUF RTX 5070 12GB", "category": "gpu", "price": "1099"},
        {"id": "o4", "product_id": "p-5070b", "name": "Gigabyte RTX 5070 WINDFORCE 12GB", "category": "gpu", "price": "999"},
    ]


def ids(questions):
    return [q["question_id"] for q in questions]


class RuleLayerTests(unittest.TestCase):
    def test_rules_only_desktop_with_budget(self):
        req, questions = parse_requirements("Build me a gaming PC under S$3,500", {})
        self.assertEqual(req["device_type"], "desktop")
        self.assertEqual(req["budget"]["maximum_minor"], 350000)
        self.assertIn("gaming", req["workloads"])
        self.assertEqual(questions, [])
        self.assertEqual(req["understanding_source"], "rules")

    def test_compare_route(self):
        req, _ = parse_requirements("Should I get a desktop or laptop for SolidWorks? Budget 2500 SGD", {})
        self.assertEqual(req["device_type"], "compare")
        req, _ = parse_requirements("预算3000新币，不知道买台式还是笔记本", {})
        self.assertEqual(req["device_type"], "compare")

    def test_ambiguous_request_asks_for_device_and_budget(self):
        _, questions = parse_requirements("I need a computer for university", {})
        self.assertEqual(ids(questions), ["q_device_type", "q_budget"])

    def test_gpu_vram_is_not_a_memory_requirement(self):
        req, _ = parse_requirements("desktop with an RTX 4060 8GB, budget S$1500", {})
        self.assertNotIn("minimum_memory_gb", req.get("hard_constraints", {}))

    def test_storage_requirement(self):
        req, _ = parse_requirements("laptop with 1TB SSD and 16GB RAM under $1800", {})
        self.assertEqual(req["hard_constraints"], {"minimum_memory_gb": 16, "minimum_storage_gb": 1024})

    def test_follow_up_keeps_previous_constraints(self):
        first, _ = parse_requirements("gaming desktop, 32GB RAM, budget S$3500", {})
        second, questions = parse_requirements("Actually make the budget S$3000", first)
        self.assertEqual(second["budget"]["maximum_minor"], 300000)
        self.assertEqual(second["device_type"], "desktop")
        self.assertEqual(second["hard_constraints"]["minimum_memory_gb"], 32)
        self.assertEqual(questions, [])

    def test_very_low_budget_is_questioned_not_refused(self):
        req, questions = parse_requirements("desktop for gaming, budget S$300", {})
        self.assertEqual(ids(questions), ["q_budget_low"])
        req, questions = parse_requirements("Keep my budget and show what is possible.", req)
        self.assertEqual(questions, [])


class LLMLayerTests(unittest.TestCase):
    def test_capacities_are_requirements_not_products(self):
        model = FakeModel(device_type="desktop", must_buy_components=["64GB RAM", "2TB SSD"])
        req, questions = parse_requirements("Deep learning desktop with 64GB RAM and a 2TB SSD, budget S$5,000", {},
                                            model, Catalogue())
        self.assertEqual(req["hard_constraints"], {"minimum_memory_gb": 64, "minimum_storage_gb": 2048})
        self.assertEqual(req["locked_product_ids"], [])
        self.assertEqual(questions, [])

    def test_owned_part_needs_an_ownership_statement(self):
        model = FakeModel(device_type="desktop", owned_components=["128GB RAM"])
        req, _ = parse_requirements("Desktop with 128GB RAM, budget S$1,000", {}, model)
        self.assertEqual(req["owned_components"], [])
        self.assertEqual(req["hard_constraints"]["minimum_memory_gb"], 128)

    def test_owned_part_without_model_number(self):
        model = FakeModel(owned_components=["2TB SSD"])
        current = {"device_type": "desktop", "budget": {"currency": "SGD", "maximum_minor": 250000, "is_hard_limit": True}}
        req, _ = parse_requirements("I already have a 2TB SSD, reuse it", current, model)
        self.assertEqual([(o["category"], o["specs"].get("capacity_gb")) for o in req["owned_components"]], [("ssd", 2048)])

    def test_pc_means_desktop(self):
        req, _ = parse_requirements("Office PC without a graphics card, budget S$900", {})
        self.assertEqual(req["device_type"], "desktop")

    def test_llm_fills_free_text_fields(self):
        model = FakeModel(device_type="laptop", workloads=["Python development"], preferences=["quiet", "light"])
        req, questions = parse_requirements("Something light and quiet for Python dev, S$1,600 max", {}, model)
        self.assertEqual(req["device_type"], "laptop")
        self.assertIn("Python development", req["workloads"])
        self.assertIn("quiet", req["preferences"])
        self.assertIn("light", req["preferences"])        # from the model
        self.assertIn("portable", req["preferences"])     # from the rule layer
        self.assertEqual(len(req["preferences"]), len(set(req["preferences"])))
        self.assertEqual(req["understanding_source"], "llm+rules")
        self.assertEqual(questions, [])

    def test_llm_cannot_invent_a_budget(self):
        model = FakeModel(device_type="desktop", budget_sgd=5000)
        req, questions = parse_requirements("a desktop for office work", {}, model)
        self.assertNotIn("budget", req)
        self.assertIn("q_budget", ids(questions))

    def test_budget_disagreement_is_asked(self):
        model = FakeModel(device_type="desktop", budget_sgd=2000)
        req, questions = parse_requirements("desktop, budget S$3000 (maybe 2000)", {}, model)
        self.assertEqual(req["budget"]["maximum_minor"], 300000)
        self.assertIn("q_budget_confirm", ids(questions))

    def test_unique_locked_component_is_resolved_against_catalogue(self):
        model = FakeModel(device_type="desktop", must_buy_components=["RTX 4070 Super"])
        req, questions = parse_requirements("desktop under S$3000, it must include an RTX 4070 Super", {}, model, Catalogue())
        self.assertEqual(req["locked_product_ids"], ["p-4070"])
        self.assertEqual(questions, [])

    def test_ambiguous_locked_component_triggers_question(self):
        model = FakeModel(device_type="desktop", must_buy_components=["RTX 5070"])
        req, questions = parse_requirements("desktop under S$3000 with an RTX 5070", {}, model, Catalogue())
        self.assertEqual(req["locked_product_ids"], [])
        self.assertEqual(ids(questions), ["q_locked_0"])
        self.assertEqual(len(questions[0]["options"]), 2)

    def test_owned_component_gets_category_and_specs(self):
        model = FakeModel(device_type="desktop", owned_components=["RTX 4070"])
        req, _ = parse_requirements("I already have an RTX 4070, build the rest for S$2000", {}, model, Catalogue())
        owned = req["owned_components"][0]
        self.assertEqual(owned["category"], "gpu")
        self.assertEqual(owned["specs"]["tdp_w"], 200)

    def test_keeping_a_previously_recommended_part_locks_it(self):
        recent = [{"name": "Gigabyte RTX 5070 WINDFORCE 12GB", "product_id": "p-5070b", "category": "gpu"}]
        # The model misreads "keep" as "already owned"; the previous recommendation corrects it.
        model = FakeModel(owned_components=["Gigabyte RTX 5070 WINDFORCE 12GB"])
        req, questions = parse_requirements("Keep the Gigabyte RTX 5070 WINDFORCE 12GB and lower the budget to S$2500",
                                            {"device_type": "desktop"}, model, Catalogue(), recent)
        self.assertEqual(req["locked_product_ids"], ["p-5070b"])
        self.assertEqual(req["owned_components"], [])
        self.assertEqual(questions, [])

    def test_generic_or_invented_component_is_not_locked(self):
        model = FakeModel(device_type="laptop", must_buy_components=["laptop", "RTX 5090"])
        req, questions = parse_requirements("I need a laptop under S$2000", {}, model, Catalogue())
        self.assertEqual(req["locked_product_ids"], [])
        self.assertNotIn("q_locked_0", [q["question_id"] for q in questions])

    def test_model_failure_falls_back_to_rules(self):
        req, questions = parse_requirements("gaming desktop under S$2000", {}, BrokenModel())
        self.assertEqual(req["device_type"], "desktop")
        self.assertTrue(req["understanding_source"].startswith("rules (model unavailable"))
        self.assertEqual(questions, [])


if __name__ == "__main__":
    unittest.main()
