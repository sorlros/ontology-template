import json
from pathlib import Path

import pytest

from onto.extract import ExtractionError, extract_llm, extract_rules, llm_request, schema_summary
from onto.model import FRONTMATTER, ROW, Chunk, Document, Fact, Mention, load_mapping

DOM = Path(__file__).parent.parent / "domains" / "contracts"


def doc(doc_type="contract", path="a.md"):
    return Document("d1", path, "h", "t", "2026-01-01", "법무팀", 1, doc_type)


def chunk(text, hp=("본문",)):
    return Chunk("d1", "d1#abc", hp, text, "x")


def test_frontmatter_rules():
    text = "contract: 한빛-2026-01\nsupplier: 한빛물산\nbuyer: 우리\namount: 100000000\nsigned: 2026-01-10\nsupersedes: [A, B]\n"
    facts = extract_rules(doc(), chunk(text, FRONTMATTER), load_mapping(DOM))
    got = {(f.predicate, f.object) for f in facts}
    assert ("amount", "100000000") in got
    assert ("signedOn", "2026-01-10") in got
    assert ("hasSupplier", Mention("한빛물산", "Organization")) in got
    assert ("supersedes", Mention("A", "Contract")) in got and ("supersedes", Mention("B", "Contract")) in got
    assert all(f.subject == Mention("한빛-2026-01", "Contract") for f in facts)
    assert all(f.chunk_id == "d1#abc" and f.doc_id == "d1" for f in facts)


def test_rules_edge_cases():
    m = load_mapping(DOM)
    assert extract_rules(doc("memo"), chunk("title: x\n", FRONTMATTER), m) == []
    assert extract_rules(doc(), chunk("본문"), m) == []
    assert extract_rules(doc(), chunk("supplier: X\n", FRONTMATTER), m) == []  # 주어 값 없음
    assert extract_rules(doc(), chunk("contract: C\nsupplier:\nbuyer: ''\n", FRONTMATTER), m) == []


def test_row_rules():
    row = json.dumps({"name": "한빛물산", "country": "KR", "ceo": "김", "aliases": "한빛물산(주)|Hanbit|"}, ensure_ascii=False)
    facts = extract_rules(doc("master", "x/suppliers.csv"), chunk(row, ROW), load_mapping(DOM))
    got = {(f.predicate, f.object) for f in facts}
    assert got == {("locatedIn", "KR"), ("hasCEO", Mention("김", "Person")),
                   ("alias", "한빛물산(주)"), ("alias", "Hanbit")}


def test_schema_summary():
    s = schema_summary(DOM)
    assert "Organization" in s and "hasSupplier" in s
    assert "Contract → Organization" in s


BODY = "한빛물산의 대표이사는 박영희 이다.\n본사는 서울에 있다."
GOOD = {"s_label": "한빛물산", "s_cls": "Organization", "p": "hasCEO", "o": "박영희", "o_cls": "Person",
        "quote": "대표이사는 박영희 이다"}


def run(items, text=BODY):
    reply = json.dumps({"facts": items}, ensure_ascii=False)
    return extract_llm(doc(), chunk(text), DOM, [], lambda system, user, schema: reply)


def test_llm_entity_and_literal():
    lit = {**GOOD, "p": "locatedIn", "o": "서울", "o_cls": "literal", "quote": "본사는 서울에 있다"}
    facts = run([GOOD, lit])
    assert facts[0].subject == Mention("한빛물산", "Organization") and facts[0].object == Mention("박영희", "Person")
    assert facts[1].object == "서울"
    assert all(f.chunk_id == "d1#abc" and f.doc_id == "d1" for f in facts)


def test_llm_에_넘기는_데이터():
    d = Document("d1", "/secret/path/a.md", "h", "갱신 계약서", "2026-06-01", "법무팀", 1, "contract")
    known = [Fact(Mention("한빛-2026-02", "Contract"), "hasSupplier", Mention("한빛물산", "Organization"), "d1#f", "d1"),
             Fact(Mention("한빛-2026-02", "Contract"), "amount", "150000000", "d1#f", "d1")]
    system, user, schema = llm_request(d, chunk("이 계약은 한빛-2026-01 을 대체한다.", ("갱신 계약", "계약 개요")), DOM, known)
    # system: 도메인 스키마와 규칙만. 문서에 따라 바뀌는 내용이 없어야 캐시 접두사가 유지된다.
    assert "supersedes(대체함): Contract → Contract" in system and "Organization(조직)" in system
    assert "alias" not in system and "갱신 계약서" not in system
    assert system == llm_request(doc(), chunk("다른 본문"), DOM, [])[0]
    # user: 문서 제목·유형·날짜, 제목 경로, 머리말에서 확정된 사실, 청크 본문
    for part in ["갱신 계약서", "contract", "2026-06-01", "갱신 계약 > 계약 개요",
                 "한빛-2026-02(Contract) hasSupplier 한빛물산(Organization)", "한빛-2026-02(Contract) amount 150000000",
                 "<document>\n이 계약은 한빛-2026-01 을 대체한다.\n</document>"]:
        assert part in user
    # 보내지 않는 것: 파일 경로, 출처 부서와 등급
    assert "/secret/path" not in user and "법무팀" not in user
    item = schema["properties"]["facts"]["items"]
    assert item["properties"]["s_cls"]["enum"] == ["Contract", "Organization", "Person"]
    assert "alias" not in item["properties"]["p"]["enum"] and "hasCEO" in item["properties"]["p"]["enum"]
    assert item["properties"]["o_cls"]["enum"][-1] == "literal"
    assert item["additionalProperties"] is False and schema["additionalProperties"] is False


def test_llm_근거_구절이_본문에_없으면_버린다():
    assert run([{**GOOD, "quote": "대표이사는 김철수 이다"}]) == []
    # 공백·줄바꿈 차이는 허용한다
    assert len(run([{**GOOD, "quote": "대표이사는  박영희\n이다"}])) == 1


def test_llm_스키마_밖의_값은_버린다():
    assert run([{**GOOD, "p": "foo"}]) == []
    assert run([{**GOOD, "s_cls": "Alien"}]) == []
    assert run([{**GOOD, "o_cls": "Alien"}]) == []
    assert run([{**GOOD, "p": "alias", "o": "Hanbit", "o_cls": "literal"}]) == []  # 별칭은 LLM 에서 받지 않는다


def test_llm_틀린_항목만_버리고_나머지는_남긴다():
    bad = [{k: v for k, v in GOOD.items() if k != "quote"}, {**GOOD, "o": "  "}, "str", None, {**GOOD, "s_label": 3}]
    facts = run(bad[:2] + [GOOD] + bad[2:])
    assert len(facts) == 1 and facts[0].predicate == "hasCEO"


@pytest.mark.parametrize("reply", ["모르겠습니다", "", "[]", '{"a": 1}', '{"facts": "none"}'])
def test_llm_형식이_깨진_응답은_빈_결과가_아니라_오류다(reply):
    # 빈 결과로 넘기면 그 청크가 추출 완료로 기록돼 다시 시도되지 않는다
    with pytest.raises(ExtractionError):
        extract_llm(doc(), chunk(BODY), DOM, [], lambda *a: reply)
