import json
import math
import re
from collections import Counter
from hashlib import sha256
from pathlib import Path
from typing import Any
from langchain_core.documents import Document


TOKEN = re.compile(r"[a-z0-9][a-z0-9._+-]*|[\u4e00-\u9fff]", re.I)


def tokens(text: str) -> list[str]:
    return TOKEN.findall(text.casefold())


def vector(text: str, dimensions: int = 256) -> list[float]:
    values = [0.0] * dimensions
    for term in tokens(text):
        digest = sha256(term.encode()).digest()
        values[int.from_bytes(digest[:4], "big") % dimensions] += 1 if digest[4] & 1 else -1
    norm = math.sqrt(sum(x * x for x in values)) or 1
    return [x / norm for x in values]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


class LocalCorpus:
    """Runnable fallback that preserves the production retrieval contract."""
    def __init__(self, data_dir: Path):
        pointer = json.loads((data_dir / "latest.json").read_text(encoding="utf-8"))
        self.snapshot_id = pointer["run_id"]
        folder = data_dir / pointer["path"]
        self.prices = self._read(folder / "prices.jsonl")
        self.reviews = self._read(folder / "reviews.jsonl")
        # Structured specs from backend.ingest.specs; absent specs simply mean "unknown".
        spec_path = folder / "specs.jsonl"
        spec_rows = self._read(spec_path) if spec_path.exists() else []
        self.specs = {s["offer_id"]: {k: (v or {}).get("value") for k, v in s["specs"].items()} for s in spec_rows}
        self.spec_meta = {s["offer_id"]: s for s in spec_rows}
        self.flags = {s["offer_id"]: s["flags"] for s in spec_rows}
        self.by_id = {row["id"]: row for row in self.prices}
        self.documents = self._documents()
        self.df = Counter(term for d in self.documents for term in set(tokens(d.page_content)))
        self.avg_len = sum(len(tokens(d.page_content)) for d in self.documents) / max(len(self.documents), 1)

    @staticmethod
    def _read(path: Path) -> list[dict]:
        # JSON strings may legally contain U+2028/U+2029. splitlines() would
        # incorrectly split those characters inside a product description.
        return [json.loads(line) for line in path.read_text(encoding="utf-8").split("\n") if line.strip()]

    def _documents(self) -> list[Document]:
        docs = []
        for row in self.prices:
            text = " ".join(str(row.get(k) or "") for k in ("name", "brand", "category", "description"))
            docs.append(Document(page_content=text, metadata={"evidence_id": f"price:{row['id']}", "kind": "offer", "product_id": str(row.get("product_id") or row["id"]), "category": row["category"], "source_url": row["source_url"], "snapshot_id": self.snapshot_id, "record": row}))
        for row in self.reviews:
            text = " ".join(str(row.get(k) or "") for k in ("product_name", "category", "text"))
            docs.append(Document(page_content=text, metadata={"evidence_id": f"review:{row['id']}", "kind": "review", "product_id": str(row.get("product_id") or ""), "category": row["category"], "source_url": row["source_url"], "snapshot_id": self.snapshot_id, "record": row}))
        return docs

    def enrich(self, row: dict) -> dict:
        """Attach the extracted specs and data-quality flags to a catalogue row."""
        return {**row, "specs": self.specs.get(row["id"], {}), "flags": self.flags.get(row["id"], {})}

    def offer(self, offer_id: str) -> dict | None:
        row = self.by_id.get(offer_id)
        return self.enrich(row) if row else None

    def products(self, product_ids: list[str]) -> list[dict]:
        wanted = {str(p) for p in product_ids}
        rows = [r for r in self.prices if str(r.get("product_id") or r["id"]) in wanted and r.get("available") is True]
        return [self.enrich(r) for r in sorted(rows, key=lambda x: float(x["price"]))]

    def candidates(self, category: str, maximum_minor: int | None = None, limit: int = 25,
                   minimum_minor: int | None = None, require: dict | None = None,
                   minimum_specs: dict | None = None, exclude_ids: list[str] | None = None,
                   order: str = "price_asc") -> list[dict]:
        """In-stock single-component offers, optionally filtered by spec equality (``require``) and
        spec lower bounds (``minimum_specs``). Offers whose spec is unknown are kept after the known
        matches, so a missing spec never silently counts as compatible."""
        excluded = set(exclude_ids or [])
        rows = [r for r in self.prices if r["category"] == category and r.get("available") is True
                and r["id"] not in excluded and not self.flags.get(r["id"], {}).get("bundle_suspect")]
        price = lambda r: int(round(float(r["price"]) * 100))
        if maximum_minor is not None:
            rows = [r for r in rows if price(r) <= maximum_minor]
        if minimum_minor is not None:
            rows = [r for r in rows if price(r) >= minimum_minor]
        known, unknown = [], []
        for row in rows:
            specs = self.specs.get(row["id"], {})
            verdicts = [specs.get(k) == v if specs.get(k) is not None else None for k, v in (require or {}).items()]
            verdicts += [specs.get(k) >= v if specs.get(k) is not None else None for k, v in (minimum_specs or {}).items()]
            if False in verdicts:
                continue
            (unknown if None in verdicts else known).append(row)
        key = {"price_asc": lambda r: float(r["price"]), "price_desc": lambda r: -float(r["price"])}[order]
        ranked = sorted(known, key=key) + sorted(unknown, key=key)
        return [self.enrich(r) for r in ranked[:limit]]

    def search(self, query: str, categories: list[str] | None = None, product_ids: list[str] | None = None, top_k: int = 8) -> dict[str, Any]:
        docs = [d for d in self.documents if (not categories or d.metadata["category"] in categories)
                and (not product_ids or d.metadata["product_id"] in product_ids)]
        qterms, qvec = tokens(query), vector(query)
        n = max(len(self.documents), 1)
        bm25, dense, graph = [], [], []
        for d in docs:
            terms = tokens(d.page_content); counts = Counter(terms); dl = len(terms)
            score = 0.0
            for term in qterms:
                df = self.df.get(term, 0)
                idf = math.log(1 + (n - df + .5) / (df + .5))
                tf = counts.get(term, 0)
                score += idf * tf * 2.2 / (tf + 1.2 * (1 - .75 + .75 * dl / max(self.avg_len, 1))) if tf else 0
            bm25.append((score, d)); dense.append((cosine(qvec, vector(d.page_content)), d))
            graph_score = (2 if d.metadata["product_id"] in (product_ids or []) else 0) + (1 if d.metadata["category"] in (categories or []) else 0)
            graph.append((graph_score, d))
        routes = {"bm25": sorted(bm25, key=lambda x: x[0], reverse=True)[:top_k * 2],
                  "vector": sorted(dense, key=lambda x: x[0], reverse=True)[:top_k * 2],
                  "graph": sorted(graph, key=lambda x: x[0], reverse=True)[:top_k * 2]}
        fused: dict[str, dict] = {}
        for route, ranked in routes.items():
            for rank, (raw, doc) in enumerate(ranked, 1):
                if raw <= 0:
                    continue
                key = doc.metadata["evidence_id"]
                hit = fused.setdefault(key, {"evidence_id": key, "score": 0.0, "route_ranks": {}, "document": doc})
                hit["score"] += 1 / (60 + rank); hit["route_ranks"][route] = rank
        items = sorted(fused.values(), key=lambda x: x["score"], reverse=True)[:top_k]
        return {"snapshot_id": self.snapshot_id, "retrieval_backend": "local_contract_fallback", "items": [
            {"evidence_id": x["evidence_id"], "score": x["score"], "route_ranks": x["route_ranks"],
             "kind": x["document"].metadata["kind"], "product_id": x["document"].metadata["product_id"],
             "category": x["document"].metadata["category"], "source_url": x["document"].metadata["source_url"],
             "excerpt": x["document"].page_content[:700]} for x in items]}
