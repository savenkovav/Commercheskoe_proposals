from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass

from src.config import (
    RAG_CHUNK_OVERLAP,
    RAG_CHUNK_SIZE,
    RAG_EMBEDDING_MODEL,
    RAG_ENABLED,
    RAG_TOP_K,
)
from src.services.ai_agent import AIAgent
from src.services.models import TZItem

logger = logging.getLogger(__name__)


@dataclass
class RagIndex:
    chunks: list[dict[str, str | int | float]]
    vectors: list[list[float]]


class TZRagService:
    def __init__(self, ai: AIAgent) -> None:
        self.ai = ai

    def build_index(
        self,
        document_text: str,
        tz_items: list[TZItem],
        *,
        filename: str = "",
    ) -> RagIndex:
        if not RAG_ENABLED:
            return RagIndex(chunks=[], vectors=[])

        chunks = self._build_chunks(document_text, tz_items, filename=filename)
        if not chunks:
            return RagIndex(chunks=[], vectors=[])

        vectors = self._embed([str(chunk["text"]) for chunk in chunks])
        if vectors and len(vectors) == len(chunks):
            return RagIndex(chunks=chunks, vectors=vectors)
        return RagIndex(chunks=chunks, vectors=[])

    def retrieve_context(self, query: str, index: RagIndex, *, top_k: int = RAG_TOP_K) -> str:
        if not query.strip() or not index.chunks:
            return ""

        selected = self._retrieve(query, index, top_k=top_k)
        if not selected:
            return ""

        return "\n\n".join(
            f"[chunk {row['chunk_id']}] {row['text']}"
            for row in selected
            if row.get("text")
        )[:5000]

    def retrieve_debug(
        self,
        query: str,
        index: RagIndex,
        *,
        top_k: int = RAG_TOP_K,
    ) -> list[dict[str, str | int | float]]:
        if not query.strip() or not index.chunks:
            return []
        return self._retrieve_with_scores(query, index, top_k=top_k)

    def _retrieve(self, query: str, index: RagIndex, *, top_k: int) -> list[dict[str, str | int | float]]:
        scored = self._retrieve_with_scores(query, index, top_k=top_k)
        return [
            {key: value for key, value in row.items() if key != "score"}
            for row in scored
        ]

    def _retrieve_with_scores(
        self,
        query: str,
        index: RagIndex,
        *,
        top_k: int,
    ) -> list[dict[str, str | int | float]]:
        if index.vectors:
            query_vecs = self._embed([query])
            if query_vecs:
                qv = query_vecs[0]
                scored = [
                    (self._cosine(qv, vec), chunk)
                    for vec, chunk in zip(index.vectors, index.chunks)
                ]
                scored.sort(key=lambda item: item[0], reverse=True)
                return [
                    {
                        **chunk,
                        "score": round(score, 6),
                    }
                    for score, chunk in scored[:top_k]
                ]

        return self._retrieve_lexical(query, index.chunks, top_k=top_k)

    def _retrieve_lexical(
        self,
        query: str,
        chunks: list[dict[str, str | int | float]],
        *,
        top_k: int,
    ) -> list[dict[str, str | int | float]]:
        query_terms = self._tokenize(query)
        if not query_terms:
            return chunks[:top_k]

        scored: list[tuple[float, dict[str, str | int | float]]] = []
        for chunk in chunks:
            text = str(chunk.get("text", ""))
            chunk_terms = self._tokenize(text)
            overlap = len(query_terms & chunk_terms)
            density = overlap / max(1, len(query_terms))
            scored.append((density, chunk))

        scored.sort(key=lambda item: item[0], reverse=True)
        ranked = [
            {
                **chunk,
                "score": round(score, 6),
            }
            for score, chunk in scored[:top_k]
            if score > 0
        ]
        if ranked:
            return ranked
        return [{**chunk, "score": 0.0} for chunk in chunks[:top_k]]

    def _embed(self, texts: list[str]) -> list[list[float]]:
        if not self.ai.enabled or not self.ai.client:
            return []
        try:
            response = self.ai.client.embeddings.create(
                model=RAG_EMBEDDING_MODEL,
                input=texts,
            )
            vectors = [item.embedding for item in response.data]
            if len(vectors) != len(texts):
                return []
            return vectors
        except Exception:
            logger.exception("RAG embeddings failed")
            return []

    @staticmethod
    def _build_chunks(
        document_text: str,
        tz_items: list[TZItem],
        *,
        filename: str = "",
    ) -> list[dict[str, str | int | float]]:
        content = document_text.strip()
        if not content and tz_items:
            lines = [
                (
                    f"{item.number}. {item.name}; кол-во: {item.quantity} {item.unit}; "
                    f"характеристики: {item.specifications or '-'}"
                )
                for item in tz_items
            ]
            content = "\n".join(lines)
        if not content:
            return []

        chunks: list[dict[str, str | int | float]] = []
        start = 0
        chunk_id = 1
        while start < len(content):
            end = min(len(content), start + RAG_CHUNK_SIZE)
            piece = content[start:end].strip()
            if piece:
                chunks.append(
                    {
                        "chunk_id": chunk_id,
                        "filename": filename,
                        "text": piece,
                        "start": start,
                        "end": end,
                    }
                )
                chunk_id += 1
            if end >= len(content):
                break
            start = max(0, end - RAG_CHUNK_OVERLAP)
        return chunks

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        return set(re.findall(r"[a-zA-Zа-яА-Я0-9]{3,}", text.lower()))

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        dot = sum(a * b for a, b in zip(left, right))
        left_norm = math.sqrt(sum(a * a for a in left))
        right_norm = math.sqrt(sum(b * b for b in right))
        if left_norm == 0 or right_norm == 0:
            return 0.0
        return dot / (left_norm * right_norm)
