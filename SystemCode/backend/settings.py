from dataclasses import dataclass
from pathlib import Path
import os
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(item.strip() for item in os.getenv(name, default).split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(os.getenv("BUILDRIG_DATA_DIR", ROOT / "data"))
    state_db: Path = Path(os.getenv("BUILDRIG_STATE_DB", ROOT / "runtime" / "buildrig.db"))
    retrieval_backend: str = os.getenv("BUILDRIG_RETRIEVAL_BACKEND", "local")
    neo4j_uri: str = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user: str = os.getenv("NEO4J_USER", "neo4j")
    neo4j_password: str = os.getenv("NEO4J_PASSWORD", "")
    neo4j_database: str = os.getenv("NEO4J_DATABASE", "neo4j")
    milvus_uri: str = os.getenv("MILVUS_URI", "http://localhost:19530")
    milvus_token: str = os.getenv("MILVUS_TOKEN", "")
    milvus_user: str = os.getenv("MILVUS_USER", "")
    milvus_password: str = os.getenv("MILVUS_PASSWORD", "")
    milvus_database: str = os.getenv("MILVUS_DATABASE", "default")
    milvus_collection: str = os.getenv("MILVUS_COLLECTION", "buildrig_evidence")
    pi_runtime_url: str | None = os.getenv("BUILDRIG_PI_RUNTIME_URL")
    pi_runtime_timeout_seconds: int = int(os.getenv("BUILDRIG_PI_RUNTIME_TIMEOUT_SECONDS", "180"))
    internal_api_token: str = os.getenv("BUILDRIG_INTERNAL_API_TOKEN", "")
    model_name: str | None = os.getenv("BUILDRIG_MODEL")
    model_provider: str | None = os.getenv("BUILDRIG_MODEL_PROVIDER")
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    ollama_reasoning: bool = env_bool("BUILDRIG_OLLAMA_REASONING", False)
    ollama_num_ctx: int = int(os.getenv("BUILDRIG_OLLAMA_NUM_CTX", "8192"))
    ollama_num_predict: int = int(os.getenv("BUILDRIG_OLLAMA_NUM_PREDICT", "1200"))
    embedding_provider: str = os.getenv("BUILDRIG_EMBEDDING_PROVIDER", "hash")
    embedding_model: str | None = os.getenv("BUILDRIG_EMBEDDING_MODEL")
    embedding_dimensions: int = int(os.getenv("BUILDRIG_EMBEDDING_DIMENSIONS", "256"))
    embedding_batch_size: int = int(os.getenv("BUILDRIG_EMBEDDING_BATCH_SIZE", "32"))
    cors_origins: tuple[str, ...] = env_list(
        "BUILDRIG_CORS_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173"
    )
    max_revisions: int = int(os.getenv("BUILDRIG_MAX_REVISIONS", "3"))
    default_top_k: int = int(os.getenv("BUILDRIG_TOP_K", "8"))


settings = Settings()
