"""A file EDIT's tool_input is a document, and its gate is the target path (#8812).

``_dispatch.derive_edit_diff`` renders an edit's new content as a unified diff and
that text is what ``event.tool_input`` carries. Feeding it to the shell-command
scan refused writing prose that names ``~/.ssh``, a Markdown page that says
``git push origin main``, a docstring that says ``kirocrew restart`` -- and any
body over the command-line size cap, for its length. These tests pin the
replacement: an edit is judged by where it writes (``is_sensitive_write_path`` over
every accepted path spelling), the title tier still runs, and a non-edit tool
keeps the document scan byte for byte.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

import kiro_crew.sel as sel_mod
from kiro_crew import llm_helpers
from kiro_crew.llm_helpers import ToolApprovalPolicy, _resolve_permission
from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent

_PROSE = (
    "# Sandbox notes\n\n"
    "The sandbox masks ~/.ssh and ~/.aws for the child process.\n"
    "Run git push origin main only after review.\n"
    "kirocrew restart is blocked by kiro-cli's filter.\n"
)


class _RecordingProvider:
    def __init__(self) -> None:
        self.approved: list[str] = []
        self.rejected: list[str] = []

    async def approve_tool(self, request_id: str) -> None:
        self.approved.append(request_id)

    async def reject_tool(self, request_id: str) -> None:
        self.rejected.append(request_id)


def _diff_for(path: str, body: str) -> str:
    lines = "\n".join(f"+{line}" for line in body.rstrip("\n").split("\n"))
    return f"--- /dev/null\n+++ {path}\n@@ -0,0 +1 @@\n{lines}\n"


def _edit_event(path: str, body: str = _PROSE, *, kind: str = "edit", params=...) -> LLMEvent:
    raw = {"command": "create", "path": path, "fileText": body} if params is ... else params
    return LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title="Editing notes.md",
        request_id="r1",
        tool_kind=kind,
        tool_input=_diff_for(path, body),
        raw_tool_params=raw,
    )


async def _resolve(event: LLMEvent) -> tuple[bool, _RecordingProvider, list[dict]]:
    provider = _RecordingProvider()
    rows: list[dict] = []
    sel_stub = MagicMock()
    sel_stub.log_tool_invocation.side_effect = lambda **kw: rows.append(kw)
    with patch.object(sel_mod, "sel", lambda: sel_stub):
        approved = await _resolve_permission(
            provider,  # type: ignore[arg-type]
            event,
            ToolApprovalPolicy.AUTO_APPROVE,
            None,
        )
    return approved, provider, rows


def _error(rows: list[dict]) -> str:
    assert len(rows) == 1, rows
    return str(rows[0].get("error") or "")


class TestEditContentIsNotACommandLine:
    @pytest.mark.asyncio
    async def test_prose_naming_fenced_paths_and_denied_commands_is_writable(self) -> None:
        # The document scan refused every one of these three sentences on its
        # own; the edit gate judges the target instead and this path is benign.
        approved, provider, rows = await _resolve(_edit_event("/tmp/proj/docs/notes.md"))
        assert approved is True, _error(rows)
        assert provider.approved == ["r1"]

    @pytest.mark.asyncio
    async def test_document_scan_is_never_run_on_an_edit(self) -> None:
        with patch.object(
            llm_helpers, "_first_tool_input_denial", side_effect=AssertionError("scanned")
        ):
            approved, _provider, _rows = await _resolve(_edit_event("/tmp/proj/a.py"))
        assert approved is True

    @pytest.mark.asyncio
    async def test_a_body_over_the_command_cap_is_not_refused_for_its_length(self) -> None:
        body = "x = 1\n" * (llm_helpers._MAX_SCANNABLE_TOOL_INPUT_CHARS // 4)
        assert len(body) > llm_helpers._MAX_SCANNABLE_TOOL_INPUT_CHARS
        approved, _provider, rows = await _resolve(_edit_event("/tmp/proj/big.py", body))
        assert approved is True, _error(rows)

    @pytest.mark.asyncio
    async def test_a_python_source_that_opens_a_credential_is_still_writable(self) -> None:
        # The document is not the fence; the sandbox and the cron vetter are.
        # An agent may WRITE this file; running it is a different gate.
        body = 'open(os.path.expanduser("~/.aws/credentials")).read()\n'
        approved, _provider, rows = await _resolve(_edit_event("/tmp/proj/x.py", body))
        assert approved is True, _error(rows)


class TestEditTargetIsTheGate:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path",
        [
            "~/.ssh/authorized_keys",
            "~/.aws/credentials",
            "~/.kiro/crew/security_policy.json",
            "~/.kiro/crew/config.json",  # write-only tier
            "~/.kiro/agents/pwn.json",  # write-only tier
        ],
    )
    async def test_write_to_a_protected_path_is_denied(self, path: str) -> None:
        approved, provider, rows = await _resolve(_edit_event(path, "harmless\n"))
        assert approved is False
        assert provider.rejected == ["r1"]
        assert _error(rows).startswith("Blocked: write to protected path: ")
        assert rows[0]["metadata"]["mechanism"] == "always_deny_input"

    @pytest.mark.asyncio
    async def test_every_path_spelling_is_judged(self) -> None:
        # filePath alias, nested under a batch key -- target_paths walks them all.
        params = {"operations": [{"filePath": "~/.ssh/id_rsa", "text": "x"}]}
        approved, _p, rows = await _resolve(_edit_event("/tmp/ok", params=params))
        assert approved is False
        assert "id_rsa" in _error(rows)

    @pytest.mark.asyncio
    async def test_truncated_walk_is_denied_as_unverifiable(self) -> None:
        params = {"path": "/tmp/ok", "deep": [{"path": f"/tmp/f{i}"} for i in range(300)]}
        approved, _p, rows = await _resolve(_edit_event("/tmp/ok", params=params))
        assert approved is False
        assert "too large to verify" in _error(rows)

    @pytest.mark.asyncio
    async def test_title_tier_still_runs_first(self) -> None:
        ev = _edit_event("/tmp/proj/a.md")
        ev.title = "cat ~/.aws/credentials"
        approved, _p, rows = await _resolve(ev)
        assert approved is False
        assert rows[0]["metadata"]["mechanism"] == "always_deny"


class TestOnlyEditKindWithParamsIsRerouted:
    @pytest.mark.asyncio
    async def test_non_edit_kind_keeps_the_document_scan(self) -> None:
        ev = _edit_event("/tmp/proj/a.md", kind="read")
        approved, _p, rows = await _resolve(ev)
        assert approved is False
        assert "sensitive credential path" in _error(rows)

    @pytest.mark.asyncio
    async def test_edit_without_params_falls_back_to_the_document_scan(self) -> None:
        # No target to judge -> fail closed on the document, never approve blind.
        ev = _edit_event("/tmp/proj/a.md", params=None)
        approved, _p, rows = await _resolve(ev)
        assert approved is False
        assert "sensitive credential path" in _error(rows)

    @pytest.mark.asyncio
    async def test_bash_tool_input_is_unchanged(self) -> None:
        ev = LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            title="Bash",
            request_id="r1",
            tool_kind="execute",
            tool_input=json.dumps({"command": "cat ~/.aws/credentials"}),
            raw_tool_params={"command": "cat ~/.aws/credentials"},
        )
        approved, _p, rows = await _resolve(ev)
        assert approved is False
        assert "sensitive credential path" in _error(rows)
