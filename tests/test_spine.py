"""주요 사건(뼈대) 검증: 샘플 뼈대와, 그것을 일부러 망가뜨린 뼈대를 LLM 없이 검사한다."""

from pathlib import Path

import pytest

from onto.skeleton import walk_from
from onto.spine import check_craft, check_segment, check_spine, load, offer, task, walk_spine
from onto.story import initial_state

DOMAIN = Path(__file__).parent.parent / "domains" / "story"


@pytest.fixture
def sp():
    return load(DOMAIN)


def event(world, eid):
    return next(e for e in world["spine"] if e["id"] == eid)


def after_first(world, schema):
    """첫 주요 사건이 끝난 뒤의 상태."""
    return walk_spine(world, schema, world["spine"][:1], initial_state(world))[1]


def test_샘플_뼈대는_성립하고_사건_사이의_틈을_과제로_돌려준다(sp):
    report = check_spine(*sp)
    assert report.errors == [] and report.warnings == []
    gaps = {r.id: r.gaps for r in report.events}
    assert gaps["e01"] == [] and gaps["e03"] == []
    assert gaps["e02"] == [("together", "도윤", "카엘")]          # 건네려면 같은 장소에 있어야 한다(모델이 적지 않아도 코드가 더한다)
    assert gaps["e05"] == [("at", "세라", "항구"), ("together", "카엘", "세라")]


def test_죽은_인물이_필요한_사건은_틈이_아니라_오류다(sp):
    world, schema = sp
    event(world, "e05")["needs"].append(["alive", "도윤"])
    assert check_spine(world, schema).errors == [("e05", "도윤 은(는) 이미 사망했다 — 필요한 조건: 도윤 이(가) 살아 있다")]


def test_죽은_인물이_가진_물건은_넘겨받을_수_없다(sp):
    world, schema = sp
    event(world, "e02")["events"] = [{"type": "act", "who": "카엘", "with": "도윤"}]  # 단검을 받지 않은 채 도윤이 죽는다
    assert ("e05", "은빛 단검 을(를) 가진 도윤 이(가) 이미 사망했다 — 필요한 조건: 카엘 이(가) 은빛 단검 을(를) 갖고 있다") in check_spine(world, schema).errors


def test_원인이_없거나_뒤의_사건을_가리키면_오류다(sp):
    world, schema = sp
    event(world, "e03")["because"] = []
    event(world, "e02")["because"] = ["e04"]
    errors = check_spine(world, schema).errors
    assert ("e03", "원인(because)이 없다") in errors and ("e02", "원인 'e04' 이(가) 앞선 사건이 아니다") in errors


def test_시간이_거꾸로_가면_오류다(sp):
    world, schema = sp
    event(world, "e04")["when"] = 2
    assert ("e04", "시간이 거꾸로 간다(3일 → 2일)") in check_spine(world, schema).errors


def test_정의되지_않은_이름을_쓰면_오류다(sp):
    world, schema = sp
    event(world, "e05")["needs"] = [["at", "세라", "사막"], ["has", "하린", "은빛 단검"]]
    errors = [why for _, why in check_spine(world, schema).errors]
    assert "'사막' 은(는) 정의되지 않은 장소다" in errors and "'하린' 은(는) 등장인물이 아니다" in errors


def test_심지_않은_복선과_회수되지_않는_복선(sp):
    world, schema = sp
    event(world, "e01")["plants"] = ["왕의 밀명"]
    report = check_spine(world, schema)
    assert ("e04", "심지 않은 복선을 회수한다: 세라의 망설임") in report.errors
    assert any("왕의 밀명" in w for w in report.warnings)


def test_오랜만에_필요한_인물은_앞_구간에서_다시_등장시키라고_알린다(sp):
    world, schema = sp
    world["characters"]["하린"] = {"at": "항구"}
    event(world, "e01")["events"].append({"type": "act", "who": "하린"})
    event(world, "e05")["needs"].append(["alive", "하린"])
    assert any(w.startswith("e05: 하린 이(가) 오랜만에 나온다") for w in check_spine(world, schema).warnings)
    assert task(world, schema, 4)["reintroduce"] == ["하린"]


def test_구간의_과제에는_틈과_흐른_날수가_들어간다(sp):
    t = task(*sp, 3)
    assert t == {"gaps": [("together", "세라", "카엘")], "days": 7, "reintroduce": []}


def test_샘플_구간은_다음_주요_사건에_도착한다(sp):
    world, schema = sp
    assert check_segment(world, schema, after_first(world, schema), world["segment"], world["spine"][1]) == ([], [])


def test_구간이_틈을_메우지_못하면_남은_과제를_돌려준다(sp):
    world, schema = sp
    beats = [{"id": "s1", "fixed": [{"type": "act", "who": "카엘"}]}]  # 숲으로 가지 않는다
    errors, unmet = check_segment(world, schema, after_first(world, schema), beats, world["spine"][1])
    assert errors == [] and unmet == [("together", "도윤", "카엘")]


def test_구간의_장면이_성립하지_않으면_그_장면을_짚는다(sp):
    world, schema = sp
    beats = [{"id": "s1", "fixed": [{"type": "act", "who": "카엘", "with": "도윤"}]}]  # 아직 다른 장소다
    errors, _ = check_segment(world, schema, after_first(world, schema), beats, world["spine"][1])
    assert errors[0][0] == "s1" and "같은 장소에 없다" in errors[0][1]


def choices(world, schema):
    beats = world["segment"]
    _, state = walk_from(world, schema, beats[:1], after_first(world, schema))
    return {c["id"]: c for c in offer(world, schema, state, beats[0], beats[1:], world["spine"][1:], want=6)}


def test_성립하지_않는_선택지와_뼈대를_깨는_선택지는_내놓지_않는다(sp):
    shown = choices(*sp)
    assert "c4" not in shown  # 세라를 죽이면 뒤의 '배신'이 불가능해진다
    assert "c5" not in shown  # 카엘은 아직 단검을 갖고 있지 않다
    assert set(shown) == {"c1", "c2", "c3", "c6", "c7"}


def test_구간의_남은_장면을_깨는_선택지는_다시_계획이_필요하다고_표시한다(sp):
    shown = choices(*sp)
    assert shown["c3"]["replan"] is True       # 도윤이 왕성으로 가면 '숲의 오두막' 장면이 성립하지 않는다
    assert shown["c1"]["replan"] is False and shown["c2"]["replan"] is False


def test_선택지는_세_개를_고르되_태도와_행동이_하나씩은_들어간다(sp):
    world, schema = sp
    beats = world["segment"]
    _, state = walk_from(world, schema, beats[:1], after_first(world, schema))
    shown = offer(world, schema, state, beats[0], beats[1:], world["spine"][1:])
    assert [c["id"] for c in shown] == ["c1", "c2", "c3"]
    beats[0]["choices"] = [c for c in beats[0]["choices"] if c["kind"] == "tone"] + [beats[0]["choices"][1]]
    shown = offer(world, schema, state, beats[0], beats[1:], world["spine"][1:], want=2)
    assert [c["kind"] for c in shown] == ["tone", "action"]


def test_검증은_넘겨받은_상태를_바꾸지_않는다(sp):
    world, schema = sp
    state = after_first(world, schema)
    before = repr(state)
    check_segment(world, schema, state, world["segment"], world["spine"][1])
    walk_spine(world, schema, world["spine"][1:], state)
    assert repr(state) == before


# ---------- 짜임새 ----------

def craft(world, schema):
    return [why for _, why in check_craft(world, schema)]


def test_샘플_뼈대는_짜임새_검사를_통과한다(sp):
    assert check_craft(*sp) == []


def test_결말에_대가가_없으면_알린다(sp):
    world, schema = sp
    event(world, "e04")["events"] = [{"type": "act", "who": "세라", "with": "카엘"}]  # 관계가 끊기지 않는다
    assert "마지막 2개 사건에 되돌릴 수 없는 대가(죽음, 관계의 단절, 주인공이 물건을 내놓음)가 없다" in craft(world, schema)
    event(world, "e05")["events"].append({"type": "give", "who": "카엘", "to": "세라", "item": "은빛 단검"})
    assert not any("대가(죽음" in w for w in craft(world, schema))  # 주인공이 물건을 내놓는 것도 대가다


def test_주인공이_끌려다니기만_하면_알린다(sp):
    world, schema = sp
    event(world, "e01")["events"] = [{"type": "act", "who": "세라"}]
    event(world, "e05")["events"] = [{"type": "act", "who": "세라", "with": "카엘"}]
    assert "주인공이 직접 행동하는 사건이 5개 중 1개뿐이다(절반 이상 필요)" in craft(world, schema)


def test_적대자와_조력자가_스스로_움직이지_않으면_알린다(sp):
    world, schema = sp
    event(world, "e04")["events"] = [{"type": "relate", "a": "카엘", "b": "세라", "kind": "적대"}]  # 카엘이 먼저 움직인다
    assert "적대자가 먼저 움직이는 사건이 0개뿐이다(1개 이상 필요)" in craft(world, schema)
    event(world, "e02")["events"] = [{"type": "act", "who": "카엘", "with": "도윤"}]
    assert "도윤(ally) 이(가) 어느 사건에서도 직접 행동하지 않는다" in craft(world, schema)


def test_긴장의_굴곡을_본다(sp):
    world, schema = sp
    levels = lambda xs: [e.update(intensity=x) for e, x in zip(world["spine"], xs)]
    levels([1, 2, 3, 4, 5])
    assert craft(world, schema) == ["후반부에 절정을 앞두고 강도가 한 번 꺾이는 사건이 없다"]
    levels([5, 4, 3, 3, 3])
    problems = craft(world, schema)
    assert "강도가 가장 높은 사건(e01)이 마지막 2개 안에 없다" in problems
    assert any("뒤로 갈수록 강도가 오르지 않는다" in w for w in problems)
    levels([1, 2, 4, 3, 9])
    assert ("e05", "강도(intensity)가 1~5 의 정수가 아니다") in check_craft(world, schema)


def test_대가와_딜레마_칸이_비면_그_사건을_짚는다(sp):
    world, schema = sp
    event(world, "e03")["cost"] = " "
    event(world, "e05")["dilemma"] = ""
    assert check_craft(world, schema) == [("e03", "강도 3 이상인 사건인데 대가(cost)가 비어 있다"),
                                          ("e05", "주인공이 행동하는 사건인데 딜레마(dilemma)가 비어 있다")]


def test_역할이_없는_세계와_군상극은_해당_검사를_건너뛴다(sp):
    world, schema = sp
    world["viewpoint"] = None
    for c in world["characters"].values():
        c.pop("role")
    assert check_craft(world, schema) == []


# ---------- 성립한 사실 ----------

def test_사실은_사건으로_성립하고_뒤의_사건이_조건으로_쓴다(sp):
    world, schema = sp
    results, end = walk_spine(world, schema, world["spine"], initial_state(world))
    assert end["facts"] == {"세라가 매복을 알린 사실이 드러났다": {"people": ["세라", "카엘"], "origin": "spine"}}
    assert ("fact", "세라가 매복을 알린 사실이 드러났다") not in results[4].gaps  # e04 가 성립시켰다


def test_성립하지_않은_사실이_필요하면_앞_구간의_과제가_된다(sp):
    world, schema = sp
    event(world, "e04")["events"] = [{"type": "relate", "a": "세라", "b": "카엘", "kind": "적대"}]  # 드러내지 않는다
    report = check_spine(world, schema)
    assert report.errors == []
    assert ("fact", "세라가 매복을 알린 사실이 드러났다") in {r.id: r.gaps for r in report.events}["e05"]


def test_목록에_없는_사실의_이름은_오류다(sp):
    world, schema = sp
    event(world, "e05")["needs"].append(["fact", "세라의 배신이 드러남"])  # 이름이 어긋났다
    event(world, "e03")["events"].append({"type": "mark", "who": "카엘", "fact": "카엘이 복수를 맹세했다"})
    errors = check_spine(world, schema).errors
    assert ("e05", "'세라의 배신이 드러남' 은(는) 정의되지 않은 사실이다") in errors
    assert ("e03", "'카엘이 복수를 맹세했다' 은(는) 정의되지 않은 사실이다") in errors


def test_쓰이지_않는_사실은_알린다(sp):
    world, schema = sp
    world["facts"].append("왕이 밀명을 내렸다")
    assert "사실 '왕이 밀명을 내렸다' 이(가) 어느 주요 사건에서도 쓰이지 않는다" in check_spine(world, schema).warnings


def test_선택지가_남긴_사실은_그_인물이_나오는_다음_장면의_지시서에_들어간다(sp):
    from onto.skeleton import brief
    from onto.story import apply
    world, schema = sp
    beats = world["segment"]
    _, state = walk_from(world, schema, beats[:1], after_first(world, schema))
    c7 = next(c for c in beats[0]["choices"] if c["id"] == "c7")
    for ev in c7["events"]:
        apply(state, ev, schema, world["locations"], "choice")
    assert "독자의 선택: 카엘이 세라에게 그날 밤의 일을 묻기로 다짐했다" in brief(beats[1], state, world)  # 카엘이 나오는 장면
    other = {"id": "x", "title": "왕성의 세라", "fixed": [{"type": "act", "who": "세라"}]}
    assert "다짐했다" not in brief(other, state, world)                                                # 카엘이 없는 장면


# ---------- 사실의 중요도 ----------

def marked(world, schema, *marks):
    """(origin, 인물, 사실) 들을 순서대로 성립시킨 상태."""
    from onto.story import apply
    state = initial_state(world)
    for origin, who, fact in marks:
        apply(state, {"type": "mark", "who": who, "fact": fact}, schema, world["locations"], origin)
    return state


SCENE_BEAT = {"id": "x", "title": "카엘의 장면", "fixed": [{"type": "act", "who": "카엘"}]}


def test_중요도는_사실이_어디서_성립했는지로_정한다(sp):
    from onto.skeleton import CHOICE, CORE, SCENE, fact_tier
    world, schema = sp
    state = marked(world, schema, ("scene", "카엘", "카엘이 말에게 물을 먹였다"), ("choice", "카엘", "카엘이 돌아오겠다고 약속했다"),
                   ("scene", "세라", "세라가 매복을 알린 사실이 드러났다"))  # 뼈대가 정한 이름은 누가 성립시켜도 핵심이다
    tiers = {name: fact_tier(name, rec, world) for name, rec in state["facts"].items()}
    assert tiers == {"카엘이 말에게 물을 먹였다": SCENE, "카엘이 돌아오겠다고 약속했다": CHOICE, "세라가 매복을 알린 사실이 드러났다": CORE}


def test_지시서는_등급이_높은_사실부터_이름표를_붙여_넣는다(sp):
    from onto.skeleton import brief
    world, schema = sp
    state = marked(world, schema, ("scene", "카엘", "카엘이 말에게 물을 먹였다"), ("choice", "카엘", "카엘이 돌아오겠다고 약속했다"),
                   ("spine", "카엘", "세라가 매복을 알린 사실이 드러났다"))
    lines = [x for x in brief(SCENE_BEAT, state, world).split("\n") if ": 카엘이" in x or "매복" in x]
    assert lines == ["사실: 세라가 매복을 알린 사실이 드러났다", "독자의 선택: 카엘이 돌아오겠다고 약속했다", "최근의 일: 카엘이 말에게 물을 먹였다"]


def test_사소한_사실이_아무리_많아도_중요한_사실을_밀어내지_못한다(sp):
    from onto.skeleton import brief
    world, schema = sp
    marks = [("spine", "카엘", "세라가 매복을 알린 사실이 드러났다"), ("choice", "카엘", "카엘이 돌아오겠다고 약속했다")]
    marks += [("scene", "카엘", f"사소한 일 {i}") for i in range(20)]  # 그 뒤로 사소한 일이 스무 번 쌓인다
    text = brief(SCENE_BEAT, marked(world, schema, *marks), world)
    assert "사실: 세라가 매복을 알린 사실이 드러났다" in text and "독자의 선택: 카엘이 돌아오겠다고 약속했다" in text
    assert [x for x in text.split("\n") if x.startswith("최근의 일")] == ["최근의 일: 사소한 일 18", "최근의 일: 사소한 일 19"]  # 가장 최근 둘만


def test_독자의_선택도_상한을_넘으면_최근_것만_넣는다(sp):
    from onto.skeleton import brief
    world, schema = sp
    state = marked(world, schema, *[("choice", "카엘", f"선택 {i}") for i in range(6)])
    assert [x for x in brief(SCENE_BEAT, state, world).split("\n") if x.startswith("독자의 선택")] == [f"독자의 선택: 선택 {i}" for i in range(2, 6)]


def test_장면이_조건으로_삼는_사실은_오래됐어도_다른_인물의_것이어도_넣는다(sp):
    from onto.skeleton import brief
    world, schema = sp
    marks = [("spine", "세라", "세라가 매복을 알린 사실이 드러났다")] + [("spine", "카엘", f"핵심 {i}") for i in range(10)]
    beat = {**SCENE_BEAT, "needs": [["fact", "세라가 매복을 알린 사실이 드러났다"]]}
    text = brief(beat, marked(world, schema, *marks), world)
    assert "사실: 세라가 매복을 알린 사실이 드러났다" in text                        # 세라의 사실이고 가장 오래됐지만 들어간다
    assert len([x for x in text.split("\n") if x.startswith("사실:")]) == 8        # 전체 상한은 그대로다
    assert "세라가 매복" not in brief(SCENE_BEAT, marked(world, schema, *marks), world)  # 조건이 아니면 세라의 사실은 빠진다


def test_주요_사건이_끝나면_장면의_사실은_잊고_선택과_핵심은_남는다(sp):
    world, schema = sp
    state = marked(world, schema, ("scene", "카엘", "카엘이 말에게 물을 먹였다"), ("choice", "카엘", "카엘이 돌아오겠다고 약속했다"))
    _, end = walk_spine(world, schema, world["spine"][:4], state)
    assert list(end["facts"]) == ["카엘이 돌아오겠다고 약속했다", "세라가 매복을 알린 사실이 드러났다"]
    assert "카엘이 말에게 물을 먹였다" in state["facts"]  # 넘겨받은 상태는 바꾸지 않는다
