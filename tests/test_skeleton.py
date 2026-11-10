"""골격 사전 검증: 샘플 골격과, 그것을 일부러 망가뜨린 골격을 LLM 없이 검사한다."""

import copy
from pathlib import Path

import pytest

from onto.cli import main
from onto.skeleton import KEEP, REPLAN, REWRITE, impact, load, validate

DOMAIN = Path(__file__).parent.parent / "domains" / "story"


@pytest.fixture
def sk():
    return load(DOMAIN)


def beat(world, beat_id):
    return next(b for b in world["beats"] if b["id"] == beat_id)


def verdicts(world, schema, beat_id, events, replace=False):
    blocked, after = impact(world, schema, beat_id, events, replace)
    return blocked, {bid: v for bid, v, _ in after}


def test_샘플_골격은_처음부터_끝까지_성립한다(sk):
    report = validate(*sk)
    assert report.errors == [] and report.warnings == [] and len(report.beats) == 9


def test_죽은_인물이_뒤_장면에_나오면_그_장면을_짚는다(sk):
    world, schema = sk
    beat(world, "b08")["fixed"].append({"type": "act", "who": "도윤"})
    assert validate(world, schema).errors == [("b08", "도윤 은(는) 이미 사망했다")]


def test_문제가_여러_곳이면_한_번에_모두_찾는다(sk):
    world, schema = sk
    beat(world, "b02")["fixed"] = [{"type": "move", "who": "카엘", "to": "숲"}]  # 세라가 숲에 가지 않는다
    beat(world, "b05")["fixed"] = [{"type": "act", "who": "카엘", "with": "세라"}]  # 그런데 숲에서 세라와 이야기한다
    beat(world, "b08")["fixed"].append({"type": "give", "who": "세라", "to": "카엘", "item": "은빛 단검"})
    assert [bid for bid, _ in validate(world, schema).errors] == ["b05", "b08"]


def test_복선은_심은_뒤에만_회수할_수_있고_회수되지_않으면_알린다(sk):
    world, schema = sk
    beat(world, "b01")["plants"] = []
    beat(world, "b04")["plants"] = ["도윤의 유언"]
    report = validate(world, schema)
    assert ("b06", "심지 않은 복선을 회수한다: 세라의 망설임") in report.errors
    assert any("도윤의 유언" in w and "회수되지 않는다" in w for w in report.warnings)


def test_장면이_쓰는_물건은_그_장면의_인물이_갖고_있어야_한다(sk):
    world, schema = sk
    beat(world, "b07")["uses"] = ["은빛 단검"]  # 단검은 카엘에게 있는데 장면에는 세라만 있다
    assert validate(world, schema).errors == [("b07", "'은빛 단검' 을(를) 가진 인물이 이 장면에 없다(소지자: 카엘)")]


def test_겹치는_장면_ID_와_나오지_않는_인물을_알린다(sk):
    world, schema = sk
    beat(world, "b09")["id"] = "b08"
    world["characters"]["하린"] = {"at": "항구"}
    report = validate(world, schema)
    assert ("b08", "장면 ID 가 겹친다") in report.errors
    assert "하린 이(가) 어느 장면에도 나오지 않는다" in report.warnings


def test_지시서에는_그_장면이_읽는_사실만_들어간다(sk):
    report = validate(*sk)
    brief = {b.id: b.brief for b in report.beats}
    assert "세라" not in brief["b08"] and "단검" not in brief["b08"]  # 세라는 이미 항구로 떠났고 단검은 이 장면과 무관하다
    assert "같은 장소: 카엘, 세라" in brief["b04"]                    # 사건에는 없지만 곁에 있다
    assert "물건: 은빛 단검 — 카엘" in brief["b09"] and "관계: 세라·카엘 적대" in brief["b09"]


def test_인물을_죽이면_그_인물이_필요한_뒤_장면만_다시_계획한다(sk):
    blocked, v = verdicts(*sk, "b03", [{"type": "die", "who": "세라"}])
    assert blocked == []
    assert v == {"b04": REWRITE, "b05": REWRITE, "b06": REPLAN, "b07": REPLAN, "b08": KEEP, "b09": REPLAN}


def test_인물이_따라오지_않으면_그_인물이_있던_장면은_다시_쓰고_합류한_뒤는_그대로_둔다(sk):
    _, v = verdicts(*sk, "b02", [{"type": "move", "who": "카엘", "to": "숲"}], replace=True)
    assert v == {"b03": REWRITE, "b04": REWRITE, "b05": REWRITE, "b06": REWRITE, "b07": REWRITE, "b08": KEEP, "b09": KEEP}


def test_물건을_받지_않으면_그_물건을_쓰는_장면만_다시_계획한다(sk):
    _, v = verdicts(*sk, "b03", [], replace=True)
    assert v == {"b04": KEEP, "b05": KEEP, "b06": KEEP, "b07": KEEP, "b08": KEEP, "b09": REPLAN}


def test_그_장면에서_성립하지_않는_변경은_사유와_함께_막는다(sk):
    blocked, v = verdicts(*sk, "b05", [{"type": "act", "who": "도윤"}])
    assert blocked == ["도윤 은(는) 이미 사망했다"] and set(v.values()) == {KEEP}


def test_변경은_원래_골격을_바꾸지_않는다(sk):
    world, schema = sk
    before = copy.deepcopy(world)
    impact(world, schema, "b03", [{"type": "die", "who": "세라"}])
    assert world == before


def test_없는_장면을_바꾸려_하면_오류다(sk):
    with pytest.raises(ValueError):
        impact(*sk, "b99", [])


def test_명령줄은_골격에_오류가_없으면_0_을_돌려준다(capsys):
    assert main(["skeleton", str(DOMAIN), "--change", "b03", '{"type": "die", "who": "세라"}']) == 0
    out = capsys.readouterr().out
    assert "오류 0" in out and "[다시 계획] b06" in out and "[유지] b08" in out


def test_다시_쓰는_장면은_지시서에서_달라진_줄을_알려준다(sk):
    _, after = impact(*sk, "b03", [{"type": "die", "who": "세라"}])
    reasons = {bid: r for bid, _, r in after}
    assert reasons["b04"] == ["- 같은 장소: 카엘, 세라", "+ 같은 장소: 카엘"]
    assert reasons["b05"] == ["- 같은 장소: 세라"]
