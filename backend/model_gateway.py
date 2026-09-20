import json
from typing import Type
from pydantic import BaseModel


class ModelGateway:
    """Optional LangChain boundary for local chat and structured output."""
    def __init__(self, model_name: str | None, provider: str | None = None,
                 base_url: str | None = None, reasoning: bool = False,
                 num_ctx: int = 8192, num_predict: int = 1200):
        self.model_name, self.provider = model_name, provider
        self.base_url = base_url
        self.reasoning = reasoning
        self.num_ctx = num_ctx
        self.num_predict = num_predict
        self._model = None

    @property
    def enabled(self) -> bool:
        return bool(self.model_name)

    def _load(self):
        if self._model is None:
            if (self.provider or "").lower() == "ollama":
                from langchain_ollama import ChatOllama
                self._model = ChatOllama(
                    model=self.model_name,
                    base_url=self.base_url or "http://127.0.0.1:11434",
                    temperature=0,
                    reasoning=self.reasoning,
                    num_ctx=self.num_ctx,
                    num_predict=self.num_predict,
                )
                return self._model
            from langchain.chat_models import init_chat_model
            kwargs = {"model_provider": self.provider} if self.provider else {}
            self._model = init_chat_model(self.model_name, temperature=0, **kwargs)
        return self._model

    def structured(self, system: str, payload: dict, schema: Type[BaseModel]) -> BaseModel:
        model = self._load().with_structured_output(schema)
        return model.invoke([("system", system), ("user", json.dumps(payload, ensure_ascii=False))])

    def text(self, system: str, payload: dict) -> str:
        response = self._load().invoke([
            ("system", system),
            ("user", json.dumps(payload, ensure_ascii=False)),
        ])
        content = response.content
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return "".join(
                str(part.get("text", "")) if isinstance(part, dict) else str(part)
                for part in content
            ).strip()
        return str(content).strip()
