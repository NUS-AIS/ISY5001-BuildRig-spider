"""Read-only dependency preflight for the selected production profile."""
import json
from urllib.request import urlopen

from backend.settings import settings


def check_dependencies() -> dict:
    checks: dict[str, dict] = {}

    try:
        with urlopen(settings.ollama_base_url.rstrip("/") + "/api/tags", timeout=5) as response:
            models = {row["name"] for row in json.loads(response.read()).get("models", [])}
        chat_ok = any(name == settings.model_name or name.startswith(f"{settings.model_name}:") for name in models)
        embedding_ok = any(name == settings.embedding_model or name.startswith(f"{settings.embedding_model}:") for name in models)
        checks["ollama"] = {"ok": chat_ok and embedding_ok, "chat_model": chat_ok,
                            "embedding_model": embedding_ok}
    except Exception as exc:
        checks["ollama"] = {"ok": False, "error": str(exc)}

    try:
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))
        driver.verify_connectivity()
        records, _, _ = driver.execute_query("RETURN 1 AS ok", database_=settings.neo4j_database)
        driver.close()
        checks["neo4j"] = {"ok": bool(records and records[0]["ok"] == 1),
                            "database": settings.neo4j_database}
    except Exception as exc:
        checks["neo4j"] = {"ok": False, "error": str(exc)}

    try:
        from pymilvus import MilvusClient
        connection = {"uri": settings.milvus_uri, "db_name": settings.milvus_database}
        if settings.milvus_token:
            connection["token"] = settings.milvus_token
        elif settings.milvus_user:
            connection["user"] = settings.milvus_user
            connection["password"] = settings.milvus_password
        client = MilvusClient(**connection)
        collections = client.list_collections()
        checks["milvus"] = {"ok": True, "database": settings.milvus_database,
                            "collection_exists": settings.milvus_collection in collections}
        client.close()
    except Exception as exc:
        checks["milvus"] = {"ok": False, "error": str(exc)}

    try:
        with urlopen(settings.pi_runtime_url.rstrip("/") + "/health", timeout=5) as response:
            pi = json.loads(response.read())
        checks["pi_runtime"] = {"ok": pi.get("status") == "ok", "runtime": pi.get("runtime")}
    except Exception as exc:
        checks["pi_runtime"] = {"ok": False, "error": str(exc)}

    return {"ok": all(item["ok"] for item in checks.values()), "checks": checks}


if __name__ == "__main__":
    result = check_dependencies()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["ok"] else 1)
