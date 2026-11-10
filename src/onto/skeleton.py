"""골격 사전 검증: 미리 정해 둔 장면들이 처음부터 끝까지 성립하는지, 유저의 변경이 뒤 장면 어디에 닿는지 코드로 판정한다.

LLM 호출은 없다. 장면마다 '지시서'(그 장면을 쓸 때 모델에 줄 사실)를 만들고, 지시서가 같으면 본문을 다시 쓸 필요가 없다고 본다.
"""

import copy
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .story import Rejected, _pair, apply, initial_state

KEEP, REWRITE, REPLAN = "유지", "다시 쓰기", "다시 계획"
CHAR_KEYS, ITEM_KEYS = ("who", "with", "a", "b", "to"), ("item",)

# 사실의 중요도. 모델이 매기지 않고 어디서 성립했는지로 정한다.
CORE, CHOICE, SCENE = 3, 2, 1
FACT_LABEL = {CORE: "사실", CHOICE: "독자의 선택", SCENE: "최근의 일"}
FACTS_IN_BRIEF = 8                     # 지시서에 넣는 사실의 수
FACT_CAPS = {CHOICE: 4, SCENE: 2}      # 그 가운데 등급별 상한. 사소한 사실이 중요한 사실을 밀어내지 못하게 한다


def fact_tier(name: str, record: dict, world: dict) -> int:
    """사실의 중요도: 뼈대가 정한 사실이면 핵심, 독자가 고른 결과면 선택, 장면에서 일어난 일이면 장면."""
    if name in (world.get("facts") or []) or record.get("origin") == "spine":
        return CORE
    return CHOICE if record.get("origin") == "choice" else SCENE


def brief_facts(beat: dict, state: dict, world: dict, near: set[str]) -> list[tuple[int, str]]:
    """지시서에 넣을 사실을 고른다. 반환: [(등급, 이름)], 등급이 높은 것부터, 같은 등급 안에서는 성립한 순서.

    - 이 장면이 조건으로 삼는 사실은 인물·순서와 무관하게 항상 넣는다.
    - 나머지는 이 장면의 인물에게 걸린 것 가운데 등급이 높고 최근인 것부터 넣되, 등급별 상한을 지킨다.
    """
    facts = state.get("facts") or {}
    order = {name: i for i, name in enumerate(facts)}
    tier = {name: fact_tier(name, rec, world) for name, rec in facts.items()}
    must = [n[1] for n in beat.get("needs") or [] if n[0] == "fact" and n[1] in facts]
    picked, used = list(dict.fromkeys(must)), {}
    rest = [n for n, rec in facts.items() if n not in picked and (not rec["people"] or near & set(rec["people"]))]
    for name in sorted(rest, key=lambda n: (-tier[n], -order[n])):
        t = tier[name]
        if len(picked) < FACTS_IN_BRIEF and used.get(t, 0) < FACT_CAPS.get(t, FACTS_IN_BRIEF):
            picked.append(name)
            used[t] = used.get(t, 0) + 1
    return sorted(((CORE if n in must else tier[n], n) for n in picked), key=lambda x: (-x[0], order[x[1]]))


def forget_scene_facts(state: dict, world: dict) -> list[str]:
    """장면 등급의 사실을 상태에서 지운다. 주요 사건이 끝날 때 불러 상태가 사소한 사실로 불어나지 않게 한다. 반환: 지운 이름."""
    facts = state.get("facts") or {}
    gone = [name for name, rec in facts.items() if fact_tier(name, rec, world) == SCENE]
    for name in gone:
        del facts[name]
    return gone


@dataclass
class BeatResult:
    id: str
    brief: str
    errors: list[str] = field(default_factory=list)


@dataclass
class Report:
    errors: list[tuple[str, str]]  # (장면 ID, 사유)
    warnings: list[str]
    beats: list[BeatResult]


def load(domain_dir: Path) -> tuple[dict, dict]:
    """반환: (골격, 규칙)."""
    domain_dir = Path(domain_dir)
    read = lambda name: yaml.safe_load((domain_dir / name).read_text(encoding="utf-8"))
    return read("skeleton.yaml"), read("schema.yaml")


def participants(beat: dict, world: dict) -> tuple[list[str], list[str]]:
    """장면의 사건에 나오는 인물과, 사건이나 uses 가 가리키는 물건. 지시서에는 이들의 사실만 들어간다."""
    chars, items = [], list(beat.get("uses") or [])
    for ev in beat.get("fixed") or []:
        chars += [ev[k] for k in CHAR_KEYS if ev.get(k) in world["characters"]]
        items += [ev[k] for k in ITEM_KEYS if ev.get(k)]
    return list(dict.fromkeys(chars)), list(dict.fromkeys(items))


def brief(beat: dict, state: dict, world: dict) -> str:
    """장면을 쓸 때 모델에 줄 지시서. 장면에 들어서는 시점의 상태에서, 이 장면이 읽는 사실만 고른다."""
    chars, items = participants(beat, world)
    c = state["chars"]
    lines = [f"장면: {beat['id']} {beat.get('title', '')}".rstrip()]
    lines.append("인물: " + ", ".join(f"{n}({c[n]['at'] if c[n]['alive'] else '사망'})" for n in chars))
    # 사건에는 없지만 같은 장소에 있는 인물. 본문에 함께 그려질 수 있으므로 지시서에 넣는다.
    here = {c[n]["at"] for n in chars if c[n]["alive"]}
    others = [n for n in c if n not in chars and c[n]["alive"] and c[n]["at"] in here]
    if others:
        lines.append("같은 장소: " + ", ".join(others))
    for i in items:
        lines.append(f"물건: {i} — {state['items'].get(i, '없음')}")
    for n, a in enumerate(chars):
        for b in chars[n + 1:]:
            if _pair(a, b) in state["rels"]:
                lines.append(f"관계: {_pair(a, b)} {state['rels'][_pair(a, b)]}")
    lines += [f"{FACT_LABEL[t]}: {name}" for t, name in brief_facts(beat, state, world, set(chars) | set(others))]
    for ev in beat.get("fixed") or []:
        lines.append("반드시: " + " ".join(f"{k}={v}" for k, v in ev.items()))
    for key, label in (("open", "열린 부분"), ("plants", "복선 심기"), ("pays", "복선 회수")):
        if beat.get(key):
            lines.append(f"{label}: " + ", ".join(beat[key]))
    return "\n".join(lines)


def walk(world: dict, schema: dict, beats: list[dict]) -> list[BeatResult]:
    """장면을 처음 상태에서 순서대로 적용한다."""
    return walk_from(world, schema, beats, initial_state(world))[0]


def walk_from(world: dict, schema: dict, beats: list[dict], state: dict, origin: str = "scene") -> tuple[list[BeatResult], dict]:
    """주어진 상태에서 장면을 순서대로 적용한다. 성립하지 않는 사건은 사유를 남기고 건너뛴 채 끝까지 간다(문제를 한 번에 다 찾기 위해).

    반환: (장면별 결과, 끝났을 때의 상태). 넘겨받은 state 는 바꾸지 않는다.
    """
    state, out = copy.deepcopy(state), []
    for beat in beats:
        r = BeatResult(str(beat.get("id")), brief(beat, state, world))
        chars, _ = participants(beat, world)
        for item in beat.get("uses") or []:
            holder = state["items"].get(item)
            if item not in state["items"]:
                r.errors.append(f"'{item}' 은(는) 정의되지 않은 물건이다")
            elif holder not in chars or not state["chars"][holder]["alive"]:
                r.errors.append(f"'{item}' 을(를) 가진 인물이 이 장면에 없다(소지자: {holder})")
        for ev in beat.get("fixed") or []:
            try:
                apply(state, ev, schema, world["locations"], origin)
            except Rejected as e:
                r.errors.append(str(e))
            except KeyError as e:
                r.errors.append(f"사건에 필요한 값이 없다: {e}")
        out.append(r)
    return out, state


def validate(world: dict, schema: dict) -> Report:
    """골격 전체를 검사한다. 오류는 고쳐야 하는 것, 경고는 확인할 것."""
    beats = world.get("beats") or []
    results = walk(world, schema, beats)
    errors = [(r.id, e) for r in results for e in r.errors]
    warnings = []

    seen = set()
    for b in beats:
        if b.get("id") in seen:
            errors.append((str(b.get("id")), "장면 ID 가 겹친다"))
        seen.add(b.get("id"))

    planted, paid = {}, set()  # 복선 → 심은 장면
    for b in beats:
        for p in b.get("pays") or []:
            if p not in planted:
                errors.append((b["id"], f"심지 않은 복선을 회수한다: {p}"))
            elif p in paid:
                warnings.append(f"{b['id']}: 복선 '{p}' 을(를) 두 번 회수한다")
            paid.add(p)
        for p in b.get("plants") or []:
            planted.setdefault(p, b["id"])
    warnings += [f"{bid}: 복선 '{p}' 이(가) 끝까지 회수되지 않는다" for p, bid in planted.items() if p not in paid]

    used = {n for b in beats for n in participants(b, world)[0]}
    warnings += [f"{n} 이(가) 어느 장면에도 나오지 않는다" for n in world["characters"] if n not in used]
    return Report(errors, warnings, results)


def impact(world: dict, schema: dict, beat_id: str, events: list[dict], replace: bool = False) -> tuple[list[str], list[tuple[str, str, list[str]]]]:
    """유저가 장면 beat_id 의 사건을 바꿨을 때 뒤 장면이 받는 영향.

    events 는 그 장면에 더할 사건이고, replace 면 그 장면의 사건을 통째로 바꾼다.
    반환: (바꾼 장면 자체가 성립하지 않는 사유, [(뒤 장면 ID, 유지|다시 쓰기|다시 계획, 사유)]).
    """
    beats = world.get("beats") or []
    at = next((i for i, b in enumerate(beats) if b.get("id") == beat_id), None)
    if at is None:
        raise ValueError(f"장면 '{beat_id}' 가 없다")
    changed = copy.deepcopy(beats)
    changed[at]["fixed"] = list(events) if replace else [*(beats[at].get("fixed") or []), *events]

    base, new = walk(world, schema, beats), walk(world, schema, changed)
    blocked = [e for e in new[at].errors if e not in base[at].errors]
    out = []
    for b, n in zip(base[at + 1:], new[at + 1:]):
        broken = [e for e in n.errors if e not in b.errors]
        if broken:
            out.append((n.id, REPLAN, broken))
        elif n.brief != b.brief:  # 지시서가 달라졌으면 본문이 딛고 선 사실이 달라진 것이다
            old, cur = b.brief.split("\n"), n.brief.split("\n")
            out.append((n.id, REWRITE, [f"- {x}" for x in old if x not in cur] + [f"+ {x}" for x in cur if x not in old]))
        else:
            out.append((n.id, KEEP, []))
    return blocked, out
