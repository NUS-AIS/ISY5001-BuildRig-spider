"""Common user requirements beyond budget and capacities: understood, enforced, or declined out loud."""
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import create_router
from backend.attributes import derive, gpu_model
from backend.orchestrator import RecommendationEngine
from backend.requirement_rules import inapplicable, unsupported_requests
from backend.requirements_parser import _specific_mention, parse_requirements
from backend.retrieval import LocalCorpus
from backend.settings import Settings
from backend.store import StateStore

DATA = Path(__file__).resolve().parents[1] / "data"
CORPUS = LocalCorpus(DATA)
DESKTOP, LAPTOP = "Gaming desktop, budget S$3,000. ", "Laptop for university, budget S$1,800. "


def hard(text: str, current: dict | None = None) -> dict:
    return parse_requirements(text, current or {})[0].get("hard_constraints") or {}


class UnderstandingTests(unittest.TestCase):
    def test_desktop_requirements(self):
        cases = {
            "I want an NVIDIA graphics card, not AMD.": {"gpu_vendor": "nvidia"},
            "I prefer an AMD Radeon card.": {"gpu_vendor": "amd"},
            "I don't need a graphics card.": {"no_graphics_card": True},
            "I want an Intel CPU.": {"cpu_maker": "intel"},
            "No Intel.": {"cpu_maker": "amd"},
            "AMD processor please, AM5 platform.": {"cpu_maker": "amd", "socket": "AM5"},
            "DDR5 memory only.": {"memory_type": "DDR5"},
            "The power supply should be at least 850W.": {"minimum_psu_watts": 850},
            "I want a Mini-ITX build.": {"maximum_form_factor": "Mini-ITX"},
            "Liquid cooling please.": {"cooler_kind": "liquid"},
            "It needs Wi-Fi.": {"wifi": True},
            "Please avoid MSI products.": {"excluded_brands": ["msi"]},
            "Only from Dynacore.": {"stores": ["dynacore"]},
            "要英特尔的处理器": {"cpu_maker": "intel"},
            "内存32G，硬盘2T": {"minimum_memory_gb": 32, "minimum_storage_gb": 2048},
        }
        for phrase, expected in cases.items():
            self.assertEqual(hard(DESKTOP + phrase), expected, phrase)

    def test_model_floor_is_not_a_request_for_that_exact_card(self):
        for phrase in ("The graphics card must be an RTX 5070 or better.", "显卡要RTX 5070以上", "at least an RTX 5070"):
            wanted = hard(DESKTOP + phrase)
            self.assertEqual(wanted["minimum_gpu_model"], {"vendor": "nvidia", "rank": [70, 50, 0], "label": "RTX 5070"}, phrase)
            self.assertEqual(wanted["gpu_vendor"], "nvidia")
        self.assertNotIn("minimum_gpu_model", hard(DESKTOP + "I like the RTX 5070."))

    def test_chip_ordering(self):
        rank = lambda name: gpu_model(name)["rank"]
        self.assertLess(rank("RTX 5060"), rank("RTX 5070"))
        self.assertLess(rank("RTX 5070"), rank("RTX 5070 Ti"))
        self.assertLess(rank("RTX 5070 Ti"), rank("RTX 4080"))
        self.assertEqual(gpu_model("XFX Radeon RX 9070XT")["vendor"], "amd")

    def test_laptop_requirements(self):
        self.assertEqual(hard(LAPTOP + "A 14 inch OLED screen, Lenovo or ASUS only."),
                         {"laptop_brands": ["asus", "lenovo"], "laptop_screen_inches": [14.0, 14.99], "laptop_oled": True})
        self.assertEqual(hard(LAPTOP + "With a dedicated graphics card."), {"laptop_dedicated_gpu": True})
        self.assertEqual(hard(LAPTOP + "I want a MacBook."), {"laptop_brands": ["apple"]})

    def test_product_names_from_the_lock_button_are_not_requirements(self):
        first, _ = parse_requirements(DESKTOP, {})
        kept, _ = parse_requirements("I want to keep the ASUS B760M-AYW WIFI D4, an Intel B760 LGA 1700 mATX motherboard.", first)
        self.assertEqual(kept.get("hard_constraints") or {}, {})

    def test_requirement_can_be_changed_and_removed(self):
        first, _ = parse_requirements(DESKTOP + "I want an Intel CPU and an NVIDIA graphics card.", {})
        second, _ = parse_requirements("Actually AMD processor please.", first)
        self.assertEqual(second["hard_constraints"], {"gpu_vendor": "nvidia", "cpu_maker": "amd"})
        third, _ = parse_requirements("Remove the graphics card brand requirement.", second)
        self.assertEqual(third["hard_constraints"], {"cpu_maker": "amd"})
        fourth, _ = parse_requirements("I don't need a graphics card.", parse_requirements("NVIDIA card please", third)[0])
        self.assertEqual(fourth["hard_constraints"], {"cpu_maker": "amd", "no_graphics_card": True})

    def test_preferences(self):
        prefs = lambda text: parse_requirements(DESKTOP + text, {})[0]["preferences"]
        self.assertEqual(prefs("No RGB lighting at all."), ["no RGB lighting"])
        self.assertEqual(prefs("要白色机箱，不要灯"), ["white", "no RGB lighting"])
        self.assertEqual(prefs("Lots of RGB please."), ["RGB lighting"])
        self.assertEqual(prefs("It should run Cyberpunk at 1440p 144Hz."), ["1440p", "high refresh rate"])
        self.assertEqual(prefs("I play at 4K."), ["4K"])            # and 4K is not read as a S$4,000 budget

    def test_budget_wordings(self):
        budget = lambda text: (parse_requirements("Gaming desktop. " + text, {})[0].get("budget") or {}).get("maximum_minor")
        self.assertEqual(budget("My budget is around 2.5k."), 250000)
        self.assertEqual(budget("Budget between S$2,000 and S$2,500."), 250000)
        self.assertEqual(budget("预算三千新币"), 300000)
        self.assertEqual(budget("I play at 4K."), None)

    def test_amounts_that_are_not_the_budget(self):
        first, _ = parse_requirements(DESKTOP, {})
        for follow_up in ("Spend no more than S$800 on the graphics card.", "But I want to spend at least S$2,500."):
            self.assertEqual(parse_requirements(follow_up, first)[0]["budget"]["maximum_minor"], 300000, follow_up)

    def test_other_currencies_are_asked_about_not_converted(self):
        for text in ("Gaming desktop. I can spend 2000 USD.", "游戏台式机，预算一万人民币"):
            req, questions = parse_requirements(text, {})
            self.assertNotIn("budget", req, text)
            self.assertEqual([q["question_id"] for q in questions], ["q_budget_currency"], text)

    def test_unsupported_wishes_are_named(self):
        named = lambda text, device="desktop": unsupported_requests(text, device)
        self.assertEqual(named("Include a monitor and a keyboard."), ["a monitor", "a keyboard, mouse or other peripherals"])
        self.assertEqual(named("It should weigh less than 1.5kg with 10 hours of battery.", "laptop"), ["a weight limit", "battery life"])
        self.assertEqual(named("Show me three options, delivered this week."), ["delivery or assembly", "the number of options"])
        self.assertEqual(named("Gaming desktop with 32GB RAM"), [])
        self.assertEqual(named("A PC used for video editing."), [])              # not "used parts"
        self.assertEqual(named("A touchscreen and two USB-C ports.", "laptop"), ["a touchscreen", "particular ports"])
        self.assertEqual(named("A B650 motherboard."), ["a motherboard chipset"])

    def test_a_feature_named_to_decline_it_is_not_required(self):
        cases = {
            "I don't need Wi-Fi.": {}, "Wi-Fi is not needed.": {}, "No water cooling please.": {"cooler_kind": "air"},
            "不要水冷，要wifi": {"cooler_kind": "air", "wifi": True}, "I hate ASUS.": {"excluded_brands": ["asus"]},
            "Intel or AMD, either is fine.": {}, "NVIDIA or AMD card, I don't mind.": {}, "No need for a graphics card.": {"no_graphics_card": True},
        }
        for phrase, expected in cases.items():
            self.assertEqual(hard(DESKTOP + phrase), expected, phrase)
        for phrase, expected in {"I don't like MSI.": {"excluded_brands": ["msi"]}, "No dedicated GPU needed.": {},
                                 "No need for OLED.": {}}.items():
            self.assertEqual(hard(LAPTOP + phrase), expected, phrase)
        with_wifi, _ = parse_requirements(DESKTOP + "It needs Wi-Fi and an NVIDIA card.", {})
        self.assertEqual(hard("I don't need Wi-Fi after all.", with_wifi), {"gpu_vendor": "nvidia"})
        self.assertEqual(hard("Any graphics card brand is fine.", with_wifi), {"wifi": True})
        prefs = lambda text: parse_requirements(DESKTOP + text, {})[0]["preferences"]
        self.assertEqual(prefs("Not a fan of RGB."), ["no RGB lighting"])

    def test_device_type_named_only_to_rule_it_out(self):
        device = lambda text: parse_requirements(text, {})[0].get("device_type")
        self.assertEqual(device("I want a desktop, not a laptop. Budget S$2,000."), "desktop")
        self.assertEqual(device("I don't want a laptop, budget S$2,000 for gaming."), "desktop")
        self.assertEqual(device("No desktop please, something portable for S$1,500."), "laptop")
        self.assertEqual(device("A ThinkPad under S$2,000."), "laptop")
        self.assertEqual(device("Build me a gaming rig, budget S$2,000."), "desktop")

    def test_maker_of_one_part(self):
        self.assertEqual(hard(DESKTOP + "ASUS ROG Strix motherboard and a Samsung 990 Pro SSD."),
                         {"part_brands": {"motherboard": ["asus"], "ssd": ["samsung"]}})
        self.assertEqual(hard(DESKTOP + "Power supply from Seasonic. 三星的固态"), {"part_brands": {"psu": ["seasonic"], "ssd": ["samsung"]}})
        self.assertEqual(hard(DESKTOP + "I like Corsair in any case."), {})
        first, _ = parse_requirements(DESKTOP + "I want an ASUS motherboard.", {})
        second, _ = parse_requirements("Corsair RAM too.", first)
        self.assertEqual(second["hard_constraints"], {"part_brands": {"motherboard": ["asus"], "ram": ["corsair"]}})
        self.assertEqual(hard("Remove the part brand requirement.", second), {})
        # On a laptop a maker named for a part is not the laptop's maker, and the reply says it cannot be applied.
        req, _ = parse_requirements(LAPTOP + "With a Samsung SSD.", {})
        self.assertNotIn("laptop_brands", req["hard_constraints"])
        self.assertEqual(inapplicable(LAPTOP + "With a Samsung SSD.", req), ["an SSD from Samsung for a laptop"])

    def test_more_ways_to_name_chips_sizes_and_capacities(self):
        floor = lambda text: (hard(DESKTOP + text).get("minimum_gpu_model") or {}).get("label")
        self.assertEqual(floor("at least a 5070."), "RTX 5070")
        self.assertEqual(floor("a 9070 XT or better"), "RX 9070 XT")
        self.assertIsNone(floor("an upgrade from my 3060"))
        self.assertEqual(hard(DESKTOP + "I want an i7.")["cpu_maker"], "intel")
        self.assertEqual(hard(DESKTOP + "2x16GB RAM."), {"minimum_memory_gb": 32})
        self.assertEqual(hard(DESKTOP + "ATX case, 850W gold PSU."), {"minimum_psu_watts": 850})
        screen = lambda text: hard(LAPTOP + text)["laptop_screen_inches"]
        self.assertEqual(screen("13 or 14 inch."), [13.0, 14.99])
        self.assertEqual(screen("Screen at least 15 inches."), [15.0, 19.99])
        self.assertEqual(screen("Under 15 inches."), [10.0, 15.99])

    def test_more_budget_wordings(self):
        budget = lambda text: (parse_requirements(text, {})[0].get("budget") or {}).get("maximum_minor")
        prefs = lambda text: parse_requirements(text, {})[0]["preferences"]
        self.assertEqual(budget("Gaming PC, budget 2k."), 200000)
        self.assertEqual(prefs("Gaming PC, budget 2k."), [])                 # not a 1440p screen
        self.assertEqual(budget("Gaming PC, 3k budget."), 300000)
        self.assertEqual(budget("Gaming PC, I can spend around 2000."), 200000)
        self.assertEqual(budget("Gaming PC, I have 2500 to spend."), 250000)
        self.assertEqual(budget("台式机，预算一万五"), 1500000)
        self.assertEqual(budget("台式机，预算三千五"), 350000)
        self.assertEqual(budget("台式机，预算1万2"), 1200000)
        self.assertEqual(budget("Gaming rig 3k."), 300000)
        self.assertEqual(budget("1.5k laptop for school."), 150000)
        self.assertIsNone(budget("Gaming rig for 2k."))                      # 2k is also a resolution: the budget is asked for
        self.assertIsNone(budget("Gaming PC. I usually spend around 2 hours a day gaming."))
        self.assertIsNone(budget("Gaming PC with around 1000W."))
        req, questions = parse_requirements("Laptop, 1500 euros.", {})
        self.assertEqual([q["question_id"] for q in questions], ["q_budget_currency"])


class EnforcementTests(unittest.TestCase):
    """Every option of a recommendation honours what was asked, and the matching check says so."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        settings = Settings(data_dir=DATA, state_db=Path(cls.temp.name) / "s.db", model_name=None, model_provider=None,
                            embedding_provider="hash", embedding_model=None, embedding_dimensions=256,
                            retrieval_backend="local", pi_runtime_url=None, internal_api_token="t")
        store = StateStore(settings.state_db)
        app = FastAPI()
        app.include_router(create_router(store, CORPUS, RecommendationEngine(store, CORPUS, settings)))
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def ask(self, text: str, session: str | None = None, version: int = 0) -> tuple[str, dict]:
        session = session or self.client.post("/api/v1/sessions", json={}).json()["id"]
        reply = self.client.post(f"/api/v1/sessions/{session}/messages", json={
            "client_message_id": f"m{version}", "text": text, "expected_requirements_version": version}).json()
        return session, reply

    def options(self, text: str, checks: list[str]) -> list[dict]:
        session, reply = self.ask(text)
        self.assertTrue(reply["can_generate"], reply.get("questions"))
        run = self.client.post(f"/api/v1/sessions/{session}/runs", json={"requirements_version": reply["requirements_version"]}).json()["id"]
        result = self.client.get(f"/api/v1/runs/{run}/result").json()
        self.assertEqual(result["outcome"], "recommendations_available", text)
        for option in result["options"]:
            status = {c["code"]: c["status"] for c in option["validation"]["checks"]}
            self.assertEqual(option["validation"]["failed_codes"], [])
            for code in checks:
                self.assertIn(status.get(code), ("passed", "unknown"), f"{code} in {text}")
        return result["options"]

    def parts(self, options: list[dict], category: str) -> list[dict]:
        return [derive(i) for o in options for i in o["items"] if i["category"] == category]

    def test_graphics_card_maker_and_floor(self):
        for card in self.parts(self.options(DESKTOP + "I want an NVIDIA graphics card, not AMD.", ["gpu_brand"]), "gpu"):
            self.assertEqual(card["gpu_vendor"], "nvidia")
        for card in self.parts(self.options(DESKTOP + "I prefer an AMD Radeon card.", ["gpu_brand"]), "gpu"):
            self.assertEqual(card["gpu_vendor"], "amd")
        floor = self.parts(self.options("Gaming desktop, budget S$3,500. The graphics card must be an RTX 5070 Ti or better.",
                                        ["minimum_gpu_model"]), "gpu")
        self.assertTrue(floor and all(card["gpu_rank"] >= [70, 50, 2] for card in floor))

    def test_no_graphics_card(self):
        options = self.options(DESKTOP + "I don't need a graphics card.", ["no_graphics_card"])
        self.assertEqual(self.parts(options, "gpu"), [])

    def test_processor_platform_memory_and_power(self):
        options = self.options(DESKTOP + "I want an Intel CPU. DDR5 memory only. The power supply should be at least 850W.",
                               ["processor_brand", "memory_generation", "minimum_psu_wattage"])
        self.assertTrue(all(cpu["cpu_maker"] == "intel" for cpu in self.parts(options, "cpu")))
        for option in options:
            psu = next(i for i in option["items"] if i["category"] == "psu")
            self.assertGreaterEqual(psu["specs"]["wattage_w"], 850)
        amd = self.options(DESKTOP + "AMD processor please, AM5 platform.", ["processor_brand", "platform_socket"])
        self.assertTrue(all(i["specs"]["socket"] == "AM5" for o in amd for i in o["items"] if i["category"] == "cpu"))

    def test_brand_store_cooling_wifi_and_size(self):
        options = self.options(DESKTOP + "Please avoid MSI products. Liquid cooling please. It needs Wi-Fi. Only from Dynacore.",
                               ["excluded_brands", "cooling_type", "wifi", "allowed_stores"])
        for option in options:
            for item in option["items"]:
                self.assertNotEqual(derive(item)["brand"], "msi", item["name"])
                self.assertIn("dynacore", item["merchant"].casefold())
        self.assertTrue(all(c["cooler_kind"] == "liquid" for c in self.parts(options, "cooler")))
        self.assertTrue(all(b["wifi"] for b in self.parts(options, "motherboard")))
        small = self.options("Office desktop, budget S$2,000. I want a Mini-ITX build.", ["maximum_size"])
        self.assertTrue(all(c["form_factor"] == "Mini-ITX" for c in self.parts(small, "case")))

    def test_laptop_screen_brand_and_graphics(self):
        laptops = self.parts(self.options("Laptop for university, budget S$2,500. A 14 inch OLED screen, Lenovo or ASUS only.",
                                          ["laptop_brand", "screen_size", "oled_screen"]), "laptop")
        self.assertTrue(laptops)
        for laptop in laptops:
            self.assertIn(laptop["brand"], ("asus", "lenovo"))
            self.assertTrue(laptop["oled"] and 14 <= laptop["screen_inches"] < 15)
        gaming = self.parts(self.options("Laptop for gaming, budget S$2,500. With a dedicated graphics card.", ["dedicated_graphics"]), "laptop")
        self.assertTrue(gaming and all(laptop["dedicated_gpu"] for laptop in gaming))

    def test_air_cooling_excludes_all_in_one_coolers_named_by_series(self):
        kind = lambda name: derive({"category": "cooler", "name": name})["cooler_kind"]
        self.assertEqual(kind("NZXT Kraken 120 (LGA1700/AM5), One 120mm Aer P radiator fan"), "liquid")
        self.assertEqual(kind("Thermalright Frozen Horizon 360 ARGB BLACK V2 Cooler"), "liquid")
        self.assertEqual(kind("Thermalright Peerless Assassin 120 SE Dual Tower CPU Air Cooler"), "air")
        self.assertIsNone(kind("DEEPCOOL UD551 ARGB CPU COOLER"))
        coolers = self.parts(self.options(DESKTOP + "Air cooling please.", ["cooling_type"]), "cooler")
        self.assertTrue(coolers and all(cooler["cooler_kind"] != "liquid" for cooler in coolers))

    def test_laptop_graphics_chip_and_processor(self):
        chip = self.parts(self.options("Laptop for gaming, budget S$3,500. With an RTX 5070 or better and an Intel CPU.",
                                       ["minimum_gpu_model", "processor_brand"]), "laptop")
        self.assertTrue(chip)
        for laptop in chip:
            self.assertEqual((laptop["gpu_vendor"], laptop["cpu_maker"]), ("nvidia", "intel"))
            self.assertGreaterEqual(laptop["gpu_rank"], [70, 50, 0])
        ryzen = self.parts(self.options("Laptop for university, budget S$1,800. AMD Ryzen please.", ["processor_brand"]), "laptop")
        self.assertTrue(ryzen and all(laptop["cpu_maker"] == "amd" for laptop in ryzen))
        memory = self.parts(self.options("Laptop for gaming, budget S$3,500. At least 8GB VRAM.", ["minimum_gpu_memory"]), "laptop")
        self.assertTrue(memory and all((laptop["gpu_memory_gb"] or 8) >= 8 and laptop["dedicated_gpu"] for laptop in memory))

    def test_maker_of_one_desktop_part(self):
        options = self.options(DESKTOP + "I want an ASUS motherboard, a Corsair power supply and a Samsung SSD.", ["part_brand"])
        for category, maker in (("motherboard", "asus"), ("psu", "corsair"), ("ssd", "samsung")):
            self.assertTrue(all(part["brand"] == maker for part in self.parts(options, category)), category)
        # A maker named for the graphics card means a card is wanted, even for everyday use.
        office = self.options("Office desktop, budget S$2,500. MSI graphics card.", ["part_brand"])
        cards = self.parts(office, "gpu")
        self.assertTrue(len(cards) == len(office) and all(card["brand"] == "msi" for card in cards))

    def test_processor_class(self):
        self.assertEqual(hard(DESKTOP + "I want an i7."), {"cpu_maker": "intel", "minimum_cpu_tier": 7})
        self.assertEqual(hard(DESKTOP + "Ryzen 7 or better."), {"cpu_maker": "amd", "minimum_cpu_tier": 7})
        self.assertFalse(_specific_mention("i7", "I want an i7."))            # a class, not a product to lock
        self.assertTrue(_specific_mention("i7-14700F", "I want the i7-14700F."))
        tier = lambda name: derive({"category": "cpu", "name": name})["cpu_tier"]
        self.assertEqual([tier(n) for n in ("AMD Ryzen™ 7 Ryzen 7 9800X3D TRAY", "INTEL i5-14400F 2.5GHz", "INTEL CORE ULTRA 270K PLUS",
                                            "Intel Core Ultra 5 225F")], [7, 5, 7, 5])
        desktops = self.parts(self.options(DESKTOP + "I want an i7.", ["processor_brand", "processor_class"]), "cpu")
        self.assertTrue(desktops and all(cpu["cpu_maker"] == "intel" and cpu["cpu_tier"] >= 7 for cpu in desktops))
        laptops = self.parts(self.options("Laptop for university, budget S$2,500. Ryzen 7 or better.", ["processor_class"]), "laptop")
        self.assertTrue(laptops and all(laptop["cpu_maker"] == "amd" and laptop["cpu_tier"] >= 7 for laptop in laptops))

    def test_several_makers_for_one_part(self):
        self.assertEqual(hard(DESKTOP + "ASUS or MSI motherboard."), {"part_brands": {"motherboard": ["asus", "msi"]}})
        boards = self.parts(self.options(DESKTOP + "ASUS or MSI motherboard.", ["part_brand"]), "motherboard")
        self.assertTrue(boards and all(board["brand"] in ("asus", "msi") for board in boards))

    def test_editing_build_spends_what_is_left_on_memory(self):
        capacity = lambda options: [i["specs"]["capacity_gb"] for o in options for i in o["items"] if i["category"] == "ram"]
        editing = self.options("Desktop for video editing, budget S$3,500.", [])
        self.assertTrue(any(size >= 32 for size in capacity(editing)), capacity(editing))
        for option in editing:
            self.assertLessEqual(option["validation"]["total_minor"], 350000)

    def test_requirement_that_does_not_fit_the_device_is_named(self):
        _, reply = self.ask(LAPTOP + "With an 850W power supply.")
        self.assertEqual(reply["not_acted_on"], ["a power supply of at least 850W for a laptop"])

    def test_requirement_nothing_meets_is_asked_about_and_can_be_dropped(self):
        session, reply = self.ask(LAPTOP + "I want a MacBook.")
        self.assertFalse(reply["can_generate"])
        self.assertEqual([q["question_id"] for q in reply["questions"]], ["q_unavailable"])
        _, dropped = self.ask(reply["reply_options"][0]["value"], session, reply["requirements_version"])
        self.assertTrue(dropped["can_generate"])
        self.assertNotIn("laptop_brands", dropped["requirements"].get("hard_constraints") or {})

    def test_costly_requirement_raises_the_budget_floor(self):
        _, reply = self.ask("Gaming desktop, budget S$1,200. The graphics card must be an RTX 5080 or better.")
        self.assertEqual([q["question_id"] for q in reply["questions"]], ["q_budget_low"])
        self.assertIn("RTX 5080", reply["questions"][0]["text"])

    def test_reply_names_what_it_cannot_act_on(self):
        _, reply = self.ask(DESKTOP + "Include a monitor and Windows 11.")
        self.assertEqual(reply["not_acted_on"], ["a monitor", "an operating system or software licence"])
        self.assertIn("I cannot act on a monitor, an operating system or software licence", reply["assistant_message"])


if __name__ == "__main__":
    unittest.main()
