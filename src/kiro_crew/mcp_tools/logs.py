"""The kiro-cli log reader tool: what it advertises and what it does.

``schemas()`` returns the ADVERTISEMENT half; ``HANDLERS`` maps the name to the
function that runs it. Both halves live here so the contract and behavior are
read together, and ``test_mcp_tool_registry`` fails if one arrives without the
other (see ``skills.py`` — this module follows the same template).

WHY THIS TOOL EXISTS: kiro-cli's identity store is fenced (it also holds SSO
tokens), and the command gate is a deliberately coarse, verb-independent path
matcher — so the agent driving kiro-cli has no sanctioned way to read kiro-cli's
own logs when the backend rejects a turn (issue #6023, Class 2). This is that
sanctioned channel: ONE reviewed code path that reads only log files and runs
every byte through the shared redaction stack (``redact_credentials`` /
``redact_exfiltration_urls`` plus the diagnostics collector's ``_EXTRA_REDACTIONS``
for the bearer/Authorization/mc_token shapes those two miss) before returning it.
It does NOT widen the gate and does NOT take a path carve-out on the fence:
redaction is what makes returning the bytes safe.

Handlers reach shared plumbing as attributes of ``mcp_core`` (``mcp_core.sel``,
``mcp_core._resolve_session_key``) so a test that rebinds one still intercepts —
an attribute lookup resolves at CALL time.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kiro_crew import diagnostics, mcp_core
from kiro_crew.validation import KIRO_CLI_LOGS_SCHEMA, validate_tool_args


def schemas() -> list[dict[str, Any]]:
    """Descriptor for the kiro-cli log reader."""
    return [
        {
            "name": "kiro_cli_logs",
            "description": (
                "Read a REDACTED tail of kiro-cli's OWN logs — the runtime you "
                "are driving — to diagnose a rejected or failed turn. Reads only "
                "the chat/mcp/lsp log files, never the fenced identity/token "
                "store and not session transcripts, and runs "
                "the output through the same credential + exfiltration-URL "
                "scrubbers the diagnostics bundle uses, so live tokens, "
                "Authorization headers and auth cookies are stripped before you "
                "see them. Use it when the backend rejected a turn, an ACP "
                "request errored, or you need first-hand evidence of what "
                "kiro-cli did. `tail` bounds the number of lines (default 200); "
                "`since` keeps lines at/after a leading-timestamp prefix like "
                '"2026-09-06T16:". Output is byte-capped per source regardless '
                "of `tail`. Returns the log text with one section per source, or "
                "a note when no logs exist."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "tail": {
                        "type": "integer",
                        "description": (
                            "Max number of lines to return per source (default "
                            "200). The output is byte-capped independently, so a "
                            "large value cannot enlarge the response past the cap."
                        ),
                    },
                    "since": {
                        "type": "string",
                        "description": (
                            "Keep only lines at or after this leading-timestamp "
                            "prefix (lexical match against the start of each log "
                            'line), e.g. "2026-09-06" or "2026-09-06T16:". '
                            "Continuation lines of a kept event ride along."
                        ),
                    },
                },
            },
        },
    ]


def kiro_cli_logs(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, KIRO_CLI_LOGS_SCHEMA)
    tail = args.get("tail")
    if tail is not None:
        try:
            tail = int(tail)
        except (TypeError, ValueError):
            tail = None
    since = str(args.get("since", "") or "").strip() or None

    try:
        # diagnostics.read_kiro_cli_logs reads only log files and scrubs every
        # byte through the shared redaction stack before returning.
        out = diagnostics.read_kiro_cli_logs(tail=tail, since=since)
    except Exception as exc:  # pragma: no cover — defensive
        mcp_core.sel().log_tool_invocation(
            session_key=mcp_core._resolve_session_key(),
            source="mcp",
            tool_name="kiro_cli_logs",
            tool_kind="read",
            outcome="error",
            metadata={"error": type(exc).__name__},
        )
        # "Error:" prefix is load-bearing: the shared call_tool_with_logging
        # wrapper classifies a result by result.startswith("Error:").
        return f"Error: kiro_cli_logs failed: {type(exc).__name__}: {exc}"

    mcp_core.sel().log_tool_invocation(
        session_key=mcp_core._resolve_session_key(),
        source="mcp",
        tool_name="kiro_cli_logs",
        tool_kind="read",
        outcome="success",
        metadata={"tail": tail, "since_set": since is not None},
    )
    return out


HANDLERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "kiro_cli_logs": kiro_cli_logs,
}
