import unittest

from backend.explanation_guard import guard, guard_message, option_facts, unsupported


def option(*names, device="desktop"):
    categories = {"Ryzen": "cpu", "RX": "gpu", "RTX": "gpu", "B850": "motherboard"}
    items = [{"name": n, "category": next((c for k, c in categories.items() if k in n), "case")} for n in names]
    return {"device_type": device, "items": items}


class ExplanationGuardTests(unittest.TestCase):
    def test_sentence_naming_a_model_outside_the_option_is_dropped(self):
        opt = option("AMD Ryzen™ 5 8500G", "XFX Swift AMD Radeon RX 9070XT")
        self.assertIsNotNone(unsupported("The RTX 4090 gives the best frame rates.", opt))
        self.assertIsNone(unsupported("The RX 9070 XT handles 1440p gaming well.", opt))
        self.assertIsNone(unsupported("The Ryzen 5 8500G keeps power draw low.", opt))

    def test_integrated_graphics_claim_contradicting_a_graphics_card_is_dropped(self):
        opt = option("AMD Ryzen 5 8500G", "XFX Swift AMD Radeon RX 9070XT")
        claim = "The CPU has integrated graphics, reducing the need for a dedicated GPU."
        self.assertIsNotNone(unsupported(claim, opt))
        no_gpu = option("AMD Ryzen 5 8500G", "MSI B850M")
        self.assertIsNone(unsupported(claim, no_gpu))

    def test_guard_filters_in_place_and_reports(self):
        opt = {**option("AMD Ryzen 5 8500G", "XFX Swift AMD Radeon RX 9070XT"),
               "reasons": ["Strong 1440p performance from the RX 9070 XT.", "Beats an RTX 5080 for the price."]}
        dropped = guard(opt, "reasons")
        self.assertEqual(opt["reasons"], ["Strong 1440p performance from the RX 9070 XT."])
        self.assertEqual(len(dropped), 1)

    def test_facts(self):
        facts = option_facts(option("AMD Ryzen 5 8500G", "MSI B850M"))
        self.assertFalse(facts["has_graphics_card"])
        self.assertTrue(facts["uses_integrated_graphics_only"])


class SummaryGuardTests(unittest.TestCase):
    def test_false_over_budget_and_all_passed_claims_are_removed(self):
        opt = {**option("AMD Ryzen 7 9800X3D", "MSI RTX 5060"),
               "validation": {"failed_codes": [], "checks": [{"code": "gpu_case_clearance", "status": "unknown"}]}}
        message = ("Option 1 offers an RTX 5060 but is slightly over the budget. All checks passed. "
                   "Some fit checks need confirmation.")
        kept, dropped = guard_message(message, [opt])
        self.assertEqual(kept, "Some fit checks need confirmation.")
        self.assertEqual(len(dropped), 2)

    def test_true_over_budget_statement_is_kept(self):
        opt = {**option("AMD Ryzen 7 9800X3D"), "validation": {"failed_codes": ["budget_limit"], "checks": []}}
        kept, dropped = guard_message("This option is over the budget.", [opt])
        self.assertEqual(dropped, [])


if __name__ == "__main__":
    unittest.main()
