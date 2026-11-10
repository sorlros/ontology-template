"""수집: md(frontmatter + 본문)와 CSV 를 Document 와 Chunk 목록으로 바꾼다."""

import csv
import datetime
import hashlib
import io
import json
import re
from pathlib import Path

import yaml

from .model import FRONTMATTER, ROW, Chunk, Document

REQUIRED = ("date", "source", "type")
FRONT = re.compile(r"\A---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
HEADING = re.compile(r"^(#{1,3})[ \t]+(.+?)[ \t]*#*[ \t]*$")


def _sha(data: str | bytes) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def _chunk(doc_id: str, path: tuple[str, ...], text: str) -> Chunk:
    h = _sha(text)
    return Chunk(doc_id, f"{doc_id}#{h[:12]}", path, text, h)


def _dedup(chunks: list[Chunk]) -> list[Chunk]:
    seen: dict[str, Chunk] = {}
    for c in chunks:
        seen.setdefault(c.chunk_id, c)  # 먼저 나온 청크를 남긴다
    return list(seen.values())


def _body_chunks(doc_id: str, body: str) -> list[Chunk]:
    out, stack, buf = [], [], []

    def flush():
        text = "\n".join(buf).strip()
        if text:
            out.append(_chunk(doc_id, tuple(t for _, t in stack), text))
        buf.clear()

    for line in body.splitlines():
        m = HEADING.match(line)
        if not m:
            buf.append(line)
            continue
        flush()
        level = len(m.group(1))
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, m.group(2)))
    flush()
    return out


def _document(path: Path, raw: bytes, meta: dict, doc_id: str) -> Document:
    """메타를 검증해 Document 를 만든다. 메타가 틀린 문서는 신뢰도·충돌 계산을 망치므로 받지 않는다."""
    missing = [k for k in REQUIRED if not meta.get(k)]
    if missing:
        raise ValueError(f"{path}: 필수 메타 누락: {', '.join(missing)}")
    try:
        # YAML 이 만든 date 객체도, 따옴표로 감싼 문자열도 ISO 날짜 문자열로 맞춘다
        date = datetime.date.fromisoformat(str(meta["date"])).isoformat()
        grade = int(meta.get("grade") or 3)
    except (ValueError, TypeError) as e:
        raise ValueError(f"{path}: 메타 값 오류(date 는 ISO 날짜, grade 는 정수): {e}") from e
    if grade not in (1, 2, 3):
        raise ValueError(f"{path}: grade 는 1~3 이어야 한다: {grade}")
    return Document(
        doc_id=doc_id,
        path=str(path),
        sha256=_sha(raw),
        title=str(meta.get("title") or path.stem),
        date=date,
        source=str(meta["source"]),
        grade=grade,
        doc_type=str(meta["type"]),
    )


def ingest_markdown(path: Path) -> tuple[Document, list[Chunk]]:
    path = Path(path)
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    m = FRONT.match(text)
    try:
        meta = (yaml.safe_load(m.group(1)) if m else None) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"{path}: frontmatter YAML 오류: {e}") from e
    if not isinstance(meta, dict):
        raise ValueError(f"{path}: frontmatter 가 매핑이 아니다")
    doc_id = str(meta.get("id") or path.stem)
    doc = _document(path, raw, meta, doc_id)
    chunks = [_chunk(doc_id, FRONTMATTER, m.group(1))] + _body_chunks(doc_id, text[m.end():])
    return doc, _dedup(chunks)


def ingest_csv(path: Path, meta: dict) -> tuple[Document, list[Chunk]]:
    path = Path(path)
    raw = path.read_bytes()
    doc = _document(path, raw, meta, path.stem)
    # newline="" 이어야 따옴표 안의 줄바꿈이 한 필드로 읽힌다
    rows = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
    chunks = []
    for r in rows:
        if None in r:  # 머리글보다 값이 많은 행: 열이 밀렸을 수 있어 추측하지 않고 거부한다
            raise ValueError(f"{path}: {rows.line_num}번째 줄의 열 수가 머리글보다 많다")
        if any(r.values()):
            chunks.append(_chunk(doc.doc_id, ROW, json.dumps(r, ensure_ascii=False, sort_keys=True)))
    return doc, _dedup(chunks)
