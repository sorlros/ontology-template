"""엔티티 해소: 이름을 정규화하고 학습한 별칭으로 같은 IRI 에 모은다."""

import json
import os
import unicodedata
from pathlib import Path
from urllib.parse import quote

from rdflib import URIRef

from onto.model import ONTO, Fact, Mention


def normalize(name: str) -> str:
    """NFKC → casefold → 공백·문장부호 제거."""
    s = unicodedata.normalize("NFKC", name).casefold()
    return "".join(c for c in s if not c.isspace() and not unicodedata.category(c).startswith("P"))


class Registry:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else None
        self.aliases: dict[str, str] = {}  # "<cls>\t<별칭 키>" → 대표 키
        if self.path and self.path.exists():
            self.aliases = json.loads(self.path.read_text(encoding="utf-8"))

    @staticmethod
    def _k(cls: str, key: str) -> str:
        return f"{cls}\t{key}"

    def _canon(self, m: Mention) -> str:
        # 문장부호뿐인 이름은 키가 비므로 원문을 키로 써서 서로 합쳐지지 않게 한다
        key = normalize(m.label) or m.label.strip()
        return self.aliases.get(self._k(m.cls, key), key)

    def learn(self, facts: list[Fact]) -> list[str]:
        """별칭을 등록한다. 이미 다른 엔티티의 별칭이면 덮어쓰지 않고 그 사유를 반환한다."""
        ignored = []
        for f in facts:
            if f.predicate == "alias" and isinstance(f.object, str):
                alias = normalize(f.object)
                if not alias:
                    continue
                k, canon = self._k(f.subject.cls, alias), self._canon(f.subject)
                # 덮어쓰면 기존 언급이 조용히 다른 엔티티로 넘어간다. 먼저 등록된 쪽을 지킨다.
                if self.aliases.setdefault(k, canon) != canon:
                    ignored.append(f"별칭 '{f.object}' 는 이미 '{self.aliases[k]}' 의 별칭이라 '{f.subject.label}' 에 등록하지 않음")
        return ignored

    def resolve(self, mention: Mention) -> URIRef:
        return ONTO[f"entity:{mention.cls}:{quote(self._canon(mention))}"]

    def save(self) -> None:
        if self.path:
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.aliases, ensure_ascii=False, sort_keys=True, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
