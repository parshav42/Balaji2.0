"""Local document ingestion and hybrid keyword/optional Ollama embedding retrieval."""
from __future__ import annotations
import io
import json
import math
import re
from pathlib import Path
from . import config, storage
from .ollama_client import embed, OllamaError

WORD_RE = re.compile(r"[a-zA-Z0-9_]{2,}")


def chunk_text(text: str, size: int = 900, overlap: int = 130) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    out = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            split = text.rfind(" ", start + size // 2, end)
            if split > start:
                end = split
        out.append(text[start:end].strip())
        if end == len(text):
            break
        start = max(start + 1, end - overlap)
    return out


def ingest_bytes(filename: str, data: bytes) -> dict:
    name = Path(filename).name
    suffix = Path(name).suffix.lower()
    if suffix not in {".txt", ".md", ".pdf"}:
        raise ValueError("Only .txt, .md and .pdf files are supported")
    if not data or len(data) > config.MAX_FILE_BYTES:
        raise ValueError("File must contain data and be 5 MB or smaller")
    pages: list[tuple[int | None, str]] = []
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ValueError("Install pypdf to upload PDFs: pip install pypdf") from exc
        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                raise ValueError("Encrypted PDFs are not supported")
            if len(reader.pages) > 100:
                raise ValueError("PDFs cannot exceed 100 pages")
            for number, page in enumerate(reader.pages, 1):
                pages.append((number, page.extract_text() or ""))
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("Could not parse PDF") from exc
    else:
        try:
            pages = [(None, data.decode("utf-8"))]
        except UnicodeDecodeError as exc:
            raise ValueError("Text files must be UTF-8") from exc

    chunks = []
    for page, content in pages:
        for item in chunk_text(content):
            vector = None
            if config.EMBED_MODEL:
                try:
                    vector = embed(item)
                except OllamaError as exc:
                    raise ValueError(f"Embedding model unavailable: {exc}") from exc
            chunks.append((page, len(chunks) + 1, item, vector))
    if not chunks:
        raise ValueError("No extractable text was found in the document (scanned PDFs need OCR)")
    doc_id = storage.add_document(name, chunks)
    return {"id": doc_id, "name": name, "chunks": len(chunks)}


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    norm = math.sqrt(sum(x*x for x in a) * sum(x*x for x in b))
    return sum(x*y for x, y in zip(a, b)) / norm if norm else 0.0


def retrieve(query: str, top_k: int = 4) -> list[dict]:
    chunks = storage.all_chunks()
    if not chunks:
        return []
    terms = set(WORD_RE.findall(query.lower()))
    q_vec = []
    if config.EMBED_MODEL and any(r["embedding"] for r in chunks):
        try:
            q_vec = embed(query)
        except OllamaError:
            pass   # Continue with keyword retrieval while Ollama embed model is down.
    scored = []
    for row in chunks:
        words = set(WORD_RE.findall(row["content"].lower()))
        lexical = len(terms & words) / max(1, len(terms))
        vector = json.loads(row["embedding"]) if row["embedding"] else []
        semantic = max(0, cosine(q_vec, vector)) if vector else 0.0
        score = (0.35 * lexical + 0.65 * semantic) if q_vec and vector else lexical
        if score <= 0:
            continue
        citation = f"{row['name']}"
        if row["page"]:
            citation += f" p.{row['page']}"
        citation += f" chunk {row['chunk_no']}"
        scored.append({"citation": citation, "content": row["content"][:1100], "score": round(score, 3)})
    return sorted(scored, key=lambda x: x["score"], reverse=True)[:max(1, min(top_k, 8))]
