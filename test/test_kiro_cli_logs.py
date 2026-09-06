"""Tests for the kiro_cli_logs reader (diagnostics.read_kiro_cli_logs) + MCP tool.

The security-critical property is REDACTION: every live-secret shape kiro-cli
writes into its logs — a bearer token, a mid-line ``Authorization: Basic <b64>``
header, an ``mc_token`` auth cookie, and the serialized ``"authorization": ...``
JSON form — must be gone from the returned text. The mutation guard
(``test_redaction_is_load_bearing``) proves the test is not vacuous: with the
extra-redaction stack emptied the very same secret leaks, so a green assertion
means the redaction did the work, not that the secret was never present.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew import diagnostics
from kiro_crew.mcp_tools import logs as logs_tool

# All four secret shapes the issue names, in one log body.
_LOG = (
    "2026-09-06T16:00:01 boot ok\n"
    "2026-09-06T16:00:02 Authorization: Bearer sk-ant-SECRETtoken1234567890abcXYZ\n"
    "2026-09-06T16:00:03 Set-Cookie: mc_token_5476=supersecretcookievalueABCDEF123456\n"
    "2026-09-06T16:00:04 ERROR request used Authorization: Basic TWlkTGluZUxFQUsxMjNhYmM=\n"
    '2026-09-06T16:00:05 {"headers": {"authorization": "Basic UVVPVEVEbGVha0FCQzEyMw=="}}\n'
    "2026-09-06T16:00:06 a perfectly normal log line\n"
)

_SECRETS = (
    "sk-ant-SECRETtoken1234567890abcXYZ",
    "supersecretcookievalueABCDEF123456",
    "TWlkTGluZUxFQUsxMjNhYmM=",
    "UVVPVEVEbGVha0FCQzEyMw==",
)


def _isolate(monkeypatch, chat: Path | None) -> None:
    """Point the reader at a single chat log and stub the other sources off."""
    monkeypatch.setattr(diagnostics, "_kiro_cli_chat_log", lambda: chat)
    monkeypatch.setattr(diagnostics, "_kiro_cli_extra_logs", lambda: [])


def test_reads_and_redacts_every_secret_shape(tmp_path, monkeypatch):
    chat = tmp_path / "kiro-chat.log"
    chat.write_text(_LOG)
    _isolate(monkeypatch, chat)

    out = diagnostics.read_kiro_cli_logs()

    for secret in _SECRETS:
        assert secret not in out, f"secret leaked from kiro_cli_logs: {secret!r}"
    assert "a perfectly normal log line" in out
    assert "[REDACTED]" in out
    assert "kiro-chat.log" in out


def test_redaction_is_load_bearing(tmp_path, monkeypatch):
    """Mutation guard: disable the extra-redaction stack and the secrets leak.

    A redaction test that still passes with redaction disabled proves nothing.
    Here we empty ``_EXTRA_REDACTIONS`` (the stack that covers the bearer /
    Authorization / mc_token shapes) and assert the SAME secrets now appear —
    so the green assertion in ``test_reads_and_redacts_every_secret_shape`` is
    attributable to that stack, not to the secrets never being there.
    """
    chat = tmp_path / "kiro-chat.log"
    chat.write_text(_LOG)
    _isolate(monkeypatch, chat)
    monkeypatch.setattr(diagnostics, "_EXTRA_REDACTIONS", ())

    out = diagnostics.read_kiro_cli_logs()

    # With the extra stack gone, the header/bearer/cookie shapes leak verbatim
    # (redact_credentials / redact_exfiltration_urls do not cover them — that is
    # exactly why _EXTRA_REDACTIONS exists).
    leaked = [s for s in _SECRETS if s in out]
    assert leaked, "no secret leaked with _EXTRA_REDACTIONS disabled — the test is vacuous"


def test_byte_cap_bounds_output_regardless_of_tail(tmp_path, monkeypatch):
    chat = tmp_path / "kiro-chat.log"
    # Write a large log the honest way.
    big = "".join(f"2026-09-06T16:00:{i:02d} {'y' * 80}\n" for i in range(1000))
    chat.write_text(big)
    _isolate(monkeypatch, chat)

    # A huge tail cannot enlarge the output past the byte cap.
    out = diagnostics.read_kiro_cli_logs(tail=100000, max_bytes=4096)
    # Output = header + section framing + <=4096 bytes of log; give generous slack.
    assert len(out) < 4096 + 500
    assert "truncated" in out  # the tail marker _read_log_tail prepends


def test_tail_limits_line_count(tmp_path, monkeypatch):
    chat = tmp_path / "kiro-chat.log"
    chat.write_text("".join(f"2026-09-06T16:00:00 line{i}\n" for i in range(50)))
    _isolate(monkeypatch, chat)

    out = diagnostics.read_kiro_cli_logs(tail=3)

    assert "line49" in out
    assert "line47" in out
    assert "line46" not in out  # only the last 3 survive


def test_since_filters_by_leading_timestamp(tmp_path, monkeypatch):
    chat = tmp_path / "kiro-chat.log"
    chat.write_text(
        "2026-09-06T15:00:00 early line\n"
        "2026-09-06T16:00:00 kept line\n"
        "  continuation of kept event\n"
        "2026-09-06T17:00:00 later line\n"
    )
    _isolate(monkeypatch, chat)

    out = diagnostics.read_kiro_cli_logs(since="2026-09-06T16:")

    assert "early line" not in out
    assert "kept line" in out
    assert "continuation of kept event" in out  # non-timestamp line rides along
    assert "later line" in out


def test_no_logs_returns_a_note(tmp_path, monkeypatch):
    _isolate(monkeypatch, None)
    out = diagnostics.read_kiro_cli_logs()
    assert "No kiro-cli logs found" in out


def test_sensitive_path_source_is_refused(tmp_path, monkeypatch):
    """Defense in depth: a source that resolves to a fenced/sensitive path is skipped."""
    chat = tmp_path / "kiro-chat.log"
    chat.write_text(_LOG)
    _isolate(monkeypatch, chat)
    # Force every path to read as sensitive; the reader must return zero sources.
    monkeypatch.setattr(diagnostics, "is_sensitive_path", lambda p: True)

    out = diagnostics.read_kiro_cli_logs()
    assert "No kiro-cli logs found" in out


@requires_symlinks
def test_symlinked_chat_log_is_not_followed(tmp_path, monkeypatch):
    off_tree = tmp_path / "off_tree_secret.txt"
    off_tree.write_text("TOPSECRETsymlinkVALUE123\n")
    link = tmp_path / "kiro-chat.log"
    link.symlink_to(off_tree)
    _isolate(monkeypatch, link)

    out = diagnostics.read_kiro_cli_logs()
    assert "TOPSECRETsymlinkVALUE123" not in out
    assert "No kiro-cli logs found" in out


@requires_symlinks
@pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW"),
    reason=(
        "O_NOFOLLOW is a POSIX flag absent on Windows; the kernel-enforced "
        "no-follow open this test exercises cannot be applied there. On Windows "
        "the symlink defense is the caller's pre-open is_symlink check (covered "
        "by test_symlinked_chat_log_is_not_followed, which runs on both), so "
        "this platform-specific strengthening is gated rather than asserted "
        "cross-platform."
    ),
)
def test_toctou_symlink_swap_is_refused_by_nofollow(tmp_path, monkeypatch):
    """A path swapped to a symlink AFTER the checks must not be followed (POSIX).

    The source dirs include the world-writable /tmp/kiro-log, so a local writer
    could replace a validated regular log with a symlink to ~/.ssh/config
    between the is_sensitive_path check and the open. _read_log_tail opens with
    O_NOFOLLOW and tails that same descriptor, so the swapped-in link is refused
    (ELOOP) rather than read. Simulated by making the source path a symlink and
    pushing the pre-open checks past it: only the O_NOFOLLOW open stands between
    the reader and the target.
    """
    target = tmp_path / "secret_target"
    target.write_text("TOCTOUsymlinkTARGETleak5555\n")
    link = tmp_path / "kiro-chat.log"
    link.symlink_to(target)

    # The reader is what we exercise; force the pre-open guards to pass so the
    # O_NOFOLLOW open is the ONLY thing that can stop the swapped-in link.
    monkeypatch.setattr(diagnostics, "_kiro_cli_chat_log", lambda: link)
    monkeypatch.setattr(diagnostics, "_kiro_cli_extra_logs", lambda: [])
    monkeypatch.setattr(diagnostics.Path, "is_symlink", lambda self: False)
    monkeypatch.setattr(diagnostics, "is_sensitive_path", lambda p: False)

    out = diagnostics.read_kiro_cli_logs()
    assert "TOCTOUsymlinkTARGETleak5555" not in out
    assert "No kiro-cli logs found" in out


def test_read_log_tail_reads_a_regular_file(tmp_path):
    """_read_log_tail returns a regular file's bytes on every platform."""
    regular = tmp_path / "plain.log"
    regular.write_text("regular content\n")
    assert diagnostics._read_log_tail(regular, 4096) == "regular content\n"


def test_reader_works_without_o_nofollow(tmp_path, monkeypatch):
    """The tool still reads when O_NOFOLLOW is unavailable (the Windows path).

    Regression guard: an earlier version returned None (read nothing) whenever
    O_NOFOLLOW was absent, which silently disabled the whole tool on Windows.
    The no-follow open is a POSIX strengthening, not a precondition for reading.
    Exercised on any platform by deleting the attribute so `getattr(os,
    "O_NOFOLLOW", 0)` degrades to 0 (a plain open), matching Windows.
    """
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    chat = tmp_path / "kiro-chat.log"
    chat.write_text("2026-09-06T16:00:00 windows path reads fine\n")
    _isolate(monkeypatch, chat)

    out = diagnostics.read_kiro_cli_logs()
    assert "windows path reads fine" in out
    assert "No kiro-cli logs found" not in out


@requires_symlinks
@pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW"),
    reason=(
        "O_NOFOLLOW is POSIX-only; on Windows the open would follow the link "
        "and _read_log_tail would return the target's bytes, so the None result "
        "this asserts is a POSIX guarantee. The Windows symlink defense is the "
        "caller's pre-open is_symlink check, exercised by "
        "test_symlinked_chat_log_is_not_followed."
    ),
)
def test_read_log_tail_refuses_a_symlink_on_posix(tmp_path):
    """_read_log_tail: None for a symlinked final component (O_NOFOLLOW ELOOP)."""
    target = tmp_path / "target.log"
    target.write_text("SECRETviaSymlink\n")
    link = tmp_path / "link.log"
    link.symlink_to(target)
    assert diagnostics._read_log_tail(link, 4096) is None


def test_session_transcripts_are_not_read(tmp_path, monkeypatch):
    """Cross-session conversation content is deliberately out of scope.

    Session transcripts (`sessions/cli/<sid>.jsonl`) are shared across every
    gateway session, including incognito/temporary ones, and `_scrub` is a
    credential pass that does not narrow conversation prose — so returning the
    globally-newest ones would disclose another session's private conversation.
    The tool stays scoped to the chat/mcp/lsp LOGS: with no kiro-log present it
    reports none found rather than falling back to transcripts.
    """
    # A populated sessions dir must NOT be a source.
    sessions = tmp_path / "sessions" / "cli"
    sessions.mkdir(parents=True)
    (sessions / "abc.jsonl").write_text("2026-09-06T16:00:00 private conversation\n")
    monkeypatch.setattr(diagnostics, "_kiro_cli_chat_log", lambda: None)
    monkeypatch.setattr(diagnostics, "_kiro_cli_extra_logs", lambda: [])

    # There is no session-log reader to stub — the feature was removed.
    assert not hasattr(diagnostics, "_kiro_cli_session_logs")
    out = diagnostics.read_kiro_cli_logs()
    assert "private conversation" not in out
    assert "No kiro-cli logs found" in out


# ── MCP tool wrapper ─────────────────────────────────────────────────────────


def _stub_sel(monkeypatch):
    from unittest.mock import MagicMock

    sel = MagicMock()
    monkeypatch.setattr("kiro_crew.mcp_core.sel", lambda: sel)
    monkeypatch.setattr("kiro_crew.mcp_core._resolve_session_key", lambda: "sk")
    return sel


def test_tool_returns_reader_output_and_audits(tmp_path, monkeypatch):
    sel = _stub_sel(monkeypatch)
    monkeypatch.setattr(
        logs_tool.diagnostics, "read_kiro_cli_logs", lambda **kw: "REDACTED LOGS OK"
    )

    result = logs_tool.kiro_cli_logs("kiro_cli_logs", {"tail": 10})

    assert result == "REDACTED LOGS OK"
    assert sel.log_tool_invocation.call_args.kwargs["outcome"] == "success"
    assert sel.log_tool_invocation.call_args.kwargs["tool_kind"] == "read"


def test_tool_error_is_prefixed_and_audited(monkeypatch):
    sel = _stub_sel(monkeypatch)

    def _boom(**kw):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(logs_tool.diagnostics, "read_kiro_cli_logs", _boom)

    result = logs_tool.kiro_cli_logs("kiro_cli_logs", {})

    assert result.startswith("Error:")
    assert sel.log_tool_invocation.call_args.kwargs["outcome"] == "error"


def test_tool_rejects_out_of_range_tail(monkeypatch):
    _stub_sel(monkeypatch)
    # Schema bound is 1..100000; a validation error surfaces as a string, not a raise.
    from kiro_crew.validation import ValidationError

    with pytest.raises(ValidationError):
        logs_tool.kiro_cli_logs("kiro_cli_logs", {"tail": 0})
