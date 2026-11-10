"""트리플 저장소. 청크 하나 = named graph 하나, 출처·버전은 메타 그래프에 둔다."""

import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote, unquote

import pyshacl
from rdflib import RDF, RDFS, XSD, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL

from onto.model import ONTO, Chunk, Document, Fact, Mention, domain_ns

SH = Namespace("http://www.w3.org/ns/shacl#")
META = ONTO.meta
CHUNK_PREFIX = str(ONTO["chunk:"])
DOC_PREFIX = str(ONTO["doc:"])
WEIGHT = {1: 0.9, 2: 0.7, 3: 0.5}


# 문서 ID 는 파일 이름에서 오므로 공백·괄호가 들어올 수 있다. 그대로 IRI 에 넣으면 직렬화가 실패한다.
def _ciri(chunk_id: str) -> URIRef:
    return URIRef(CHUNK_PREFIX + quote(chunk_id, safe="#"))


def _diri(doc_id: str) -> URIRef:
    return URIRef(DOC_PREFIX + quote(doc_id, safe=""))


def _cid(iri) -> str:
    return unquote(str(iri).removeprefix(CHUNK_PREFIX))


def _did(iri) -> str:
    return unquote(str(iri).removeprefix(DOC_PREFIX))


@dataclass
class SyncReport:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    quarantined: list[tuple[Fact, str]] = field(default_factory=list)


class Store:
    def __init__(self, domain_dir: Path, path: Path | None = None):
        domain_dir = Path(domain_dir)
        self.ns = domain_ns(domain_dir)
        self.tbox = Graph().parse(domain_dir / "tbox.ttl", format="turtle")
        self.shapes = Graph().parse(domain_dir / "shapes.ttl", format="turtle")
        self.tbox_version = self.tbox.value(None, OWL.versionInfo)
        self.path = Path(path) if path else None
        self.ds = Dataset()
        if self.path and self.path.exists():
            # 직렬화 후 재적재해도 그래프 이름과 메타 그래프가 유지된다(rdflib 7.6 확인)
            self.ds.parse(self.path, format="trig")
        self.meta = self.ds.graph(META)

    # ---------- 조회 ----------
    def _chunk_iris(self, doc_id: str | None = None):
        for c, d in self.meta.subject_objects(ONTO.fromDoc):
            if doc_id is None or d == _diri(doc_id):
                yield c

    def known_chunks(self, doc_id: str) -> set[str]:
        return {_cid(c) for c in self._chunk_iris(doc_id)}

    def _remove_chunk(self, iri: URIRef) -> None:
        self.ds.remove_graph(self.ds.graph(iri))
        self.meta.remove((iri, None, None))

    def retract_doc(self, doc_id: str) -> int:
        iris = list(self._chunk_iris(doc_id))
        for c in iris:
            self._remove_chunk(c)
        self.meta.remove((_diri(doc_id), None, None))
        return len(iris)

    # ---------- 적재 ----------
    def _to_triple(self, f: Fact):
        """사실 → (s, p, o, 엔티티 Mention 목록). 변환 불가면 사유 문자열."""
        p = self.ns[f.predicate]
        if (p, RDF.type, None) not in self.tbox:
            return f"TBox 에 없는 속성: {f.predicate}"
        is_obj = (p, RDF.type, OWL.ObjectProperty) in self.tbox
        for m in [f.subject] + ([f.object] if isinstance(f.object, Mention) else []):
            if (self.ns[m.cls], RDF.type, RDFS.Class) not in self.tbox:
                return f"TBox 에 없는 클래스: {m.cls}"
        # shapes 는 targetClass 의 주어만 검사하므로 주어 클래스가 틀리면 통과한다. 방향이 뒤집힌 추출을 여기서 잡는다.
        # shortcut: 클래스가 정확히 일치해야 한다, TBox 에 rdfs:subClassOf 를 쓰기 시작하면 상위 클래스도 허용
        dom = self.tbox.value(p, RDFS.domain)
        if dom is not None and self.ns[f.subject.cls] != dom:
            return f"{f.predicate} 의 정의역은 {self._local(str(dom))} 인데 주어가 {f.subject.cls}"
        if isinstance(f.object, Mention):
            if not is_obj:
                return f"{f.predicate} 는 리터럴 속성인데 엔티티가 왔다"
            rng = self.tbox.value(p, RDFS.range)
            if rng is not None and self.ns[f.object.cls] != rng:
                return f"{f.predicate} 의 치역은 {self._local(str(rng))} 인데 목적어가 {f.object.cls}"
            return p, f.object
        if is_obj:
            return f"{f.predicate} 는 객체 속성인데 리터럴이 왔다"
        rng = self.tbox.value(p, RDFS.range)
        try:
            if rng == XSD.integer:
                o = Literal(int(f.object), datatype=XSD.integer)
            elif rng == XSD.date:
                o = Literal(date.fromisoformat(f.object).isoformat(), datatype=XSD.date)
            else:
                o = Literal(f.object)
        except ValueError:
            return f"{f.predicate} 값 변환 실패: {f.object!r} → {rng}"
        return p, o

    def _build(self, items, resolve):
        """items: (Fact, p, o) 목록 → 청크 그래프."""
        g = Graph()
        for f, p, o in items:
            ends = [(f.subject, resolve(f.subject))]
            if isinstance(o, Mention):
                o = resolve(o)
                ends.append((f.object, o))
            s = ends[0][1]
            g.add((s, p, o))
            for m, iri in ends:
                g.add((iri, RDF.type, self.ns[m.cls]))
                g.add((iri, RDFS.label, Literal(m.label)))
        return g

    def _load_chunk(self, chunk: Chunk, facts, resolve, report: SyncReport):
        items = []
        for f in facts:
            r = self._to_triple(f)
            if isinstance(r, str):
                report.quarantined.append((f, r))
            else:
                items.append((f, *r))
        g = self._build(items, resolve)
        _, rg, _ = pyshacl.validate(g, shacl_graph=self.shapes, inference="none")
        bad = {}
        for r in rg.subjects(RDF.type, SH.ValidationResult):
            # maxCount 는 청크 안에서 격리하지 않는다(문서 간 충돌로 따로 다룬다)
            comp = rg.value(r, SH.sourceConstraintComponent)
            if comp != SH.MaxCountConstraintComponent:
                key = (rg.value(r, SH.focusNode), rg.value(r, SH.resultPath), rg.value(r, SH.value))
                bad[key] = f"{str(comp).rsplit('#', 1)[-1]}: {rg.value(r, SH.resultMessage)}"
        good = []
        for item in items:
            f, p, o = item
            s = resolve(f.subject)
            oo = resolve(o) if isinstance(o, Mention) else o
            reason = bad.get((s, p, oo)) or bad.get((s, p, None))
            if reason:
                report.quarantined.append((f, reason))
            else:
                good.append(item)
        cg = self.ds.graph(_ciri(chunk.chunk_id))
        for t in self._build(good, resolve):
            cg.add(t)

    def sync(self, doc: Document, chunks: list[Chunk], facts: list[Fact],
             resolve: Callable[[Mention], URIRef]) -> SyncReport:
        report = SyncReport()
        diri = _diri(doc.doc_id)
        want = {c.chunk_id: c for c in chunks}
        have = self.known_chunks(doc.doc_id)
        for cid in sorted(have - want.keys()):
            self._remove_chunk(_ciri(cid))
            report.removed.append(cid)
        report.kept = [c for c in want if c in have]
        new = [c for c in want if c not in have]

        self.meta.remove((diri, None, None))
        for k, v in [(ONTO.sha256, doc.sha256), (ONTO.title, doc.title), (ONTO.date, doc.date),
                     (ONTO.source, doc.source), (ONTO.grade, Literal(doc.grade))]:
            self.meta.add((diri, k, v if isinstance(v, Literal) else Literal(v)))

        by_chunk: dict[str, list[Fact]] = {c: [] for c in new}
        for f in facts:
            if f.chunk_id in by_chunk:
                by_chunk[f.chunk_id].append(f)
            else:
                report.quarantined.append((f, "새 청크가 아닌 청크의 사실"))
        now = Literal(datetime.now(timezone.utc).isoformat(), datatype=XSD.dateTime)
        for cid in new:
            ch = want[cid]
            self._load_chunk(ch, by_chunk[cid], resolve, report)
            ci = _ciri(cid)
            for k, v in [(ONTO.fromDoc, diri), (ONTO.text, Literal(ch.text)),
                         (ONTO.heading, Literal(" > ".join(ch.heading_path))),
                         (ONTO.tboxVersion, self.tbox_version), (ONTO.extractedAt, now)]:
                if v is not None:
                    self.meta.add((ci, k, v))
            report.added.append(cid)
        return report

    # ---------- 내보내기 ----------
    def _single_valued(self) -> set[URIRef]:
        return {p for ps in self.shapes.objects(None, SH.property)
                for p in self.shapes.objects(ps, SH.path)
                if Literal(1) in set(self.shapes.objects(ps, SH.maxCount))}

    def facts(self) -> list[dict]:
        labels: dict[URIRef, Counter] = {}
        types: dict[URIRef, URIRef] = {}
        groups: dict[tuple, dict] = {}
        aliases: dict[URIRef, set[str]] = {}
        for ci in sorted(self._chunk_iris()):
            cid = _cid(ci)
            doc = _did(self.meta.value(ci, ONTO.fromDoc))
            for s, p, o in self.ds.graph(ci):
                if p == RDFS.label:
                    labels.setdefault(s, Counter())[str(o)] += 1
                elif p == RDF.type:
                    types.setdefault(s, o)
                else:
                    if p == self.ns["alias"]:
                        aliases.setdefault(s, set()).add(str(o))
                    g = groups.setdefault((s, p, o), {"docs": set(), "chunks": []})
                    g["docs"].add(doc)
                    g["chunks"].append(cid)

        def label(x):
            if not isinstance(x, URIRef) or x not in labels:
                return str(x)
            # 문서들이 별칭으로 더 자주 적더라도 별칭이 아닌 이름(대표 이름)을 보여준다
            names = [l for l in labels[x] if l not in aliases.get(x, ())] or list(labels[x])
            return min(names, key=lambda l: (-labels[x][l], l))

        single = self._single_valued()
        objs: dict[tuple, set] = {}
        for s, p, o in groups:
            objs.setdefault((s, p), set()).add(o)

        out = []
        for (s, p, o), g in sorted(groups.items(), key=lambda kv: tuple(map(str, kv[0]))):
            best: dict[str, int] = {}
            for d in g["docs"]:
                di = _diri(d)
                src, gr = str(self.meta.value(di, ONTO.source)), int(self.meta.value(di, ONTO.grade) or 3)
                best[src] = min(best.get(src, 3), gr)
            miss = 1.0
            for gr in best.values():
                miss *= 1 - WEIGHT.get(gr, 0.5)
            out.append({
                "s": str(s), "p": str(p), "o": str(o),
                "s_label": label(s), "o_label": label(o),
                "o_is_entity": isinstance(o, URIRef),
                "s_type": str(types.get(s, "")), "o_type": str(types.get(o, "")),
                "docs": sorted(g["docs"]), "chunks": sorted(g["chunks"]),
                "confidence": round(1 - miss, 2),
                "conflict": p in single and len(objs[(s, p)]) > 1,
            })
        return out

    def _local(self, iri: str) -> str:
        return iri.removeprefix(str(self.ns))

    def export_json(self) -> dict:
        fs = self.facts()
        nodes: dict[str, dict] = {}
        edges = []
        for f in fs:
            ends = [(f["s"], f["s_label"], f["s_type"])]
            if f["o_is_entity"]:
                ends.append((f["o"], f["o_label"], f["o_type"]))
            for iri, lab, t in ends:
                nodes.setdefault(iri, {"id": iri, "label": lab, "type": self._local(t), "degree": 0})
            if f["o_is_entity"]:
                nodes[f["s"]]["degree"] += 1
                nodes[f["o"]]["degree"] += 1
                edges.append({"id": f"e{len(edges)}", "source": f["s"], "target": f["o"],
                              "predicate": self._local(f["p"]), "confidence": f["confidence"],
                              "docs": f["docs"], "chunks": f["chunks"], "conflict": f["conflict"]})
        docs = [{"id": _did(d), "title": str(self.meta.value(d, ONTO.title)),
                 "date": str(self.meta.value(d, ONTO.date)), "source": str(self.meta.value(d, ONTO.source)),
                 "grade": int(self.meta.value(d, ONTO.grade))}
                for d in sorted(self.meta.subjects(ONTO.grade))]
        used = {c for f in fs for c in f["chunks"]}
        evidence = {}
        for cid in sorted(used):
            ci = _ciri(cid)
            evidence[cid] = {"doc": _did(self.meta.value(ci, ONTO.fromDoc)),
                             "heading": str(self.meta.value(ci, ONTO.heading)),
                             "text": str(self.meta.value(ci, ONTO.text))}
        return {
            "nodes": list(nodes.values()), "edges": edges,
            "facts": [{"s": f["s_label"], "p": self._local(f["p"]), "o": f["o_label"],
                       "o_is_entity": f["o_is_entity"], "confidence": f["confidence"],
                       "docs": f["docs"], "chunks": f["chunks"], "conflict": f["conflict"]} for f in fs],
            "docs": docs, "evidence": evidence,
        }

    def save(self) -> None:
        if not self.path:
            raise ValueError("저장 경로가 없다")
        tmp = self.path.with_name(self.path.name + ".tmp")
        self.ds.serialize(tmp, format="trig")
        os.replace(tmp, self.path)  # 직렬화가 실패하거나 중단돼도 기존 저장소 파일은 그대로다
