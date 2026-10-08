"""Structured specification extraction for collected offers (FR02).

Each offer gets the fields its category needs for compatibility checks. Extraction runs in
three tiers and records which tier produced every value:

  1. ``regex``          - explicit text in the variant, title or description
  2. ``rule_inferred``  - deterministic domain rules (e.g. Ryzen 9000 -> AM5, RTX 4060 -> 115 W)
  3. ``llm``            - the local model reads the description, but a value is only accepted
                          when the model also quotes the sentence it came from and that quote
                          really appears in the source text

Anything still unknown stays ``None`` and is listed under ``flags.missing``; values that
disagree between title and description are listed under ``flags.conflict`` and set to
``None``. Nothing is guessed.

Usage (from SystemCode/):
    python -m backend.ingest.specs                 # latest snapshot, regex + rules + LLM
    python -m backend.ingest.specs --no-llm        # regex + rules only
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------- field plan

FIELDS: dict[str, list[str]] = {
    "cpu": ["socket", "tdp_w", "integrated_graphics"],
    "motherboard": ["socket", "memory_type", "form_factor"],
    "ram": ["memory_type", "capacity_gb"],
    "gpu": ["chip", "vram_gb", "tdp_w", "length_mm"],
    "psu": ["wattage_w"],
    "case": ["max_form_factor", "max_gpu_length_mm"],
    "ssd": ["capacity_gb"],
    "laptop": ["ram_gb", "storage_gb"],
}

# Fields worth asking the model about when regex and rules found nothing.
LLM_FIELDS = {"cpu": ["tdp_w"], "gpu": ["length_mm"], "psu": ["wattage_w"],
              "case": ["max_gpu_length_mm", "max_form_factor"], "motherboard": ["memory_type", "form_factor"],
              "laptop": ["ram_gb", "storage_gb"]}

FORM_FACTOR_RANK = {"Mini-ITX": 1, "Micro-ATX": 2, "ATX": 3, "E-ATX": 4}

# Sanity ranges: values outside them are rejected as mis-parses.
RANGES = {"tdp_w": (15, 700), "wattage_w": (300, 2800), "length_mm": (120, 400),
          "max_gpu_length_mm": (150, 500), "capacity_gb": (4, 16384), "ram_gb": (4, 256),
          "storage_gb": (64, 16384), "vram_gb": (2, 64)}

# Typical board power (W) of desktop GPU chips. Used only when the listing gives no figure.
GPU_TDP = {
    "RTX 5090": 575, "RTX 5080": 360, "RTX 5070 TI": 300, "RTX 5070": 250, "RTX 5060 TI": 180,
    "RTX 5060": 145, "RTX 5050": 130, "RTX 4090": 450, "RTX 4080 SUPER": 320, "RTX 4080": 320,
    "RTX 4070 TI SUPER": 285, "RTX 4070 TI": 285, "RTX 4070 SUPER": 220, "RTX 4070": 200,
    "RTX 4060 TI": 160, "RTX 4060": 115, "RTX 3060": 170, "RTX 3050": 130,
    "RX 9070 XT": 304, "RX 9070": 220, "RX 9060 XT": 160, "RX 7900 XTX": 355, "RX 7900 XT": 315,
    "RX 7900 GRE": 260, "RX 7800 XT": 263, "RX 7700 XT": 245, "RX 7600 XT": 190, "RX 7600": 165,
    "ARC B580": 190, "ARC B570": 150, "ARC A770": 225, "ARC A750": 225,
}

AMD_CHIPSET_SOCKET = {**{c: "AM5" for c in ("A620", "B650", "B650E", "X670", "X670E", "B840", "B850", "X870", "X870E")},
                      **{c: "AM4" for c in ("A320", "A520", "B350", "B450", "B550", "X370", "X470", "X570")}}
INTEL_CHIPSET_SOCKET = {**{c: "LGA1700" for c in ("H610", "B660", "H670", "Z690", "B760", "H770", "Z790")},
                        **{c: "LGA1851" for c in ("H810", "B860", "Z890")},
                        **{c: "LGA1200" for c in ("H410", "B460", "H470", "Z490", "H510", "B560", "H570", "Z590")}}


# ---------------------------------------------------------------------------- helpers

def _value(value, method: str, evidence: str, confidence: str = "high") -> dict:
    return {"value": value, "method": method, "evidence": evidence.strip()[:160], "confidence": confidence}


def _distinct(values) -> list:
    out = []
    for v in values:
        if v not in out:
            out.append(v)
    return out


def _in_range(field: str, value) -> bool:
    if field not in RANGES or not isinstance(value, (int, float)):
        return True
    lo, hi = RANGES[field]
    return lo <= value <= hi


def _norm_socket(raw: str) -> str:
    raw = raw.upper().replace(" ", "")
    return raw if raw.startswith(("AM", "STR")) else "LGA" + raw.replace("LGA", "")


def _gb(amount: str, unit: str) -> int:
    value = float(amount)
    return int(round(value * 1024)) if unit.upper() == "TB" else int(round(value))


# ---------------------------------------------------------------------------- extractors
# Each extractor receives the three text sources and returns a spec dict or None. Sources are
# searched in order variant -> title -> description; a value found in the variant is the most
# specific because one listing can bundle several variants in its title.

SOCKET_RE = re.compile(r"\b(AM4|AM5|sTR5|LGA\s?(?:1151|1200|1700|1851))\b", re.I)
SOCKET_DIGITS_RE = re.compile(r"\b(?:socket|lga)\s*(1151|1200|1700|1851)\b", re.I)
MEMORY_RE = re.compile(r"\b(DDR[45])(?:-\d{4})?\b|\bD([45])\b", re.I)
FORM_RE = re.compile(r"\b(E-?ATX|EATX|Mini[\s-]?ITX|Micro[\s-]?ATX|m-?ATX|ATX|ITX)\b", re.I)


def _form(raw: str) -> str:
    raw = raw.upper().replace(" ", "").replace("-", "")
    if raw in ("EATX",):
        return "E-ATX"
    if "ITX" in raw:
        return "Mini-ITX"
    if raw in ("MICROATX", "MATX"):
        return "Micro-ATX"
    return "ATX"


def extract_socket(texts: dict[str, str], category: str):
    found = []
    for source in ("variant", "title", "description"):
        text = texts[source]
        hits = [_norm_socket(m.group(1)) for m in SOCKET_RE.finditer(text)]
        hits += ["LGA" + m.group(1) for m in SOCKET_DIGITS_RE.finditer(text)]
        if hits:
            found.append((source, _distinct(hits)))
    if found:
        source, hits = found[0]
        if len(hits) == 1:
            others = {h for _, hs in found[1:] for h in hs}
            if others and hits[0] not in others and len(others) == 1:
                return {"conflict": [hits[0], *others]}
            return _value(hits[0], "regex", f"{source}: {hits[0]}")
        if category == "motherboard":
            return {"conflict": hits}
    title = texts["title"].upper()
    if category == "motherboard":
        for chipset, socket in {**AMD_CHIPSET_SOCKET, **INTEL_CHIPSET_SOCKET}.items():
            if re.search(rf"\b{chipset}[A-Z]?\b", title):
                return _value(socket, "rule_inferred", f"chipset {chipset}")
    if category == "cpu":
        ryzen = re.search(r"RYZEN\S*\s+(?:THREADRIPPER\s+)?\d\s+(\d)(\d)\d{2}", title)
        if ryzen:
            series = int(ryzen.group(1))
            if series in (7, 8, 9):
                return _value("AM5", "rule_inferred", f"Ryzen {series}000 series")
            if series in (3, 5):
                return _value("AM4", "rule_inferred", f"Ryzen {series}000 series")
        intel = re.search(r"\bI[3579]-?1([234])\d{3}", title)
        if intel:
            return _value("LGA1700", "rule_inferred", f"Intel Core 1{intel.group(1)}th gen")
        ultra = re.search(r"ULTRA\s*[3579]\s*2\d{2}", title)
        if ultra:
            return _value("LGA1851", "rule_inferred", "Intel Core Ultra 200S")
    return None


SOCKET_MEMORY = {"AM5": "DDR5", "LGA1851": "DDR5", "AM4": "DDR4"}  # LGA1700 boards exist with either


def extract_memory_type(texts: dict[str, str], category: str):
    found = _memory_from_text(texts, category)
    if found or category != "motherboard":
        return found
    socket = extract_socket(texts, category)
    if socket and socket.get("value") in SOCKET_MEMORY:
        return _value(SOCKET_MEMORY[socket["value"]], "rule_inferred", f"{socket['value']} platform supports only {SOCKET_MEMORY[socket['value']]}")
    return None


def _memory_from_text(texts: dict[str, str], category: str):
    for source in ("variant", "title", "description"):
        hits = _distinct("DDR" + (m.group(1) or "DDR" + m.group(2))[-1] for m in MEMORY_RE.finditer(texts[source]))
        if source == "description" and category == "motherboard":
            # Long descriptions sometimes mention both generations ("unlike DDR4 ...").
            if len(hits) > 1:
                return {"conflict": hits}
        if len(hits) == 1:
            return _value(hits[0], "regex", f"{source}: {hits[0]}")
        if len(hits) > 1 and source != "description":
            return {"conflict": hits}
    return None


def extract_form_factor(texts: dict[str, str], category: str):
    if category == "case":
        # A case supports every board up to its largest stated size, wherever that is stated.
        hits = _distinct(_form(m.group(1)) for s in ("title", "description") for m in FORM_RE.finditer(texts[s]))
        if hits:
            best = max(hits, key=FORM_FACTOR_RANK.get)
            return _value(best, "regex", f"supports up to {best}")
        return None
    for source in ("title", "description"):
        hits = _distinct(_form(m.group(1)) for m in FORM_RE.finditer(texts[source]))
        if not hits:
            continue
        if category == "case":
            best = max(hits, key=FORM_FACTOR_RANK.get)
            return _value(best, "regex", f"{source}: supports up to {best}")
        if len(hits) == 1:
            return _value(hits[0], "regex", f"{source}: {hits[0]}")
        if source == "title":
            return {"conflict": hits}
    return None


def _number(pattern: str, field: str, texts: dict[str, str], sources=("variant", "title", "description"),
            transform: Callable = int, allow_multiple_in=("description",)):
    """Find one numeric value. If a source lists several different values (bundled variants)
    and is not the description, the field is ambiguous rather than guessed."""
    regex = re.compile(pattern, re.I)
    for source in sources:
        values = []
        for m in regex.finditer(texts[source]):
            try:
                value = transform(m)
            except (TypeError, ValueError):
                continue
            if value is not None and _in_range(field, value):
                values.append((value, m.group(0)))
        distinct = _distinct(v for v, _ in values)
        if len(distinct) == 1:
            return _value(distinct[0], "regex", f"{source}: {values[0][1]}")
        if len(distinct) > 1:
            if source in allow_multiple_in:
                continue
            return {"conflict": distinct}
    return None


def extract_tdp(texts, category):
    if category == "cpu":
        hit = _number(r"(?:TDP|Base Power|Processor Base Power|Default TDP)\D{0,25}?(\d{2,3})\s?W\b", "tdp_w", texts,
                      sources=("title", "description"), transform=lambda m: int(m.group(1)))
        if hit:
            return hit
        hit = _number(r"(?:,|\()\s*(\d{2,3})\s?W\s*\)", "tdp_w", texts, sources=("title",),
                      transform=lambda m: int(m.group(1)))
        return hit
    chip = extract_chip(texts, category)
    if chip and "value" in chip and chip["value"] in GPU_TDP:
        return _value(GPU_TDP[chip["value"]], "rule_inferred", f"typical board power of {chip['value']}", "medium")
    return None


GPU_CHIP_RE = re.compile(r"\b(RTX|GTX|GT)\s?(\d{3,4})\s?(TI\s?SUPER|TI|SUPER)?\b|\b(RX)\s?(\d{4})\s?(XTX|XT|GRE)?\b|\bARC\s?([AB]\d{3})\b", re.I)


def extract_chip(texts, category):
    for source in ("title", "variant"):
        hits = []
        for m in GPU_CHIP_RE.finditer(texts[source]):
            if m.group(1):
                suffix = (" " + re.sub(r"\s+", " ", m.group(3).upper())) if m.group(3) else ""
                hits.append(f"{m.group(1).upper()} {m.group(2)}{suffix}")
            elif m.group(4):
                hits.append(f"RX {m.group(5)}" + (f" {m.group(6).upper()}" if m.group(6) else ""))
            else:
                hits.append(f"ARC {m.group(7).upper()}")
        hits = _distinct(hits)
        if len(hits) == 1:
            return _value(hits[0], "regex", f"{source}: {hits[0]}")
        if len(hits) > 1:
            return {"conflict": hits}
    return None


def extract_gpu_length(texts, category):
    hit = _number(r"(?:length|card length|L)\s*[:=]?\s*(\d{3}(?:\.\d)?)\s?mm", "length_mm", texts,
                  sources=("description",), transform=lambda m: int(float(m.group(1))), allow_multiple_in=())
    if hit:
        return hit
    return _number(r"(\d{3}(?:\.\d)?)\s?(?:mm)?\s?[x×]\s?\d{2,3}(?:\.\d)?\s?(?:mm)?\s?[x×]\s?\d{2}(?:\.\d)?\s?mm", "length_mm",
                   texts, sources=("description",), transform=lambda m: int(float(m.group(1))), allow_multiple_in=())


def extract_case_gpu_clearance(texts, category):
    return _number(r"(?:GPU|graphics card|VGA|video card)[^.]{0,60}?(?:up to|max(?:imum)?|length|clearance)?[^.\d]{0,20}(\d{3})\s?mm(?!\s*(?:radiator|fans?|AIO|cooler))",
                   "max_gpu_length_mm", texts, sources=("description",),
                   transform=lambda m: int(m.group(1)), allow_multiple_in=())


PSU_RATINGS = set(range(450, 2001, 50))


def extract_wattage(texts, category):
    explicit = _number(r"\b(\d{3,4})\s?(?:W|Watt)s?\b", "wattage_w", texts, sources=("variant", "title"),
                       transform=lambda m: int(m.group(1)), allow_multiple_in=())
    if explicit and "value" in explicit:
        return explicit
    # Model numbers carry the rating without a unit: "RM750e", "SF1000L", "GX-1000".
    bare = lambda text: _distinct(int(m) for m in re.findall(r"(?<!\d)(\d{3,4})(?!\d)", text) if int(m) in PSU_RATINGS)
    title_ratings, variant_ratings = bare(texts["title"]), bare(texts["variant"])
    if len(variant_ratings) == 1 and (not title_ratings or variant_ratings[0] in title_ratings):
        return _value(variant_ratings[0], "rule_inferred", f"variant model number {texts['variant']}")
    if not texts["variant"] and len(title_ratings) == 1:
        return _value(title_ratings[0], "rule_inferred", f"title model number ({title_ratings[0]})")
    return explicit


def _capacity(m) -> int:
    if m.group("kit_n"):
        return int(m.group("kit_n")) * _gb(m.group("kit_size"), m.group("kit_unit"))
    return _gb(m.group("amount"), m.group("unit"))


CAPACITY_PATTERN = (r"(?:(?P<kit_n>\d)\s?[x×]\s?(?P<kit_size>\d{1,3})\s?(?P<kit_unit>GB|TB))|"
                    r"(?:\b(?P<amount>\d{1,4}(?:\.\d)?)\s?(?P<unit>GB|TB)\b)")


def extract_ram_capacity(texts, category):
    # "32GB (2x16GB)": prefer the explicit kit total that appears first in the variant/title.
    regex = re.compile(CAPACITY_PATTERN, re.I)
    for source in ("variant", "title"):
        matches = list(regex.finditer(texts[source]))
        if not matches:
            continue
        totals = _distinct(_capacity(m) for m in matches if not m.group("kit_n"))
        kits = _distinct(_capacity(m) for m in matches if m.group("kit_n"))
        if len(totals) == 1:
            return _value(totals[0], "regex", f"{source}: {matches[0].group(0)}")
        if not totals and len(kits) == 1:
            return _value(kits[0], "regex", f"{source}: {matches[0].group(0)}")
        if len(totals) > 1:
            return {"conflict": totals}
    return None


def extract_storage_capacity(texts, category):
    return _number(r"\b(\d{1,4}(?:\.\d)?)\s?(GB|TB)\b", "capacity_gb", texts, sources=("variant", "title"),
                   transform=lambda m: _gb(m.group(1), m.group(2)), allow_multiple_in=())


def extract_vram(texts, category):
    return _number(r"\b(\d{1,2})\s?GB\b", "vram_gb", texts, sources=("variant", "title"),
                   transform=lambda m: int(m.group(1)), allow_multiple_in=())


LAPTOP_RAM_SIZES = {4, 8, 12, 16, 24, 32, 48, 64, 96, 128}


def extract_laptop_ram(texts, category):
    # Titles look like "(CORE 7 150U/ 16GB (8GX2)RAM/1TB SSD/...)" or "/32GB(16X2)/1TB/".
    for source in ("title", "description"):
        text = re.sub(r"\(\s*\d+\s?G?B?\s?[xX]\s?\d\s*\)", " ", texts[source])  # drop "(8GX2)" kit notes
        sizes = _distinct(int(m.group(1)) for m in re.finditer(
            r"(?<![\d.])(\d{1,3})\s?GB?\b(?!\s*(?:SSD|PCIe|NVMe|M\.2|eMMC|GDDR|VRAM|storage|HDD))", text, re.I)
            if int(m.group(1)) in LAPTOP_RAM_SIZES)
        if len(sizes) == 1:
            return _value(sizes[0], "regex", f"{source}: {sizes[0]}GB")
        if len(sizes) > 1 and source == "title":
            return {"conflict": sizes}
    return None


def extract_laptop_storage(texts, category):
    return _number(r"\b(\d(?:\.\d)?)\s?TB\b(?:\s*(?:PCIe|NVMe|M\.2|SSD))?|\b(\d{3,4})\s?GB\s*(?:PCIe\s*)?(?:NVMe\s*)?(?:M\.2\s*)?SSD",
                   "storage_gb", texts, sources=("title", "description"),
                   transform=lambda m: _gb(m.group(1), "TB") if m.group(1) else int(m.group(2)),
                   allow_multiple_in=())


def extract_integrated_graphics(texts, category):
    """Whether the CPU can drive a display without a graphics card, from its model-number suffix."""
    pattern = r"\bI[3579]-?1[234]\d{3}([A-Z]*)|ULTRA\s*[3579]\s*2\d{2}([A-Z]*)"
    # The variant names the exact model when one listing covers several ("14700KF / 14700K").
    title = texts["variant"].upper() if re.search(pattern, texts["variant"].upper()) else texts["title"].upper()
    intel = re.findall(pattern, title)
    if intel:
        suffixes = {a or b for a, b in intel}
        has = {"F" not in s for s in suffixes}
        return _value(has.pop(), "rule_inferred", "Intel 'F' models have no integrated graphics") if len(has) == 1 else {"conflict": sorted(suffixes)}
    ryzen = re.search(r"RYZEN\S*\s+\d\s+(\d)\d{3}([A-Z0-9]*)", title)
    if ryzen:
        series, suffix = int(ryzen.group(1)), ryzen.group(2)
        if series in (7, 8, 9):
            return _value("F" not in suffix, "rule_inferred", f"Ryzen {series}000: integrated graphics unless an F model")
        return _value("G" in suffix, "rule_inferred", f"Ryzen {series}000: integrated graphics only on G models")
    return None


EXTRACTORS: dict[tuple[str, str], Callable] = {
    ("cpu", "integrated_graphics"): extract_integrated_graphics,
    ("cpu", "socket"): extract_socket, ("motherboard", "socket"): extract_socket,
    ("cpu", "tdp_w"): extract_tdp, ("gpu", "tdp_w"): extract_tdp,
    ("motherboard", "memory_type"): extract_memory_type, ("ram", "memory_type"): extract_memory_type,
    ("motherboard", "form_factor"): extract_form_factor, ("case", "max_form_factor"): extract_form_factor,
    ("gpu", "chip"): extract_chip, ("gpu", "vram_gb"): extract_vram, ("gpu", "length_mm"): extract_gpu_length,
    ("case", "max_gpu_length_mm"): extract_case_gpu_clearance, ("psu", "wattage_w"): extract_wattage,
    ("ram", "capacity_gb"): extract_ram_capacity, ("ssd", "capacity_gb"): extract_storage_capacity,
    ("laptop", "ram_gb"): extract_laptop_ram, ("laptop", "storage_gb"): extract_laptop_storage,
}


# ---------------------------------------------------------------------------- LLM tier

class LLMSpecExtractor:
    """Asks the local model for fields regex could not find, with a verbatim-evidence guard."""

    def __init__(self, model, cache_path: Path | None = None):
        self.model = model
        self.cache_path = cache_path
        self.cache: dict[str, dict] = {}
        if cache_path and cache_path.exists():
            for line in cache_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    self.cache[row["key"]] = row["answer"]

    def _ask(self, category: str, fields: list[str], text: str) -> dict:
        from pydantic import BaseModel

        class FieldAnswer(BaseModel):
            field: str
            value: str | None
            evidence_quote: str | None

        class Answer(BaseModel):
            answers: list[FieldAnswer]

        system = ("You extract PC hardware specifications from a product listing. For each requested field "
                  "return the value ONLY if the listing states it explicitly, and copy the exact sentence "
                  "fragment that states it into evidence_quote. If the listing does not state it, return "
                  "value null and evidence_quote null. Never guess or use outside knowledge. Units: watts for "
                  "power, millimetres for lengths, GB for memory and storage; form factors as E-ATX, ATX, "
                  "Micro-ATX or Mini-ITX; memory type as DDR4 or DDR5.")
        result = self.model.structured(system, {"category": category, "fields": fields, "listing": text}, Answer)
        return {a.field: {"value": a.value, "evidence_quote": a.evidence_quote} for a in result.answers}

    def extract(self, category: str, fields: list[str], text: str) -> dict[str, dict]:
        key = hashlib.sha256(json.dumps([category, sorted(fields), text]).encode()).hexdigest()
        if key not in self.cache:
            try:
                answer = self._ask(category, fields, text)
            except Exception as exc:  # model unavailable or malformed output: leave fields missing
                return {"_error": {"message": type(exc).__name__}}
            self.cache[key] = answer
            if self.cache_path:
                with self.cache_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"key": key, "answer": answer}, ensure_ascii=False) + "\n")
        accepted = {}
        for field in fields:
            raw = (self.cache[key] or {}).get(field) or {}
            value = parse_llm_value(field, raw.get("value"))
            quote = (raw.get("evidence_quote") or "").strip()
            if value is None or not quote:
                continue
            if not evidence_supports(quote, value, text, field):
                continue
            accepted[field] = _value(value, "llm", quote, "medium")
        return accepted


def parse_llm_value(field: str, raw):
    if raw is None:
        return None
    text = str(raw).strip()
    if field in ("memory_type",):
        m = re.search(r"DDR[45]", text, re.I)
        return m.group(0).upper() if m else None
    if field in ("form_factor", "max_form_factor"):
        m = FORM_RE.search(text)
        return _form(m.group(1)) if m else None
    m = re.search(r"\d+(?:\.\d+)?", text.replace(",", ""))
    if not m:
        return None
    number = float(m.group(0))
    if field in ("ram_gb", "storage_gb", "capacity_gb") and re.search(r"TB", text, re.I):
        number *= 1024
    value = int(round(number))
    return value if _in_range(field, value) else None


# The quoted evidence must talk about the right thing: "up to 240mm radiator" is not a GPU length.
FIELD_KEYWORDS = {
    "max_gpu_length_mm": r"gpu|graphic|vga|card",
    "length_mm": r"length|long|dimension|size|\d\s?[x×]\s?\d",
    "tdp_w": r"tdp|power|watt",
    "wattage_w": r"watt|\d\s?w\b|power",
    "ram_gb": r"ram|memory|ddr",
    "storage_gb": r"ssd|storage|nvme|m\.2|tb",
    "memory_type": r"ddr",
    "form_factor": r"atx|itx",
    "max_form_factor": r"atx|itx",
}
FIELD_EXCLUDE = r"radiator|\bfans?\b|cooler|aio|psu length|power supply length"


def evidence_supports(quote: str, value, text: str, field: str | None = None) -> bool:
    """The quote must appear in the listing, must contain the value itself and must be about the field."""
    squash = lambda s: re.sub(r"\s+", " ", s).casefold()
    if squash(quote) not in squash(text):
        return False
    if field in FIELD_KEYWORDS and not re.search(FIELD_KEYWORDS[field], quote, re.I):
        return False
    if field in ("max_gpu_length_mm", "length_mm") and re.search(FIELD_EXCLUDE, quote, re.I) \
            and not re.search(r"gpu|graphic|vga|video card", quote, re.I):
        return False
    if isinstance(value, int):
        candidates = {str(value)}
        if value % 1024 == 0:
            candidates.add(str(value // 1024))
        return any(re.search(rf"(?<!\d){c}(?!\d)", quote) for c in candidates)
    return value.replace("-", "").casefold() in quote.replace("-", "").replace(" ", "").casefold() or \
        value.casefold() in quote.casefold()


# ---------------------------------------------------------------------------- per-offer

def _clean(text: str) -> str:
    return re.sub(r"[™®©]", "", text or "")


def texts_of(row: dict) -> dict[str, str]:
    variant = row.get("variant") or ""
    return {"variant": "" if variant.casefold() == "default title" else _clean(variant),
            "title": _clean(row.get("name")), "description": _clean(row.get("description"))[:6000]}


def bundle_suspect(row: dict) -> bool:
    """Component listings that actually bundle a CPU with a motherboard (or similar)."""
    if row.get("category") not in ("motherboard", "cpu", "gpu"):
        return False
    title = (row.get("name") or "").upper()
    has_cpu = bool(re.search(r"RYZEN|CORE\s?(?:I[3579]|ULTRA)|PROCESSOR", title))
    has_board = bool(re.search(r"MOTHERBOARD|\b[ABHXZ]\d{3}E?\b", title))
    has_gpu = bool(re.search(r"\bRTX\s?\d{4}|\bRX\s?\d{4}", title))
    return sum((has_cpu, has_board, has_gpu)) >= 2 and ("+" in title or "&" in title or "AND " in title)


def extract_offer(row: dict, llm: LLMSpecExtractor | None = None) -> dict | None:
    category = row.get("category")
    if category not in FIELDS:
        return None
    texts = texts_of(row)
    specs, missing, conflicts = {}, [], {}
    for field in FIELDS[category]:
        result = EXTRACTORS[(category, field)](texts, category)
        if result and "conflict" in result:
            conflicts[field] = result["conflict"]
            specs[field] = None
        elif result:
            specs[field] = result
        else:
            specs[field] = None
    if llm:
        wanted = [f for f in LLM_FIELDS.get(category, []) if specs.get(f) is None and f not in conflicts]
        if wanted:
            source = f"{texts['title']}\n{texts['variant']}\n{texts['description'][:2500]}"
            for field, value in llm.extract(category, wanted, source).items():
                if field in wanted:
                    specs[field] = value
    missing = [f for f, v in specs.items() if v is None and f not in conflicts]
    return {"offer_id": row["id"], "product_id": str(row.get("product_id") or row["id"]),
            "category": category, "store": row.get("store"), "name": row.get("name"),
            "specs": specs, "flags": {"missing": missing, "conflict": conflicts, "possible_same_product": [],
                                      "bundle_suspect": bundle_suspect(row)}}


def link_possible_duplicates(records: list[dict], rows: list[dict]) -> None:
    """Flag offers from different stores that share a SKU. They are never merged automatically."""
    by_id = {r["id"]: r for r in rows}
    groups = defaultdict(list)
    for rec in records:
        sku = (by_id[rec["offer_id"]].get("sku") or "").strip().upper()
        if len(sku) >= 6:
            groups[sku].append(rec)
    for group in groups.values():
        if len({g["store"] for g in group}) > 1:
            for rec in group:
                rec["flags"]["possible_same_product"] = [g["offer_id"] for g in group if g is not rec and g["store"] != rec["store"]]


def build_report(records: list[dict]) -> dict:
    report = {}
    for category, fields in FIELDS.items():
        recs = [r for r in records if r["category"] == category]
        if not recs:
            continue
        per_field = {}
        for field in fields:
            methods = Counter((r["specs"][field] or {}).get("method", "missing") for r in recs)
            conflicts = sum(1 for r in recs if field in r["flags"]["conflict"])
            found = len(recs) - methods.get("missing", 0)
            per_field[field] = {"found": found, "coverage": round(found / len(recs), 3),
                                "by_method": {k: v for k, v in methods.items() if k != "missing"},
                                "missing": methods.get("missing", 0) - conflicts, "conflict": conflicts}
        report[category] = {"offers": len(recs), "fields": per_field}
    report["_cross_store_possible_duplicates"] = sum(1 for r in records if r["flags"]["possible_same_product"])
    report["_bundle_suspects"] = sum(1 for r in records if r["flags"]["bundle_suspect"])
    return report


def run(snapshot_dir: Path, use_llm: bool = True, model=None) -> dict:
    rows = [json.loads(line) for line in (snapshot_dir / "prices.jsonl").read_text(encoding="utf-8").split("\n") if line.strip()]
    llm = None
    if use_llm:
        if model is None:
            from backend.model_gateway import ModelGateway
            from backend.settings import settings
            model = ModelGateway(settings.model_name, settings.model_provider, settings.ollama_base_url,
                                 settings.ollama_reasoning, settings.ollama_num_ctx, settings.ollama_num_predict)
        llm = LLMSpecExtractor(model, snapshot_dir / "spec_llm_cache.jsonl")
    records = []
    for i, row in enumerate(rows, 1):
        rec = extract_offer(row, llm)
        if rec:
            records.append(rec)
        if use_llm and i % 100 == 0:
            print(f"  processed {i}/{len(rows)} offers", flush=True)
    link_possible_duplicates(records, rows)
    with (snapshot_dir / "specs.jsonl").open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    report = build_report(records)
    report["_llm_enabled"] = use_llm
    (snapshot_dir / "spec_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def latest_snapshot(data_dir: Path) -> Path:
    pointer = json.loads((data_dir / "latest.json").read_text(encoding="utf-8"))
    return data_dir / pointer["path"]


def main():
    from backend.settings import settings
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--snapshot", help="snapshot folder; defaults to data/latest.json")
    parser.add_argument("--no-llm", action="store_true", help="skip the LLM tier")
    args = parser.parse_args()
    folder = Path(args.snapshot) if args.snapshot else latest_snapshot(settings.data_dir)
    report = run(folder, use_llm=not args.no_llm)
    for category, info in report.items():
        if category.startswith("_"):
            continue
        cov = ", ".join(f"{f}={v['coverage']:.0%}" for f, v in info["fields"].items())
        print(f"{category:12s} {info['offers']:4d} offers  {cov}")
    print(f"wrote {folder / 'specs.jsonl'} and spec_report.json")


if __name__ == "__main__":
    main()
