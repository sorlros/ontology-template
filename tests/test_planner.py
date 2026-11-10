"""뼈대 생성 절차: 가짜 모델로 정상 경로와 재작성 경로를 확인한다(실제 호출 없음)."""

import json
from pathlib import Path

import yaml

from onto.planner import run

SCHEMA = yaml.safe_load((Path(__file__).parent.parent / "domains" / "story" / "schema.yaml").read_text(encoding="utf-8"))
SEED = {"genre": "판타지", "tone": "진지한", "name": "카엘", "traits": ["용감한"]}

WORLD = {
    "premise": "기사가 스승의 죽음 뒤에 숨은 배신을 좇는다.",
    "locations": ["왕성", "숲", "항구"],
    "characters": [
        {"name": "카엘", "role": "protagonist", "wants": "진실", "voice": "짧게 말한다", "at": "왕성"},
        {"name": "세라", "role": "antagonist", "wants": "자유", "voice": "돌려 말한다", "at": "왕성"},
        {"name": "도윤", "role": "ally", "wants": "속죄", "voice": "느리게 말한다", "at": "숲"},
    ],
    "items": [{"name": "은빛 단검", "holder": "도윤"}],
    "relations": [{"a": "카엘", "b": "세라", "kind": "동맹"}],
}


def ch(type, a, b=None, item=None, kind=None):
    return {"type": type, "a": a, "b": b, "item": item, "kind": kind}


def ev(id, when, changes, because, needs=(), plants=(), pays=(), intensity=1, dilemma="", cost=""):
    return {"id": id, "when": when, "title": id, "meaning": "…", "intensity": intensity, "dilemma": dilemma, "cost": cost, "changes": changes,
            "needs": [{"pred": p, "a": a, "b": b, "kind": None} for p, a, b in needs],
            "because": because, "plants": list(plants), "pays": list(pays)}


E1 = ev("e1", 1, [ch("act", "카엘", "세라")], [], plants=["망설임"], dilemma="캐물을지 넘어갈지")
E2 = ev("e2", 3, [ch("give", "도윤", "카엘", item="은빛 단검")], ["e1"], intensity=3, cost="단검이 손을 떠난다")
E3 = ev("e3", 4, [ch("die", "도윤")], ["e2"], intensity=2)
E4 = ev("e4", 9, [ch("relate", "세라", "카엘", kind="적대")], ["e3"], pays=["망설임"], intensity=5, dilemma="붙잡을지 보낼지", cost="동맹이 끝난다")
BAD4 = ev("e4", 9, [ch("act", "도윤", "카엘")], [], pays=["망설임"], intensity=5, cost="…")  # 죽은 인물이 행동하고 원인도 없다
FLAT4 = {**E4, "intensity": 1, "cost": ""}  # 사실은 맞지만 절정이 없다

SEGMENT = {"beats": [{"title": "숲으로", "changes": [ch("move", "카엘", "숲")], "open": ["길"], "choices": [
    {"label": "말없이 걷는다", "kind": "tone", "changes": []},
    {"label": "세라와 간다", "kind": "action", "changes": [ch("move", "세라", "숲")]},
    {"label": "세라를 벤다", "kind": "action", "changes": [ch("die", "세라")]},
    {"label": "서두른다", "kind": "tone", "changes": []},
]}]}
TONES = [SEGMENT["beats"][0]["choices"][0], SEGMENT["beats"][0]["choices"][3]]  # 태도만 고르는 선택지 둘
STUCK = {"beats": [{"title": "머뭇거림", "changes": [ch("act", "카엘")], "open": [], "choices": SEGMENT["beats"][0]["choices"]}]}


class Fake:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, system, user, schema):
        self.calls.append((system, user, schema))
        return json.dumps(self.replies.pop(0), ensure_ascii=False)


def test_한_번에_통과하면_모델을_세_번만_부른다():
    llm = Fake(WORLD, {"ending": "끝", "facts": [], "events": [E4, E3, E2, E1]}, SEGMENT)  # 모델은 결말부터 거꾸로 적는다
    r = run(SEED, SCHEMA, llm)
    assert r["passed"] and len(llm.calls) == 3 and r["repairs"] == []
    assert [e["id"] for e in r["world"]["spine"]] == ["e1", "e2", "e3", "e4"]
    assert r["first_draft"] == {"events": 4, "errors": [], "craft": [], "warnings": [], "because_filled": 1.0, "needs_filled": 0.0, "gaps": 1,
                                "facts": 0, "marks": 0, "fact_needs": 0}
    assert r["choices"] == [{"candidates": 4, "offered": 3, "replan": 0, "by_viewpoint": 2,
                             "changing": 1, "marks": 0, "traces": 1, "scene_marks": 0}]  # 세라를 죽이는 선택지는 빠진다. 세라가 따라오면 다음 장면의 지시서가 달라진다
    assert r["later_gaps"] == {"without_segment": 0, "with_segment": 0}


def test_걸린_사건만_다시_받아_끼워_넣는다():
    llm = Fake(WORLD, {"ending": "끝", "facts": [], "events": [BAD4, E3, E2, E1]}, {"events": [E4], "remove": []}, SEGMENT)
    r = run(SEED, SCHEMA, llm)
    assert r["passed"] and len(r["repairs"]) == 1 and r["repairs"][0]["errors"] == []
    assert {why for _, why in r["first_draft"]["errors"]} == {"도윤 은(는) 이미 사망했다 — 필요한 조건: 도윤 와(과) 카엘 이(가) 같은 장소에 있다", "원인(because)이 없다"}
    _, user, _ = llm.calls[2]
    assert "e4: 원인(because)이 없다" in user and "지금의 주요 사건" in user
    assert r["world"]["spine"][3]["because"] == ["e3"]


def test_두_번_고쳐도_안_되면_실패로_끝내고_구간은_만들지_않는다():
    llm = Fake(WORLD, {"ending": "끝", "facts": [], "events": [BAD4, E3, E2, E1]}, {"events": [], "remove": []}, {"events": [], "remove": []})
    r = run(SEED, SCHEMA, llm)
    assert not r["passed"] and not r["spine_passed"] and len(r["repairs"]) == 2 and "segment_tries" not in r


def test_구간이_도착하지_못하면_남은_과제를_알려주고_다시_받는다():
    llm = Fake(WORLD, {"ending": "끝", "facts": [], "events": [E4, E3, E2, E1]}, STUCK, SEGMENT)
    r = run(SEED, SCHEMA, llm)
    assert r["passed"] and len(r["segment_tries"]) == 2
    assert r["segment_tries"][0]["unmet"] == ["도윤 와(과) 카엘 이(가) 같은 장소에 있다"]
    assert "끝났을 때 갖춰지지 않음: 도윤 와(과) 카엘 이(가) 같은 장소에 있다" in llm.calls[3][1]


def test_세계가_어긋나면_한_번_다시_받고_그래도_안_되면_멈춘다():
    broken = {**WORLD, "items": [{"name": "은빛 단검", "holder": "하린"}]}
    r = run(SEED, SCHEMA, Fake(broken, broken))
    assert not r["passed"] and r["world_problems"] == ["물건 '은빛 단검' 의 소지자 '하린' 이(가) 인물 목록에 없다"]


def test_출력_형식은_세계의_이름만_허용한다():
    llm = Fake(WORLD, {"ending": "끝", "facts": [], "events": [E4, E3, E2, E1]}, SEGMENT)
    run(SEED, SCHEMA, llm)
    props = llm.calls[1][2]["properties"]["events"]["items"]["properties"]
    assert props["changes"]["items"]["properties"]["a"]["enum"] == ["카엘", "세라", "도윤"]
    assert "은빛 단검" in props["needs"]["items"]["properties"]["b"]["enum"]


def test_저장된_결과에서_구간만_다시_만들_수_있다():
    from onto.planner import resegment
    first = run(SEED, SCHEMA, Fake(WORLD, {"ending": "끝", "facts": [], "events": [E4, E3, E2, E1]}, STUCK, STUCK, STUCK))
    assert not first["passed"]
    llm = Fake(SEGMENT)
    again = resegment(first, SCHEMA, llm)
    assert again["passed"] and len(llm.calls) == 1 and "시점 인물: 카엘" in llm.calls[0][1]
    assert "아직 일어나면 안 되는 일:" in llm.calls[0][1] and "- e3" in llm.calls[0][1]


def test_선택지가_세_개_미만인_장면은_다시_받는다():
    bare = {"beats": [{**SEGMENT["beats"][0], "choices": SEGMENT["beats"][0]["choices"][:2]}]}
    llm = Fake(WORLD, {"ending": "끝", "facts": [], "events": [E4, E3, E2, E1]}, bare, SEGMENT)
    r = run(SEED, SCHEMA, llm)
    assert r["passed"] and r["segment_tries"][0]["errors"] == [["s1", "내놓을 수 있는 선택지가 2개뿐이다(3개 이상 필요)"]]


def test_행동의_상대_칸에_장소를_적으면_상대_없는_행동으로_받는다():
    seg = {"beats": [{**SEGMENT["beats"][0], "changes": [ch("move", "카엘", "숲"), ch("act", "카엘", "숲")]}]}
    r = run(SEED, SCHEMA, Fake(WORLD, {"ending": "끝", "facts": [], "events": [E4, E3, E2, E1]}, seg))
    assert r["passed"] and r["world"]["segment"][0]["fixed"][1] == {"type": "act", "who": "카엘"}


def test_사실은_맞아도_짜임새가_걸리면_사유를_돌려주고_다시_받는다():
    llm = Fake(WORLD, {"ending": "끝", "facts": [], "events": [FLAT4, E3, E2, E1]}, {"events": [E4], "remove": []}, SEGMENT)
    r = run(SEED, SCHEMA, llm)
    assert r["passed"] and r["first_draft"]["errors"] == [] and len(r["repairs"]) == 1
    assert ["전체", "강도가 가장 높은 사건(e2)이 마지막 2개 안에 없다"] in r["first_draft"]["craft"]
    system, user, _ = llm.calls[2]
    assert "전체: 강도가 가장 높은 사건(e2)이 마지막 2개 안에 없다" in user and "강도 1" in user
    assert "relate 로 적대·남남" in system  # 규칙 파일의 값이 지시문에 들어간다


def test_행동이라고_적어도_상태를_바꾸지_않으면_태도_선택지로_본다():
    talk = {"label": "주변을 다시 살핀다", "kind": "action", "changes": [ch("act", "카엘")]}
    seg = {"beats": [{**SEGMENT["beats"][0], "choices": TONES + [talk]}]}  # 태도 둘 + 말 걸기
    llm = Fake(WORLD, {"ending": "끝", "facts": [], "events": [E4, E3, E2, E1]}, seg, SEGMENT)
    r = run(SEED, SCHEMA, llm)
    assert r["segment_tries"][0]["errors"] == [["s1", "뒤에 흔적을 남기는 선택지(mark, move, give, relate)가 없다"]]
    assert r["passed"] and len(r["segment_tries"]) == 2


def test_사실을_남기는_선택지는_다음_장면의_지시서를_바꾼다():
    vow = {"label": "도윤에게 반드시 돌아오겠다고 약속한다", "kind": "action",
           "changes": [{**ch("mark", "카엘"), "fact": "카엘이 돌아오겠다고 약속했다"}]}
    seg = {"beats": [{**SEGMENT["beats"][0], "choices": TONES + [vow]}]}
    r = run(SEED, SCHEMA, Fake(WORLD, {"ending": "끝", "facts": [], "events": [E4, E3, E2, E1]}, seg))
    assert r["passed"]
    assert (r["choices"][0]["changing"], r["choices"][0]["marks"], r["choices"][0]["traces"]) == (1, 1, 1)
    assert r["world"]["segment"][0]["choices"][2]["events"] == [{"type": "mark", "who": "카엘", "fact": "카엘이 돌아오겠다고 약속했다"}]


def test_뼈대의_사실_목록은_세계에_저장되고_재작성에서_더할_수_있다():
    reveal = {**E4, "changes": [{**ch("mark", "세라", "카엘"), "fact": "세라의 배신이 드러났다"}, ch("relate", "세라", "카엘", kind="적대")]}
    llm = Fake(WORLD, {"ending": "끝", "facts": [], "events": [reveal, E3, E2, E1]},
               {"events": [], "remove": [], "add_facts": ["세라의 배신이 드러났다"]}, SEGMENT)
    r = run(SEED, SCHEMA, llm)
    assert ["e4", "'세라의 배신이 드러났다' 은(는) 정의되지 않은 사실이다"] in r["first_draft"]["errors"]
    assert r["passed"] and r["world"]["facts"] == ["세라의 배신이 드러났다"] and r["first_draft"]["marks"] == 1
