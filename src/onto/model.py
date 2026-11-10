"""모든 단계가 공유하는 데이터 계약. 단계 간에는 이 타입만 오간다."""

from dataclasses import dataclass
from pathlib import Path

import yaml
from rdflib import Namespace

# 메타 어휘(출처·버전 등)와 엔티티 IRI 의 접두사. 도메인 어휘는 domain_ns() 로 얻는다.
ONTO = Namespace("urn:onto:")

# 본문이 아닌 청크를 구분하는 heading_path 표식
FRONTMATTER = ("@frontmatter",)  # text = frontmatter YAML 원문
ROW = ("@row",)  # text = CSV 한 행의 JSON


@dataclass(frozen=True)
class Document:
    doc_id: str
    path: str
    sha256: str  # 파일 전체 내용 해시
    title: str
    date: str  # ISO 날짜, 필수
    source: str  # 발행 주체, 필수. 같은 source 의 문서들은 독립 출처로 세지 않는다
    grade: int  # 출처 등급 1(가장 신뢰)~3
    doc_type: str  # mapping.yaml 의 doc_types 키


@dataclass(frozen=True)
class Chunk:
    doc_id: str
    chunk_id: str  # f"{doc_id}#{sha256[:12]}" — 내용이 같으면 ID 도 같다
    heading_path: tuple[str, ...]
    text: str
    sha256: str  # 청크 내용 해시


@dataclass(frozen=True)
class Mention:
    label: str
    cls: str  # TBox 클래스의 로컬 이름 (예: "Organization")


@dataclass(frozen=True)
class Fact:
    subject: Mention
    predicate: str  # TBox 속성의 로컬 이름 (예: "hasSupplier")
    object: Mention | str  # Mention 이면 엔티티, str 이면 리터럴
    chunk_id: str
    doc_id: str


def load_mapping(domain_dir: Path) -> dict:
    return yaml.safe_load((Path(domain_dir) / "mapping.yaml").read_text(encoding="utf-8"))


def domain_ns(domain_dir: Path) -> Namespace:
    return Namespace(load_mapping(domain_dir)["namespace"])
