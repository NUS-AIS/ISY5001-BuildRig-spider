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

    def spec_text(self, offer_id: str) -> str:
        specs = {k: v for k, v in self.specs.get(offer_id, {}).items() if v is not None}
        return ("Specifications: " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in specs.items())) if specs else ""

    def _documents(self) -> list[Document]:
        """Searchable chunks. A chunk never mixes two products; every chunk of a listing repeats the
        title and extracted specs so keyword search can match "AM5" or "DDR5" in any chunk."""
        docs = []
        for row in self.prices:
            head = " ".join(str(row.get(k) or "") for k in ("name", "brand", "category")) + ". " + self.spec_text(row["id"])
            for n, body in enumerate(chunk_text(row.get("description") or "")):
                docs.append(Document(page_content=f"{head}\n{body}".strip(), metadata={
                    "chunk_id": f"price:{row['id']}#{n}", "evidence_id": f"price:{row['id']}", "kind": "offer",
                    "product_id": str(row.get("product_id") or row["id"]), "category": row["category"],
                    "source_url": row["source_url"], "match_level": "exact_offer", "snapshot_id": self.snapshot_id}))
        for row in self.reviews:
            head = " ".join(str(row.get(k) or "") for k in ("product_name", "category"))
            for n, body in enumerate(chunk_text(row.get("text") or "")):
                docs.append(Document(page_content=f"{head}\n{body}".strip(), metadata={
                    "chunk_id": f"review:{row['id']}#{n}", "evidence_id": f"review:{row['id']}", "kind": "review",
                    "product_id": str(row.get("product_id") or ""), "category": row["category"],
                    "source_url": row["source_url"], "match_level": row.get("match_level") or "unknown",
                    "snapshot_id": self.snapshot_id}))
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

    def search(self, query: str, categories: list[str] | None = None, product_ids: list[str] | None = None,
               top_k: int = 8, routes: list[str] | None = None) -> dict[str, Any]:
        """Development stand-in for hybrid retrieval: in-memory BM25, hashed vectors and a
        product-link "graph" route, fused with the same RRF as production."""
        routes = routes or list(ROUTES)
        docs = [d for d in self.documents if (not categories or d.metadata["category"] in categories)
                and (not product_ids or d.metadata["product_id"] in product_ids)]
        qterms, qvec = tokens(query), vector(query)
        n = max(len(self.documents), 1)
        ranked: dict[str, list[tuple[float, Document]]] = {}
        if "bm25" in routes:
            scored = []
            for d in docs:
                terms = tokens(d.page_content); counts = Counter(terms); dl = len(terms)
                score = 0.0
                for term in qterms:
                    df = self.df.get(term, 0)
                    idf = math.log(1 + (n - df + .5) / (df + .5))
                    tf = counts.get(term, 0)
                    score += idf * tf * 2.2 / (tf + 1.2 * (1 - .75 + .75 * dl / max(self.avg_len, 1))) if tf else 0
                scored.append((score, d))
            ranked["bm25"] = scored
        if "dense" in routes:
            ranked["dense"] = [(cosine(qvec, vector(d.page_content)), d) for d in docs]
        if "graph" in routes:
            ranked["graph"] = [((2 if d.metadata["product_id"] in (product_ids or []) else 0)
                                + (1 if d.metadata["category"] in (categories or []) else 0), d) for d in docs]
        hits = {route: [(doc.metadata["evidence_id"], doc.page_content, doc.metadata) for raw, doc in
                        sorted(rows, key=lambda x: x[0], reverse=True) if raw > 0] for route, rows in ranked.items()}
        items = rrf_fuse(hits, top_k)
        return {"snapshot_id": self.snapshot_id, "retrieval_backend": "local_contract_fallback",
                "routes": routes, "degraded_routes": [], "items": items}


ROUTES = ("bm25", "dense", "graph")


def chunk_text(text: str, size: int = 1200, overlap: int = 150) -> list[str]:
    """Split one listing's text into overlapping chunks on sentence-ish boundaries."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= size:
        return [text]
    chunks, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        cut = text.rfind(". ", start + size // 2, end)
        end = cut + 1 if cut != -1 and end < len(text) else end
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def rrf_fuse(route_hits: dict[str, list[tuple[str, str, dict]]], top_k: int, k: int = 60) -> list[dict]:
    """Reciprocal Rank Fusion over routes. Each route contributes the rank of its best chunk per
    evidence item; raw scores from different routes are never added together."""
    fused: dict[str, dict] = {}
    for route, hits in route_hits.items():
        seen: set[str] = set()
        rank = 0
        for evidence_id, text, meta in hits:
            if evidence_id in seen:
                continue
            seen.add(evidence_id)
            rank += 1
            hit = fused.setdefault(evidence_id, {
                "evidence_id": evidence_id, "score": 0.0, "route_ranks": {}, "kind": meta.get("kind"),
                "product_id": meta.get("product_id"), "category": meta.get("category"),
                "source_url": meta.get("source_url"), "match_level": meta.get("match_level"), "excerpt": text[:700]})
            hit["score"] += 1 / (k + rank)
            hit["route_ranks"][route] = rank
    return sorted(fused.values(), key=lambda x: x["score"], reverse=True)[:top_k]
