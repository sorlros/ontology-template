# 파일럿 구현 계획

도메인에 무관한 온톨로지 파이프라인의 최소 동작 버전. 도메인별로 바꾸는 것은
`domains/<이름>/` 의 `tbox.ttl`, `shapes.ttl`, `mapping.yaml` 세 파일뿐이다.

흐름: 수집(ingest) → 추출(extract) → 엔티티 해소(resolve) → 검증·적재(store) → 내보내기 → 시각화(viz)

## 공통 규칙

- 단계 간 데이터는 `src/onto/model.py` 의 타입만 쓴다. **이 파일과 `domains/contracts/` 의 ttl·yaml 은 수정 금지**(계약). 계약이 부족하면 고치지 말고 보고한다.
- 의존성은 설치돼 있다(`rdflib`, `pyshacl`, `pyyaml`, `pytest`). 새 의존성 추가 금지, `pip install` 금지.
- 실행: `.venv/bin/pytest tests/test_<영역>.py -q` (작업 디렉터리는 리포 루트).
- 자기 영역 파일만 만든다. 다른 영역 모듈을 import 하지 않는다(`onto.model` 만 가능). 시험에 다른 영역의 산출물이 필요하면 `model` 타입으로 직접 만든다.
- 주석·docstring 은 한국어, 식별자는 영어. 요청에 없는 옵션·추상화는 넣지 않는다.
- git commit 하지 않는다.

## 영역 A — 수집 `src/onto/ingest.py`, `tests/test_ingest.py`, 샘플 데이터

```python
def ingest_markdown(path: Path) -> tuple[Document, list[Chunk]]
def ingest_csv(path: Path, meta: dict) -> tuple[Document, list[Chunk]]
```

- md frontmatter 키: `id`(없으면 파일 stem), `title`(없으면 stem), `date`, `source`, `grade`(없으면 3), `type` 과 도메인 필드.
- `date`·`source`·`type` 이 없으면 `ValueError`(메시지에 파일 경로와 빠진 키). 메타 없는 문서는 받지 않는다.
- 청크: 첫 청크는 `heading_path=FRONTMATTER`, `text`=frontmatter YAML 원문. 이후 본문을 제목(`#`~`###`) 단위로 자르고 `heading_path` 는 상위 제목부터의 경로. 내용이 빈 청크는 만들지 않는다.
- `chunk_id = f"{doc_id}#{sha256(text)[:12]}"`. 한 문서 안에 내용이 같은 청크가 둘이면 하나만 남긴다.
- CSV: `meta` 는 `mapping.yaml` 의 `csv.<파일명>.meta`. 행마다 청크 하나, `heading_path=ROW`, `text`=행의 JSON(`ensure_ascii=False`, `sort_keys=True`). `doc_id` 는 파일 stem.
- 샘플 데이터 `domains/contracts/docs/` (`mapping.yaml`·`tbox.ttl` 을 읽고 맞춰 쓴다):
  - `suppliers.csv`: 열 `name,country,ceo,aliases`, 공급사 3곳. `aliases` 는 `|` 구분(예: `한빛물산(주)|Hanbit`).
  - 계약 md 5개(`type: contract`, frontmatter 에 `contract, supplier, buyer, amount, signed`, 일부는 `supersedes`) + 메모 md 1개(`type: memo`).
  - 반드시 포함: ① 같은 계약을 다른 `source` 두 곳이 같은 내용으로 기술(교차 확인) ② 같은 계약의 `amount` 가 문서 간에 다름(충돌) ③ 공급사를 별칭으로 적은 문서 ④ 같은 `source` 의 거의 같은 사본 ⑤ `supersedes` 로 이어지는 계약 2개 ⑥ 본문에 frontmatter 에 없는 사실을 서술한 문단(LLM 추출용, 예: 대표자 교체).
  - `amount` 는 정수(원), `signed`·`date` 는 따옴표로 감싼 ISO 날짜 문자열.

## 영역 B — 추출·엔티티 해소 `src/onto/extract.py`, `src/onto/resolve.py`, `tests/test_extract.py`, `tests/test_resolve.py`

```python
# extract.py
def extract_rules(doc: Document, chunk: Chunk, mapping: dict) -> list[Fact]
def schema_summary(domain_dir: Path) -> str
def extract_llm(doc: Document, chunk: Chunk, domain_dir: Path, complete: Callable[[str], str]) -> list[Fact]

# resolve.py
class Registry:
    def __init__(self, path: Path | None = None)   # path 가 있고 파일이 있으면 불러온다
    def learn(self, facts: list[Fact]) -> list[str] # predicate == "alias" 인 사실로 별칭 등록. 반환: 무시한 별칭과 사유
    def resolve(self, mention: Mention) -> URIRef
    def save(self) -> None
```

- `extract_rules`: `FRONTMATTER` 청크는 `yaml.safe_load(chunk.text)` 후 `mapping["doc_types"][doc.doc_type]` 로, `ROW` 청크는 `json.loads(chunk.text)` 후 `mapping["csv"][Path(doc.path).name]` 로 변환. 그 외 청크와 매핑이 빈 유형은 `[]`.
  - 주어: `Mention(값[label_from], class)`. `label_from` 값이 없으면 `[]`.
  - 필드: `class` 가 있으면 목적어는 `Mention`, 없으면 `str(값)`. 값이 리스트면 사실 여러 개. `split` 이 있으면 그 구분자로 나눠 여러 개. 빈 값은 건너뛴다.
- `schema_summary`: `tbox.ttl` 에서 클래스 목록과 속성별 `정의역 → 치역` 을 LLM 프롬프트용 텍스트로 만든다.
- `extract_llm`: 프롬프트(스키마 요약 + 청크 본문 + JSON 출력 지시)를 `complete` 에 넘기고 응답 JSON 을 `Fact` 로 바꾼다. 실제 API 연동은 범위 밖 — `complete` 는 주입받는 함수다.
  - 출력 형식: `[{"s": {"label","cls"}, "p": "<속성>", "o": {"label","cls"} 또는 "<리터럴>"}]`.
  - 신뢰 경계다: JSON 이 아니거나(코드 펜스 감싼 경우는 벗겨서 허용), TBox 에 없는 속성·클래스, 필수 키 누락인 항목은 버린다. 전체가 깨져도 예외 없이 `[]`.
- `Registry`:
  - 정규화 키: NFKC → casefold → 공백과 문장부호 제거.
  - IRI: `ONTO[f"entity:{cls}:{quote(대표 키)}"]`. 같은 입력이면 항상 같은 IRI(멱등).
  - `learn`: `Fact(subject, "alias", "<별칭>")` 을 보면 `(cls, 별칭 키) → 주어의 대표 키` 로 등록.
  - `resolve`: 별칭 표에 있으면 대표 IRI, 없으면 자기 키로 IRI. 클래스가 다르면 같은 이름이어도 다른 엔티티.
  - 저장은 JSON. 유사 표기(오타, 약칭) 매칭은 범위 밖.

## 영역 C — 저장소 `src/onto/store.py`, `tests/test_store.py`

```python
class Store:
    def __init__(self, domain_dir: Path, path: Path | None = None)  # path(.trig) 가 있으면 불러온다
    def known_chunks(self, doc_id: str) -> set[str]
    def sync(self, doc: Document, chunks: list[Chunk], facts: list[Fact],
             resolve: Callable[[Mention], URIRef]) -> SyncReport
    def retract_doc(self, doc_id: str) -> int       # 제거한 청크 수
    def facts(self) -> list[dict]
    def export_json(self) -> dict
    def save(self) -> None

@dataclass
class SyncReport:
    added: list[str]; removed: list[str]; kept: list[str]   # chunk_id
    quarantined: list[tuple[Fact, str]]                      # (사실, 사유)
```

- `rdflib.Dataset` 사용. **청크 하나 = named graph 하나**(`ONTO[f"chunk:{chunk_id}"]`). 문서 수정·삭제 시 해당 그래프만 지우면 그 출처의 사실이 회수된다.
- 메타 그래프 `ONTO.meta` 에 기록: 청크 그래프 → `ONTO.fromDoc`, `ONTO.text`, `ONTO.heading`, `ONTO.tboxVersion`(tbox 의 `owl:versionInfo`), `ONTO.extractedAt`; 문서 → `ONTO.sha256`, `ONTO.title`, `ONTO.date`, `ONTO.source`, `ONTO.grade`.
- `sync`(멱등): `chunks` 는 문서의 현재 전체 청크, `facts` 는 그중 **새 청크**에서 뽑은 사실.
  - 저장된 청크 중 `chunks` 에 없는 것은 제거(`removed`), 이미 있는 것은 그대로(`kept`), 없던 것은 적재(`added`). 같은 입력으로 두 번 호출하면 두 번째는 전부 `kept`.
  - 사실 → 트리플: 주어·목적어 `Mention` 은 `resolve` 로 IRI 를 얻고 `rdf:type`(도메인 클래스)과 `rdfs:label` 을 같은 청크 그래프에 넣는다. 속성 IRI 는 `domain_ns(domain_dir)[predicate]`. 리터럴은 TBox 의 `rdfs:range` 가 `xsd:integer`·`xsd:date` 면 그 타입으로, 변환이 안 되면 격리.
  - 검증: 청크 그래프를 `shapes.ttl` 로 `pyshacl.validate`. 위반한 focus node 를 주어로 갖는 사실만 그래프에서 빼고 `quarantined` 에 사유와 함께 넣는다. TBox 에 없는 속성·클래스도 격리.
  - 단, `sh:maxCount` 위반은 청크 검증에서 격리하지 않는다(문서 간 충돌로 따로 다룬다 — 아래 `conflict`).
- `facts()`: `rdf:type`·`rdfs:label` 을 뺀 모든 사실을 (주어, 속성, 목적어)별로 묶어 반환.
  - 키: `s`, `p`, `o`(엔티티는 IRI 문자열, 리터럴은 값 문자열), `s_label`, `o_label`, `o_is_entity`, `docs`(doc_id 정렬 목록), `chunks`(chunk_id 목록), `confidence`, `conflict`.
  - `confidence = 1 - Π(1 - w)`, 서로 다른 `source` 마다 한 번(같은 source 의 사본은 한 번만), `w` 는 그 source 문서 중 가장 좋은 등급 기준 `{1: 0.9, 2: 0.7, 3: 0.5}`. 소수 둘째 자리 반올림.
  - `conflict`: `shapes.ttl` 에서 `sh:maxCount 1` 인 속성인데 같은 주어에 서로 다른 목적어가 둘 이상이면 그 사실들 모두 `True`.
- `export_json()` 형식(시각화 계약):

```json
{
  "nodes": [{"id": "<IRI>", "label": "한빛물산", "type": "Organization", "degree": 3}],
  "edges": [{"id": "e0", "source": "<IRI>", "target": "<IRI>", "predicate": "hasSupplier",
             "confidence": 0.97, "docs": ["c-001", "c-002"], "chunks": ["c-001#ab12cd34ef56"], "conflict": false}],
  "facts": [{"s": "한빛-2026-01", "p": "amount", "o": "100000000", "o_is_entity": false,
             "confidence": 0.9, "docs": ["c-001"], "chunks": ["c-001#ab12cd34ef56"], "conflict": true}],
  "docs": [{"id": "c-001", "title": "...", "date": "2026-01-10", "source": "법무팀", "grade": 1}],
  "evidence": {"c-001#ab12cd34ef56": {"doc": "c-001", "heading": "@frontmatter", "text": "..."}}
}
```

  - `edges` 는 목적어가 엔티티인 사실, `facts` 는 `alias` 를 포함한 전체 사실(`s`·`o` 는 라벨, `p` 는 로컬 이름). `nodes` 의 `type` 은 클래스 로컬 이름, `degree` 는 연결된 edge 수. `heading` 은 경로를 ` > ` 로 이은 문자열.
- 저장은 TriG 파일. 추론(역관계·대칭)과 유효 기간 관리는 범위 밖.

## 영역 D — 시각화 `viz/index.html`, `viz/sample.json`

- 의존성 없는 정적 페이지 한 장. Cytoscape.js 만 cdnjs 에서 버전 고정으로 불러온다. 빌드 도구·프레임워크 금지.
- 데이터: `fetch(new URLSearchParams(location.search).get("data") || "graph.json")`. 형식은 위 `export_json()` 계약. 계약대로 `viz/sample.json` 을 손으로 만든다(노드 8개 안팎, 충돌 1건, 출처 2개인 사실 1건 포함).
- 화면 두 개(탭 전환):
  1. **엔티티 탐색**: 검색창에서 엔티티를 고르면 그 노드를 중심으로 2홉 이웃만 `concentric` 배치(중심에서 홉 수가 거리). 노드 클릭 시 그 노드로 중심 이동. 노드 색은 `type`, 크기는 `degree`, 엣지 굵기·불투명도는 `confidence`, 화살표로 방향, `conflict` 는 빨간색. 엣지 라벨은 `predicate`. 신뢰도 하한 슬라이더로 엣지 필터.
  2. **사실 대조표**: 행은 `facts`, 열은 `docs`. 그 문서가 그 사실을 말하면 칸에 표시, `conflict` 행은 빨간 배경. 열 머리글에 문서 제목·출처·등급.
- 엣지나 표의 칸을 클릭하면 옆 패널에 `evidence` 의 근거 원문(문서 제목, heading, text)을 보여준다.
- 접근성: 탭·검색창·슬라이더는 키보드로 조작 가능하고 label 이 있어야 한다. 색만으로 충돌을 구분하지 않는다(텍스트 표시 병행). 밝은·어두운 화면 모두 읽히게 `prefers-color-scheme` 대응.
- 확인: `cd viz && python3 -m http.server 8765` 후 `http://localhost:8765/?data=sample.json`. `node --check` 로 스크립트 문법을 확인한다(인라인 스크립트를 임시 파일로 뽑아서). 타임라인·계층 배치 화면은 범위 밖.

## 통합 (메인 세션)

`src/onto/cli.py`(`onto ingest <도메인>`, `onto export <도메인>`), `tests/test_pipeline.py`. 투입 순서는 CSV 먼저, md 는 날짜순.

## 통합 검토에서 바뀐 것 (계약 대비)

- `Registry.learn` 은 이미 다른 엔티티의 별칭인 표기를 덮어쓰지 않고, 무시한 사유 목록을 반환한다.
- 수집은 `grade` 가 1~3 이 아니거나 `date` 가 ISO 날짜가 아닌 문서, YAML 오류, 열 수가 머리글보다 많은 CSV 행을 `ValueError` 로 거부한다.
- 저장소는 SHACL 전에 TBox 의 `rdfs:domain`·`rdfs:range` 로 주어·목적어 클래스를 검사한다(방향이 뒤집힌 추출 격리). 격리 단위는 focus node 전체가 아니라 위반한 사실 하나다.
- 문서 ID·청크 ID 는 IRI 에 넣을 때 percent-encoding 한다(파일 이름의 공백·괄호).
- 엔티티 표시 이름은 별칭으로 등록된 표기보다 대표 이름을 우선한다.
- 통합 단계는 LLM 이 낸 `alias` 사실을 버리고, 같은 문서 ID 를 가진 파일들은 모두 거부한다.

## 실행

```
.venv/bin/onto ingest domains/contracts          # data/ 에 저장소 생성·갱신
.venv/bin/onto export domains/contracts          # viz/graph.json 생성
cd viz && python3 -m http.server 8765            # http://localhost:8765/
.venv/bin/pytest -q
```

### 웹 화면에서 문서 올리기

```
.venv/bin/onto serve domains/contracts [--llm] [--port 8765]
```

`viz/` 화면을 보여 주고, 사이드바의 "문서 추가"로 올린 `.md`·등록된 `.csv` 를 `docs/` 에 저장한 뒤 바로 투입한다(`src/onto/server.py`).
`graph.json` 은 파일이 아니라 저장소에서 매번 만들어 돌려주므로 export 를 따로 실행하지 않는다.

- 저장 전에 검사한다: 파일 이름(경로 구분자·숨김 파일 거부), 형식, 크기(2MB), 수집기 파싱(필수 메타), 다른 파일과의 문서 ID 중복. 걸리면 아무것도 저장하지 않는다.
- 같은 이름이 있으면 409 를 돌려주고, 화면에서 "바꿔 넣기"를 눌러야 덮어쓴다(`?replace=1`).
- 삭제(`DELETE /api/docs/<문서 ID>`): 문서를 고르면 근거 패널에 "이 문서 삭제"가 나오고 한 번 더 확인한다. 파일은 지우지 않고 `data/trash/<시각>-<이름>` 으로 옮기며, 그 문서의 사실만 저장소에서 회수한다. 되돌리려면 그 파일을 다시 올린다.
- `127.0.0.1` 에만 바인딩하고 `Host` 가 localhost 가 아닌 요청은 거부한다. 업로드는 PUT 이라 다른 사이트의 페이지가 보낼 수 없다.
- `--llm` 없이 띄우면 머리말만 적용되고 본문 청크는 저장하지 않는다(나중에 `--llm` 으로 띄우면 그때 추출된다).

## LLM 연동 (본문 추출)

`onto ingest <도메인> --llm` 일 때만 동작한다. 기본은 꺼져 있고, 켜면 **문서 본문이 Codex CLI 를 거쳐 OpenAI 로 전송된다**.
준비: Codex CLI 설치 후 `codex login`. API 키 입력은 필요 없다(Codex 의 로그인을 그대로 쓴다).

단계 (청크 하나 기준):
1. 수집이 문서를 청크로 나눈다. 머리말(frontmatter)·CSV 행은 규칙으로 추출하고 LLM 에 보내지 않는다.
2. 저장소에 없는 **새 본문 청크만** 대상이 된다. 바뀌지 않은 청크는 다시 보내지 않는다.
3. `extract.llm_request` 가 요청을 만든다.
   - system(도메인마다 고정): 추출 규칙 + TBox 요약(클래스, 속성별 정의역 → 치역, 한국어 라벨). `alias` 는 뺀다.
   - user(청크마다): 문서 제목·유형·작성일, 제목 경로, 같은 문서 머리말에서 확정된 사실, 청크 본문.
   - 출력 JSON 스키마: 속성·클래스를 TBox 의 값으로만 고를 수 있게 enum 으로 제한, 사실마다 근거 구절 `quote`.
   - 보내지 않는 것: 파일 경로, 출처 부서·등급, 다른 문서, 기존 그래프, 별칭 등록부.
4. `llm.codex_complete` 가 `codex exec` 를 실행한다(빈 폴더·읽기 전용 샌드박스, `--output-schema` 로 형식 강제, 본문은 stdin, 모델은 Codex 설정의 기본값).
5. 응답 검증: 스키마 밖의 값, 빈 값, 근거 구절이 본문에 없는 사실은 버린다.
6. 이후는 규칙 추출과 같은 경로: 엔티티 해소 → 정의역·치역 검사 → SHACL → 격리 또는 적재.

실패 처리: 호출·형식 오류가 난 청크는 저장하지 않아 다음 실행에서 다시 시도한다. 인증 오류처럼 모든 호출이 실패할
오류면 남은 호출을 건너뛴다. 어느 경우든 규칙 추출분은 적재되고 종료 코드는 1 이다.
