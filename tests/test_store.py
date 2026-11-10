import hashlib
from pathlib import Path

import pytest
from rdflib import URIRef

from onto.model import FRONTMATTER, ONTO, Chunk, Document, Fact, Mention
from onto.store import Store

DOMAIN = Path(__file__).resolve().parents[1] / "domains" / "contracts"


def resolve(m: Mention) -> URIRef:
    return ONTO[f"entity:{m.cls}:{m.label}"]


def doc(did, source="법무팀", grade=1):
    return Document(did, f"{did}.md", "h", f"문서 {did}", "2026-01-10", source, grade, "contract")


def chunk(did, text):
    h = hashlib.sha256(text.encode()).hexdigest()
    return Chunk(did, f"{did}#{h[:12]}", FRONTMATTER, text, h)


def F(c, s, p, o, cls="Contract"):
    return Fact(Mention(s, cls), p, o, c.chunk_id, c.doc_id)


def put(store, d, text, *facts):
    c = chunk(d.doc_id, text)
    return store.sync(d, [c], [F(c, *f) for f in facts], resolve), c


@pytest.fixture
def store():
    return Store(DOMAIN)


def by_key(store):
    return {(f["s_label"], f["p"].split("#")[-1], f["o_label"]): f for f in store.facts()}


def test_sync_idempotent(store):
    d = doc("a")
    c = chunk("a", "t")
    facts = [F(c, "K1", "amount", "100"), F(c, "K1", "hasSupplier", Mention("한빛", "Organization"))]
    r1 = store.sync(d, [c], facts, resolve)
    n = len(store.ds)
    r2 = store.sync(d, [c], [], resolve)
    assert r1.added == [c.chunk_id] and r2.kept == [c.chunk_id] and not r2.added and not r2.removed
    assert len(store.ds) == n and len(store.facts()) == 2


def test_chunk_replaced(store):
    d = doc("a")
    c1, c2 = chunk("a", "one"), chunk("a", "two")
    store.sync(d, [c1, c2], [F(c1, "K1", "amount", "1"), F(c2, "K2", "amount", "2")], resolve)
    c3 = chunk("a", "three")
    r = store.sync(d, [c1, c3], [F(c3, "K3", "amount", "3")], resolve)
    assert r.removed == [c2.chunk_id] and r.kept == [c1.chunk_id] and r.added == [c3.chunk_id]
    assert {k[0] for k in by_key(store)} == {"K1", "K3"}


def test_retract_doc(store):
    put(store, doc("a", "법무팀"), "x", ("K1", "amount", "1"), ("K2", "amount", "5"))
    put(store, doc("b", "구매팀", 2), "y", ("K1", "amount", "1"))
    before = by_key(store)[("K1", "amount", "1")]
    assert store.retract_doc("a") == 1
    k = by_key(store)
    assert ("K2", "amount", "5") not in k
    after = k[("K1", "amount", "1")]
    assert after["docs"] == ["b"] and after["confidence"] < before["confidence"]
    assert store.known_chunks("a") == set()


def test_confidence(store):
    put(store, doc("a", "법무팀", 1), "x", ("K1", "amount", "1"))
    assert by_key(store)[("K1", "amount", "1")]["confidence"] == 0.9
    put(store, doc("b", "법무팀", 3), "y", ("K1", "amount", "1"))
    assert by_key(store)[("K1", "amount", "1")]["confidence"] == 0.9  # 같은 source 사본
    put(store, doc("c", "구매팀", 2), "z", ("K1", "amount", "1"))
    assert by_key(store)[("K1", "amount", "1")]["confidence"] == 0.97


def test_conflict(store):
    put(store, doc("a"), "x", ("K1", "amount", "1"), ("K2", "amount", "7"))
    put(store, doc("b", "구매팀"), "y", ("K1", "amount", "2"))
    k = by_key(store)
    assert k[("K1", "amount", "1")]["conflict"] and k[("K1", "amount", "2")]["conflict"]
    assert not k[("K2", "amount", "7")]["conflict"]


def test_quarantine(store):
    d = doc("a")
    c = chunk("a", "t")
    org = Mention("한빛", "Organization")
    facts = [
        F(c, "K1", "hasSupplier", Mention("홍길동", "Person")),
        F(c, "K1", "amount", "많이"),
        F(c, "K1", "bogus", "x"),
        F(c, "K1", "hasBuyer", org),
        F(c, "K1", "amount", "42"),
    ]
    r = store.sync(d, [c], facts, resolve)
    assert {f.predicate for f, _ in r.quarantined} == {"bogus", "amount", "hasSupplier"}
    assert len(r.quarantined) == 3 and all(why for _, why in r.quarantined)
    assert set(by_key(store)) == {("K1", "hasBuyer", "한빛"), ("K1", "amount", "42")}
    assert not any("홍길동" in str(t) for t in store.ds.graph(ONTO[f"chunk:{c.chunk_id}"]))


def test_save_load(store, tmp_path):
    p = tmp_path / "s.trig"
    s = Store(DOMAIN, p)
    put(s, doc("a"), "x", ("K1", "amount", "1"), ("K1", "hasBuyer", Mention("한빛", "Organization")))
    put(s, doc("b", "구매팀", 2), "y\n줄바꿈", ("K1", "amount", "1"))
    s.save()
    s2 = Store(DOMAIN, p)
    assert s2.facts() == s.facts()
    assert s2.known_chunks("a") == s.known_chunks("a") != set()
    assert s2.export_json() == s.export_json()


def test_export_json(store):
    org = Mention("한빛", "Organization")
    put(store, doc("a"), "원문", ("K1", "hasSupplier", org), ("K1", "amount", "1"), ("K1", "amount", "2"))
    e = store.export_json()
    assert set(e) == {"nodes", "edges", "facts", "docs", "evidence"}
    assert {n["label"]: (n["type"], n["degree"]) for n in e["nodes"]} == {"K1": ("Contract", 1), "한빛": ("Organization", 1)}
    (edge,) = e["edges"]
    assert set(edge) == {"id", "source", "target", "predicate", "confidence", "docs", "chunks", "conflict"}
    assert edge["predicate"] == "hasSupplier" and edge["source"] == str(resolve(Mention("K1", "Contract")))
    assert {"s": "K1", "p": "amount", "o": "1", "o_is_entity": False}.items() <= e["facts"][0].items()
    assert e["facts"][0]["conflict"] is True
    assert e["docs"] == [{"id": "a", "title": "문서 a", "date": "2026-01-10", "source": "법무팀", "grade": 1}]
    (cid, ev), = e["evidence"].items()
    assert ev == {"doc": "a", "heading": "@frontmatter", "text": "원문"} and cid in edge["chunks"]


# --- 메인 세션 검토에서 추가한 회귀 시험 ---

def test_주어_클래스가_정의역과_다르면_격리한다():
    from onto.model import ONTO, Chunk, Document, Fact, Mention
    from onto.store import Store
    domain = Path(__file__).parent.parent / "domains" / "contracts"
    doc = Document("d1", "d1.md", "x", "t", "2026-01-01", "법무팀", 1, "contract")
    ch = Chunk("d1", "d1#aaa", ("본문",), "text", "aaa")
    org = Mention("한빛물산", "Organization")
    # hasSupplier 의 정의역은 Contract 다. 방향이 뒤집힌 추출을 흉내 낸다.
    flipped = Fact(org, "hasSupplier", Mention("한빛-2026-01", "Contract"), ch.chunk_id, "d1")
    wrong_subject = Fact(org, "hasSupplier", Mention("대한소재", "Organization"), ch.chunk_id, "d1")
    store = Store(domain)
    report = store.sync(doc, [ch], [flipped, wrong_subject], lambda m: ONTO[f"entity:{m.cls}:{m.label}"])
    assert len(report.quarantined) == 2
    assert store.facts() == []
