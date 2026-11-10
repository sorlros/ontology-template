"""주요 사건(뼈대) 검증: 이야기의 전환점만 미리 정해 두고, 그 사이를 잇는 구간과 선택지를 코드로 검사한다.

- 주요 사건마다 '갖춰져 있어야 하는 것'(needs)을 적는다. 앞 사건 뒤의 상태와의 차이가 그 사이 구간이 해결할 과제(틈)다.
- 틈 가운데 구간으로 메울 수 없는 것(죽은 인물이 필요함)은 오류다.
LLM 호출은 없다.
"""

import copy
import math
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .skeleton import CHAR_KEYS, forget_scene_facts, walk_from
from .story import Rejected, _pair, apply, initial_state

REAPPEAR_AFTER = 3  # 이만큼의 주요 사건 동안 나오지 않은 인물이 다시 필요하면 알린다


@dataclass
class EventResult:
    id: str
    gaps: list[tuple] = field(default_factory=list)   # 구간이 메워야 하는 것
    errors: list[str] = field(default_factory=list)   # 구간으로 메울 수 없는 것


@dataclass
class SpineReport:
    errors: list[tuple[str, str]]
    warnings: list[str]
    events: list[EventResult]


def load(domain_dir: Path) -> tuple[dict, dict]:
    """반환: (세계 + 주요 사건, 규칙)."""
    domain_dir = Path(domain_dir)
    read = lambda name: yaml.safe_load((domain_dir / name).read_text(encoding="utf-8"))
    return read("spine.yaml"), read("schema.yaml")


def describe(need: tuple) -> str:
    kind, *a = need
    return {"alive": "{0} 이(가) 살아 있다", "at": "{0} 이(가) {1}에 있다", "together": "{0} 와(과) {1} 이(가) 같은 장소에 있다",
            "has": "{0} 이(가) {1} 을(를) 갖고 있다", "rel": "{0} 와(과) {1} 의 관계가 {2}",
            "fact": "'{0}' 이(가) 성립해 있다"}.get(kind, f"{kind} {a}").format(*a)


def _unknown(state: dict, world: dict, need: tuple) -> str | None:
    """조건이 정의되지 않은 이름을 가리키면 사유를 돌려준다."""
    kind, *a = need
    if kind == "fact":  # 뼈대가 사실의 목록을 정해 두었으면 그 안의 이름이어야 한다(이름이 어긋나는 것을 잡는다)
        known = world.get("facts")
        return f"'{a[0]}' 은(는) 정의되지 않은 사실이다" if known is not None and a[0] not in known else None
    chars = {"alive": a[:1], "at": a[:1], "together": a[:2], "has": a[:1], "rel": a[:2]}.get(kind)
    if chars is None:
        return f"정의되지 않은 조건: {kind}"
    for n in chars:
        if n not in state["chars"]:
            return f"'{n}' 은(는) 등장인물이 아니다"
    if kind == "at" and a[1] not in world["locations"]:
        return f"'{a[1]}' 은(는) 정의되지 않은 장소다"
    if kind == "has" and a[1] not in state["items"]:
        return f"'{a[1]}' 은(는) 정의되지 않은 물건이다"
    return None


def holds(state: dict, need: tuple) -> bool:
    kind, *a = need
    c = state["chars"]
    if kind == "fact":
        return a[0] in (state.get("facts") or {})
    if kind == "alive":
        return c[a[0]]["alive"]
    if kind == "at":
        return c[a[0]]["alive"] and c[a[0]]["at"] == a[1]
    if kind == "together":
        return c[a[0]]["alive"] and c[a[1]]["alive"] and c[a[0]]["at"] == c[a[1]]["at"]
    if kind == "has":
        return c[a[0]]["alive"] and state["items"][a[1]] == a[0]
    return c[a[0]]["alive"] and c[a[1]]["alive"] and state["rels"].get(_pair(a[0], a[1])) == a[2]


def recoverable(state: dict, need: tuple) -> bool:
    """충족되지 않은 조건을 그 사이의 장면들로 메울 수 있는가. 죽은 인물이 걸려 있으면 메울 수 없다."""
    kind, *a = need
    c = state["chars"]
    if kind == "fact":  # 사이의 장면에서 누군가 성립시키면 된다
        return True
    if kind == "alive":
        return False
    if kind in ("together", "rel"):
        return c[a[0]]["alive"] and c[a[1]]["alive"]
    if kind == "has":  # 물건은 가진 사람이 살아서 건네야 옮겨진다
        return c[a[0]]["alive"] and c[state["items"][a[1]]]["alive"]
    return c[a[0]]["alive"]


def _why_lost(state: dict, need: tuple) -> str:
    """메울 수 없는 조건이 왜 메울 수 없는지."""
    kind, *a = need
    c = state["chars"]
    names = {"alive": a[:1], "at": a[:1], "together": a[:2], "has": a[:1], "rel": a[:2]}[kind]
    dead = [n for n in names if not c[n]["alive"]]
    if dead:
        return f"{dead[0]} 은(는) 이미 사망했다"
    return f"{a[1]} 을(를) 가진 {state['items'][a[1]]} 이(가) 이미 사망했다"  # 남는 경우는 has 뿐이다


def _assume(state: dict, need: tuple) -> None:
    """구간이 이 조건을 메웠다고 보고 상태를 맞춘다."""
    kind, *a = need
    if kind == "at":
        state["chars"][a[0]]["at"] = a[1]
    elif kind == "together":
        state["chars"][a[1]]["at"] = state["chars"][a[0]]["at"]
    elif kind == "has":
        state["items"][a[1]] = a[0]
    elif kind == "rel":
        state["rels"][_pair(a[0], a[1])] = a[2]
    elif kind == "fact":
        state.setdefault("facts", {})[a[0]] = {"people": [], "origin": "spine"}


def event_needs(ev: dict) -> list[tuple]:
    """사건 하나가 성립하려면 필요한 조건. 모델이 needs 에 적지 않아도 코드가 더한다."""
    t = ev.get("type")
    if t == "act":
        return [("together", ev["who"], ev["with"])] if ev.get("with") else [("alive", ev["who"])]
    if t in ("move", "die", "mark"):
        return [("alive", ev["who"])]
    if t == "give":
        return [("together", ev["who"], ev["to"]), ("has", ev["who"], ev["item"])]
    if t == "relate":
        return [("alive", ev["a"]), ("alive", ev["b"])]
    return []


def cast(event: dict, world: dict) -> list[str]:
    """주요 사건에 걸려 있는 인물(사건과 조건에 나오는 이름)."""
    names = [ev.get(k) for ev in event.get("events") or [] for k in CHAR_KEYS]
    names += [x for need in event.get("needs") or [] for x in need[1:3]]
    return list(dict.fromkeys(n for n in names if n in world["characters"]))


def walk_spine(world: dict, schema: dict, events: list[dict], state: dict) -> tuple[list[EventResult], dict]:
    """주요 사건을 순서대로 적용한다. 메울 수 있는 틈은 메워졌다고 보고 넘어가고, 메울 수 없는 것은 오류로 남긴다.

    반환: (사건별 결과, 끝났을 때의 상태). 넘겨받은 state 는 바꾸지 않는다.
    """
    state, out = copy.deepcopy(state), []

    def require(r: EventResult, need: tuple) -> None:
        need = tuple(need)
        why = _unknown(state, world, need)
        if why:
            r.errors.append(why)
        elif not holds(state, need):
            if recoverable(state, need):
                r.gaps.append(need)
                _assume(state, need)
            else:
                r.errors.append(f"{_why_lost(state, need)} — 필요한 조건: {describe(need)}")

    for event in events:
        r = EventResult(str(event.get("id")))
        for need in event.get("needs") or []:
            require(r, need)
        for ev in event.get("events") or []:
            try:
                for need in event_needs(ev):
                    require(r, need)
                apply(state, ev, schema, world["locations"], "spine")
            except Rejected as e:
                if not any(str(e).split(" 은(는)")[0] in x for x in r.errors):  # 같은 원인을 두 번 적지 않는다
                    r.errors.append(str(e))
            except KeyError as e:
                r.errors.append(f"사건에 필요한 값이 없다: {e}")
        forget_scene_facts(state, world)  # 주요 사건이 끝나면 그 앞 구간의 사소한 사실은 잊는다
        out.append(r)
    return out, state


def check_spine(world: dict, schema: dict) -> SpineReport:
    """주요 사건 전체를 검사한다."""
    events = world.get("spine") or []
    results, _ = walk_spine(world, schema, events, initial_state(world))
    errors = [(r.id, e) for r in results for e in r.errors]
    warnings = []

    seen, last_when = {}, None
    for i, ev in enumerate(events):
        eid = str(ev.get("id"))
        if eid in seen:
            errors.append((eid, "사건 ID 가 겹친다"))
        when = ev.get("when")
        if not isinstance(when, int):
            errors.append((eid, "시점(when)이 정수가 아니다"))
        elif last_when is not None and when < last_when:
            errors.append((eid, f"시간이 거꾸로 간다({last_when}일 → {when}일)"))
        else:
            last_when = when
        because = ev.get("because") or []
        if i and not because:
            errors.append((eid, "원인(because)이 없다"))
        for b in because:
            if b not in seen:
                errors.append((eid, f"원인 '{b}' 이(가) 앞선 사건이 아니다"))
        seen[eid] = i

    known = world.get("facts")
    if known is not None:
        made = [(str(ev.get("id")), x.get("fact")) for ev in events for x in ev.get("events") or [] if x.get("type") == "mark"]
        errors += [(eid, f"'{name}' 은(는) 정의되지 않은 사실이다") for eid, name in made if name not in known]
        used = {name for _, name in made} | {n[1] for ev in events for n in ev.get("needs") or [] if n[0] == "fact"}
        warnings += [f"사실 '{name}' 이(가) 어느 주요 사건에서도 쓰이지 않는다" for name in known if name not in used]

    planted, paid = {}, set()
    for ev in events:
        for p in ev.get("pays") or []:
            if p not in planted:
                errors.append((str(ev.get("id")), f"심지 않은 복선을 회수한다: {p}"))
            paid.add(p)
        for p in ev.get("plants") or []:
            planted.setdefault(p, str(ev.get("id")))
    warnings += [f"{eid}: 복선 '{p}' 이(가) 끝까지 회수되지 않는다" for p, eid in planted.items() if p not in paid]

    used = {n for ev in events for n in cast(ev, world)}
    warnings += [f"{n} 이(가) 어느 주요 사건에도 나오지 않는다" for n in world["characters"] if n not in used]
    for i, names in enumerate(reintroduce(world)):
        warnings += [f"{events[i]['id']}: {n} 이(가) 오랜만에 나온다. 앞 구간에서 다시 등장시켜야 한다" for n in names]
    return SpineReport(errors, warnings, results)


def actors(event: dict) -> list[str]:
    """사건에서 스스로 움직이는 인물(순서대로). 죽는 것과 물건을 받는 것은 행동으로 치지 않는다."""
    out = []
    for ev in event.get("events") or []:
        t = ev.get("type")
        out += [ev.get("a"), ev.get("b")] if t == "relate" else [] if t == "die" else [ev.get("who")]
    return list(dict.fromkeys(n for n in out if n))


def check_craft(world: dict, schema: dict) -> list[tuple[str, str]]:
    """이야기의 짜임새 검사. 사실의 모순이 아니라 극의 형식(대가, 주인공과 적대자의 능동성, 긴장의 굴곡)을 본다.

    형식이 갖춰졌는지만 본다. 내용이 설득력 있는지는 판단하지 못한다. 반환: [(사건 ID 또는 '전체', 사유)].
    """
    events = world.get("spine") or []
    n, out = len(events), []
    if n < 3:
        return out
    third = math.ceil(n / 3)
    hero = world.get("viewpoint")
    roles = {name: c.get("role") for name, c in world["characters"].items()}
    acting = [actors(e) for e in events]

    for e, who in zip(events, acting):
        level = e.get("intensity")
        if not isinstance(level, int) or not 1 <= level <= 5:
            out.append((str(e.get("id")), "강도(intensity)가 1~5 의 정수가 아니다"))
        elif level >= 3 and not (e.get("cost") or "").strip():
            out.append((str(e.get("id")), "강도 3 이상인 사건인데 대가(cost)가 비어 있다"))
        if hero in who and not (e.get("dilemma") or "").strip():
            out.append((str(e.get("id")), "주인공이 행동하는 사건인데 딜레마(dilemma)가 비어 있다"))

    # 결말의 대가: 마지막 3분의 1에 죽음, 관계의 단절, 주인공이 물건을 내놓는 일 가운데 하나
    loss = set(schema.get("loss_relations") or [])
    paid = any(ev.get("type") == "die" or (ev.get("type") == "relate" and ev.get("kind") in loss)
               or (ev.get("type") == "give" and (hero is None or ev.get("who") == hero))
               for e in events[-third:] for ev in e.get("events") or [])
    if not paid:
        out.append(("전체", f"마지막 {third}개 사건에 되돌릴 수 없는 대가(죽음, 관계의 단절, 주인공이 물건을 내놓음)가 없다"))

    if hero:
        mine = sum(hero in who for who in acting)
        if mine * 2 < n:
            out.append(("전체", f"주인공이 직접 행동하는 사건이 {n}개 중 {mine}개뿐이다(절반 이상 필요)"))

    foes = [name for name, r in roles.items() if r == "antagonist"]
    if foes:
        first = sum(bool(who) and who[0] in foes for who in acting)
        need = max(1, n // 5)
        if first < need:
            out.append(("전체", f"적대자가 먼저 움직이는 사건이 {first}개뿐이다({need}개 이상 필요)"))
    for name, r in roles.items():
        if r in ("ally", "antagonist") and not any(name in who for who in acting):
            out.append(("전체", f"{name}({r}) 이(가) 어느 사건에서도 직접 행동하지 않는다"))

    levels = [e.get("intensity") for e in events]
    if all(isinstance(x, int) for x in levels):
        peak = max(range(n), key=lambda i: (levels[i], i))  # 가장 높은 사건(같으면 뒤의 것)
        if peak < n - third:
            out.append(("전체", f"강도가 가장 높은 사건({events[peak].get('id')})이 마지막 {third}개 안에 없다"))
        if sum(levels[-third:]) / third <= sum(levels[:third]) / third:
            out.append(("전체", "뒤로 갈수록 강도가 오르지 않는다(마지막 3분의 1의 평균이 처음 3분의 1보다 높아야 한다)"))
        if not any(levels[i] < levels[i - 1] for i in range(max(1, math.ceil(n / 2)), peak)):
            out.append(("전체", "후반부에 절정을 앞두고 강도가 한 번 꺾이는 사건이 없다"))
    return out


def reintroduce(world: dict) -> list[list[str]]:
    """사건마다, 앞의 여러 사건 동안 나오지 않다가 다시 필요한 인물."""
    last, out = {}, []
    for i, ev in enumerate(world.get("spine") or []):
        names = cast(ev, world)
        out.append([n for n in names if n in last and i - last[n] > REAPPEAR_AFTER])
        last.update({n: i for n in names})
    return out


def task(world: dict, schema: dict, index: int) -> dict:
    """index 번째 주요 사건 앞의 구간이 해결할 과제: 메울 틈, 흐른 날수, 다시 등장시킬 인물."""
    events = world["spine"]
    results, _ = walk_spine(world, schema, events[:index + 1], initial_state(world))
    days = events[index]["when"] - events[index - 1]["when"] if index else 0
    return {"gaps": results[index].gaps, "days": days, "reintroduce": reintroduce(world)[index]}


def check_segment(world: dict, schema: dict, state: dict, beats: list[dict], target: dict) -> tuple[list[tuple[str, str]], list[tuple]]:
    """구간의 장면들을 state 에서 적용해 보고, 끝났을 때 다음 주요 사건(target)이 바로 일어날 수 있는지 본다.

    반환: (장면의 오류 [(장면 ID, 사유)], 아직 갖춰지지 않은 조건).
    """
    results, end = walk_from(world, schema, beats, state)
    errors = [(r.id, e) for r in results for e in r.errors]
    (r,), _ = walk_spine(world, schema, [target], end)
    return errors + [(str(target.get("id")), e) for e in r.errors], r.gaps


def changes_state(choice: dict) -> bool:
    """선택지가 상태를 바꾸는가. 상대에게 말을 거는 것(act)만으로는 뒤에 아무것도 남지 않는다."""
    return any(ev.get("type") != "act" for ev in choice.get("events") or [])


def offer(world: dict, schema: dict, state: dict, beat: dict, rest: list[dict], spine_rest: list[dict], want: int = 3) -> list[dict]:
    """장면이 끝난 뒤 독자에게 보여 줄 선택지. state 는 그 장면의 사건까지 적용된 상태다.

    성립하지 않는 선택지와, 남은 주요 사건을 불가능하게 만드는 선택지는 뺀다.
    남은 선택지에는 replan(이 구간의 남은 장면을 다시 짜야 하는가)을 붙인다. 태도 선택과 행동 선택이 하나씩은 들어가게 고른다.
    """
    ok = []
    for c in beat.get("choices") or []:
        s = copy.deepcopy(state)
        try:
            for ev in c.get("events") or []:
                apply(s, ev, schema, world["locations"], "choice")
        except (Rejected, KeyError):
            continue
        if any(r.errors for r in walk_spine(world, schema, spine_rest, s)[0]):
            continue
        errors, unmet = check_segment(world, schema, s, rest, spine_rest[0]) if spine_rest else ([], [])
        ok.append({**c, "replan": bool(errors or unmet)})
    first = [next((c for c in ok if not changes_state(c)), None), next((c for c in ok if changes_state(c)), None)]
    picked = [c for c in first if c]
    picked += [c for c in ok if c not in picked][:max(0, want - len(picked))]
    return [c for c in ok if c in picked]  # 원래 순서대로
