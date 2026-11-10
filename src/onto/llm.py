"""LLM 호출. Codex CLI(`codex exec`)를 하위 프로세스로 실행한다. 인증은 `codex login` 의 로그인을 그대로 쓴다."""

import json
import re
import subprocess
import tempfile
from pathlib import Path

from .extract import ExtractionError

TIMEOUT = 300  # 초. 청크 하나의 추출이 이보다 길면 멈춘 것으로 본다
AUTH = re.compile(r"not logged in|log ?in|unauthorized|401|403|authentication", re.I)


def codex_complete(system: str, user: str, schema: dict) -> str:
    """추출 요청 한 건을 Codex 에 보내고 JSON 문자열을 돌려준다. 실패는 ExtractionError 로 바꾼다."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "schema.json").write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
        work = tmp / "work"
        work.mkdir()
        cmd = [
            "codex", "exec",
            # Codex 는 셸 명령을 실행할 수 있는 에이전트다. 문서 본문에 지시문이 섞여 있어도
            # 할 수 있는 일이 없도록 빈 폴더에서, 읽기 전용으로 실행한다.
            "--cd", str(work), "--sandbox", "read-only", "--skip-git-repo-check",
            "--ephemeral",  # 추출 요청을 Codex 대화 기록에 남기지 않는다
            "--output-schema", str(tmp / "schema.json"),
            "--output-last-message", str(tmp / "out.json"),
            "--color", "never",
        ]
        try:
            # 프롬프트는 인자가 아니라 stdin 으로 넘긴다(본문이 프로세스 목록에 노출되지 않는다)
            proc = subprocess.run(cmd, input=f"{system}\n\n{user}", capture_output=True, text=True, timeout=TIMEOUT)
        except FileNotFoundError as e:
            raise ExtractionError("codex 명령을 찾을 수 없다. Codex CLI 를 설치하고 `codex login` 을 실행한다", fatal=True) from e
        except subprocess.TimeoutExpired as e:
            raise ExtractionError(f"Codex 가 {TIMEOUT}초 안에 끝나지 않았다") from e
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout).strip()[-400:]
            # 로그인 문제는 다른 청크도 똑같이 실패한다
            raise ExtractionError(f"codex exec 실패(rc={proc.returncode}): {tail}", fatal=bool(AUTH.search(tail)))
        out = tmp / "out.json"
        if not out.exists() or not out.read_text(encoding="utf-8").strip():
            raise ExtractionError("Codex 가 최종 응답을 남기지 않았다")
        return out.read_text(encoding="utf-8")
