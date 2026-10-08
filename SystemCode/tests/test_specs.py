import unittest

from backend.ingest.specs import (LLMSpecExtractor, bundle_suspect, evidence_supports, extract_offer,
                                  link_possible_duplicates)


def offer(category, name, description="", variant="Default Title", **extra):
    return {"id": extra.pop("id", name[:12]), "product_id": "p", "category": category, "name": name,
            "variant": variant, "description": description, "store": extra.pop("store", "Shop A"), **extra}


def spec(record, field):
    value = record["specs"][field]
    return value["value"] if value else None


class RegexAndRuleTests(unittest.TestCase):
    def test_explicit_socket_in_title(self):
        rec = extract_offer(offer("cpu", "AMD Ryzen 9 9950X3D (16-Cores, Socket AM5, 170W)"))
        self.assertEqual(spec(rec, "socket"), "AM5")
        self.assertEqual(rec["specs"]["socket"]["method"], "regex")
        self.assertEqual(spec(rec, "tdp_w"), 170)

    def test_socket_inferred_from_cpu_generation(self):
        rec = extract_offer(offer("cpu", "AMD RYZEN 7 9800X3D TRAY PROCESSOR"))
        self.assertEqual(spec(rec, "socket"), "AM5")
        self.assertEqual(rec["specs"]["socket"]["method"], "rule_inferred")
        intel = extract_offer(offer("cpu", "Intel CORE i7-14700KF 14th Gen"))
        self.assertEqual(spec(intel, "socket"), "LGA1700")

    def test_motherboard_socket_and_memory_from_chipset(self):
        rec = extract_offer(offer("motherboard", "ASUS ROG STRIX B850-F GAMING WIFI"))
        self.assertEqual(spec(rec, "socket"), "AM5")
        self.assertEqual(spec(rec, "memory_type"), "DDR5")
        self.assertEqual(rec["specs"]["memory_type"]["method"], "rule_inferred")

    def test_lga1700_board_memory_is_not_guessed(self):
        rec = extract_offer(offer("motherboard", "MSI PRO B760M-A WIFI"))
        self.assertEqual(spec(rec, "socket"), "LGA1700")
        self.assertIsNone(spec(rec, "memory_type"))
        self.assertIn("memory_type", rec["flags"]["missing"])

    def test_variant_disambiguates_bundled_title(self):
        rec = extract_offer(offer("ram", "G.SKILL Trident Z5: 32GB/48GB/64GB DDR5-6000", variant="64GB (2x32GB)"))
        self.assertEqual(spec(rec, "capacity_gb"), 64)
        self.assertEqual(spec(rec, "memory_type"), "DDR5")

    def test_bundled_title_without_variant_is_conflict_not_guess(self):
        rec = extract_offer(offer("ram", "G.SKILL Trident Z5: 32GB/48GB/64GB DDR5-6000"))
        self.assertIsNone(spec(rec, "capacity_gb"))
        self.assertEqual(rec["flags"]["conflict"]["capacity_gb"], [32, 48, 64])

    def test_psu_rating_from_model_number(self):
        rec = extract_offer(offer("psu", "Corsair SF850L- 850W / SF1000L-1000W SFX", variant="SF1000L"))
        self.assertEqual(spec(rec, "wattage_w"), 1000)
        single = extract_offer(offer("psu", "Corsair RMe Series RM750e Fully Modular ATX 3.1"))
        self.assertEqual(spec(single, "wattage_w"), 750)

    def test_gpu_chip_with_trademark_symbol_and_power_table(self):
        rec = extract_offer(offer("gpu", "ASUS TUF Gaming GeForce RTX™ 5070 Ti 16GB GDDR7 OC"))
        self.assertEqual(spec(rec, "chip"), "RTX 5070 TI")
        self.assertEqual(spec(rec, "vram_gb"), 16)
        self.assertEqual(spec(rec, "tdp_w"), 300)
        self.assertEqual(rec["specs"]["tdp_w"]["confidence"], "medium")

    def test_case_clearance_and_form_factor(self):
        rec = extract_offer(offer("case", "Lian Li O11 Vision Compact ATX Case",
                                  "Supports E-ATX motherboards. GPU length up to 400mm."))
        self.assertEqual(spec(rec, "max_form_factor"), "E-ATX")
        self.assertEqual(spec(rec, "max_gpu_length_mm"), 400)

    def test_laptop_ram_ignores_kit_note_and_storage(self):
        rec = extract_offer(offer("laptop", "ASUS VIVOBOOK 14 (CORE 7 150U/ 16GB (8GX2)RAM/1TB SSD/14\"FHD)"))
        self.assertEqual(spec(rec, "ram_gb"), 16)
        self.assertEqual(spec(rec, "storage_gb"), 1024)

    def test_missing_values_are_flagged_not_invented(self):
        rec = extract_offer(offer("case", "Phanteks Evolv S2 Case Tempered Glass"))
        self.assertIsNone(spec(rec, "max_gpu_length_mm"))
        self.assertIn("max_gpu_length_mm", rec["flags"]["missing"])

    def test_title_description_socket_conflict(self):
        rec = extract_offer(offer("cpu", "Mystery CPU Socket AM4", "Socket AM5 desktop processor"))
        self.assertIsNone(spec(rec, "socket"))
        self.assertEqual(sorted(rec["flags"]["conflict"]["socket"]), ["AM4", "AM5"])

    def test_unsupported_category_is_skipped(self):
        self.assertIsNone(extract_offer(offer("cooler", "Thermalright Peerless Assassin 120")))


class FlagTests(unittest.TestCase):
    def test_bundle_suspect(self):
        self.assertTrue(bundle_suspect(offer("motherboard", "ASUS TUF X870E-PLUS Motherboard & AMD Ryzen 7 7800X3D Processor")))
        self.assertFalse(bundle_suspect(offer("motherboard", "MSI MPG B650 EDGE WIFI AM5 ATX MOTHERBOARD")))

    def test_cross_store_duplicates_are_flagged_not_merged(self):
        rows = [offer("ssd", "Crucial P3 1TB", id="a", sku="CT1000P3SSD8", store="Shop A"),
                offer("ssd", "Crucial P3 Plus 1TB NVMe", id="b", sku="ct1000p3ssd8", store="Shop B"),
                offer("ssd", "Samsung 990 1TB", id="c", sku="MZ-V9P1T0", store="Shop B")]
        records = [extract_offer(r) for r in rows]
        link_possible_duplicates(records, rows)
        self.assertEqual(records[0]["flags"]["possible_same_product"], ["b"])
        self.assertEqual(records[2]["flags"]["possible_same_product"], [])
        self.assertEqual(len(records), 3)


class FakeModel:
    """Stands in for the LLM so the evidence guard can be tested deterministically."""
    def __init__(self, answers):
        self.answers = answers
        self.calls = 0

    def structured(self, system, payload, schema):
        self.calls += 1
        return schema(answers=[{"field": f, **a} for f, a in self.answers.items()])


class LLMTierTests(unittest.TestCase):
    DESCRIPTION = "Compact chassis with excellent airflow. Maximum card size: 365 mm for add-in boards."

    def test_llm_value_with_verbatim_evidence_is_accepted(self):
        model = FakeModel({"max_gpu_length_mm": {"value": "365", "evidence_quote": "Maximum card size: 365 mm"},
                           "max_form_factor": {"value": None, "evidence_quote": None}})
        rec = extract_offer(offer("case", "Brand X Case", self.DESCRIPTION), LLMSpecExtractor(model))
        self.assertEqual(spec(rec, "max_gpu_length_mm"), 365)
        self.assertEqual(rec["specs"]["max_gpu_length_mm"]["method"], "llm")

    def test_llm_value_without_matching_evidence_is_rejected(self):
        model = FakeModel({"max_gpu_length_mm": {"value": "400", "evidence_quote": "supports 400mm GPUs"},
                           "max_form_factor": {"value": "ATX", "evidence_quote": None}})
        rec = extract_offer(offer("case", "Brand X Case", self.DESCRIPTION), LLMSpecExtractor(model))
        self.assertIsNone(spec(rec, "max_gpu_length_mm"))
        self.assertIsNone(spec(rec, "max_form_factor"))
        self.assertIn("max_gpu_length_mm", rec["flags"]["missing"])

    def test_quote_must_contain_the_value(self):
        self.assertFalse(evidence_supports("Maximum card size: 365 mm", 400, self.DESCRIPTION))
        self.assertTrue(evidence_supports("Maximum card size: 365 mm", 365, self.DESCRIPTION))

    def test_quote_must_be_about_the_field(self):
        text = "Support up to 240mm radiator on top. Graphics card clearance 330mm."
        self.assertFalse(evidence_supports("Support up to 240mm radiator", 240, text, "max_gpu_length_mm"))
        self.assertTrue(evidence_supports("Graphics card clearance 330mm", 330, text, "max_gpu_length_mm"))

    def test_model_failure_leaves_fields_missing(self):
        class Broken:
            def structured(self, *args):
                raise ConnectionError("ollama down")
        rec = extract_offer(offer("case", "Brand X Case", self.DESCRIPTION), LLMSpecExtractor(Broken()))
        self.assertIn("max_gpu_length_mm", rec["flags"]["missing"])

    def test_answers_are_cached(self):
        model = FakeModel({"max_gpu_length_mm": {"value": "365", "evidence_quote": "Maximum card size: 365 mm"},
                           "max_form_factor": {"value": None, "evidence_quote": None}})
        llm = LLMSpecExtractor(model)
        extract_offer(offer("case", "Brand X Case", self.DESCRIPTION), llm)
        extract_offer(offer("case", "Brand X Case", self.DESCRIPTION), llm)
        self.assertEqual(model.calls, 1)


if __name__ == "__main__":
    unittest.main()
