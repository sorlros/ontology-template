"""Codex 호출 모듈. 실제 Codex 는 부르지 않고 subprocess.run 을 가짜로 바꿔 명령 형태와 오류 분류를 본다."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from onto import llm
from onto.extract import ExtractionError

SCHEMA = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}


def fake(monkeypatch, reply='{"facts": []}', returncode=0, stderr="", error=None):
    calls = []

    def run(cmd, **kwargs):
        if error:
            raise error
        # 호출 시점에만 있는 임시 파일 내용을 함께 기록한다
        arg = lambda flag: cmd[cmd.index(flag) + 1]
        calls.append({"cmd": cmd, **kwargs, "schema": json.loads(Path(arg("--output-schema")).read_text(encoding="utf-8")),
                      "cwd_files": list(Path(arg("--cd")).iterdir())})
        if reply is not None:
            Path(arg("--output-last-message")).write_text(reply, encoding="utf-8")
        return SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)

    monkeypatch.setattr(llm.subprocess, "run", run)
    return calls


def test_명령_형태(monkeypatch):
    calls = fake(monkeypatch)
    assert llm.codex_complete("시스템 규칙", "문서 본문", SCHEMA) == '{"facts": []}'
    (c,) = calls
    cmd = c["cmd"]
    assert cmd[:2] == ["codex", "exec"]
    assert cmd[cmd.index("--sandbox") + 1] == "read-only" and c["cwd_files"] == []  # 빈 폴더, 읽기 전용
    assert "--ephemeral" in cmd and "--skip-git-repo-check" in cmd
    assert not any("dangerously" in a for a in cmd)
    assert c["schema"] == SCHEMA
    # 본문은 stdin 으로만 간다
    assert c["input"] == "시스템 규칙\n\n문서 본문" and not any("문서 본문" in a for a in cmd)
    assert c["timeout"] == llm.TIMEOUT


@pytest.mark.parametrize("kwargs, fatal", [
    ({"error": FileNotFoundError("codex")}, True),
    ({"error": subprocess.TimeoutExpired("codex", 300)}, False),
    ({"returncode": 1, "stderr": "Error: Not logged in. Run `codex login`."}, True),
    ({"returncode": 1, "stderr": "stream disconnected before completion"}, False),
    ({"reply": None}, False),   # 응답 파일을 남기지 않음
    ({"reply": "  "}, False),
])
def test_오류_분류(monkeypatch, kwargs, fatal):
    fake(monkeypatch, **kwargs)
    with pytest.raises(ExtractionError) as e:
        llm.codex_complete("s", "u", SCHEMA)
    assert e.value.fatal is fatal
