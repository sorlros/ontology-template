"""웹 업로드: 실제 서버를 띄워 문서를 올리고, 검사에서 걸린 파일은 저장되지 않는지 확인한다."""

import json
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

import pytest

from onto.cli import ingest
from onto.server import make_server

SRC = Path(__file__).parent.parent / "domains" / "contracts"
NEW = """---
title: 서연전자 공급계약서
date: "2026-07-01"
source: 법무팀
grade: 1
type: contract
contract: 서연-2026-01
supplier: 서연전자
buyer: 오리온제조
amount: 30000000
signed: "2026-07-01"
---
# 계약
본문.
"""


@pytest.fixture
def srv(tmp_path):
    domain, data = tmp_path / "contracts", tmp_path / "data"
    shutil.copytree(SRC, domain)
    (tmp_path / "viz").mkdir()
    ingest(domain, data)
    server = make_server(domain, data, tmp_path / "viz", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", domain
    server.shutdown()
    server.server_close()


def call(url, body=None, host=None, method=None):
    req = urllib.request.Request(url, data=body, method=method or ("PUT" if body is not None else "GET"))
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def put(base, name, text, replace=False):
    return call(f"{base}/api/docs/{quote(name, safe='')}" + ("?replace=1" if replace else ""), text.encode("utf-8"))


def amounts(base, contract):
    return {f["o"] for f in call(base + "/graph.json")[1]["facts"] if f["s"] == contract and f["p"] == "amount"}


def test_올린_문서가_저장되고_바로_그래프에_반영된다(srv):
    base, domain = srv
    status, body = put(base, "s-001.md", NEW)
    assert status == 200 and body["doc_id"] == "s-001" and body["added"] == 1 and body["rejected"] == {}
    assert (domain / "docs" / "s-001.md").read_text(encoding="utf-8") == NEW
    assert amounts(base, "서연-2026-01") == {"30000000"}


def test_같은_이름은_확인_없이_덮어쓰지_않고_바꿔_넣으면_사실이_교체된다(srv):
    base, _ = srv
    put(base, "s-001.md", NEW)
    assert put(base, "s-001.md", NEW)[0] == 409
    status, body = put(base, "s-001.md", NEW.replace("30000000", "40000000"), replace=True)
    assert status == 200 and (body["added"], body["removed"]) == (1, 1)
    assert amounts(base, "서연-2026-01") == {"40000000"}


@pytest.mark.parametrize("name, text", [
    ("../evil.md", NEW),                                  # docs/ 밖
    (".hidden.md", NEW),
    ("note.txt", NEW),                                    # 받지 않는 형식
    ("bad.md", "---\ntitle: 메타 없음\n---\n본문\n"),     # 필수 메타 누락
    ("other.csv", "name\n가\n"),                          # 매핑 없는 CSV
])
def test_받을_수_없는_파일은_저장하지_않는다(srv, name, text):
    base, domain = srv
    before = sorted(p.name for p in domain.parent.rglob("*") if p.is_file())
    assert put(base, name, text)[0] == 400
    assert sorted(p.name for p in domain.parent.rglob("*") if p.is_file()) == before


def test_다른_파일과_ID_가_겹치면_저장하지_않는다(srv):
    base, domain = srv
    status, body = put(base, "copy.md", (domain / "docs" / "c-001.md").read_text(encoding="utf-8"))
    assert status == 409 and "c-001.md" in body["error"]
    assert not (domain / "docs" / "copy.md").exists()


def test_localhost_가_아닌_이름으로_온_요청은_거부한다(srv):
    base, domain = srv
    assert call(base + "/graph.json", host="evil.example")[0] == 403
    assert call(base + "/api/docs/x.md", NEW.encode(), host="evil.example")[0] == 403
    assert not (domain / "docs" / "x.md").exists()


def test_문서를_삭제하면_그_사실만_회수되고_파일은_휴지통으로_간다(srv):
    base, domain = srv
    assert amounts(base, "한빛-2026-01") == {"100000000", "120000000"}
    status, body = call(base + "/api/docs/c-003", method="DELETE")
    assert status == 200 and len(body["trash"]) == 1
    assert amounts(base, "한빛-2026-01") == {"100000000"}
    assert "c-003" not in {d["id"] for d in call(base + "/graph.json")[1]["docs"]}
    assert not (domain / "docs" / "c-003.md").exists()
    assert (domain.parent / "data" / "trash" / body["trash"][0]).read_text(encoding="utf-8").startswith("---")
    # 다시 투입해도 되살아나지 않고, 나머지는 그대로다
    reports, rejected, gone, _ = ingest(domain, domain.parent / "data")
    assert rejected == {} and gone == [] and all(r.added == [] and r.removed == [] for r in reports.values())


def test_없는_문서나_외부_요청의_삭제는_아무것도_지우지_않는다(srv):
    base, domain = srv
    before = sorted(p.name for p in (domain / "docs").iterdir())
    assert call(base + "/api/docs/nope", method="DELETE")[0] == 404
    assert call(base + "/api/docs/" + quote("../c-001", safe=""), method="DELETE")[0] == 404
    assert call(base + "/api/docs/c-001", method="DELETE", host="evil.example")[0] == 403
    assert sorted(p.name for p in (domain / "docs").iterdir()) == before
