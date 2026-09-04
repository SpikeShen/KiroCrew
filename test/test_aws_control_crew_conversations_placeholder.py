"""``crew/conversations.py`` is a deliberate placeholder, and that has to stay true.

The S3 conversation reader for a persistent-memory crew is intentionally not written. The
reason is in the module's own docstring: `--memory-mode chatbot` is the DEFAULT, nothing is
persisted in it (the driver's gate 7 asserts `state=disabled` and the task role is granted
no S3 action at all), so a reader would answer empty for every default deployment. An empty
list is indistinguishable from a crew nobody has asked a question yet, and neither is
distinguishable from a reader that is broken -- the exact shape that cost this project once
already, when the old UI read a `fetch-status.json` that nothing wrote.

So the module stays importable and declares NOTHING, which makes a premature `from ...
conversations import read_conversations` fail at the NAME, loudly, at the call site that
wanted a reader.

This suite exists because that invariant had no test, which the coverage gate reported as
`0.0% ... (0/1)`: the module's single executable statement is its `__future__` import, and
nothing imported the module. The honest fix for an uncovered placeholder is a test that
pins what it promises, not a baseline entry.
"""

from __future__ import annotations

import importlib
import sys

MODULE = "kiro_crew.apps.builtins.aws_control.crew.conversations"


def _fresh_import():
    """Import the module with its body ACTUALLY executed, not served from the cache.

    `importlib.import_module` returns the cached object when something earlier in the run
    already imported the module, and then the file's one statement never runs -- which is
    the whole point of covering it. Measured: with a plain `import_module` a trace hook saw
    zero lines execute in the target, while a fresh import traces line 46. Dropping the
    entry first makes the execution unconditional.
    """
    sys.modules.pop(MODULE, None)
    return importlib.import_module(MODULE)


def test_the_placeholder_is_importable():
    """Importable on purpose: the failure must land on the NAME, not on the import."""
    assert _fresh_import() is not None


def test_the_import_really_re_executes_the_module():
    """The pop is load-bearing, so it gets its own assertion rather than a comment.

    This suite exists to cover one statement. A cached import satisfies every other
    assertion here while executing nothing, so removing the `sys.modules.pop` would leave
    the file at 0% with the tests still green -- the exact shape of a test that passes for
    the wrong reason.

    Object identity is the instrument, chosen after two others gave no answer: local
    `coverage --include` reported no data at all, and a `sys.settrace` hook sees nothing
    because pytest clears the trace function. A new module object can only come from the
    body running again.
    """
    first = _fresh_import()
    assert importlib.import_module(MODULE) is first, "a plain import should cache"
    assert _fresh_import() is not first, (
        "popping MODULE from sys.modules no longer forces a re-import, so this suite "
        "would stop executing the statement it exists to cover"
    )


def test_it_declares_no_reader():
    """A premature reader import must fail, so no public callable may appear here.

    `annotations` is the `__future__` import itself and is the one permitted name. Anything
    else means someone started the port in place, and the module's own docstring says the
    port belongs elsewhere -- read `test_transcript_key_agrees_with_sidecar.py` first,
    because the front process and the sidecar already agree on a key and a reader is the
    third party to that agreement.
    """
    mod = _fresh_import()
    public = {n for n in dir(mod) if not n.startswith("_")} - {"annotations"}
    assert not public, (
        f"{MODULE} has grown public names {sorted(public)}. It is a placeholder: a reader "
        f"here answers empty for every default (chatbot) deployment, which reads exactly "
        f"like a crew with no history and like a broken reader."
    )


def test_it_still_explains_why_it_is_empty():
    """An undocumented empty module is indistinguishable from an unfinished one.

    Asserted on the load-bearing claims rather than on length, so reformatting is free but
    deleting the reasoning is not.
    """
    doc = _fresh_import().__doc__ or ""
    for claim in ("NOT PORTED YET", "chatbot", "resolve_open_slots", "layout.py"):
        assert claim in doc, f"the docstring no longer explains {claim!r}"
