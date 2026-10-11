"""Requirements about what a part *is* - its maker, chip, store, Wi-Fi, screen - rather than a numeric spec.

The snapshot's specs.jsonl holds numbers and sockets. A user also says "an NVIDIA card", "no MSI",
"an RTX 5070 or better", "a 14 inch OLED laptop". Those are answered from the listing itself (brand,
name, chip, store) through three small functions shared by candidate queries and validation:

    derive(row)                  attributes of one offer or option item
    filters_for(category, req)   the filters a requirement set puts on one category
    verdict(attributes, filters) True / False / None (None: the listing does not say)

Every key of ``hard_constraints`` is described once in LABELS, which also drives the wording shown to
the user and the phrases that remove a requirement again.
"""
from __future__ import annotations

import re

FORM_FACTOR_RANK = {"Mini-ITX": 1, "Micro-ATX": 2, "ATX": 3, "E-ATX": 4}
# ASCII boundaries, not \b: in "显卡要RTX 5070以上" the chip name touches Chinese characters, which count as word characters.
GPU_CHIP = re.compile(r"(?<![A-Za-z0-9])(RTX|GTX|RX|ARC)\s*([A-Z]?)(\d{3,4})\s*(TI\s*SUPER|TI|SUPER|XTX|XT|GRE)?(?![A-Za-z0-9])", re.I)
GPU_VENDOR = {"RTX": "nvidia", "GTX": "nvidia", "RX": "amd", "ARC": "intel"}
SUFFIX_RANK = {"": 0, "GRE": 1, "XT": 1, "SUPER": 1, "TI": 2, "XTX": 2, "TI SUPER": 3}
VENDOR_NAME = {"nvidia": "NVIDIA", "amd": "AMD", "intel": "Intel"}
SCREEN = re.compile(r"(\d{2}(?:\.\d)?)\s*(?:-?\s*inch|in\b|\"|”|''|英寸|寸)", re.I)
LAPTOP_GPU = re.compile(r"\brtx\s?\d|\bgtx\s?\d|radeon rx|\barc\s?[ab]?\d", re.I)
LAPTOP_VRAM = re.compile(r"(?:RTX|GTX|RX)\s*\d{4}\s*(?:TI|SUPER)?\s*-\s*(\d{1,2})\s*GB", re.I)      # "RTX5070-8GB", not "RTX5060 32GB RAM"
# Many all-in-one coolers are listed by series name and radiator size only ("Frozen Horizon 360", "Kraken 280").
LIQUID_COOLER = re.compile(r"liquid|\baio\b|all[- ]in[- ]one|water|kraken|hydroshift|\baqua\b|frozen|radiator|\b(?:240|280|360|420)\b", re.I)
AIR_COOLER = re.compile(r"\bair\b|heat\s?sink|tower|heat[- ]?pipes?|low[- ]profile", re.I)
PART_WORD = {"cpu": "processor", "gpu": "graphics card", "motherboard": "motherboard", "ram": "memory kit", "ssd": "SSD",
             "psu": "power supply", "case": "case", "cooler": "cooler"}


def gpu_model(text: str | None) -> dict | None:
    """Vendor and an ordering for a graphics chip name. "RTX 5070 Ti" -> tier 70, generation 50, suffix Ti;
    a card is "at least as good" when its (tier, generation, suffix) is not lower. This is a rule of
    thumb within one vendor's line-up, not a benchmark."""
    match = GPU_CHIP.search(text or "")
    if not match:
        return None
    family, number = match.group(1).upper(), int(match.group(3))
    suffix = re.sub(r"\s+", " ", (match.group(4) or "").upper())
    generation, tier = divmod(number, 100)
    return {"vendor": GPU_VENDOR[family], "rank": [tier, generation, SUFFIX_RANK[suffix]],
            "label": f"{family} {number}{' ' + suffix.title().replace('Xtx', 'XTX').replace('Xt', 'XT') if suffix else ''}"}


def brand_of(row: dict) -> str:
    """The maker of a listing. Some listings carry the store's company name as brand; the name then leads."""
    brand = (row.get("brand") or "").strip()
    if not brand or "pte ltd" in brand.casefold():
        brand = (row.get("name") or "").split(" ")[0]
    brand = brand.casefold()
    return "wd" if brand.startswith("western") else brand


def cpu_maker(row: dict, specs: dict) -> str | None:
    text = f"{row.get('brand') or ''} {row.get('name') or ''}".casefold()
    if "ryzen" in text:          # before "intel": a Ryzen laptop may list an Intel Wi-Fi card
        return "amd"
    if "intel" in text or re.search(r"\bcore\s?(?:i?\d|ultra)|\bultra\s?\d|\bi[3579][- ]?\d", text):
        return "intel"
    if "amd" in text or "ryzen" in text:
        return "amd"
    socket = specs.get("socket") or ""
    return "amd" if socket.startswith("AM") else "intel" if socket.startswith("LGA") else None


CPU_TIER = re.compile(r"ryzen\s*(?:ai\s*|ri\s*)?([3579])\b|\bi([3579])[- ]?\d{4,5}|(?:core|ultra)\s*(?:ultra\s*)?([3579])\b|"
                      r"ultra\s*\d([3579])\d[a-z]", re.I)


def cpu_tier(name: str) -> int | None:
    """The class digit of a processor name: 7 for "Ryzen 7 9800X3D", "i7-14700F", "Core Ultra 7 265K", "Ultra 270K"."""
    match = CPU_TIER.search(re.sub(r"[™®©]", "", name or ""))
    return int(next(g for g in match.groups() if g)) if match else None


def derive(row: dict) -> dict:
    """Attributes of a catalogue row or an option item (which calls the store ``merchant``)."""
    specs, name, category = row.get("specs") or {}, row.get("name") or "", row.get("category")
    out = {"brand": brand_of(row), "store": (row.get("store") or row.get("merchant") or "").casefold()}
    if category == "gpu":
        model = gpu_model(specs.get("chip")) or gpu_model(name)
        out.update(gpu_vendor=model and model["vendor"], gpu_rank=model and model["rank"])
    elif category == "cpu":
        out.update(cpu_maker=cpu_maker(row, specs), cpu_tier=cpu_tier(name))
    elif category == "motherboard":
        out.update(wifi=bool(re.search(r"wi-?fi", name, re.I)), form_factor=specs.get("form_factor"))
    elif category == "case":
        out["form_factor"] = specs.get("max_form_factor")
    elif category == "cooler":
        out["cooler_kind"] = "liquid" if LIQUID_COOLER.search(name) else "air" if AIR_COOLER.search(name) else None
    elif category == "laptop":
        size, model, memory = SCREEN.search(name), gpu_model(name), LAPTOP_VRAM.search(name)
        dedicated = bool(LAPTOP_GPU.search(name))
        # A laptop without a dedicated chip has integrated graphics: it meets no maker, model or memory wish.
        out.update(screen_inches=float(size.group(1)) if size else None, oled="oled" in name.casefold(),
                   dedicated_gpu=dedicated, cpu_maker=cpu_maker(row, specs), cpu_tier=cpu_tier(name),
                   gpu_vendor=model["vendor"] if model else None if dedicated else "integrated",
                   gpu_rank=model["rank"] if model else None if dedicated else [0, 0, 0],
                   gpu_memory_gb=int(memory.group(1)) if memory else None if dedicated else 0)
    return out


def filters_for(category: str, requirements: dict) -> dict:
    """The attribute filters the user's hard constraints put on one category (empty when there are none)."""
    hard = requirements.get("hard_constraints") or {}
    wanted = {"exclude_brands": hard.get("excluded_brands"), "stores": hard.get("stores")}
    if category == "gpu":
        wanted.update(gpu_vendor=hard.get("gpu_vendor"), min_gpu_model=hard.get("minimum_gpu_model"))
    elif category == "cpu":
        wanted.update(cpu_maker=hard.get("cpu_maker"), min_cpu_tier=hard.get("minimum_cpu_tier"))
    elif category == "motherboard":
        wanted.update(wifi=hard.get("wifi"), max_form_factor=hard.get("maximum_form_factor"))
    elif category == "case":
        wanted["max_form_factor"] = hard.get("maximum_form_factor")
    elif category == "cooler":
        wanted["cooler_kind"] = hard.get("cooler_kind")
    elif category == "laptop":
        wanted.update(brands=hard.get("laptop_brands"), screen_inches=hard.get("laptop_screen_inches"),
                      oled=hard.get("laptop_oled"), dedicated_gpu=hard.get("laptop_dedicated_gpu"),
                      gpu_vendor=hard.get("gpu_vendor"), min_gpu_model=hard.get("minimum_gpu_model"),
                      cpu_maker=hard.get("cpu_maker"), min_gpu_memory=hard.get("minimum_gpu_memory_gb"),
                      min_cpu_tier=hard.get("minimum_cpu_tier"))
    if category != "laptop" and (hard.get("part_brands") or {}).get(category):
        wanted["part_brand"] = {category: hard["part_brands"][category]}
    return {key: value for key, value in wanted.items() if value}


def _test(key: str, wanted, have: dict) -> bool | None:
    if key == "exclude_brands":
        return have["brand"] not in wanted
    if key == "brands":
        return have["brand"] in wanted
    if key == "part_brand":
        return have["brand"] in next(iter(wanted.values()))
    if key == "min_cpu_tier":
        tier = have.get("cpu_tier")
        return None if tier is None else tier >= wanted
    if key == "min_gpu_memory":
        size = have.get("gpu_memory_gb")
        return None if size is None else size >= wanted
    if key == "stores":
        return any(store in have["store"] for store in wanted)
    if key == "min_gpu_model":
        if have.get("gpu_rank") is None:
            return None
        return have["gpu_vendor"] == wanted["vendor"] and have["gpu_rank"] >= wanted["rank"]
    if key == "max_form_factor":
        size = have.get("form_factor")
        return None if size is None else FORM_FACTOR_RANK[size] <= FORM_FACTOR_RANK[wanted]
    if key == "screen_inches":
        size = have.get("screen_inches")
        return None if size is None else wanted[0] <= size <= wanted[1]
    value = have.get(key)
    return None if value is None else value == wanted


def verdict(have: dict, filters: dict) -> bool | None:
    """False if any filter is violated, None if none is violated but one cannot be told, else True."""
    results = [_test(key, wanted, have) for key, wanted in filters.items()]
    return False if False in results else None if None in results else True


def _names(values: list[str]) -> str:
    return " or ".join(v.upper() if len(v) <= 4 else v.title() for v in values)


def _screen(span: list[float]) -> str:
    low, high = span
    if high - low < 1:
        return f"a {low:g}-inch screen"
    if high >= 19:
        return f"a screen of at least {low:g} inches"
    if low <= 10:
        return f"a screen of at most {int(high)} inches"
    return f"a {low:g} to {int(high)}-inch screen"


# key in hard_constraints -> (topic used in "Remove the <topic> requirement.", wording of the requirement)
LABELS: dict[str, tuple[str, object]] = {
    "minimum_memory_gb": ("memory", lambda v: f"{v}GB of memory"),
    "minimum_storage_gb": ("storage", lambda v: f"{v}GB of storage"),
    "minimum_gpu_memory_gb": ("graphics memory", lambda v: f"{v}GB of graphics memory"),
    "gpu_vendor": ("graphics card brand", lambda v: f"a graphics card from {VENDOR_NAME[v]}"),
    "minimum_gpu_model": ("graphics card model", lambda v: f"a graphics card at least as good as the {v['label']}"),
    "no_graphics_card": ("no graphics card", lambda v: "no graphics card"),
    "cpu_maker": ("processor brand", lambda v: f"a processor from {VENDOR_NAME[v]}"),
    "minimum_cpu_tier": ("processor class", lambda v: f"a processor of class {v} or higher (Core i{v}, Core Ultra {v}, Ryzen {v})"),
    "socket": ("platform", lambda v: f"the {v} platform"),
    "memory_type": ("memory generation", lambda v: f"{v} memory"),
    "minimum_psu_watts": ("power supply", lambda v: f"a power supply of at least {v}W"),
    "maximum_form_factor": ("size", lambda v: f"a {v} build"),
    "wifi": ("Wi-Fi", lambda v: "built-in Wi-Fi"),
    "cooler_kind": ("cooling", lambda v: f"{v} cooling"),
    "part_brands": ("part brand", lambda v: " and ".join(f"{'an' if PART_WORD[c] == 'SSD' else 'a'} {PART_WORD[c]} from {_names(b)}" for c, b in v.items())),
    "excluded_brands": ("excluded brands", lambda v: f"nothing from {_names(v)}"),
    "stores": ("store", lambda v: f"only from {_names(v)}"),
    "laptop_brands": ("laptop brand", lambda v: f"a laptop from {_names(v)}"),
    "laptop_screen_inches": ("screen size", _screen),
    "laptop_oled": ("OLED screen", lambda v: "an OLED screen"),
    "laptop_dedicated_gpu": ("dedicated graphics", lambda v: "a dedicated graphics card"),
}
LAPTOP_ONLY = {"laptop_brands", "laptop_screen_inches", "laptop_oled", "laptop_dedicated_gpu"}
DESKTOP_ONLY = {"no_graphics_card", "socket", "memory_type", "minimum_psu_watts", "maximum_form_factor", "wifi", "cooler_kind",
                "part_brands"}


def names_a_card(hard: dict) -> bool:
    """The user asked for something only a graphics card can meet, so integrated graphics will not do."""
    return any(hard.get(k) for k in ("minimum_gpu_memory_gb", "gpu_vendor", "minimum_gpu_model"))         or bool((hard.get("part_brands") or {}).get("gpu"))


def active(requirements: dict) -> dict:
    """The hard constraints that apply to the requested device type."""
    device = requirements.get("device_type")
    skip = LAPTOP_ONLY if device == "desktop" else DESKTOP_ONLY if device == "laptop" else set()
    return {k: v for k, v in (requirements.get("hard_constraints") or {}).items() if v and k in LABELS and k not in skip}


def describe(requirements: dict) -> list[str]:
    """The user's hard constraints in words, e.g. ["32GB of memory", "a graphics card from NVIDIA"]."""
    wanted = active(requirements)
    if wanted.get("minimum_gpu_model"):      # the model floor already names the maker
        wanted.pop("gpu_vendor", None)
    return [LABELS[key][1](value) for key, value in wanted.items()]
