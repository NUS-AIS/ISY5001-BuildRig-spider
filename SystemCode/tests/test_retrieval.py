import unittest
from pathlib import Path

from backend.retrieval import LocalCorpus, chunk_text, rrf_fuse

DATA = Path(__file__).resolve().parents[1] / "data"


class ChunkingTests(unittest.TestCase):
    def test_short_text_is_one_chunk(self):
        self.assertEqual(chunk_text("A short description."), ["A short description."])

    def test_long_text_is_split_with_overlap_and_nothing_lost(self):
        text = " ".join(f"Sentence number {i} about the product." for i in range(200))
        chunks = chunk_text(text, size=500, overlap=80)
        self.assertGreater(len(chunks), 5)
        self.assertTrue(all(len(c) <= 500 for c in chunks))
        self.assertIn("Sentence number 199", chunks[-1])
        self.assertIn(chunks[0][-40:].strip()[-20:], text)

    def test_every_chunk_belongs_to_exactly_one_listing_and_repeats_its_specs(self):
        corpus = LocalCorpus(DATA)
        offer = next(r for r in corpus.prices if len(r.get("description") or "") > 3000 and corpus.specs.get(r["id"]))
        chunks = [d for d in corpus.documents if d.metadata["evidence_id"] == f"price:{offer['id']}"]
        self.assertGreater(len(chunks), 1)
        self.assertEqual(len({d.metadata["chunk_id"] for d in chunks}), len(chunks))
        for chunk in chunks:
            self.assertTrue(chunk.page_content.startswith(offer["name"]))
            self.assertIn("Specifications:", chunk.page_content)


class FusionTests(unittest.TestCase):
    def test_rrf_uses_ranks_and_best_chunk_per_item(self):
        hits = {
            "bm25": [("a", "a1", {}), ("a", "a2", {}), ("b", "b1", {})],
            "dense": [("b", "b1", {}), ("c", "c1", {})],
            "graph": [("b", "b1", {})],
        }
        fused = rrf_fuse(hits, top_k=3)
        self.assertEqual([x["evidence_id"] for x in fused], ["b", "a", "c"])
        self.assertEqual(fused[0]["route_ranks"], {"bm25": 2, "dense": 1, "graph": 1})
        self.assertEqual(fused[1]["route_ranks"], {"bm25": 1})   # the second chunk of "a" does not count twice

    def test_local_search_supports_route_ablation(self):
        corpus = LocalCorpus(DATA)
        for routes in (["dense"], ["bm25", "dense"], ["bm25", "dense", "graph"]):
            result = corpus.search("AM5 DDR5 motherboard", routes=routes, top_k=5)
            self.assertTrue(result["items"])
            used = {r for item in result["items"] for r in item["route_ranks"]}
            self.assertTrue(used <= set(routes), (routes, used))


if __name__ == "__main__":
    unittest.main()
