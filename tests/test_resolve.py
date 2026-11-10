from pathlib import Path

from onto.model import Fact, Mention
from onto.resolve import Registry


def alias_fact(name, alias, cls="Organization"):
    return Fact(Mention(name, cls), "alias", alias, "c#1", "c")


def test_alias_resolves_same_iri():
    r = Registry()
    r.learn([alias_fact("한빛물산", "Hanbit"), alias_fact("한빛물산", "한빛물산(주)")])
    base = r.resolve(Mention("한빛물산", "Organization"))
    assert r.resolve(Mention("Hanbit", "Organization")) == base
    assert r.resolve(Mention("HANBIT", "Organization")) == base
    assert r.resolve(Mention("한빛물산 (주)", "Organization")) == base
    assert r.resolve(Mention("한빛물산(주)", "Organization")) == base


def test_unknown_names_normalized():
    r = Registry()
    assert r.resolve(Mention("Acme  Corp.", "Organization")) == r.resolve(Mention("acme corp", "Organization"))
    assert r.resolve(Mention("Acme", "Organization")) != r.resolve(Mention("Other", "Organization"))


def test_class_separates_entities():
    r = Registry()
    r.learn([alias_fact("한빛", "H")])
    assert r.resolve(Mention("한빛", "Organization")) != r.resolve(Mention("한빛", "Person"))
    assert r.resolve(Mention("H", "Person")) != r.resolve(Mention("한빛", "Person"))


def test_save_and_reload(tmp_path: Path):
    p = tmp_path / "reg.json"
    r = Registry(p)
    r.learn([alias_fact("한빛물산", "Hanbit")])
    r.save()
    r2 = Registry(p)
    assert r2.resolve(Mention("Hanbit", "Organization")) == r.resolve(Mention("한빛물산", "Organization"))


def test_idempotent():
    assert Registry().resolve(Mention("X", "Person")) == Registry().resolve(Mention("X", "Person"))


# --- 메인 세션 검토에서 추가한 회귀 시험 ---

def test_다른_엔티티가_같은_별칭을_주장하면_먼저_등록된_쪽을_지키고_알린다():
    r = Registry()
    a, b = Mention("한빛물산", "Organization"), Mention("한빛전자", "Organization")
    assert r.learn([Fact(a, "alias", "Hanbit", "c1", "d1")]) == []
    ignored = r.learn([Fact(b, "alias", "Hanbit", "c2", "d2")])
    assert len(ignored) == 1 and "Hanbit" in ignored[0]
    assert r.resolve(Mention("Hanbit", "Organization")) == r.resolve(a)
    # 같은 주장을 다시 배우는 것은 충돌이 아니다
    assert r.learn([Fact(a, "alias", "Hanbit", "c1", "d1")]) == []


def test_문장부호뿐인_이름끼리_한_엔티티로_합쳐지지_않는다():
    r = Registry()
    assert r.resolve(Mention("(주)", "Organization")) != r.resolve(Mention("[?]", "Organization"))
