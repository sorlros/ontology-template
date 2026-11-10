"""통합 시험: 샘플 도메인을 실제로 수집 → 추출 → 해소 → 적재 → 내보내기 한다."""

import json
import shutil
from pathlib import Path

import pytest

from onto.cli import ingest, main
from onto.store import Store

SRC = Path(__file__).parent.parent / "domains" / "contracts"
OLD = "한빛-2026-01"


@pytest.fixture
def env(tmp_path):
    domain = tmp_path / "contracts"
    shutil.copytree(SRC, domain)
    return domain, tmp_path / "data"


def export(env):
    domain, data = env
    return Store(domain, data / "store.trig").export_json()


def facts(env, s, p):
    return {f["o"]: f for f in export(env)["facts"] if f["s"] == s and f["p"] == p}


def edit(env, name, old, new):
    p = env[0] / "docs" / name
    text = p.read_text(encoding="utf-8")
    assert old in text
    p.write_text(text.replace(old, new), encoding="utf-8")


def test_교차확인_충돌_별칭병합(env):
    reports, rejected, gone, _ = ingest(*env)
    assert rejected == {} and gone == []
    assert all(r.quarantined == [] for r in reports.values())

    amount = facts(env, OLD, "amount")
    # 법무팀(1등급)과 구매팀(2등급)이 같은 값: 1 - 0.1 * 0.3. 법무팀 사본 c-004 는 출처 수를 늘리지 않는다.
    assert amount["100000000"]["docs"] == ["c-001", "c-002", "c-004"]
    assert amount["100000000"]["confidence"] == 0.97
    assert amount["120000000"]["docs"] == ["c-003"]
    assert amount["120000000"]["confidence"] == 0.5
    assert amount["100000000"]["conflict"] and amount["120000000"]["conflict"]

    # c-003 은 공급사를 별칭 Hanbit 으로 적었지만 같은 엔티티로 묶이고, 이름은 마스터의 대표 이름으로 보인다.
    supplier = facts(env, OLD, "hasSupplier")
    assert list(supplier) == ["한빛물산"]
    assert supplier["한빛물산"]["docs"] == ["c-001", "c-002", "c-003", "c-004"]
    assert not supplier["한빛물산"]["conflict"]
    assert "한빛-2026-01" in facts(env, "한빛-2026-02", "supersedes")


def test_다시_투입해도_변하지_않는다(env):
    ingest(*env)
    before = export(env)
    reports, _, _, _ = ingest(*env)
    assert all(r.added == [] and r.removed == [] for r in reports.values())
    assert export(env) == before


def test_문서_본문_한곳을_고치면_그_청크만_교체되고_그_청크만_LLM_에_다시_보낸다(env):
    llm = FakeLLM()
    ingest(*env, complete=llm)
    first = len(llm.calls)
    assert first > 6  # 본문 청크마다 한 번
    ingest(*env, complete=llm)
    assert len(llm.calls) == first  # 바뀐 것이 없으면 호출하지 않는다

    edit(env, "c-004.md", "지연 시 협의한다", "지연 시 위약금을 문다")
    reports, _, _, _ = ingest(*env, complete=llm)
    assert len(llm.calls) == first + 1 and "위약금" in llm.calls[-1][1]
    r = reports.pop("c-004")
    assert (len(r.added), len(r.removed), len(r.kept)) == (1, 1, 2)
    assert all(x.added == [] and x.removed == [] for x in reports.values())


def test_틀린_금액을_고치면_충돌이_사라지고_신뢰도가_오른다(env):
    ingest(*env)
    edit(env, "c-003.md", "amount: 120000000", "amount: 100000000")
    ingest(*env)
    amount = facts(env, OLD, "amount")
    assert list(amount) == ["100000000"]
    assert not amount["100000000"]["conflict"]
    assert amount["100000000"]["docs"] == ["c-001", "c-002", "c-003", "c-004"]
    assert amount["100000000"]["confidence"] > 0.97


def test_문서를_지우면_그_문서의_사실만_회수된다(env):
    ingest(*env)
    (env[0] / "docs" / "c-003.md").unlink()
    _, rejected, gone, _ = ingest(*env)
    assert rejected == {} and gone == ["c-003"]
    amount = facts(env, OLD, "amount")
    assert list(amount) == ["100000000"] and not amount["100000000"]["conflict"]
    assert facts(env, OLD, "hasSupplier")["한빛물산"]["docs"] == ["c-001", "c-002", "c-004"]


def test_메타_없는_문서는_거부하고_나머지는_적재한다(env, capsys):
    (env[0] / "docs" / "bad.md").write_text("---\ntitle: 메타 없음\n---\n본문\n", encoding="utf-8")
    assert main(["ingest", str(env[0]), "--data", str(env[1])]) == 1
    assert "bad.md" in capsys.readouterr().err
    assert facts(env, OLD, "amount")


def test_같은_id_를_가진_파일들은_모두_거부하고_기존_적재분은_지키다(env):
    ingest(*env)
    before = export(env)
    shutil.copy(env[0] / "docs" / "c-001.md", env[0] / "docs" / "c-001-copy.md")
    reports, rejected, gone, _ = ingest(*env)
    assert sorted(rejected) == ["c-001-copy.md", "c-001.md"]
    assert "c-001" not in reports and gone == []
    assert export(env) == before


def test_파일이름에_공백이_있어도_저장후_다시_읽힌다(env):
    text = (env[0] / "docs" / "m-001.md").read_text(encoding="utf-8").replace("id: m-001\n", "")
    (env[0] / "docs" / "동향 메모 (2차).md").write_text(text, encoding="utf-8")
    ingest(*env)
    reports, rejected, _, _ = ingest(*env)
    r = reports["동향 메모 (2차)"]
    assert rejected == {} and r.added == [] and len(r.kept) == 1  # 본문은 LLM 없이는 저장하지 않는다


CEO_QUOTE = "대표이사가 홍길동에서 김하늘로 교체되었다"


class FakeLLM:
    """본문에 대표 교체가 있으면 그 사실과, 걸러져야 할 항목 둘을 돌려준다. 받은 요청을 기록한다."""

    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def __call__(self, system: str, user: str, schema: dict) -> str:
        self.calls.append((system, user, schema))
        body = user.split("<document>")[1]
        if "김하늘" not in body:
            return '{"facts": []}'
        if self.fail:
            raise self.fail
        org = {"s_label": "한빛물산", "s_cls": "Organization", "quote": CEO_QUOTE}
        return json.dumps({"facts": [
            {**org, "p": "hasCEO", "o": "김하늘", "o_cls": "Person"},
            {**org, "s_label": "대한소재", "p": "alias", "o": "한빛물산", "o_cls": "literal"},  # 엔티티를 합치려는 별칭
            {**org, "p": "hasCEO", "o": "오리온제조", "o_cls": "Organization"},  # 치역이 틀림
        ]}, ensure_ascii=False)


def test_본문에서_뽑은_사실은_충돌로_드러나고_별칭으로_엔티티를_합치지_못한다(env):
    reports, _, _, warnings = ingest(*env, complete=FakeLLM())
    assert warnings == []
    ceo = facts(env, "한빛물산", "hasCEO")
    assert set(ceo) == {"홍길동", "김하늘"}
    assert ceo["홍길동"]["conflict"] and ceo["김하늘"]["conflict"]
    assert ceo["김하늘"]["docs"] == ["c-005", "m-001"]
    # 치역이 틀린 사실은 문서마다 하나씩 격리된다
    reasons = [reason for r in reports.values() for _, reason in r.quarantined]
    assert len(reasons) == 2 and all("치역" in x for x in reasons)
    # LLM 이 낸 별칭은 저장되지도, 엔티티를 합치지도 않는다
    out = export(env)
    labels = [n["label"] for n in out["nodes"]]
    assert "한빛물산" in labels and "대한소재" in labels
    assert not [f for f in out["facts"] if f["p"] == "alias" and f["o"] == "한빛물산"]
    assert facts(env, OLD, "hasSupplier")["한빛물산"]["docs"] == ["c-001", "c-002", "c-003", "c-004"]
    # 근거 패널에 본문 구절이 나온다
    assert any(CEO_QUOTE in out["evidence"][c]["text"] for c in ceo["김하늘"]["chunks"])


def test_LLM_에는_머리말의_확정_사실이_맥락으로_함께_간다(env):
    llm = FakeLLM()
    ingest(*env, complete=llm)
    user = next(u for _, u, _ in llm.calls if "이 계약은 한빛-2026-01 을 대체한다" in u)
    assert "한빛-2026-02(Contract) supersedes 한빛-2026-01(Contract)" in user
    assert "갱신 계약 > 계약 개요" in user and "2026-06-01" in user
    assert len({system for system, _, _ in llm.calls}) == 1  # system 은 모든 호출에서 같다


def test_추출에_실패한_청크는_저장하지_않고_다음_실행에서_다시_시도한다(env):
    from onto.extract import ExtractionError
    reports, _, _, warnings = ingest(*env, complete=FakeLLM(fail=ExtractionError("일시적 실패")))
    assert len(warnings) == 2 and all("다시 시도" in w for w in warnings)
    assert set(facts(env, "한빛물산", "hasCEO")) == {"홍길동"}
    assert facts(env, OLD, "amount")  # 실패와 무관한 사실은 적재된다

    llm = FakeLLM()
    _, _, _, warnings = ingest(*env, complete=llm)
    assert warnings == [] and len(llm.calls) == 2  # 실패했던 두 청크만 다시 보낸다
    assert set(facts(env, "한빛물산", "hasCEO")) == {"홍길동", "김하늘"}


def test_인증_오류처럼_모든_호출이_실패할_오류면_남은_호출을_건너뛴다(env):
    from onto.extract import ExtractionError
    llm = FakeLLM(fail=ExtractionError("API 오류 401", fatal=True))
    _, _, _, warnings = ingest(*env, complete=llm)
    failed_at = next(i for i, (_, u, _) in enumerate(llm.calls) if "김하늘" in u)
    assert len(llm.calls) == failed_at + 1 and len(warnings) == 1


def test_LLM_없이_넣은_뒤_켜면_본문이_추출되고_다시_끄고_돌려도_사실이_남는다(env):
    ingest(*env)
    assert set(facts(env, "한빛물산", "hasCEO")) == {"홍길동"}
    ingest(*env, complete=FakeLLM())
    assert set(facts(env, "한빛물산", "hasCEO")) == {"홍길동", "김하늘"}
    reports, _, _, _ = ingest(*env)
    assert all(r.removed == [] for r in reports.values())
    assert set(facts(env, "한빛물산", "hasCEO")) == {"홍길동", "김하늘"}
