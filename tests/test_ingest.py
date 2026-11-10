import json
from pathlib import Path

import pytest
import yaml

from onto.ingest import ingest_csv, ingest_markdown
from onto.model import FRONTMATTER, ROW

DOCS = Path(__file__).parent.parent / "domains" / "contracts" / "docs"

MD = """---
id: d-1
title: 제목
date: 2026-01-10
source: 법무팀
type: memo
---
서문

# 가
가 본문

## 나
나 본문

### 다
다 본문

## 빈

# 라
라 본문
"""


def write(tmp_path, text, name="d.md"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_headings_and_defaults(tmp_path):
    doc, chunks = ingest_markdown(write(tmp_path, MD))
    assert (doc.doc_id, doc.grade, doc.date, doc.doc_type) == ("d-1", 3, "2026-01-10", "memo")
    assert chunks[0].heading_path == FRONTMATTER
    assert yaml.safe_load(chunks[0].text)["id"] == "d-1"
    paths = [c.heading_path for c in chunks[1:]]
    # 빈 제목('빈')은 청크를 만들지 않는다
    assert paths == [(), ("가",), ("가", "나"), ("가", "나", "다"), ("라",)]


def test_id_and_title_default_to_stem(tmp_path):
    doc, _ = ingest_markdown(write(tmp_path, "---\ndate: 2026-01-01\nsource: s\ntype: memo\n---\n본문\n", "abc.md"))
    assert (doc.doc_id, doc.title) == ("abc", "abc")


@pytest.mark.parametrize("missing", ["date", "source", "type"])
def test_missing_meta(tmp_path, missing):
    meta = {"date": "2026-01-01", "source": "s", "type": "memo"}
    meta.pop(missing)
    text = "---\n" + "".join(f"{k}: {v}\n" for k, v in meta.items()) + "---\n본문\n"
    p = write(tmp_path, text)
    with pytest.raises(ValueError) as e:
        ingest_markdown(p)
    assert missing in str(e.value) and str(p) in str(e.value)


def test_no_frontmatter(tmp_path):
    with pytest.raises(ValueError):
        ingest_markdown(write(tmp_path, "# 제목\n본문\n"))


def test_chunk_id_stable_and_content_based(tmp_path):
    _, a = ingest_markdown(write(tmp_path, MD))
    _, b = ingest_markdown(write(tmp_path, MD.replace("나 본문", "나 수정")))
    ids_a = {c.heading_path: c.chunk_id for c in a}
    ids_b = {c.heading_path: c.chunk_id for c in b}
    assert ids_a[("가",)] == ids_b[("가",)]
    assert ids_a[("가", "나")] != ids_b[("가", "나")]
    assert a[1].chunk_id.startswith("d-1#") and len(a[1].chunk_id) == len("d-1#") + 12


def test_duplicate_chunks_removed(tmp_path):
    _, chunks = ingest_markdown(write(tmp_path, MD.replace("라 본문", "가 본문")))
    ids = [c.chunk_id for c in chunks]
    assert len(ids) == len(set(ids))
    assert [c.text for c in chunks].count("가 본문") == 1


def test_csv_rows(tmp_path):
    p = write(tmp_path, "name,aliases\n가,a|b\n나,c\n가,a|b\n", "s.csv")
    meta = {"title": "T", "date": "2026-01-02", "source": "구매팀", "grade": 1, "type": "master"}
    doc, chunks = ingest_csv(p, meta)
    assert (doc.doc_id, doc.source, doc.grade, doc.doc_type) == ("s", "구매팀", 1, "master")
    assert all(c.heading_path == ROW for c in chunks)
    assert len(chunks) == 2  # 동일 행은 하나만
    assert chunks[0].text == '{"aliases": "a|b", "name": "가"}'


def test_sample_data_ingests():
    meta = yaml.safe_load((DOCS.parent / "mapping.yaml").read_text(encoding="utf-8"))["csv"]["suppliers.csv"]["meta"]
    _, rows = ingest_csv(DOCS / "suppliers.csv", meta)
    assert len(rows) == 3
    mds = sorted(DOCS.glob("*.md"))
    assert len(mds) == 6
    for p in mds:
        doc, chunks = ingest_markdown(p)
        assert isinstance(doc.date, str) and len(chunks) >= 2


# --- 메인 세션 검토에서 추가한 회귀 시험 ---

def _md(tmp_path, front):
    p = tmp_path / "d.md"
    p.write_text(f"---\n{front}\n---\n본문\n", encoding="utf-8")
    return p


@pytest.mark.parametrize("front", [
    'date: "2026-01-10"\nsource: 법무팀\ntype: memo\ngrade: 5',      # 등급 범위 밖
    'date: "2026-01-10"\nsource: 법무팀\ntype: memo\ngrade: high',   # 등급이 숫자가 아님
    'date: "2026/01/10"\nsource: 법무팀\ntype: memo',                # ISO 날짜가 아님
    'date: "2026-01-10"\nsource: [법무팀\ntype: memo',               # YAML 문법 오류
])
def test_잘못된_메타는_파일경로와_함께_거부(tmp_path, front):
    p = _md(tmp_path, front)
    with pytest.raises(ValueError, match="d.md"):
        ingest_markdown(p)


CSV_META = {"date": "2026-01-02", "source": "구매팀", "type": "master"}


def test_csv_따옴표안_줄바꿈은_한_행(tmp_path):
    p = tmp_path / "s.csv"
    p.write_text('name,note\n한빛물산,"첫 줄\n둘째 줄"\n', encoding="utf-8")
    _, chunks = ingest_csv(p, CSV_META)
    assert len(chunks) == 1
    assert json.loads(chunks[0].text)["note"] == "첫 줄\n둘째 줄"


def test_csv_열이_남는_행은_행번호와_함께_거부(tmp_path):
    p = tmp_path / "s.csv"
    p.write_text("name,country\n한빛물산,대한민국,남는값\n", encoding="utf-8")
    with pytest.raises(ValueError, match="s.csv.*2"):
        ingest_csv(p, CSV_META)
