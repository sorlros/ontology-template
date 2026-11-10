"""소설 도메인 프로토타입: 코드가 관리하는 세계 상태가 정본이고, 턴마다 선언된 사건만 상태를 바꾼다.

LLM 호출은 없다. 본문은 "변화가 있었을 법한 곳"을 고르는 데만 쓰고(단어 목록), 판정은 전부 규칙으로 한다.
"""

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

SNAPSHOT_EVERY = 5  # 이 턴 수마다 전체 상태를 저장한다. 그 사이는 사건만 다시 적용한다.
NO_CHANGE, APPLIED, REJECTED, SUSPECT = "변화 없음", "적용", "거부", "의심"


class Rejected(Exception):
    """사건이 전제 조건에 어긋난다."""


@dataclass
class Verdict:
    kind: str
    reasons: list[str] = field(default_factory=list)  # 거부 사유 또는 의심 사유
    diff: list[tuple[str, str, str | None, str | None]] = field(default_factory=list)  # (대상, 속성, 이전, 이후)


def _pair(a: str, b: str) -> str:
    return "·".join(sorted((a, b)))


def initial_state(world: dict) -> dict:
    return {
        "chars": {n: {"alive": True, "at": c["at"]} for n, c in world["characters"].items()},
        "items": {n: i["holder"] for n, i in (world.get("items") or {}).items()},
        "rels": {_pair(a, b): kind for a, b, kind in world.get("relations") or []},
        # 성립한 사실 → {people: 걸린 인물, origin: 어디서 성립했는가}. 약속, 고백, 알아낸 것처럼 물리적 상태가 아닌 것.
        # origin 은 spine(주요 사건), choice(독자의 선택), scene(장면에서 일어난 일)이고 중요도를 정하는 근거다.
        "facts": {},
    }


def apply(state: dict, ev: dict, schema: dict, locations: list[str], origin: str = "scene") -> None:
    """사건 하나를 state 에 적용한다. 전제 조건에 어긋나면 Rejected (state 는 건드리지 않는다).

    origin 은 이 사건이 어디서 왔는지다(spine, choice, scene). 사실을 성립시킬 때 함께 기록한다.
    """
    chars = state["chars"]

    def alive(name):
        if name not in chars:
            raise Rejected(f"'{name}' 은(는) 등장인물이 아니다")
        if not chars[name]["alive"]:
            raise Rejected(f"{name} 은(는) 이미 사망했다")
        return chars[name]

    def together(a, b):
        if alive(a)["at"] != alive(b)["at"]:
            raise Rejected(f"{a}({chars[a]['at']})와 {b}({chars[b]['at']})는 같은 장소에 없다")

    t = ev.get("type")
    if t == "act":
        alive(ev["who"])
        if ev.get("with"):
            together(ev["who"], ev["with"])
    elif t == "move":
        who = alive(ev["who"])
        if ev["to"] not in locations:
            raise Rejected(f"'{ev['to']}' 은(는) 정의되지 않은 장소다")
        who["at"] = ev["to"]
    elif t == "die":
        alive(ev["who"])["alive"] = False
    elif t == "give":
        together(ev["who"], ev["to"])
        if state["items"].get(ev["item"]) != ev["who"]:
            raise Rejected(f"{ev['who']} 은(는) '{ev['item']}' 을(를) 갖고 있지 않다")
        state["items"][ev["item"]] = ev["to"]
    elif t == "relate":
        alive(ev["a"]), alive(ev["b"])
        if ev["kind"] not in schema["relations"]:
            raise Rejected(f"'{ev['kind']}' 은(는) 정의되지 않은 관계다")
        state["rels"][_pair(ev["a"], ev["b"])] = ev["kind"]
    elif t == "mark":
        alive(ev["who"])
        if ev.get("with") and ev["with"] not in chars:
            raise Rejected(f"'{ev['with']}' 은(는) 등장인물이 아니다")
        name = " ".join(str(ev.get("fact") or "").split())[:80]
        if not name:
            raise Rejected("성립시킬 사실의 이름이 없다")
        # 이미 성립한 사실은 그대로 둔다
        state.setdefault("facts", {}).setdefault(name, {"people": [n for n in (ev["who"], ev.get("with")) if n], "origin": origin})
    else:
        raise Rejected(f"정의되지 않은 사건: {t}")


def _flat(state: dict) -> dict:
    out = {}
    for n, c in state["chars"].items():
        out[(n, "생사")] = "생존" if c["alive"] else "사망"
        out[(n, "위치")] = c["at"]
    for n, h in state["items"].items():
        out[(n, "소지자")] = h
    for p, k in state["rels"].items():
        out[(p, "관계")] = k
    for name in state.get("facts") or {}:
        out[(name, "사실")] = "성립"
    return out


def diff(before: dict, after: dict) -> list[tuple[str, str, str | None, str | None]]:
    """두 상태의 차이. 코드 diff 처럼 바뀐 곳만 돌려준다."""
    a, b = _flat(before), _flat(after)
    return [(e, attr, a.get((e, attr)), b.get((e, attr))) for e, attr in sorted(a.keys() | b.keys()) if a.get((e, attr)) != b.get((e, attr))]


def _aliases(world: dict) -> dict[str, list[str]]:
    out = {}
    for group in ("characters", "items"):
        for n, spec in (world.get(group) or {}).items():
            out[n] = [n, *(spec.get("aliases") or [])]
    return out


def mentions(text: str, world: dict) -> set[str]:
    """본문에 이름이나 별칭이 나온 엔티티. 이 밖의 엔티티는 이 턴에서 볼 필요가 없다."""
    # shortcut: 부분 문자열 검색이라 짧은 이름이 다른 낱말 안에서 잘못 걸릴 수 있다. 이름이 많아지면 형태소 분석으로 바꾼다.
    return {n for n, names in _aliases(world).items() if any(a in text for a in names)}


def candidates(text: str, world: dict, schema: dict) -> list[tuple[str, set[str], str]]:
    """변화 후보: (사건 종류, 그 문장에 나온 엔티티, 문장). 사건 종류의 단어가 나온 문장만 고른다."""
    out = []
    for s in filter(str.strip, re.split(r"(?<=[.!?])\s+|\n+", text)):
        for t, spec in schema["events"].items():
            if any(w in s for w in spec.get("triggers") or []):
                out.append((t, mentions(s, world), s.strip()))
    return out


def judge(state: dict, turn: dict, world: dict, schema: dict) -> tuple[Verdict, dict]:
    """턴 하나를 판정한다. 반환: (판정, 턴 이후 상태). 거부면 상태는 그대로다."""
    text, events = turn.get("text") or "", turn.get("events") or []
    after = copy.deepcopy(state)
    try:
        for ev in events:
            apply(after, ev, schema, world["locations"])
    except (Rejected, KeyError) as e:
        why = f"사건에 필요한 값이 없다: {e}" if isinstance(e, KeyError) else str(e)
        return Verdict(REJECTED, [why]), state

    suspects = []
    # 본문은 변화를 말하는데 선언된 사건이 없는 경우
    for t, ents, sentence in candidates(text, world, schema):
        if t != "give":  # 물건은 전달 사건에서만 당사자다. "그는 단검을 쥔 채 떠났다"의 당사자는 대명사 뒤의 인물이다.
            ents = ents & world["characters"].keys()
        declared = [set(map(str, ev.values())) for ev in events if ev.get("type") == t]
        if not any(not ents or ents & vals for vals in declared):
            who = ", ".join(sorted(ents)) or "대상 불명"
            suspects.append(f"{schema['events'][t]['label']} 사건 누락 의심({who}): {sentence}")
    # 이 턴 이전에 죽은 인물이 본문에 나오는 경우. 회상 표현과 함께면 넘어간다.
    dead = {n for n, c in state["chars"].items() if not c["alive"]}
    for s in filter(str.strip, re.split(r"(?<=[.!?])\s+|\n+", text)):
        for n in sorted(dead & mentions(s, world)):
            if not any(w in s for w in schema.get("recall") or []):
                suspects.append(f"사망한 {n} 이(가) 언급됨, 회상인지 확인: {s.strip()}")

    d = diff(state, after)
    return Verdict(SUSPECT if suspects else APPLIED if d else NO_CHANGE, suspects, d), after


def scene(state: dict, world: dict) -> str:
    """다음 턴 프롬프트에 넣을 확정 사실. 시점 인물이 있는 장소를 기준으로 고른다."""
    me = world["viewpoint"]
    here = state["chars"][me]["at"]
    present = [n for n, c in state["chars"].items() if c["alive"] and c["at"] == here]
    lines = [f"장소: {here}", "등장 가능: " + ", ".join(present)]
    for n in present:
        held = [i for i, h in state["items"].items() if h == n]
        if held:
            lines.append(f"소지: {n} — {', '.join(held)}")
    for p, k in sorted(state["rels"].items()):
        if me in p.split("·"):
            lines.append(f"관계: {p} — {k}")
    for name, rec in (state.get("facts") or {}).items():
        if not rec["people"] or set(rec["people"]) & set(present):
            lines.append(f"성립한 사실: {name}")
    for n, c in state["chars"].items():
        if not c["alive"]:
            lines.append(f"등장 불가: {n} (사망)")
        elif c["at"] != here:
            lines.append(f"등장 불가: {n} ({c['at']}에 있음)")
    return "\n".join(lines)


class Story:
    """사건 기록이 정본이고 스냅샷은 캐시다. 받아들인 턴만 기록한다."""

    def __init__(self, domain_dir: Path):
        domain_dir = Path(domain_dir)
        self.schema = yaml.safe_load((domain_dir / "schema.yaml").read_text(encoding="utf-8"))
        self.world = yaml.safe_load((domain_dir / "story.yaml").read_text(encoding="utf-8"))
        self.turns: list[dict] = []
        self.snapshots = {0: initial_state(self.world)}

    def state_at(self, n: int | None = None) -> dict:
        """n 번째 턴까지 적용한 상태. 가장 가까운 스냅샷에서 시작해 그 뒤의 사건만 다시 적용한다."""
        n = len(self.turns) if n is None else n
        base = max(k for k in self.snapshots if k <= n)
        state = copy.deepcopy(self.snapshots[base])
        for turn in self.turns[base:n]:
            for ev in turn.get("events") or []:
                apply(state, ev, self.schema, self.world["locations"])
        return state

    def play(self, turn: dict) -> Verdict:
        verdict, after = judge(self.state_at(), turn, self.world, self.schema)
        if verdict.kind != REJECTED:
            self.turns.append(turn)
            if len(self.turns) % SNAPSHOT_EVERY == 0:
                self.snapshots[len(self.turns)] = after
        return verdict

    def rewind(self, n: int) -> None:
        """n 번째 턴 직후로 되돌린다."""
        self.turns = self.turns[:n]
        self.snapshots = {k: v for k, v in self.snapshots.items() if k <= n}

    def amend(self, n: int, events: list[dict]) -> list[Verdict]:
        """n 번째 턴의 사건을 고치고(누락 보정) 그 뒤를 다시 적용한다. 뒤 턴이 거부되면 고치지 않는다."""
        old_turns, old_snaps = self.turns, self.snapshots
        fixed = old_turns[:n - 1] + [{**old_turns[n - 1], "events": events}] + old_turns[n:]
        self.rewind(n - 1)
        verdicts = [self.play(t) for t in fixed[n - 1:]]
        if any(v.kind == REJECTED for v in verdicts):
            self.turns, self.snapshots = old_turns, old_snaps
            raise Rejected("고친 뒤의 턴이 규칙에 어긋난다: " + "; ".join(r for v in verdicts for r in v.reasons if v.kind == REJECTED))
        return verdicts

    def scene(self) -> str:
        return scene(self.state_at(), self.world)


def run(domain_dir: Path) -> tuple[list[tuple[dict, Verdict]], Story]:
    """story.yaml 의 턴을 순서대로 판정한다."""
    story = Story(domain_dir)
    out = [(t, story.play(t)) for t in story.world.get("turns") or []]
    return out, story


# ---------- 모델 연동: 본문과 사건을 한 번의 호출로 받는다 ----------

SYSTEM = """당신은 턴 방식 소설의 서술자다. 유저의 입력을 받아 이야기의 다음 장면을 쓴다.

규칙:
1. '확정 사실'은 이 세계에서 이미 일어난 일이다. 여기에 어긋나게 쓰지 않는다.
2. '등장 불가' 인물은 이 장면에서 행동하거나 말하지 않는다. 회상이나 언급으로만 나올 수 있다.
3. 유저의 입력이 확정 사실과 어긋나면(죽은 인물을 되살린다, 다른 장소의 인물과 대화한다) 그대로 따르지 않고, 이 세계에서 가능한 방식으로 풀어 쓴다.
4. 목록에 없는 인물, 장소, 물건의 이름을 새로 만들지 않는다.
5. text 에는 본문을 한국어 3~5문장으로 쓴다.
6. events 에는 이 장면에서 일어난 일을 빠짐없이 적는다. 본문에만 쓰고 events 에 적지 않은 변화는 일어나지 않은 것으로 처리된다.
   - act: a 가 이 장면에서 행동하거나 말한다. 상대가 있으면 b 에 그 인물을 적는다. 장면에서 행동하는 인물마다 하나씩 적는다.
   - move: a 가 b(장소)로 이동한다.
   - die: a 가 죽는다.
   - give: a 가 b(인물)에게 item 을 건넨다.
   - relate: a 와 b(인물)의 관계가 kind 로 바뀐다.
   - mark: a 가 사실 하나를 성립시킨다(약속, 고백, 무언가를 알아냄). fact 에 그 사실을 짧은 구절로 적고, 상대가 있으면 b 에 적는다.
   쓰지 않는 칸은 null 로 둔다."""


def turn_schema(world: dict, schema: dict) -> dict:
    """모델 출력의 JSON 스키마. 이름은 목록에 있는 것만 고를 수 있다."""
    chars, nullable = list(world["characters"]), lambda xs: {"type": ["string", "null"], "enum": [*xs, None]}
    event = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": list(schema["events"])},
            "a": {"type": "string", "enum": chars},
            "b": nullable(chars + world["locations"]),
            "item": nullable(list(world.get("items") or {})),
            "kind": nullable(schema["relations"]),
            "fact": {"type": ["string", "null"]},
        },
        "required": ["type", "a", "b", "item", "kind", "fact"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"text": {"type": "string"}, "events": {"type": "array", "items": event}},
        "required": ["text", "events"],
        "additionalProperties": False,
    }


def _event(e: dict) -> dict:
    """모델이 낸 납작한 사건을 엔진의 사건으로 바꾼다."""
    t, a, b = e["type"], e["a"], e.get("b")
    if t == "act":
        return {"type": t, "who": a, **({"with": b} if b else {})}
    if t == "move":
        return {"type": t, "who": a, "to": b}
    if t == "die":
        return {"type": t, "who": a}
    if t == "give":
        return {"type": t, "who": a, "to": b, "item": e.get("item")}
    if t == "mark":
        return {"type": t, "who": a, **({"with": b} if b else {}), "fact": e.get("fact")}
    return {"type": t, "a": a, "b": b, "kind": e.get("kind")}


def turn_request(story: Story, prompt: str, feedback: str | None = None) -> tuple[str, str, dict]:
    """모델에 보낼 (system, user, 출력 스키마). 과거 전체가 아니라 확정 사실과 최근 3턴만 보낸다."""
    w = story.world
    recent = "\n".join(f"- {t['text']}" for t in story.turns[-3:]) or "(이야기의 시작)"
    user = "\n\n".join(filter(None, [
        "확정 사실:\n" + story.scene(),
        f"인물: {', '.join(w['characters'])}\n장소: {', '.join(w['locations'])}\n물건: {', '.join(w.get('items') or {})}\n"
        f"관계 종류: {', '.join(story.schema['relations'])}\n시점 인물: {w['viewpoint']}",
        "최근 장면:\n" + recent,
        f"<user_input>\n{prompt}\n</user_input>",
        feedback and f"방금 쓴 장면은 다음 이유로 받아들여지지 않았다. 이 문제를 고쳐서 다시 쓴다: {feedback}",
    ]))
    return SYSTEM, user, turn_schema(w, story.schema)


def generate(story: Story, prompt: str, complete: Callable[[str, str, dict], str], retries: int = 2) -> tuple[dict | None, Verdict, int]:
    """유저 입력으로 다음 턴을 만든다. 규칙에 어긋나면 사유를 돌려주고 다시 쓰게 한다.

    반환: (받아들인 턴 또는 None, 마지막 판정, 호출 횟수).
    """
    feedback, verdict = None, Verdict(REJECTED, ["호출하지 않음"])
    for attempt in range(1, retries + 2):
        raw = complete(*turn_request(story, prompt, feedback))
        try:
            data = json.loads(raw)
            turn = {"text": str(data["text"]), "events": [_event(e) for e in data["events"]]}
        except (ValueError, KeyError, TypeError) as e:
            verdict = Verdict(REJECTED, [f"출력 형식이 올바르지 않다: {e}"])
        else:
            verdict = story.play(turn)
            if verdict.kind != REJECTED:
                return turn, verdict, attempt
        feedback = "; ".join(verdict.reasons)
    return None, verdict, attempt
