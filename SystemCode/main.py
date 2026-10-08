from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from crawler.api import router as data_router
from backend.api import create_router
from backend.orchestrator import RecommendationEngine
from backend.retrieval import LocalCorpus
from backend.settings import settings
from backend.store import StateStore


store = StateStore(settings.state_db)
store.reconcile_uncertain_tools()
if settings.retrieval_backend == "neo4j_milvus":
    from backend.production_retrieval import Neo4jMilvusCorpus
    corpus = Neo4jMilvusCorpus(settings.data_dir, settings)
else:
    corpus = LocalCorpus(settings.data_dir)
engine = RecommendationEngine(store, corpus, settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="BuildRig Recommendation API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(data_router)
app.include_router(create_router(store, corpus, engine))


@app.get("/")
async def root():
    return {"service": "BuildRig Recommendation API", "docs": "/docs", "health": "/api/v1/health"}


@app.get("/hello/{name}")
async def say_hello(name: str):
    return {"message": f"Hello {name}"}
