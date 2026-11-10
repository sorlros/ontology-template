"""통합: 도메인 폴더의 문서를 수집 → 추출 → 해소 → 적재하고 시각화용 JSON 을 내보낸다."""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Callable

from .extract import ExtractionError, extract_llm, extract_rules
from .ingest import ingest_csv, ingest_markdown
from .model import FRONTMATTER, ROW, load_mapping
from .resolve import Registry
from .store import Store, SyncReport


def ingest(
    domain_dir: Path, data_dir: Path, complete: Callable[[str, str, dict], str] | None = None
) -> tuple[dict[str, SyncReport], dict[str, str], list[str], list[str]]:
    """반환: (문서별 적재 결과, 거부된 파일과 사유, 폴더에서 사라져 회수한 문서 ID, 경고)."""
    domain_dir, data_dir = Path(domain_dir), Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    mapping = load_mapping(domain_dir)
    store = Store(domain_dir, data_dir / "store.trig")
    registry = Registry(data_dir / "registry.json")

    items, rejected = [], {}
    docs_dir = domain_dir / "docs"
    # 정형·권위 있는 출처(CSV)를 먼저 넣어 별칭을 학습한 뒤 md 를 날짜순으로 넣는다.
    for p in sorted(docs_dir.glob("*.csv")):
        spec = (mapping.get("csv") or {}).get(p.name)
        if not spec:
            rejected[p.name] = "mapping.yaml 의 csv 에 매핑이 없다"
            continue
        try:
            items.append(ingest_csv(p, spec["meta"]))
        except ValueError as e:
            rejected[p.name] = str(e)
    mds = []
    for p in sorted(docs_dir.glob("*.md")):
        try:
            mds.append(ingest_markdown(p))
        except ValueError as e:
            rejected[p.name] = str(e)
    items += sorted(mds, key=lambda dc: dc[0].date)

    # 같은 ID 로 두 번 적재하면 뒤 파일이 앞 파일의 청크를 지운다. 어느 쪽이 맞는지 추측하지 않고 둘 다 거부한다.
    ids = Counter(doc.doc_id for doc, _ in items)
    for doc, _ in items:
        if ids[doc.doc_id] > 1:
            rejected[Path(doc.path).name] = f"문서 ID '{doc.doc_id}' 가 다른 파일과 겹친다"
    items = [(doc, chunks) for doc, chunks in items if ids[doc.doc_id] == 1]

    reports, warnings = {}, []
    llm_down = False
    for doc, chunks in items:
        known = store.known_chunks(doc.doc_id)
        # 머리말에서 규칙으로 뽑은 사실. 본문 추출의 맥락("이 계약"이 무엇인지)으로 LLM 에 함께 넘긴다.
        context = [f for c in chunks if c.heading_path == FRONTMATTER for f in extract_rules(doc, c, mapping)]
        rule_facts, llm_facts, keep = [], [], []
        for c in chunks:
            if c.chunk_id in known:
                keep.append(c)  # 바뀌지 않은 청크는 다시 추출하지 않는다(LLM 에 다시 보내지 않는다)
            elif c.heading_path in (FRONTMATTER, ROW):
                rule_facts += extract_rules(doc, c, mapping)
                keep.append(c)
            elif complete and not llm_down:
                try:
                    llm_facts += extract_llm(doc, c, domain_dir, context, complete)
                    keep.append(c)
                except ExtractionError as e:
                    warnings.append(f"{doc.doc_id}: 본문 추출 실패, 다음 실행에서 다시 시도: {e}")
                    llm_down = e.fatal
            # 추출하지 못한 새 본문 청크는 저장하지 않는다. 저장하면 '추출 완료'로 남아 다시 시도되지 않는다.
        warnings += [f"{doc.doc_id}: {w}" for w in registry.learn(rule_facts)]
        reports[doc.doc_id] = store.sync(doc, keep, rule_facts + llm_facts, registry.resolve)

    # 거부된 파일의 문서는 ID 를 알 수 없어 회수 대상에서 뺄 수 없다. 그래서 거부가 있으면 회수하지 않는다.
    gone = []
    if not rejected:
        gone = sorted({d["id"] for d in store.export_json()["docs"]} - set(reports))
        for doc_id in gone:
            store.retract_doc(doc_id)

    store.save()
    registry.save()
    return reports, rejected, gone, warnings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="onto")
    ap.add_argument("command", choices=["ingest", "export", "serve", "story", "skeleton", "spine"])
    ap.add_argument("domain_dir", type=Path, help="tbox.ttl·shapes.ttl·mapping.yaml·docs/ 가 있는 폴더")
    ap.add_argument("--data", type=Path, default=Path("data"), help="저장소 파일 폴더 (기본 data/)")
    ap.add_argument("--out", type=Path, default=Path("viz/graph.json"), help="export 출력 경로")
    ap.add_argument("--llm", action="store_true", help="본문 청크를 Codex CLI 로 보내 사실을 추출한다(문서 본문이 OpenAI 로 전송된다)")
    ap.add_argument("--viz", type=Path, default=Path("viz"), help="serve 가 보여 줄 화면 폴더 (기본 viz/)")
    ap.add_argument("--port", type=int, default=8765, help="serve 포트 (기본 8765)")
    ap.add_argument("--change", nargs=2, metavar=("BEAT", "EVENTS_JSON"), help="skeleton: 이 장면에 사건을 더했을 때 뒤 장면이 받는 영향을 본다")
    ap.add_argument("--replace", action="store_true", help="skeleton --change: 사건을 더하지 않고 그 장면의 사건을 통째로 바꾼다")
    ap.add_argument("--genre", help="spine --llm: 장르")
    ap.add_argument("--tone", help="spine --llm: 분위기")
    ap.add_argument("--name", help="spine --llm: 주인공 이름")
    ap.add_argument("--traits", default="", help="spine --llm: 주인공 성격(쉼표로 구분)")
    ap.add_argument("--resegment", type=Path, help="spine --llm: 이 결과 파일의 세계와 주요 사건으로 구간만 다시 만든다")
    args = ap.parse_args(argv)

    if args.command == "spine" and args.llm:
        # 세계 → 결말·주요 사건 → 재작성 → 첫 구간을 Codex 로 실제 생성해 본다. 결과는 --out 에 JSON 으로 남긴다.
        import yaml
        from .llm import codex_complete
        from .planner import run
        schema = yaml.safe_load((args.domain_dir / "schema.yaml").read_text(encoding="utf-8"))
        if args.resegment:  # 저장된 결과에서 구간만 다시 만든다
            from .planner import resegment
            result = resegment(json.loads(args.resegment.read_text(encoding="utf-8")), schema, codex_complete, log=lambda m: print(m, flush=True))
        else:
            seed = {"genre": args.genre, "tone": args.tone, "name": args.name, "traits": [t for t in args.traits.split(",") if t]}
            result = run(seed, schema, codex_complete, log=lambda m: print(m, flush=True))
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{'통과' if result['passed'] else '실패'}: {args.out}")
        return 0 if result["passed"] else 1

    if args.command == "spine":
        from .spine import check_craft, check_segment, check_spine, describe, load, offer, task, walk_spine
        from .skeleton import walk_from
        from .story import initial_state
        world, schema = load(args.domain_dir)
        report = check_spine(world, schema)
        events = world.get("spine") or []
        print(f"뼈대 검증: 주요 사건 {len(events)}개, 오류 {len(report.errors)}, 경고 {len(report.warnings)}")
        for ev, r in zip(events, report.events):
            print(f"  {r.id} ({ev.get('when')}일) {ev.get('title', '')}")
            for g in r.gaps:
                print(f"      앞 구간의 과제: {describe(g)}")
        for eid, why in report.errors:
            print(f"  오류  {eid}: {why}")
        for w in report.warnings:
            print(f"  경고  {w}")
        craft = check_craft(world, schema)
        print(f"짜임새 검사: 문제 {len(craft)}")
        for eid, why in craft:
            print(f"  문제  {eid}: {why}")
        beats = world.get("segment") or []
        if beats and len(events) > 1 and not report.errors:
            # 샘플의 구간은 첫 주요 사건과 둘째 주요 사건 사이다.
            _, state = walk_spine(world, schema, events[:1], initial_state(world))
            errors, unmet = check_segment(world, schema, state, beats, events[1])
            print(f"\n구간 검증({events[0]['id']} → {events[1]['id']}): 장면 {len(beats)}개, 오류 {len(errors)}, 남은 과제 {len(unmet)}")
            for bid, why in errors:
                print(f"  오류  {bid}: {why}")
            for g in unmet:
                print(f"  남은 과제: {describe(g)}")
            _, after = walk_from(world, schema, beats[:1], state)
            shown = offer(world, schema, after, beats[0], beats[1:], events[1:])
            print(f"\n{beats[0]['id']} 뒤의 선택지 (후보 {len(beats[0].get('choices') or [])}개 중 {len(shown)}개)")
            for c in shown:
                print(f"  [{c['kind']}] {c['label']}" + ("  → 남은 장면을 다시 계획" if c["replan"] else ""))
        return 1 if report.errors or craft else 0

    if args.command == "skeleton":
        from .skeleton import KEEP, impact, load, validate
        world, schema = load(args.domain_dir)
        report = validate(world, schema)
        sizes = [len(b.brief) for b in report.beats]
        print(f"골격 검증: 장면 {len(report.beats)}개, 오류 {len(report.errors)}, 경고 {len(report.warnings)}"
              + (f", 지시서 {min(sizes)}~{max(sizes)}자" if sizes else ""))
        for beat_id, why in report.errors:
            print(f"  오류  {beat_id}: {why}")
        for w in report.warnings:
            print(f"  경고  {w}")
        if args.change:
            events = json.loads(args.change[1])
            blocked, after = impact(world, schema, args.change[0], events if isinstance(events, list) else [events], args.replace)
            print(f"\n{args.change[0]} 변경: {'성립하지 않음' if blocked else '성립'}")
            for why in blocked:
                print(f"  사유  {why}")
            for beat_id, verdict, reasons in after:
                print(f"  [{verdict}] {beat_id}")
                for r in reasons:
                    print(f"      {r}")
            print(f"  요약  뒤 장면 {len(after)}개 중 {sum(v == KEEP for _, v, _ in after)}개는 그대로 둔다")
        return 1 if report.errors else 0

    if args.command == "story" and args.llm:
        # 한 줄에 유저 입력 하나. 처음 세계 상태에서 시작해 Codex 가 본문과 사건을 함께 쓴다.
        from .llm import codex_complete
        from .story import Story, generate
        story = Story(args.domain_dir)
        for line in sys.stdin:
            if not line.strip():
                continue
            turn, v, calls = generate(story, line.strip(), codex_complete)
            print(f"\n> {line.strip()}\n[{v.kind}] 호출 {calls}회" + (f", 턴 {len(story.turns)}" if turn else ", 기록 안 함"))
            if turn:
                print(turn["text"])
                for ev in turn["events"]:
                    print(f"    사건  {ev}")
            for e, attr, old, new in v.diff:
                print(f"    변화  {e} {attr}: {old} → {new}")
            for r in v.reasons:
                print(f"    사유  {r}")
            sys.stdout.flush()
        print(f"\n확정 사실 (턴 {len(story.turns)} 이후)\n{story.scene()}")
        return 0

    if args.command == "story":
        from .story import REJECTED, run
        results, story = run(args.domain_dir)
        n = 0
        for turn, v in results:
            n += v.kind != REJECTED
            print(f"[{v.kind}] {'턴 ' + str(n) if v.kind != REJECTED else '기록 안 함'}: {turn['text']}")
            for e, attr, old, new in v.diff:
                print(f"    변화  {e} {attr}: {old} → {new}")
            for r in v.reasons:
                print(f"    사유  {r}")
        print(f"\n다음 턴에 넣을 확정 사실 (턴 {len(story.turns)} 이후)\n{story.scene()}")
        return 0

    if args.command == "export":
        data = Store(args.domain_dir, args.data / "store.trig").export_json()
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{args.out}: 노드 {len(data['nodes'])}, 엣지 {len(data['edges'])}, 사실 {len(data['facts'])}")
        return 0

    complete = None
    if args.llm:
        from .llm import codex_complete as complete

    if args.command == "serve":
        from .server import make_server
        ingest(args.domain_dir, args.data, complete)  # 폴더에 이미 있는 문서를 먼저 반영한다
        server = make_server(args.domain_dir, args.data, args.viz, args.port, complete)
        print(f"http://localhost:{args.port}/ (종료: Ctrl+C). 본문 추출: {'Codex 로 전송' if complete else '꺼짐(--llm 으로 켠다)'}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0

    reports, rejected, gone, warnings = ingest(args.domain_dir, args.data, complete)
    for doc_id, r in reports.items():
        print(f"{doc_id}: 추가 {len(r.added)} / 제거 {len(r.removed)} / 유지 {len(r.kept)} / 격리 {len(r.quarantined)}")
        for fact, reason in r.quarantined:
            print(f"  격리: {fact.subject.label} {fact.predicate} {fact.object} — {reason}")
    for doc_id in gone:
        print(f"{doc_id}: 폴더에서 사라져 회수")
    for w in warnings:
        print(f"경고 {w}", file=sys.stderr)
    for name, reason in rejected.items():
        print(f"거부 {name}: {reason}", file=sys.stderr)
    return 1 if rejected or warnings else 0


if __name__ == "__main__":
    sys.exit(main())
