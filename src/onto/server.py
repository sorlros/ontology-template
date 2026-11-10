"""로컬 웹 서버: 시각화 화면을 보여 주고, 화면에서 올린 문서를 docs/ 에 저장해 바로 투입하며, 문서를 빼면 그 사실을 회수한다."""

import json
import os
import shutil
import tempfile
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlsplit

from .cli import ingest
from .ingest import ingest_csv, ingest_markdown
from .model import load_mapping
from .store import Store

MAX_BYTES = 2 * 1024 * 1024
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}


class UploadError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _parse(path: Path, mapping: dict):
    """파일을 수집기로 읽는다. 받을 수 없는 파일이면 ValueError."""
    if path.suffix == ".md":
        return ingest_markdown(path)
    spec = (mapping.get("csv") or {}).get(path.name)
    if not spec:
        raise ValueError(f"mapping.yaml 의 csv 에 '{path.name}' 매핑이 없다")
    return ingest_csv(path, spec["meta"])


def upload(domain_dir: Path, data_dir: Path, name: str, raw: bytes, replace: bool,
           complete: Callable[[str, str, dict], str] | None = None) -> dict:
    """문서 하나를 검사하고 docs/ 에 저장한 뒤 투입한다. 검사에서 걸리면 아무것도 저장하지 않는다."""
    docs_dir = Path(domain_dir) / "docs"
    # 이름은 화면에서 온 값이다. 경로 구분자와 숨김 파일을 받지 않아 docs/ 밖에 쓰지 못하게 한다.
    if not name or name != Path(name).name or "\\" in name or name.startswith("."):
        raise UploadError(400, "파일 이름이 올바르지 않습니다.")
    if Path(name).suffix not in (".md", ".csv"):
        raise UploadError(400, "받을 수 있는 형식은 .md 와 .csv 입니다.")
    if len(raw) > MAX_BYTES:
        raise UploadError(413, f"파일이 너무 큽니다(최대 {MAX_BYTES // 1024 // 1024}MB).")
    target = docs_dir / name
    if target.exists() and not replace:
        raise UploadError(409, f"'{name}' 은 이미 있습니다.")

    mapping = load_mapping(domain_dir)
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / name
        probe.write_bytes(raw)
        try:
            doc, _ = _parse(probe, mapping)
        except (ValueError, UnicodeDecodeError) as e:
            raise UploadError(400, str(e).replace(str(probe), name)) from e
    # 같은 ID 를 가진 파일이 둘이면 투입이 둘 다 거부한다. 저장하기 전에 막는다.
    for other in [*docs_dir.glob("*.md"), *docs_dir.glob("*.csv")]:
        if other.name == name:
            continue
        try:
            if _parse(other, mapping)[0].doc_id == doc.doc_id:
                raise UploadError(409, f"문서 ID '{doc.doc_id}' 를 '{other.name}' 이 이미 쓰고 있습니다.")
        except ValueError:
            continue  # 이미 폴더에 있는 잘못된 파일은 투입 결과의 '거부'로 알린다

    fd, tmp_path = tempfile.mkstemp(dir=docs_dir, prefix=".upload-")
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    os.replace(tmp_path, target)

    reports, rejected, _, warnings = ingest(domain_dir, data_dir, complete)
    r = reports.get(doc.doc_id)
    return {
        "doc_id": doc.doc_id,
        "title": doc.title,
        "added": len(r.added) if r else 0,
        "removed": len(r.removed) if r else 0,
        "kept": len(r.kept) if r else 0,
        "quarantined": [f"{f.subject.label} {f.predicate} {f.object} — {reason}" for f, reason in r.quarantined] if r else [],
        "warnings": warnings,
        "rejected": rejected,
    }


def delete(domain_dir: Path, data_dir: Path, doc_id: str) -> dict:
    """문서 하나를 뺀다: 파일은 지우지 않고 data/trash/ 로 옮기고, 그 문서가 말한 사실을 저장소에서 회수한다."""
    domain_dir, data_dir = Path(domain_dir), Path(data_dir)
    docs_dir, mapping = domain_dir / "docs", load_mapping(domain_dir)
    store = Store(domain_dir, data_dir / "store.trig")
    # 화면이 보낸 ID 로 경로를 만들지 않는다. docs/ 안의 파일을 읽어 그 ID 를 가진 파일을 찾는다.
    files = []
    for p in [*docs_dir.glob("*.md"), *docs_dir.glob("*.csv")]:
        try:
            if _parse(p, mapping)[0].doc_id == doc_id:
                files.append(p)
        except ValueError:
            continue
    if not files and doc_id not in {d["id"] for d in store.export_json()["docs"]}:
        raise UploadError(404, f"문서 '{doc_id}' 가 없습니다.")
    trash = data_dir / "trash"
    trash.mkdir(parents=True, exist_ok=True)
    moved = []
    for p in files:
        dest = trash / f"{time.strftime('%Y%m%d-%H%M%S')}-{p.name}"
        shutil.move(p, dest)
        moved.append(dest.name)
    store.retract_doc(doc_id)
    store.save()
    return {"doc_id": doc_id, "trash": moved}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, domain_dir: Path, data_dir: Path, complete, lock: threading.Lock, **kwargs):
        self.domain_dir, self.data_dir, self.complete, self.lock = domain_dir, data_dir, complete, lock
        super().__init__(*args, **kwargs)

    def log_message(self, fmt, *args):  # 요청마다 찍히는 기본 로그는 끈다
        pass

    def _json(self, status: int, body: dict):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _local(self) -> bool:
        # 다른 이름으로 이 서버를 가리키게 해 브라우저의 동일 출처 검사를 속이는 요청(DNS rebinding)을 막는다.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in LOCAL_HOSTS

    def do_GET(self):
        if not self._local():
            return self._json(403, {"error": "localhost 로만 접속할 수 있습니다."})
        path = urlsplit(self.path).path
        if path == "/graph.json":
            with self.lock:
                return self._json(200, Store(self.domain_dir, self.data_dir / "store.trig").export_json())
        if path == "/api/status":
            csvs = sorted(load_mapping(self.domain_dir).get("csv") or {})
            return self._json(200, {"upload": True, "llm": self.complete is not None, "domain": self.domain_dir.name, "csv": csvs})
        super().do_GET()

    # PUT·DELETE 는 다른 사이트의 페이지가 사전 요청(preflight) 없이 보낼 수 없다. 이 서버는 사전 요청에 답하지 않는다.
    def do_PUT(self):
        if not self._local():
            return self._json(403, {"error": "localhost 로만 접속할 수 있습니다."})
        url = urlsplit(self.path)
        if not url.path.startswith("/api/docs/"):
            return self._json(404, {"error": "없는 경로입니다."})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BYTES:
            return self._json(413, {"error": f"파일이 너무 큽니다(최대 {MAX_BYTES // 1024 // 1024}MB)."})
        raw = self.rfile.read(length)
        name = unquote(url.path[len("/api/docs/"):])
        replace = parse_qs(url.query).get("replace") == ["1"]
        try:
            with self.lock:  # 저장소 파일은 한 번에 한 투입만 쓴다
                result = upload(self.domain_dir, self.data_dir, name, raw, replace, self.complete)
        except UploadError as e:
            return self._json(e.status, {"error": str(e)})
        except Exception as e:  # 투입 중 예기치 못한 오류도 화면에 사유를 돌려준다
            return self._json(500, {"error": f"투입 중 오류: {e}"})
        self._json(200, result)

    def do_DELETE(self):
        if not self._local():
            return self._json(403, {"error": "localhost 로만 접속할 수 있습니다."})
        path = urlsplit(self.path).path
        if not path.startswith("/api/docs/"):
            return self._json(404, {"error": "없는 경로입니다."})
        try:
            with self.lock:
                result = delete(self.domain_dir, self.data_dir, unquote(path[len("/api/docs/"):]))
        except UploadError as e:
            return self._json(e.status, {"error": str(e)})
        except Exception as e:
            return self._json(500, {"error": f"삭제 중 오류: {e}"})
        self._json(200, result)


def make_server(domain_dir: Path, data_dir: Path, viz_dir: Path, port: int,
                complete: Callable[[str, str, dict], str] | None = None) -> ThreadingHTTPServer:
    handler = partial(Handler, directory=str(viz_dir), domain_dir=Path(domain_dir), data_dir=Path(data_dir),
                      complete=complete, lock=threading.Lock())
    return ThreadingHTTPServer(("127.0.0.1", port), handler)  # 이 컴퓨터에서만 접속할 수 있다
