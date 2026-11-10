"""소설 도메인: 샘플 이야기를 LLM 없이 턴마다 판정한다."""

import copy
import json
from pathlib import Path

import pytest

from onto.story import APPLIED, NO_CHANGE, REJECTED, SUSPECT, Rejected, Story, diff, generate, initial_state, mentions, run, turn_schema

DOMAIN = Path(__file__).parent.parent / "domains" / "story"


@pytest.fixture
def played():
    return run(DOMAIN)


def test_샘플_이야기의_턴별_판정(played):
    results, _ = played
    assert [v.kind for _, v in results] == [
        NO_CHANGE, APPLIED, APPLIED, APPLIED, NO_CHANGE, REJECTED, SUSPECT, APPLIED, APPLIED, REJECTED, SUSPECT]


def test_죽은_인물의_행동은_거부되고_기록에_남지_않는다(played):
    results, story = played
    turn, v = results[5]
    assert v.kind == REJECTED and "도윤" in v.reasons[0] and "사망" in v.reasons[0]
    assert turn not in story.turns and len(story.turns) == 9


def test_죽은_인물이_회상으로_나오면_넘어가고_회상_표현이_없으면_의심한다(played):
    _, story = played
    assert story.play({"text": "카엘은 꿈에서 도윤을 보았다.", "events": []}).kind == NO_CHANGE
    v = story.play({"text": "도윤이 문을 열고 들어왔다.", "events": []})
    assert v.kind == SUSPECT and "회상인지 확인" in v.reasons[0]


def test_본문은_변화를_말하는데_사건이_없으면_의심하고_상태는_바꾸지_않는다(played):
    results, _ = played
    _, v = results[6]
    assert v.kind == SUSPECT and v.diff == [] and "관계 변화" in v.reasons[0]
    _, pronoun = results[10]
    assert "대상 불명" in pronoun.reasons[0]


def test_다른_장소의_인물에게는_물건을_줄_수_없다(played):
    results, story = played
    assert "같은 장소에 없다" in results[9][1].reasons[0]
    assert story.state_at()["items"]["은빛 단검"] == "카엘"


def test_다음_턴에_넣을_확정_사실(played):
    _, story = played
    text = story.scene()
    assert "등장 가능: 카엘" in text and "등장 불가: 도윤 (사망)" in text
    assert "등장 불가: 세라 (항구에 있음)" in text and "세라·카엘 — 적대" in text


def test_스냅샷에서_이어_적용한_상태는_처음부터_적용한_상태와_같다(played):
    _, story = played
    assert 5 in story.snapshots  # 5턴마다 저장
    for n in range(len(story.turns) + 1):
        via_snapshot = story.state_at(n)
        saved, story.snapshots = story.snapshots, {0: story.snapshots[0]}
        assert story.state_at(n) == via_snapshot
        story.snapshots = saved


def test_되돌리면_그_턴_직후의_상태가_되고_다른_전개를_이어갈_수_있다(played):
    _, story = played
    story.rewind(3)  # 도윤이 죽기 전
    state = story.state_at()
    assert state["chars"]["도윤"]["alive"] and state["items"]["은빛 단검"] == "카엘"
    assert story.play({"text": "도윤이 카엘의 어깨를 두드렸다.", "events": [{"type": "act", "who": "도윤", "with": "카엘"}]}).kind == NO_CHANGE


def test_누락된_사건을_나중에_보정하면_그_뒤의_상태에_반영된다(played):
    _, story = played
    story.rewind(6)  # 배신 턴(사건 누락)까지
    assert story.state_at()["rels"]["세라·카엘"] == "동맹"
    story.amend(6, [{"type": "relate", "a": "세라", "b": "카엘", "kind": "적대"}])
    assert story.state_at()["rels"]["세라·카엘"] == "적대"


def test_보정으로_뒤_턴이_성립하지_않게_되면_보정하지_않는다(played):
    _, story = played
    before = copy.deepcopy(story.turns)
    with pytest.raises(Rejected):
        story.amend(2, [])  # 숲으로 가지 않았다면 3턴의 단검 전달이 성립하지 않는다
    assert story.turns == before and story.state_at()["chars"]["카엘"]["at"] == "숲"


def test_diff_는_바뀐_곳만_돌려준다():
    story = Story(DOMAIN)
    a = initial_state(story.world)
    b = copy.deepcopy(a)
    assert diff(a, b) == []
    b["chars"]["세라"]["at"] = "항구"
    assert diff(a, b) == [("세라", "위치", "왕성", "항구")]


def test_본문에_나온_엔티티만_고른다():
    story = Story(DOMAIN)
    assert mentions("젊은 기사는 단검을 내려다보았다.", story.world) == {"카엘", "은빛 단검"}


@pytest.mark.parametrize("ev, why", [
    ({"type": "die", "who": "도윤"}, "이미 사망"),
    ({"type": "move", "who": "카엘", "to": "달"}, "정의되지 않은 장소"),
    ({"type": "give", "who": "세라", "to": "카엘", "item": "은빛 단검"}, "같은 장소에 없다"),
    ({"type": "relate", "a": "카엘", "b": "세라", "kind": "원수"}, "정의되지 않은 관계"),
    ({"type": "revive", "who": "도윤"}, "정의되지 않은 사건"),
    ({"type": "move", "who": "카엘"}, "필요한 값이 없다"),
])
def test_전제_조건에_어긋나는_사건은_거부한다(played, ev, why):
    _, story = played
    v = story.play({"text": "", "events": [ev]})
    assert v.kind == REJECTED and why in v.reasons[0]


class FakeWriter:
    """정해 둔 응답을 차례로 돌려주고 받은 요청을 기록한다."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, system, user, schema):
        self.calls.append((system, user, schema))
        r = self.replies.pop(0)
        return r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)


def ev(type, a, b=None, item=None, kind=None):
    return {"type": type, "a": a, "b": b, "item": item, "kind": kind}


def test_모델이_낸_본문과_사건이_한_번에_적용된다():
    story = Story(DOMAIN)
    llm = FakeWriter({"text": "카엘은 세라와 숲으로 향했다.", "events": [ev("move", "카엘", "숲"), ev("move", "세라", "숲"), ev("act", "카엘", "세라")]})
    turn, v, calls = generate(story, "숲으로 간다", llm)
    assert v.kind == APPLIED and calls == 1 and len(story.turns) == 1
    assert turn["events"][0] == {"type": "move", "who": "카엘", "to": "숲"}
    _, user, _ = llm.calls[0]
    assert "등장 가능: 카엘, 세라" in user and "등장 불가: 도윤 (숲에 있음)" in user and "<user_input>\n숲으로 간다" in user


def test_규칙에_어긋나면_사유를_돌려주고_다시_쓰게_한다(played):
    _, story = played  # 도윤은 사망, 카엘은 숲
    n = len(story.turns)
    llm = FakeWriter(
        {"text": "도윤이 일어나 카엘에게 말을 걸었다.", "events": [ev("act", "도윤", "카엘")]},
        "JSON 이 아님",
        {"text": "카엘은 도윤의 무덤 앞에서 그의 목소리를 떠올렸다.", "events": [ev("act", "카엘")]},
    )
    turn, v, calls = generate(story, "도윤이 되살아나 말을 건다", llm)
    assert calls == 3 and v.kind == NO_CHANGE and len(story.turns) == n + 1 and "무덤" in turn["text"]
    assert "이미 사망" in llm.calls[1][1] and "출력 형식" in llm.calls[2][1]


def test_끝까지_규칙에_어긋나면_기록하지_않는다(played):
    _, story = played
    n = len(story.turns)
    bad = {"text": "도윤이 걸어 나왔다.", "events": [ev("act", "도윤")]}
    turn, v, calls = generate(story, "도윤 등장", FakeWriter(bad, bad, bad))
    assert turn is None and v.kind == REJECTED and calls == 3 and len(story.turns) == n


def test_출력_스키마는_목록에_있는_이름만_허용한다():
    story = Story(DOMAIN)
    props = turn_schema(story.world, story.schema)["properties"]["events"]["items"]["properties"]
    assert props["a"]["enum"] == ["카엘", "세라", "도윤"]
    assert set(props["b"]["enum"]) == {"카엘", "세라", "도윤", "왕성", "숲", "항구", None}
    assert props["kind"]["enum"][-1] is None and "적대" in props["kind"]["enum"]


def test_대명사_문장의_물건_이름_때문에_선언된_이동을_누락으로_보지_않는다(played):
    _, story = played
    v = story.play({"text": "그는 단검을 쥔 채 숲을 떠나 항구로 향했다.", "events": [{"type": "move", "who": "카엘", "to": "항구"}]})
    assert v.kind == APPLIED and v.reasons == []


def test_사실은_사건으로_성립하고_살아_있는_인물만_성립시킬_수_있다(played):
    _, story = played  # 도윤은 사망
    v = story.play({"text": "", "events": [{"type": "mark", "who": "카엘", "with": "세라", "fact": "카엘이  세라를 용서하지 않기로 했다"}]})
    assert v.kind == APPLIED and v.diff == [("카엘이 세라를 용서하지 않기로 했다", "사실", None, "성립")]
    assert story.state_at()["facts"] == {"카엘이 세라를 용서하지 않기로 했다": {"people": ["카엘", "세라"], "origin": "scene"}}
    assert "성립한 사실: 카엘이 세라를 용서하지 않기로 했다" in story.scene()
    again = story.play({"text": "", "events": [{"type": "mark", "who": "카엘", "fact": "카엘이 세라를 용서하지 않기로 했다"}]})
    assert again.kind == NO_CHANGE  # 이미 성립한 사실은 그대로다
    dead = story.play({"text": "", "events": [{"type": "mark", "who": "도윤", "fact": "도윤이 비밀을 털어놓았다"}]})
    assert dead.kind == REJECTED and "이미 사망" in dead.reasons[0]
