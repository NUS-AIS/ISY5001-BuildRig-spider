import unittest

from backend.explanation_guard import guard, guard_message, option_facts, preference_notes, preference_summary, unsupported


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

    def test_dedicated_graphics_claim_on_integrated_graphics_is_dropped(self):
        integrated = {"device_type": "laptop", "items": [{"category": "laptop", "name":
                      "MSI PRESTIGE 14 AI+D3MG-228SG (Ultra 7 355,LPDDR5x 32GB,1TB SSD,INTEL GPU, WIN 11 H)"}]}
        self.assertIsNotNone(unsupported("Includes a dedicated Intel GPU for video processing.", integrated))
        self.assertIsNotNone(unsupported("Higher power consumption due to the dedicated GPU.", integrated))
        self.assertIsNone(unsupported("No dedicated graphics card, which may limit GPU-based MATLAB tasks.", integrated))
        self.assertIsNone(unsupported("Integrated graphics will not match a dedicated GPU in demanding games.", integrated))
        gaming = {"device_type": "laptop", "items": [{"category": "laptop", "name": "MSI Cyborg 15 Max, Intel Core 7 240H,RTX5060-8GB"}]}
        self.assertIsNone(unsupported("Includes a dedicated RTX 5060 GPU.", gaming))
        no_card = option("AMD Ryzen 5 8500G", "MSI B850M")
        self.assertIsNotNone(unsupported("Features a dedicated graphics card for gaming.", no_card))
        self.assertIsNone(unsupported("Uses integrated graphics, removing the need for a dedicated GPU.", no_card))

    def test_processor_class_must_be_written_in_the_listing(self):
        core7 = {"device_type": "laptop", "items": [{"category": "laptop", "name": "MSI Cyborg 15 Max, Intel Core 7 240H,RTX5060-8GB,WIFI7"}]}
        self.assertIsNotNone(unsupported("Offers a powerful RTX 5060 GPU and Intel Core i7 processor.", core7))
        self.assertIsNone(unsupported("The Intel Core 7 240H handles games well.", core7))
        i7 = {"device_type": "laptop", "items": [{"category": "laptop", "name": "ASUS TUF GAMING FX608JPR (i7-14650HX /32GB/RTX5070-8GB)"}]}
        self.assertIsNone(unsupported("An Intel i7 processor and an RTX 5070.", i7))

    def test_quiet_is_only_confirmed_by_a_listing_that_says_so(self):
        opt = {"device_type": "desktop", "items": [{"category": "gpu", "name": "XFX Swift Radeon RX 9070XT Triple Fan"},
                                                    {"category": "cooler", "name": "Thermalright Peerless Vision 240 ARGB"}],
               "reasons": ["Triple fan cooling for enhanced performance and quiet operation.", "16GB of graphics memory."],
               "trade_offs": ["A 304W card may be louder than a smaller one."]}
        reasons, trade_offs, dropped = preference_notes(opt, ["quiet"])
        self.assertEqual(opt["reasons"], ["16GB of graphics memory."])
        self.assertEqual(opt["trade_offs"], ["A 304W card may be louder than a smaller one."])       # a caution is kept
        self.assertEqual((reasons, len(trade_offs), len(dropped)), ([], 1, 1))
        self.assertIn("could not be checked", trade_offs[0])
        kept, removed = preference_summary("It includes a quiet cooling solution. The total is S$2,449.", [opt], ["quiet"])
        self.assertEqual((kept, len(removed)), ("The total is S$2,449.", 1))
        silent = {"device_type": "desktop", "items": [{"category": "cooler", "name": "Cryorig H4 ARGB Silent 120mm PWM Fan Air CPU Cooler"}],
                  "reasons": ["A quiet cooler."], "trade_offs": []}
        reasons, trade_offs, dropped = preference_notes(silent, ["quiet"])
        self.assertEqual((len(reasons), trade_offs, dropped, silent["reasons"]), (1, [], [], ["A quiet cooler."]))

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
