import re
from copy import deepcopy
from langchain_core.runnables import RunnableLambda


def _parse(payload: dict) -> dict:
    text = payload["text"]
    req = deepcopy(payload.get("current") or {})
    lower = text.casefold()
    normalized = re.sub(r"(?<=\d),(?=\d)", "", lower)
    if any(x in lower for x in ("笔记本", "laptop", "notebook")):
        req["device_type"] = "laptop"
    elif any(x in lower for x in ("台式", "组装", "装机", "desktop", "build a pc")):
        req["device_type"] = "desktop"
    money = (re.search(r"(?:预算|budget|max(?:imum)? budget|under|up to|maximum)\s*(?:是|为|is|:)?\s*(?:s\$|sgd|\$)?\s*([1-9]\d{2,5})(?:\.\d{1,2})?", normalized)
             or re.search(r"(?:s\$|sgd|\$)\s*([1-9]\d{2,5})(?:\.\d{1,2})?", normalized)
             or re.search(r"([1-9]\d{2,5})(?:\.\d{1,2})?\s*(?:新币|新加坡元|sgd)", normalized))
    if not money and not req.get("budget"):
        money = re.fullmatch(r"\s*(?:s\$|sgd|\$)?\s*([1-9]\d{2,5})(?:\.\d{1,2})?\s*(?:sgd)?\s*", normalized)
    if money:
        req["budget"] = {"currency": "SGD", "maximum_minor": int(money.group(1)) * 100, "is_hard_limit": True}
    ram = re.search(r"(\d{1,3})\s*gb\s*(?:内存|ram)?", lower)
    if ram:
        req.setdefault("hard_constraints", {})["minimum_memory_gb"] = int(ram.group(1))
    workloads = req.setdefault("workloads", [])
    for name in ("SolidWorks", "MATLAB", "ANSYS", "gaming", "video editing", "programming",
                 "machine learning", "university", "3D modelling", "编程", "游戏", "办公"):
        if name.casefold() in lower and name not in workloads:
            workloads.append(name)
    req.setdefault("locked_product_ids", [])
    req.setdefault("preferences", [])
    req.setdefault("cost_scope", {"monitor": "unspecified", "delivery": "unspecified", "assembly": "unspecified", "operating_system": "unspecified"})
    if "不包含显示器" in text or "without monitor" in lower:
        req["cost_scope"]["monitor"] = "excluded"
    return req


requirement_chain = RunnableLambda(_parse)


def parse_requirements(text: str, current: dict) -> tuple[dict, list[dict]]:
    req = requirement_chain.invoke({"text": text, "current": current})
    questions = []
    if not req.get("device_type"):
        questions.append({
            "question_id": "q_device_type",
            "text": "Would you like a desktop PC or a laptop?",
            "options": [
                {"label": "Desktop PC", "value": "I want a desktop PC."},
                {"label": "Laptop", "value": "I want a laptop."},
            ],
        })
    if not req.get("budget"):
        questions.append({
            "question_id": "q_budget",
            "text": "What is your maximum budget in Singapore dollars?",
            "options": [
                {"label": "S$1,200", "value": "My maximum budget is S$1,200."},
                {"label": "S$1,800", "value": "My maximum budget is S$1,800."},
                {"label": "S$2,500", "value": "My maximum budget is S$2,500."},
                {"label": "S$3,500", "value": "My maximum budget is S$3,500."},
            ],
        })
    return req, questions
