import unittest

from backend.validation import validate_option

DESKTOP = ["cpu", "motherboard", "ram", "ssd", "gpu", "psu", "case", "cooler"]


def item(category, price, product_id=None, available=True, **specs):
    return {"category": category, "product_id": product_id or category, "offer_id": category + "-offer",
            "name": f"test {category}", "price": str(price), "quantity": 1,
            "availability": "in_stock_at_collection" if available else "unavailable", "specs": specs}


def desktop(**overrides):
    parts = {
        "cpu": item("cpu", 449, socket="AM5", tdp_w=120),
        "motherboard": item("motherboard", 289, socket="AM5", memory_type="DDR5", form_factor="ATX"),
        "ram": item("ram", 159, memory_type="DDR5", capacity_gb=32),
        "ssd": item("ssd", 119, capacity_gb=1024),
        "gpu": item("gpu", 899, tdp_w=220, length_mm=300),
        "psu": item("psu", 139, wattage_w=750),
        "case": item("case", 129, max_form_factor="ATX", max_gpu_length_mm=360),
        "cooler": item("cooler", 59),
    }
    parts.update(overrides)
    return {"device_type": "desktop", "items": list(parts.values()), "required_categories": DESKTOP}


def status(result, code):
    return next(c for c in result["checks"] if c["code"] == code)["status"]


REQ = {"budget": {"currency": "SGD", "maximum_minor": 350000}}


class ValidationTests(unittest.TestCase):
    def test_fully_specified_compatible_build_passes(self):
        result = validate_option(desktop(), REQ)
        self.assertEqual(result["overall_status"], "passed", [c for c in result["checks"] if c["status"] != "passed"])
        self.assertEqual(result["total_minor"], 224200)

    def test_total_uses_decimal_and_equals_sum_of_offers(self):
        option = desktop(cooler=item("cooler", "59.10"), ssd=item("ssd", "119.20", capacity_gb=1024))
        self.assertEqual(validate_option(option, REQ)["total_minor"], 224230)

    def test_over_budget_fails_and_names_most_expensive_part(self):
        result = validate_option(desktop(), {"budget": {"maximum_minor": 200000}})
        self.assertEqual(status(result, "budget_limit"), "failed")
        self.assertEqual(result["affected_categories"], ["gpu"])

    def test_intel_cpu_on_am5_board_fails(self):
        result = validate_option(desktop(cpu=item("cpu", 599, socket="LGA1700", tdp_w=125)), REQ)
        self.assertEqual(status(result, "cpu_motherboard_socket"), "failed")
        self.assertEqual(result["overall_status"], "failed")
        self.assertIn("motherboard", result["affected_categories"])

    def test_missing_socket_is_unknown_not_passed(self):
        result = validate_option(desktop(cpu=item("cpu", 449, tdp_w=120)), REQ)
        self.assertEqual(status(result, "cpu_motherboard_socket"), "unknown")
        self.assertEqual(result["overall_status"], "unknown")

    def test_ddr4_ram_on_ddr5_board_fails(self):
        result = validate_option(desktop(ram=item("ram", 99, memory_type="DDR4", capacity_gb=32)), REQ)
        self.assertEqual(status(result, "motherboard_memory_type"), "failed")
        self.assertEqual(result["affected_categories"], ["ram"])

    def test_psu_headroom(self):
        weak = validate_option(desktop(psu=item("psu", 59, wattage_w=450)), REQ)
        self.assertEqual(status(weak, "psu_headroom"), "failed")   # (120 + 220 + 100) * 1.2 = 528 W
        unknown = validate_option(desktop(gpu=item("gpu", 899, length_mm=300)), REQ)
        self.assertEqual(status(unknown, "psu_headroom"), "unknown")

    def test_gpu_too_long_for_case_fails(self):
        result = validate_option(desktop(case=item("case", 89, max_form_factor="ATX", max_gpu_length_mm=280)), REQ)
        self.assertEqual(status(result, "gpu_case_clearance"), "failed")
        self.assertEqual(result["affected_categories"], ["case"])

    def test_atx_board_in_mini_itx_case_fails(self):
        result = validate_option(desktop(case=item("case", 99, max_form_factor="Mini-ITX", max_gpu_length_mm=360)), REQ)
        self.assertEqual(status(result, "case_form_factor"), "failed")

    def test_out_of_stock_part_fails(self):
        result = validate_option(desktop(gpu=item("gpu", 899, available=False, tdp_w=220, length_mm=300)), REQ)
        self.assertEqual(status(result, "in_stock"), "failed")

    def test_missing_category_fails(self):
        option = desktop()
        option["items"] = [i for i in option["items"] if i["category"] != "psu"]
        result = validate_option(option, REQ)
        self.assertEqual(status(result, "configuration_completeness"), "failed")

    def test_locked_component_must_stay(self):
        req = {**REQ, "locked_product_ids": ["my-rtx-4070"]}
        self.assertEqual(status(validate_option(desktop(), req), "locked_items_kept"), "failed")
        kept = desktop(gpu=item("gpu", 899, product_id="my-rtx-4070", tdp_w=200, length_mm=300))
        self.assertEqual(status(validate_option(kept, req), "locked_items_kept"), "passed")

    def test_owned_component_is_not_charged(self):
        owned = item("gpu", 899, product_id="my-rtx-4070", tdp_w=200, length_mm=300)
        owned["owned_by_user"] = True
        self.assertEqual(validate_option(desktop(gpu=owned), REQ)["total_minor"], 134300)

    def test_minimum_memory_and_storage(self):
        req = {**REQ, "hard_constraints": {"minimum_memory_gb": 64, "minimum_storage_gb": 2048}}
        result = validate_option(desktop(), req)
        self.assertEqual(status(result, "minimum_memory"), "failed")
        self.assertEqual(status(result, "minimum_storage"), "failed")

    def test_bundle_listing_is_rejected(self):
        bundle = item("motherboard", 699, socket="AM5", memory_type="DDR5", form_factor="ATX")
        bundle["flags"] = {"bundle_suspect": True}
        self.assertEqual(status(validate_option(desktop(motherboard=bundle), REQ), "single_component_listings"), "failed")

    def test_laptop_checks(self):
        laptop = {"device_type": "laptop", "required_categories": ["laptop"],
                  "items": [item("laptop", 1499, ram_gb=16, storage_gb=512)]}
        req = {"budget": {"maximum_minor": 200000}, "hard_constraints": {"minimum_memory_gb": 16}}
        result = validate_option(laptop, req)
        self.assertEqual(status(result, "minimum_memory"), "passed")
        self.assertEqual(status(result, "regional_configuration_match"), "unknown")
        self.assertEqual(result["overall_status"], "unknown")

    def test_every_check_has_a_reason(self):
        for check in validate_option(desktop(cpu=item("cpu", 449)), REQ)["checks"]:
            self.assertTrue(check["reason"], check["code"])
            self.assertIn(check["status"], ("passed", "failed", "unknown"))


if __name__ == "__main__":
    unittest.main()
