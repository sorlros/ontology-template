"""뼈대 생성 실험: 모델이 세계 → 결말과 주요 사건 → 첫 구간을 만들고, 코드가 검사해 걸린 곳만 다시 쓰게 한다.

모델은 제안만 한다. 형식·이름·규칙 검사는 전부 spine.py 가 한다.
"""

import json
import time
from typing import Callable

from .skeleton import brief, walk_from
from .spine import changes_state, check_craft, check_segment, check_spine, describe, offer, task, walk_spine
from .story import Rejected, apply
from .story import _event, initial_state

Complete = Callable[[str, str, dict], str]
MAX_REPAIRS = 2

WORLD_SYSTEM = """당신은 장편 소설의 기획자다. 주어진 장르, 분위기, 주인공으로 이야기의 세계를 설계한다. 본문은 쓰지 않는다.

규칙:
1. 주인공은 주어진 이름을 그대로 쓰고 role 을 protagonist 로 한다.
2. 인물은 주인공을 포함해 4~6명. 각자 원하는 것(wants)이 서로 부딪히게 만든다. voice 는 그 인물의 말투를 한 구절로.
3. 장소는 3~5곳. 이름은 짧은 명사로 한다. 인물의 at 은 이야기가 시작할 때 있는 장소다.
4. 물건은 이야기에서 역할을 할 것만 1~3개. holder 는 그 물건을 가진 인물이다.
5. relations 는 이야기가 시작할 때의 관계다. kind 는 주어진 목록에서 고른다.
6. premise 는 이 이야기가 무엇에 관한 것인지 한 문장으로 쓴다.
7. 모든 이름은 한국어로, 서로 겹치지 않게 짓는다."""

SPINE_SYSTEM = """당신은 장편 소설의 기획자다. 주어진 세계로 이야기의 주요 사건을 설계한다. 본문은 쓰지 않는다.

방법: 결말을 먼저 정한다. 그 결말이 가능하려면 직전에 무엇이 일어났어야 하는지를 물으며 거꾸로 올라간다.
events 배열은 결말에서 시작해 이야기의 첫 사건으로 끝나도록 시간의 역순으로 적는다.

규칙:
1. 이 이야기는 수백 장면에 이르는 장편이다. 주요 사건은 그 가운데 이야기의 방향을 바꾸는 전환점만 8~12개 고른 것이고, 두 주요 사건 사이에는 수십 장면이 따로 채워진다.
   - 전환점에 이르는 준비(이동, 만남, 물건을 옮기는 일)는 사이 장면의 몫이다. changes 에 넣지 말고, 그 결과만 needs 에 적는다.
   - when 은 실제로 흐른 시간을 반영한다. 사건이 하루에 하나씩 일어나는 일정으로 만들지 않는다.
2. 사건마다 다음을 적는다.
   - id: e 로 시작하는 고유한 이름.
   - when: 이야기의 첫날을 1로 한 날짜(정수).
   - title, meaning(이 사건이 왜 중요한지 한 문장).
   - intensity: 1~5. 전체적으로 뒤로 갈수록 오르고 가장 높은 사건은 마지막 3분의 1에 둔다. 다만 후반부에, 절정을 앞두고 한 번 꺾이는 사건(크게 잃은 뒤의 가라앉음)을 둔다.
   - dilemma: 이 사건에서 주인공이 무엇과 무엇 사이에서 고르는지. 주인공이 직접 행동하는 사건에는 반드시 적는다. 그 밖의 사건은 빈 문자열.
   - cost: 이 사건으로 되돌릴 수 없게 된 것. intensity 가 3 이상인 사건에는 반드시 적는다. 그 밖의 사건은 빈 문자열.
   - changes: 이 사건에서 일어나는 일.
       act    a 가 행동한다. 상대가 있으면 b 에 그 인물을 적는다.
       move   a 가 b(장소)로 이동한다.
       die    a 가 죽는다.
       give   a 가 b(인물)에게 item 을 건넨다.
       relate a 와 b(인물)의 관계가 kind 로 바뀐다.
       mark   a 가 사실 하나를 성립시킨다. fact 에 facts 목록의 이름을 그대로 적고, 상대가 있으면 b 에 그 인물을 적는다.
     비밀이 드러나는 것, 약속, 고백, 누가 무엇을 알게 되는 것처럼 위치·소지·관계로 적을 수 없는 전환은 mark 로 적는다.
     상태가 바뀌지 않는 사건이라도 누가 중심인지 act 로 적는다. 쓰지 않는 칸은 null.
   - needs: 이 사건이 일어나려면 미리 갖춰져 있어야 하는 것.
       alive    a 가 살아 있다.
       at       a 가 b(장소)에 있다.
       together a 와 b(인물)가 같은 장소에 있다.
       has      a 가 b(물건)를 갖고 있다.
       rel      a 와 b(인물)의 관계가 kind 다.
       fact     fact 에 적은 사실이 이미 성립해 있다. 앞선 사건이 성립시킨 사실에 기대는 사건은 반드시 이것으로 적는다.
   - because: 이 사건을 일으킨, 시간상 앞선 사건의 id. 이야기의 첫 사건만 비울 수 있다. 사건들은 "그리고"가 아니라 "그래서"로 이어져야 한다.
   - plants, pays: 이 사건이 심는 복선과 회수하는 복선의 이름. 회수하는 복선은 시간상 앞선 사건이 심은 것이어야 하고, 심은 복선은 끝나기 전에 회수한다.
3. 죽은 인물은 그 뒤의 사건에 나올 수 없다. 물건은 가진 인물만 건넬 수 있고, 건네려면 같은 장소에 있어야 한다.
4. 목록에 없는 인물, 장소, 물건을 쓰지 않는다.
5. ending 에 결말을 한 문장으로 쓴다.
   facts 에는 이 이야기에서 성립하는 사실의 이름을 모두 적는다(짧은 구절, 6~12개). mark 와 needs 의 fact 는 이 목록의 이름과 글자 하나까지 같아야 한다. 한 사실은 한 번만 성립시킨다.
6. 짜임새(코드가 검사한다):
   - 결말에는 대가가 있다. 마지막 3분의 1의 사건 가운데 누군가 죽거나(die), 관계가 끊기거나(relate 로 {loss}), 주인공이 물건을 내놓는다(give).
   - 주인공은 사건의 절반 이상에서 직접 행동한다(changes 에서 act·move·give 의 a, 또는 relate 의 a·b).
   - 적대자(antagonist)는 사건 다섯 개마다 하나 이상에서 먼저 움직인다(그 사건의 changes 첫 줄의 a).
   - 조력자(ally)와 적대자는 각자 한 번 이상 직접 행동한다."""

REPAIR_SYSTEM = SPINE_SYSTEM + """

지금은 이미 만든 주요 사건의 문제를 고치는 단계다.
- 문제가 있는 사건만 고쳐서 events 에 적는다. 같은 id 로 적으면 그 사건을 바꾸고, 새 id 로 적으면 사건을 추가한다.
- 없애야 할 사건은 remove 에 id 를 적는다. 새 사실이 필요하면 add_facts 에 이름을 적는다.
- 문제가 없는 사건은 적지 않는다. 순서는 when 으로 정해지므로 신경 쓰지 않는다."""

SEGMENT_SYSTEM = """당신은 장편 소설의 기획자다. 두 주요 사건 사이를 잇는 장면들을 설계한다. 본문은 쓰지 않는다.

규칙:
1. 장면은 3~6개. 마지막 장면이 끝났을 때 '다음 주요 사건의 조건'이 모두 갖춰져 있어야 한다. '해결할 과제'를 장면의 changes 로 해결한다.
2. 다음 주요 사건 자체는 쓰지 않는다. 그 직전까지만 쓴다.
3. 장면마다 title, changes(이 장면에서 반드시 일어나는 일), open(독자의 선택에 따라 달라질 수 있는 부분 1~2개), choices(독자에게 보여 줄 선택지 후보 4~5개)를 적는다.
4. choices 는 그 장면이 끝난 뒤 독자가 고르는 것이다. 독자는 시점 인물이므로 모든 선택지는 시점 인물이 하는 일이어야 한다(다른 인물의 결정을 선택지로 쓰지 않는다). label 은 한 문장.
   kind 가 tone 이면 태도나 말투의 선택이고 changes 를 비운다.
   kind 가 action 이면 뒤에 흔적을 남기는 선택이다. changes 에 그 흔적을 적는다. changes 의 a 는 시점 인물이다.
     - 약속한다, 밝힌다, 숨긴다, 알아낸다, 거절한다 같은 선택은 mark 로 적고 fact 에 그 사실을 짧은 구절로 쓴다(예: "카엘이 검을 넘기지 않겠다고 밝혔다"). 이 사실은 뒤의 장면에 전달된다.
     - 이동, 물건 전달, 관계 변화는 move, give, relate 로 적는다.
     - act 만 있는 선택지는 action 이 아니다(뒤에 아무것도 남지 않는다).
   tone 과 action 을 각각 2개 이상 넣는다. action 은 이야기의 다음 주요 사건을 불가능하게 만들지 않아야 한다.
5. changes 의 형식과 상태 규칙은 주요 사건과 같다: act, move, die, give, relate, mark.
   장면의 changes 에서 mark 는 이야기에 남을 만한 일(무언가 드러남, 인물 사이의 약속)에만 쓰고 장면당 하나까지만 쓴다. 일정, 확인, 다짐 같은 사소한 일은 mark 로 적지 않는다. 죽은 인물은 행동할 수 없고, 상대가 있는 행동과 건네기는 같은 장소에서만 된다.
6. 목록에 없는 인물, 장소, 물건을 쓰지 않는다.
7. '아직 일어나면 안 되는 일'에 적힌 뒤의 주요 사건을 앞당기지 않는다. 그 사건의 인물과 물건을 미리 움직여 놓지도 않는다. 이 구간은 다음 주요 사건까지만 책임진다."""


def _nullable(values: list[str]) -> dict:
    return {"type": ["string", "null"], "enum": [*values, None]}


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _strings() -> dict:
    return {"type": "array", "items": {"type": "string"}}


def _change_schema(world: dict, schema: dict) -> dict:
    chars = list(world["characters"])
    return _obj({
        "type": {"type": "string", "enum": [t for t in schema["events"]]},
        "a": {"type": "string", "enum": chars},
        "b": _nullable(chars + world["locations"]),
        "item": _nullable(list(world.get("items") or {})),
        "kind": _nullable(schema["relations"]),
        "fact": {"type": ["string", "null"]},
    })


def _event_schema(world: dict, schema: dict) -> dict:
    chars = list(world["characters"])
    need = _obj({
        "pred": {"type": "string", "enum": ["alive", "at", "together", "has", "rel", "fact"]},
        "a": {"type": "string", "enum": chars},
        "b": _nullable(chars + world["locations"] + list(world.get("items") or {})),
        "kind": _nullable(schema["relations"]),
        "fact": {"type": ["string", "null"]},
    })
    return _obj({
        "id": {"type": "string"}, "when": {"type": "integer"}, "title": {"type": "string"}, "meaning": {"type": "string"},
        "intensity": {"type": "integer"}, "dilemma": {"type": "string"}, "cost": {"type": "string"},
        "changes": {"type": "array", "items": _change_schema(world, schema)},
        "needs": {"type": "array", "items": need},
        "because": _strings(), "plants": _strings(), "pays": _strings(),
    })


def _change(c: dict, world: dict) -> dict:
    """모델이 낸 변화를 엔진의 사건으로 바꾼다. act 의 상대 칸에 장소를 적은 것은 상대 없는 행동으로 본다."""
    if c["type"] == "act" and c.get("b") in world["locations"]:
        c = {**c, "b": None}
    return _event(c)


def _need(n: dict) -> list:
    pred = n["pred"]
    if pred == "fact":
        return [pred, " ".join(str(n.get("fact") or "").split())]
    if pred == "alive":
        return [pred, n["a"]]
    return [pred, n["a"], n["b"], n["kind"]] if pred == "rel" else [pred, n["a"], n["b"]]


def _to_event(e: dict, world: dict) -> dict:
    """모델이 낸 주요 사건을 검증기의 형식으로 바꾼다."""
    return {"id": e["id"], "when": e["when"], "title": e["title"], "meaning": e["meaning"], "intensity": e["intensity"],
            "dilemma": e["dilemma"], "cost": e["cost"],
            "needs": [_need(n) for n in e["needs"]], "events": [_change(c, world) for c in e["changes"]],
            "because": e["because"], "plants": e["plants"], "pays": e["pays"]}


def _world_text(world: dict) -> str:
    lines = [f"전제: {world['premise']}", "장소: " + ", ".join(world["locations"]), "인물:"]
    lines += [f"- {n} ({c['role']}, 시작 위치 {c['at']}): {c['wants']}" for n, c in world["characters"].items()]
    lines += ["물건: " + ", ".join(f"{n}(소지자 {i['holder']})" for n, i in world["items"].items())]
    lines += ["관계: " + ", ".join(f"{a}·{b} {k}" for a, b, k in world["relations"])]
    return "\n".join(lines)


def plan_world(seed: dict, schema: dict, complete: Complete, feedback: str | None = None) -> tuple[dict, list[str]]:
    """반환: (세계, 문제 목록). 문제가 있으면 세계를 쓰지 말고 feedback 으로 다시 부른다."""
    out_schema = _obj({
        "premise": {"type": "string"},
        "locations": _strings(),
        "characters": {"type": "array", "items": _obj({
            "name": {"type": "string"}, "role": {"type": "string", "enum": ["protagonist", "ally", "antagonist", "neutral"]},
            "wants": {"type": "string"}, "voice": {"type": "string"}, "at": {"type": "string"}})},
        "items": {"type": "array", "items": _obj({"name": {"type": "string"}, "holder": {"type": "string"}})},
        "relations": {"type": "array", "items": _obj({
            "a": {"type": "string"}, "b": {"type": "string"}, "kind": {"type": "string", "enum": schema["relations"]}})},
    })
    user = "\n".join(filter(None, [
        f"장르: {seed['genre']}", f"분위기: {seed['tone']}", f"서사 유형: {seed.get('narrative_type', 'hero')}",
        f"주인공: {seed['name']} (성격: {', '.join(seed.get('traits') or [])})",
        f"관계 종류: {', '.join(schema['relations'])}",
        feedback and f"방금 만든 세계는 다음 문제가 있었다. 고쳐서 다시 만든다: {feedback}",
    ]))
    raw = json.loads(complete(WORLD_SYSTEM, user, out_schema))
    world = {
        "premise": raw["premise"], "viewpoint": seed["name"], "locations": raw["locations"],
        "characters": {c["name"]: {"role": c["role"], "wants": c["wants"], "voice": c["voice"], "at": c["at"]} for c in raw["characters"]},
        "items": {i["name"]: {"holder": i["holder"]} for i in raw["items"]},
        "relations": [[r["a"], r["b"], r["kind"]] for r in raw["relations"]],
    }
    chars, problems = world["characters"], []
    if seed["name"] not in chars:
        problems.append(f"주인공 '{seed['name']}' 이(가) 인물 목록에 없다")
    if len(chars) != len(raw["characters"]):
        problems.append("인물 이름이 겹친다")
    problems += [f"{n} 의 시작 위치 '{c['at']}' 이(가) 장소 목록에 없다" for n, c in chars.items() if c["at"] not in world["locations"]]
    problems += [f"물건 '{n}' 의 소지자 '{i['holder']}' 이(가) 인물 목록에 없다" for n, i in world["items"].items() if i["holder"] not in chars]
    problems += [f"관계의 '{x}' 이(가) 인물 목록에 없다" for a, b, _ in world["relations"] for x in (a, b) if x not in chars]
    return world, problems


def _spine_system(text: str, schema: dict) -> str:
    return text.replace("{loss}", "·".join(schema.get("loss_relations") or []))


def _problems(world: dict, schema: dict) -> tuple[list, list, list[str]]:
    """반환: (사실의 오류, 짜임새의 문제, 경고). 앞의 둘이 모두 비어야 통과다."""
    report = check_spine(world, schema)
    return report.errors, check_craft(world, schema), report.warnings


def _sorted(events: list[dict]) -> list[dict]:
    return sorted(events, key=lambda e: e["when"] if isinstance(e.get("when"), int) else 0)


def plan_spine(world: dict, schema: dict, complete: Complete) -> tuple[str, list[dict]]:
    """반환: (결말, 시간순 주요 사건). 모델은 결말부터 거꾸로 적고, 여기서 뒤집는다."""
    # facts 를 events 앞에 둔다: 사건을 쓰기 전에 이 이야기에서 무엇이 드러나고 약속되는지부터 정하게 한다.
    out_schema = _obj({"ending": {"type": "string"}, "facts": _strings(), "events": {"type": "array", "items": _event_schema(world, schema)}})
    raw = json.loads(complete(_spine_system(SPINE_SYSTEM, schema), _world_text(world), out_schema))
    world["facts"] = [" ".join(f.split()) for f in raw["facts"]]
    return raw["ending"], _sorted([_to_event(e, world) for e in reversed(raw["events"])])


def repair_spine(world: dict, schema: dict, events: list[dict], problems: list[tuple[str, str]], complete: Complete) -> list[dict]:
    """걸린 사건만 다시 받아 끼워 넣는다."""
    out_schema = _obj({"events": {"type": "array", "items": _event_schema(world, schema)}, "remove": _strings(), "add_facts": _strings()})
    listing = "\n".join(
        f"- {e['id']} ({e['when']}일, 강도 {e['intensity']}) {e['title']} | 딜레마: {e['dilemma'] or '없음'} | 대가: {e['cost'] or '없음'} | 일: {json.dumps(e['events'], ensure_ascii=False)} | 조건: {json.dumps(e['needs'], ensure_ascii=False)}"
        f" | 원인: {e['because']} | 심기: {e['plants']} | 회수: {e['pays']}" for e in events)
    user = f"{_world_text(world)}\n\n사실 목록: {', '.join(world.get('facts') or []) or '없음'}\n\n지금의 주요 사건(시간순):\n{listing}\n\n고쳐야 할 문제:\n" + "\n".join(f"- {eid}: {why}" for eid, why in problems)
    raw = json.loads(complete(_spine_system(REPAIR_SYSTEM, schema), user, out_schema))
    world["facts"] = list(dict.fromkeys([*(world.get("facts") or []), *(" ".join(f.split()) for f in raw.get("add_facts") or [])]))
    fixed = {e["id"]: _to_event(e, world) for e in raw["events"]}
    kept = [fixed.pop(e["id"], e) for e in events if e["id"] not in raw["remove"] or e["id"] in fixed]
    return _sorted(kept + list(fixed.values()))


def _state_text(state: dict) -> str:
    lines = ["인물: " + ", ".join(f"{n}({c['at'] if c['alive'] else '사망'})" for n, c in state["chars"].items())]
    lines.append("물건: " + ", ".join(f"{i}(소지자 {h})" for i, h in state["items"].items()))
    lines.append("관계: " + ", ".join(f"{p} {k}" for p, k in state["rels"].items()))
    lines.append("성립한 사실: " + (", ".join(state.get("facts") or {}) or "없음"))
    return "\n".join(lines)


def plan_segment(world: dict, schema: dict, state: dict, index: int, complete: Complete, feedback: str | None = None) -> list[dict]:
    """index 번째 주요 사건 앞의 구간을 만든다. state 는 그 앞 주요 사건이 끝난 뒤의 상태다."""
    change = _change_schema(world, schema)
    choice = _obj({"label": {"type": "string"}, "kind": {"type": "string", "enum": ["tone", "action"]},
                   "changes": {"type": "array", "items": change}})
    beat = _obj({"title": {"type": "string"}, "changes": {"type": "array", "items": change}, "open": _strings(),
                 "choices": {"type": "array", "items": choice}})
    out_schema = _obj({"beats": {"type": "array", "items": beat}})
    prev, nxt, t = world["spine"][index - 1], world["spine"][index], task(world, schema, index)
    user = "\n\n".join(filter(None, [
        f"전제: {world['premise']}\n장소: {', '.join(world['locations'])}",
        "지금 상태:\n" + _state_text(state),
        f"직전 주요 사건: {prev['title']} — {prev['meaning']}",
        f"다음 주요 사건: {nxt['title']} — {nxt['meaning']}\n다음 주요 사건의 조건:\n"
        + ("\n".join(f"- {describe(tuple(n))}" for n in nxt["needs"]) or "- (따로 없음)"),
        "해결할 과제:\n" + ("\n".join(f"- {describe(g)}" for g in t["gaps"]) or "- (없음. 흐름만 잇는다)"),
        f"두 사건 사이에 흐르는 시간: {t['days']}일",
        t["reintroduce"] and "오랜만에 나오는 인물(이 구간에서 다시 등장시킨다): " + ", ".join(t["reintroduce"]),
        f"시점 인물: {world['viewpoint']}",
        world.get("facts") and "뼈대가 정한 사실(이 가운데 하나를 성립시킬 때는 이름을 그대로 쓴다): " + ", ".join(world["facts"]),
        world["spine"][index + 1:index + 3] and "아직 일어나면 안 되는 일:\n" + "\n".join(
            f"- {e['title']} — {e['meaning']}" for e in world["spine"][index + 1:index + 3]),
        feedback and f"방금 만든 장면들은 다음 문제가 있었다. 고쳐서 다시 만든다: {feedback}",
    ]))
    raw = json.loads(complete(SEGMENT_SYSTEM, user, out_schema))
    return [{"id": f"s{i}", "title": b["title"], "fixed": [_change(c, world) for c in b["changes"]], "open": b["open"],
             "choices": [_choice(f"s{i}c{j}", c, world) for j, c in enumerate(b["choices"], 1)]}
            for i, b in enumerate(raw["beats"], 1)]


def _choice(cid: str, c: dict, world: dict) -> dict:
    """선택지의 종류는 모델이 적은 값이 아니라 효과로 정한다: 상태를 바꾸면 action, 아니면 tone."""
    choice = {"id": cid, "label": c["label"], "events": [_change(x, world) for x in c["changes"]]}
    return {**choice, "kind": "action" if changes_state(choice) else "tone"}


def _leaves_trace(world: dict, schema: dict, state: dict, choice: dict, nxt: dict) -> bool:
    """이 선택지를 고르면 다음 장면의 지시서가 달라지는가."""
    after = json.loads(json.dumps(state))
    try:
        for ev in choice["events"]:
            apply(after, ev, schema, world["locations"], "choice")
    except (Rejected, KeyError):
        return False
    return brief(nxt, after, world) != brief(nxt, state, world)


def later_gaps(world: dict, schema: dict, state: dict, index: int) -> int:
    """index 번째 주요 사건부터 끝까지, state 에서 출발했을 때 구간들이 메워야 하는 틈의 수."""
    return sum(len(r.gaps) for r in walk_spine(world, schema, world["spine"][index:], state)[0])


def segment_round(world: dict, schema: dict, complete: Complete, timed: Callable) -> dict:
    """첫 주요 사건과 둘째 주요 사건 사이의 구간을 만들고 검사한다. 통과할 때까지 최대 MAX_REPAIRS 번 다시 받는다."""
    _, state = walk_spine(world, schema, world["spine"][:1], initial_state(world))
    target, feedback, tries = world["spine"][1], None, []
    for _ in range(MAX_REPAIRS + 1):
        beats = timed("구간" if not tries else "구간 재작성", plan_segment, world, schema, state, 1, complete, feedback)
        errors, unmet = check_segment(world, schema, state, beats, target)
        for k, beat in enumerate(beats):
            _, after = walk_from(world, schema, beats[:k + 1], state)
            ok = offer(world, schema, after, beat, beats[k + 1:], world["spine"][1:], want=99)
            if len(ok) < 3:
                errors.append((beat["id"], f"내놓을 수 있는 선택지가 {len(ok)}개뿐이다(3개 이상 필요)"))
            if not any(changes_state(c) for c in ok):
                errors.append((beat["id"], "뒤에 흔적을 남기는 선택지(mark, move, give, relate)가 없다"))
            if all(changes_state(c) for c in ok) and ok:
                errors.append((beat["id"], "태도만 고르는 선택지(changes 가 빈 것)가 없다"))
        tries.append({"beats": len(beats), "errors": [list(e) for e in errors], "unmet": [describe(g) for g in unmet]})
        if not errors and not unmet:
            break
        feedback = "; ".join([f"{bid}: {why}" for bid, why in errors] + [f"끝났을 때 갖춰지지 않음: {describe(g)}" for g in unmet])
    world["segment"] = beats
    shown = []
    for k, beat in enumerate(beats):  # 장면마다 걸러낸 뒤 남는 선택지 수
        _, after = walk_from(world, schema, beats[:k + 1], state)
        ok = offer(world, schema, after, beat, beats[k + 1:], world["spine"][1:], want=99)
        mine = sum(all(ev.get("who", ev.get("a")) == world["viewpoint"] for ev in c["events"]) for c in beat["choices"])
        nxt = beats[k + 1] if k + 1 < len(beats) else {"id": target["id"], "title": target.get("title", ""), "fixed": target["events"]}
        acting = [c for c in ok if changes_state(c)]
        shown.append({"candidates": len(beat["choices"]), "offered": len(ok), "replan": sum(c["replan"] for c in ok), "by_viewpoint": mine,
                      "changing": len(acting), "marks": sum(any(ev["type"] == "mark" for ev in c["events"]) for c in acting),
                      "traces": sum(_leaves_trace(world, schema, after, c, nxt) for c in acting),
                      "scene_marks": sum(ev["type"] == "mark" for ev in beat["fixed"])})
    # 이 구간이 뒤 구간들에 떠넘긴 일: 구간이 없었을 때와 있었을 때, 둘째 주요 사건 뒤로 남는 틈의 수
    _, plain = walk_spine(world, schema, [target], state)
    _, end = walk_from(world, schema, beats, state)
    _, moved = walk_spine(world, schema, [target], end)
    passed = not tries[-1]["errors"] and not tries[-1]["unmet"]
    return {"segment_tries": tries, "segment_passed": passed, "choices": shown,
            "later_gaps": {"without_segment": later_gaps(world, schema, plain, 2), "with_segment": later_gaps(world, schema, moved, 2)}}


def resegment(result: dict, schema: dict, complete: Complete, log: Callable[[str], None] = lambda _: None) -> dict:
    """저장된 결과의 세계와 주요 사건은 그대로 두고 구간만 다시 만든다(프롬프트를 고친 뒤 비교할 때 쓴다)."""
    calls = []

    def timed(name, fn, *args):
        t = time.monotonic()
        try:
            return fn(*args)
        finally:
            calls.append({"step": name, "seconds": round(time.monotonic() - t, 1)})
            log(f"{name}: {calls[-1]['seconds']}초")

    world = result["world"]
    out = {**result, **segment_round(world, schema, complete, timed), "calls": result["calls"] + calls}
    return {**out, "passed": out["segment_passed"]}


def run(seed: dict, schema: dict, complete: Complete, log: Callable[[str], None] = lambda _: None) -> dict:
    """세계 → 결말·주요 사건 → 재작성 → 첫 구간까지 한 번 돌리고, 과정과 측정값을 돌려준다."""
    calls = []

    def timed(name, fn, *args):
        t = time.monotonic()
        try:
            return fn(*args)
        finally:
            calls.append({"step": name, "seconds": round(time.monotonic() - t, 1)})
            log(f"{name}: {calls[-1]['seconds']}초")

    world, problems = timed("세계", plan_world, seed, schema, complete)
    if problems:
        world, problems = timed("세계 재작성", plan_world, seed, schema, complete, "; ".join(problems))
    out = {"seed": seed, "world": world, "world_problems": problems, "calls": calls}
    if problems:
        return {**out, "passed": False}

    ending, events = timed("주요 사건", plan_spine, world, schema, complete)
    world["spine"] = events
    errors, craft, warnings = _problems(world, schema)
    later = events[1:]
    out |= {
        "ending": ending,
        "first_draft": {
            "events": len(events),
            "errors": [list(e) for e in errors],
            "craft": [list(e) for e in craft],
            "warnings": warnings,
            "because_filled": round(sum(bool(e["because"]) for e in later) / max(len(later), 1), 2),
            "needs_filled": round(sum(bool(e["needs"]) for e in later) / max(len(later), 1), 2),
            "gaps": sum(len(r.gaps) for r in check_spine(world, schema).events),
            "facts": len(world.get("facts") or []),
            "marks": sum(x["type"] == "mark" for e in events for x in e["events"]),
            "fact_needs": sum(n[0] == "fact" for e in events for n in e["needs"]),
        },
        "repairs": [],
    }
    while (errors or craft) and len(out["repairs"]) < MAX_REPAIRS:
        world["spine"] = timed("주요 사건 재작성", repair_spine, world, schema, world["spine"], errors + craft, complete)
        errors, craft, warnings = _problems(world, schema)
        out["repairs"].append({"errors": [list(e) for e in errors + craft]})
    out["spine_passed"] = not errors and not craft
    out["warnings"] = warnings
    if errors or craft:
        return {**out, "passed": False}

    out |= segment_round(world, schema, complete, timed)
    return {**out, "passed": out["segment_passed"], "seconds": round(sum(c["seconds"] for c in calls), 1)}
