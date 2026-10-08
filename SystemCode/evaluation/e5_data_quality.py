"""E5: data quality - specification coverage, LLM-tier reliability and compatibility judgements.

a) coverage of every extracted field, split by how it was obtained (regex / rule / LLM)
b) LLM tier versus rules: on values the rules already know, ask the LLM tier for the same field and
   measure agreement and how often the evidence guard rejects the model's answer
c) compatibility verdicts on configurations whose ground truth follows from the product names alone
   (platform generations), independent of the extracted specs
"""
from __future__ import annotations

import json
import random
import re
from pathlib import Path

from backend.ingest.specs import LLMSpecExtractor, texts_of
from backend.validation import validate_option

LLM_CHECKABLE = {"cpu": ["tdp_w"], "psu": ["wattage_w"], "motherboard": ["memory_type", "form_factor"],
                 "case": ["max_form_factor", "max_gpu_length_mm"], "laptop": ["ram_gb", "storage_gb"], "gpu": ["length_mm"]}


def coverage(snapshot_dir: Path) -> dict:
    return json.loads((snapshot_dir / "spec_report.json").read_text(encoding="utf-8"))


def llm_agreement(corpus, model, sample: int = 60, seed: int = 11) -> dict:
    random.seed(seed)
    pool = [(oid, meta["category"], field) for oid, meta in corpus.spec_meta.items()
            for field in LLM_CHECKABLE.get(meta["category"], [])
            if (meta["specs"].get(field) or {}).get("method") in ("regex", "rule_inferred")]
    picks = random.sample(pool, min(sample, len(pool)))
    extractor = LLMSpecExtractor(model)
    rows = []
    for oid, category, field in picks:
        row = corpus.by_id[oid]
        texts = texts_of(row)
        source = f"{texts['title']}\n{texts['variant']}\n{texts['description'][:2500]}"
        answer = extractor.extract(category, [field], source).get(field)
        truth = corpus.spec_meta[oid]["specs"][field]["value"]
        rows.append({"offer_id": oid, "category": category, "field": field, "rule_value": truth,
                     "llm_value": answer["value"] if answer else None, "accepted": answer is not None,
                     "agrees": answer is not None and answer["value"] == truth})
    accepted = [r for r in rows if r["accepted"]]
    return {"pairs": len(rows), "accepted_by_guard": len(accepted),
            "agreement_when_accepted": round(sum(r["agrees"] for r in accepted) / len(accepted), 3) if accepted else None,
            "abstained_or_rejected": len(rows) - len(accepted), "rows": rows}


def _family_socket(name: str) -> str | None:
    n = name.upper()
    if re.search(r"RYZEN\S*\s+\d\s+[789]\d{3}", n):
        return "AM5"
    if re.search(r"RYZEN\S*\s+\d\s+[35]\d{3}", n):
        return "AM4"
    if re.search(r"\bI[3579]-?1[234]\d{3}", n):
        return "LGA1700"
    if re.search(r"\b(?:A620|B650E?|X670E?|B840|B850|X870E?)\b", n):
        return "AM5"
    if re.search(r"\b(?:A520|B550|X570|B450)\b", n):
        return "AM4"
    if re.search(r"\b(?:H610|B660|B760|Z690|Z790)M?\b", n):
        return "LGA1700"
    return None


def compatibility_judgements(corpus, pairs: int = 40, seed: int = 5) -> dict:
    random.seed(seed)
    cpus = [r for r in corpus.candidates("cpu", limit=200) if _family_socket(r["name"])]
    boards = [r for r in corpus.candidates("motherboard", limit=300) if _family_socket(r["name"])]
    rams = [r for r in corpus.candidates("ram", limit=200) if re.search(r"DDR[45]", r["name"].upper())]
    ddr5_boards = [b for b in boards if _family_socket(b["name"]) == "AM5"]
    rows = []
    for _ in range(pairs // 2):
        cpu, board = random.choice(cpus), random.choice(boards)
        truth = "compatible" if _family_socket(cpu["name"]) == _family_socket(board["name"]) else "incompatible"
        verdict = validate_option({"items": [{**cpu, "category": "cpu", "price": cpu["price"], "specs": cpu["specs"]},
                                             {**board, "category": "motherboard", "price": board["price"], "specs": board["specs"]}],
                                   "required_categories": []}, {})
        status = next((c["status"] for c in verdict["checks"] if c["code"] == "cpu_motherboard_socket"), "unknown")
        rows.append({"rule": "cpu_motherboard_socket", "a": cpu["name"][:60], "b": board["name"][:60], "truth": truth, "verdict": status})
    for _ in range(pairs - pairs // 2):
        ram, board = random.choice(rams), random.choice(ddr5_boards)
        truth = "compatible" if "DDR5" in ram["name"].upper() else "incompatible"
        verdict = validate_option({"items": [{**board, "category": "motherboard", "price": board["price"], "specs": board["specs"]},
                                             {**ram, "category": "ram", "price": ram["price"], "specs": ram["specs"]}],
                                   "required_categories": []}, {})
        status = next((c["status"] for c in verdict["checks"] if c["code"] == "motherboard_memory_type"), "unknown")
        rows.append({"rule": "motherboard_memory_type", "a": ram["name"][:60], "b": board["name"][:60], "truth": truth, "verdict": status})
    decided = [r for r in rows if r["verdict"] != "unknown"]
    correct = [r for r in decided if (r["verdict"] == "passed") == (r["truth"] == "compatible")]
    false_pass = [r for r in rows if r["verdict"] == "passed" and r["truth"] == "incompatible"]
    return {"pairs": len(rows), "decided": len(decided), "accuracy_when_decided": round(len(correct) / len(decided), 3) if decided else None,
            "unknown_rate": round(1 - len(decided) / len(rows), 3), "false_passes": len(false_pass), "rows": rows}
