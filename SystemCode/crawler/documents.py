"""LangChain documents; keep numeric budget filtering outside vector search."""
import json
from pathlib import Path
from langchain_core.documents import Document


def load_documents(data_dir=None):
    root = Path(data_dir) if data_dir else Path(__file__).resolve().parents[1] / 'data'
    folder = root / json.loads((root / 'latest.json').read_text(encoding='utf-8'))['path']
    for kind in ('prices', 'reviews'):
        for line in (folder / (kind + '.jsonl')).read_text(encoding='utf-8').split('\n'):
            if not line.strip():
                continue
            row = json.loads(line)
            yield Document(page_content=json.dumps(row, ensure_ascii=False),
                           metadata={'source': row['source_url'], 'id': row['id'],
                                     'kind': kind, 'category': row['category'],
                                     'collected_at': row['collected_at']})
