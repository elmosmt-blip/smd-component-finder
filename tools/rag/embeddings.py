"""Pluggable embedding backends.

Out of the box the index needs nothing: retrieval runs on BM25 over SQLite
FTS5, which is fast, dependency-free and works offline. Dense vectors are an
optional second retrieval channel — useful when the query uses different words
than the datasheet ("how much current can it switch" vs "Collector Current
IC = 200 mA").

Backends:
  none                 — BM25 only (default)
  sentence-transformers— local model, needs HuggingFace access on first run
  openai               — any OpenAI-compatible /embeddings endpoint, needs a key
                         and network (works with Ollama, vLLM, LM Studio too)

Configure with --embed <name> or the env vars below.
"""

from __future__ import annotations

import os
from typing import List, Optional

import numpy as np


class EmbeddingError(RuntimeError):
    pass


class BaseBackend:
    name = "none"
    dim = 0

    def encode(self, texts: List[str]) -> np.ndarray:
        raise EmbeddingError("no embedding backend configured")

    def describe(self) -> str:
        return "%s (dim=%d)" % (self.name, self.dim)


class NoneBackend(BaseBackend):
    name = "none"

    def encode(self, texts: List[str]) -> np.ndarray:
        raise EmbeddingError("embeddings disabled")


class SentenceTransformerBackend(BaseBackend):
    name = "sentence-transformers"

    def __init__(self, model: Optional[str] = None):
        self.model_name = model or os.environ.get(
            "SMD_RAG_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
        )
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise EmbeddingError(
                    "sentence-transformers is not installed (pip install "
                    "sentence-transformers) and its weights come from "
                    "HuggingFace, which may be unreachable offline"
                ) from exc
            self._model = SentenceTransformer(self.model_name)
            self.dim = self._model.get_sentence_embedding_dimension() or 384
        return self._model

    def encode(self, texts: List[str]) -> np.ndarray:
        model = self._load()
        return np.asarray(model.encode(texts, show_progress_bar=False), dtype="float32")


class OpenAIBackend(BaseBackend):
    """Works with OpenAI, Azure, Ollama, vLLM, LM Studio — anything that speaks
    POST {base}/embeddings with {"input": [...], "model": "..."}."""

    name = "openai"

    def __init__(self, model: Optional[str] = None, base_url: Optional[str] = None,
                 api_key: Optional[str] = None):
        self.model_name = model or os.environ.get("SMD_RAG_EMBED_MODEL", "text-embedding-3-small")
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL")
                         or os.environ.get("SMD_RAG_EMBED_URL") or "https://api.openai.com/v1")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.dim = 1536

    def encode(self, texts: List[str]) -> np.ndarray:
        import json
        import urllib.request

        if not self.api_key:
            raise EmbeddingError("OPENAI_API_KEY is not set")
        payload = json.dumps({"input": texts, "model": self.model_name}).encode()
        req = urllib.request.Request(
            self.base_url.rstrip("/") + "/embeddings",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer %s" % self.api_key,
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
        rows = [item["embedding"] for item in data["data"]]
        return np.asarray(rows, dtype="float32")


def get_backend(preferred: str = "auto", **kwargs) -> BaseBackend:
    preferred = preferred or "none"
    if preferred == "none":
        return NoneBackend()
    if preferred == "sentence-transformers" or preferred == "st":
        return SentenceTransformerBackend(**kwargs)
    if preferred == "openai":
        return OpenAIBackend(**kwargs)
    if preferred == "auto":
        for factory in (SentenceTransformerBackend, OpenAIBackend):
            try:
                backend = factory(**kwargs)
                backend.encode(["probe"])
                return backend
            except Exception:
                continue
        return NoneBackend()
    raise EmbeddingError("unknown embedding backend: %s" % preferred)
