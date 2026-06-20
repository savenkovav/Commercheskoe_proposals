from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

from src.config import (
    CATALOG_PATH,
    GOODS_REPORT_PATH,
    PROCUREMENT_REPORT_PATH,
    RAG_DOCS_INDEX_DIR,
    REGISTRY_PATH,
)
from src.services.price_list_manager import PriceListManager
from src.services.tz_parser import extract_tz_document_text
from src.services.tz_rag_service import RagIndex, TZRagService

logger = logging.getLogger(__name__)


@dataclass
class DocumentRagEntry:
    doc_id: str
    source_type: str
    source_name: str
    filename: str
    path: str
    file_mtime: float
    chunks: list[dict[str, str | int | float]]
    vectors: list[list[float]]


class DocumentRagIndexService:
    def __init__(self, rag_service: TZRagService) -> None:
        self.rag = rag_service
        self.index_dir = RAG_DOCS_INDEX_DIR
        self.index_file = self.index_dir / "index.json"
        self._entries: dict[str, DocumentRagEntry] = {}
        self._loaded = False
        self._bootstrapped = False

    def ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.index_file.exists():
            return
        try:
            payload = json.loads(self.index_file.read_text(encoding="utf-8"))
            rows = payload.get("documents", [])
            for row in rows:
                entry = DocumentRagEntry(
                    doc_id=str(row.get("doc_id", "")),
                    source_type=str(row.get("source_type", "")),
                    source_name=str(row.get("source_name", "")),
                    filename=str(row.get("filename", "")),
                    path=str(row.get("path", "")),
                    file_mtime=float(row.get("file_mtime") or 0.0),
                    chunks=list(row.get("chunks") or []),
                    vectors=list(row.get("vectors") or []),
                )
                if entry.doc_id:
                    self._entries[entry.doc_id] = entry
        except Exception:
            logger.exception("Failed to load RAG docs index")

    def save(self) -> None:
        rows = [asdict(entry) for entry in self._entries.values()]
        self.index_file.write_text(
            json.dumps({"documents": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def index_document(
        self,
        *,
        doc_id: str,
        source_type: str,
        source_name: str,
        file_path: Path,
        force: bool = False,
    ) -> dict[str, str | int | bool]:
        self.ensure_loaded()
        if not file_path.exists():
            return {"indexed": False, "chunks": 0, "vectorized": False}

        file_mtime = file_path.stat().st_mtime
        current = self._entries.get(doc_id)
        if (
            not force
            and current
            and current.path == str(file_path)
            and abs(current.file_mtime - file_mtime) < 0.001
        ):
            return {
                "indexed": True,
                "chunks": len(current.chunks),
                "vectorized": bool(current.vectors),
                "skipped": True,
            }

        text = extract_tz_document_text(file_path)
        if not text.strip():
            return {"indexed": False, "chunks": 0, "vectorized": False}

        rag_index = self.rag.build_index(text, [], filename=file_path.name)
        self._entries[doc_id] = DocumentRagEntry(
            doc_id=doc_id,
            source_type=source_type,
            source_name=source_name,
            filename=file_path.name,
            path=str(file_path),
            file_mtime=file_mtime,
            chunks=rag_index.chunks,
            vectors=rag_index.vectors,
        )
        self.save()
        return {
            "indexed": True,
            "chunks": len(rag_index.chunks),
            "vectorized": bool(rag_index.vectors),
            "skipped": False,
        }

    def bootstrap(self, price_manager: PriceListManager) -> None:
        self.ensure_loaded()
        if self._bootstrapped:
            return
        self._bootstrapped = True
        sources: list[tuple[str, str, Path]] = [
            ("catalog:main", "catalog", CATALOG_PATH),
            ("registry:main", "registry", REGISTRY_PATH),
            ("margin:goods_report", "margin", GOODS_REPORT_PATH),
        ]
        if PROCUREMENT_REPORT_PATH:
            sources.append(("margin:procurement", "margin", PROCUREMENT_REPORT_PATH))

        for doc_id, source_type, path in sources:
            if path.exists():
                self.index_document(
                    doc_id=doc_id,
                    source_type=source_type,
                    source_name=path.stem,
                    file_path=path,
                )
        for entry in price_manager.list_entries():
            path = price_manager.file_path(entry)
            if not path.exists():
                continue
            self.index_document(
                doc_id=f"price:{entry.id}",
                source_type="price",
                source_name=entry.name,
                file_path=path,
            )

    def query(
        self,
        query: str,
        *,
        source_type: str | None = None,
        top_k: int = 5,
    ) -> list[dict[str, str | int | float]]:
        self.ensure_loaded()
        merged_chunks: list[dict[str, str | int | float]] = []
        merged_vectors: list[list[float]] = []
        has_partial_vectors = False
        for entry in self._entries.values():
            if source_type and entry.source_type != source_type:
                continue
            for idx, chunk in enumerate(entry.chunks):
                merged_chunks.append(
                    {
                        **chunk,
                        "doc_id": entry.doc_id,
                        "source_type": entry.source_type,
                        "source_name": entry.source_name,
                        "filename": entry.filename,
                    }
                )
                if idx < len(entry.vectors):
                    merged_vectors.append(entry.vectors[idx])
                else:
                    has_partial_vectors = True

        if has_partial_vectors or len(merged_vectors) != len(merged_chunks):
            merged_vectors = []

        rows = self.rag.retrieve_debug(
            query,
            RagIndex(chunks=merged_chunks, vectors=merged_vectors),
            top_k=top_k,
        )
        return rows

    def stats(self) -> dict[str, int]:
        self.ensure_loaded()
        return {
            "documents": len(self._entries),
            "chunks": sum(len(entry.chunks) for entry in self._entries.values()),
            "vectorized_documents": sum(1 for entry in self._entries.values() if entry.vectors),
        }

    def remove_document(self, doc_id: str) -> None:
        self.ensure_loaded()
        if doc_id in self._entries:
            self._entries.pop(doc_id, None)
            self.save()


_docs_index: DocumentRagIndexService | None = None


def get_document_rag_index(rag_service: TZRagService) -> DocumentRagIndexService:
    global _docs_index
    if _docs_index is None:
        _docs_index = DocumentRagIndexService(rag_service)
    return _docs_index
