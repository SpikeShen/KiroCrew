"""The hardening that rides with the md-notebook backend's mask carve-out (#8762).

The carve-out ITSELF lives on main and is not retested here: ``_APP_BACKEND_OWNED_LEAVES``
names the hidden leaves the Notes backend owns, ``app_backend_visible_targets`` resolves
them, and ``apps/backend.py`` passes them as ``extra_visible_dirs`` for exactly that spawn
(PR #8794); a spelling beneath a foreign mask is refused by
``carveout_shadowed_by_foreign_mask`` (PR #8965). Both are pinned in
``test_sandbox_governance_mask.py``.

What this file pins is what that carve-out OPENS, and the guards that close it. Making the
three state files writable on a sandboxed host for the first time exposed two credential
paths:

* an ABSENT leaf gets no mask at all — ``mount(2)`` cannot target a missing path and the
  launcher's hiding loops guard on existence — so an agent namespace spawned before the
  first vault attach reads the PAT saved after it;
* the state writers staged their temp BESIDE the target, so a file holding the real PAT
  bytes sat at a name none of the three leaf masks covers, and a SIGKILL between write and
  rename left it readable by a same-uid agent forever.

The answers are :func:`sandbox._materialize_md_notebook_mask_targets` and the masked
top-level ``md-notebook-staging`` directory every state writer now publishes through.
"""

from __future__ import annotations

import asyncio
import json
import os
import re

import pytest

import kiro_crew.sandbox as sb

_MODES = ("standard", "cc", "strict")
_CREW_PREFIXES = (".kiro/crew", ".kirocrew")


def _crew_path(prefix: str, leaf: str) -> str:
    from pathlib import Path

    return os.path.join(str(Path.home()), *prefix.split("/"), *leaf.split("/"))


def _hidden_dirs(mode: str) -> set[str]:
    script = sb._build_launcher_script(mode)
    match = re.search(r"SENSITIVE_DIRS = (\[.*?\])\n", script, re.S)
    assert match, "SENSITIVE_DIRS not found in the generated launcher script"
    return set(json.loads(match.group(1)))


class TestTheStagingDirectoryIsMaskedAndCarvedBack:
    """The write-staging directory is fenced from agents and returned to the backend.

    The three leaf masks cover exactly three names, so the temp the writers publish
    through needs its own cover. It is masked as a whole DIRECTORY (every name inside it,
    present and future) and handed back only to the spawn that owns the leaves — carving
    out the leaves alone would move the EPERM from the leaf to the staging dir.
    """

    @pytest.mark.parametrize("mode", _MODES)
    @pytest.mark.parametrize("prefix", _CREW_PREFIXES)
    def test_the_staging_dir_is_masked_in_every_mode(self, mode: str, prefix: str) -> None:
        assert _crew_path(prefix, sb._MD_NOTEBOOK_STAGING_LEAF) in _hidden_dirs(mode)

    def test_the_backend_gets_the_staging_dir_back(self) -> None:
        targets = sb.app_backend_visible_targets(sb.MD_NOTEBOOK_APP_NAME)

        for prefix in _CREW_PREFIXES:
            assert _crew_path(prefix, sb._MD_NOTEBOOK_STAGING_LEAF) in targets

    def test_the_launcher_lifts_the_staging_mask_for_that_spawn(self) -> None:
        script = sb._build_launcher_script(
            "standard",
            extra_visible_dirs=sb.app_backend_visible_targets(sb.MD_NOTEBOOK_APP_NAME),
        )
        match = re.search(r"SENSITIVE_DIRS = (\[.*?\])\n", script, re.S)
        assert match
        hidden = set(json.loads(match.group(1)))

        for prefix in _CREW_PREFIXES:
            assert _crew_path(prefix, sb._MD_NOTEBOOK_STAGING_LEAF) not in hidden

    def test_the_precreate_table_covers_exactly_the_state_leaves(self) -> None:
        """Materialising a leaf needs its own absent-equivalence argument, so the two
        tables must not drift: a leaf added to the mask without one would be created
        with no proof that empty means absent to its reader."""
        assert set(sb._MD_NOTEBOOK_PRECREATE_CONTENT) == set(sb._MD_NOTEBOOK_STATE_LEAVES)
        assert sb._MD_NOTEBOOK_STAGING_LEAF not in sb._MD_NOTEBOOK_PRECREATE_CONTENT


class TestANewOwnedLeavesAppCannotSilentlySkipMaterialization:
    """A drift gate: `_APP_BACKEND_OWNED_LEAVES` is generic, this hardening is not.

    The materialiser, the precreate table, and the `-I` startup isolation are all keyed to
    md-notebook by name, while the owned-leaves table any app can join is a plain dict. An
    app added there would get its leaves unmasked for its own spawn — and would silently
    reopen BOTH holes this file exists to close: no mount target for an absent leaf, and
    interpreter startup hooks running in a namespace holding its secret. Neither failure
    is visible at runtime, so the omission has to be loud HERE instead.
    """

    def test_every_owned_leaves_app_is_covered_by_materialization(self) -> None:
        covered = {
            sb.MD_NOTEBOOK_APP_NAME: set(sb._MD_NOTEBOOK_PRECREATE_CONTENT)
            | {sb._MD_NOTEBOOK_STAGING_LEAF},
        }
        assert set(sb._APP_BACKEND_OWNED_LEAVES) == set(covered), (
            "an app gained entries in _APP_BACKEND_OWNED_LEAVES without materialisation "
            "coverage. Its own spawn now sees those leaves, so before shipping it you must "
            "(a) give every FILE leaf an absent-equivalent document with a written "
            "per-leaf argument, the way _MD_NOTEBOOK_PRECREATE_CONTENT does, and "
            "materialise it before launch — otherwise an absent leaf gets NO mask and a "
            "namespace spawned earlier reads whatever the backend writes later; and "
            "(b) decide explicitly whether that spawn needs the -I isolated startup "
            "md-notebook uses, since a bare `python -m` runs agent-writable "
            "sitecustomize/usercustomize inside the one namespace where its secret is "
            "unmasked. Then extend this table."
        )
        for app, leaves in sb._APP_BACKEND_OWNED_LEAVES.items():
            assert set(leaves) == covered[app], (
                f"{app}'s owned leaves and its materialisation coverage have drifted: "
                f"{set(leaves) ^ covered[app]}"
            )


class TestAbsentStateFilesAreMaterializedSoTheMaskCanMount:
    """``mount(2)`` cannot target an absent path and the launcher's hiding loops
    guard on existence, so an absent leaf gets NO mask in the spawned namespace.
    The carve-out makes these three files creatable on a sandboxed host for the
    first time, so an agent namespace spawned before the first vault attach
    would read the PAT saved after it — unless the absent-equivalent documents
    are materialised before launch (server-side GPT review finding on #8778)."""

    @pytest.fixture()
    def crew_home(self, tmp_path, monkeypatch):
        home = tmp_path / ".kiro" / "crew"
        home.mkdir(parents=True)
        monkeypatch.setattr(sb, "config_dir", lambda: home)
        # The sweep resolves the LEGACY home spelling from ``Path.home()`` as well, which
        # no ``KIROCREW_HOME`` pin covers — without this the suite would scan (and delete
        # ``*.tmp`` files in) the developer's real data home.
        monkeypatch.setattr(sb.Path, "home", staticmethod(lambda: tmp_path))
        return home

    def test_the_fixture_really_isolates_the_real_home(self, crew_home, tmp_path):
        """Guard the guard: if ``Path.home`` ever stops being patched here, this suite
        would sweep the developer's own crew home. Fail loudly instead."""
        assert sb.Path.home() == tmp_path

    def test_every_leaf_is_created_with_its_absent_equivalent_document(self, crew_home):
        created = sb._materialize_md_notebook_mask_targets()
        state = crew_home / "workspace" / "md-notebook"
        assert set(created) == {str(state / n) for n in ("pat", "vaults.json", "settings.json")}
        assert (state / "vaults.json").read_bytes() == b"[]\n"
        assert (state / "settings.json").read_bytes() == b"{}\n"
        assert (state / "pat").read_bytes() == b""
        # The PAT mount target is a credential path: owner-only from birth.
        assert os.stat(state / "pat").st_mode & 0o077 == 0

    def test_the_staging_dir_is_materialized_by_the_shared_direct_child_path(self) -> None:
        """The staging directory is a DIRECT child of the data home, so it belongs to
        ``_materialize_maskable_dirs`` — whose plain ``mkdir`` is only sound for direct
        children — rather than to the nested md-notebook materialiser."""
        assert sb._MD_NOTEBOOK_STAGING_LEAF in sb._CREW_PRECREATE_HIDDEN_DIR_LEAVES
        assert "/" not in sb._MD_NOTEBOOK_STAGING_LEAF

    def test_the_staging_dir_has_no_agent_writable_ancestor(self) -> None:
        """A mask covers the leaf, NOT its ancestors. Under ``workspace/md-notebook`` — a
        tree the agent can write at OS level — the staging dir could be renamed out from
        under its own mask, and a later PAT write would publish through the replacement,
        unmasked, into a live agent's view. Top-level, like ``aws-control-staging``."""
        assert not sb._MD_NOTEBOOK_STAGING_LEAF.startswith("workspace/")
        for leaf in sb._MD_NOTEBOOK_STATE_LEAVES:
            assert not sb._MD_NOTEBOOK_STAGING_LEAF.startswith(os.path.dirname(leaf))

    def test_the_backend_and_the_mask_name_the_same_staging_dir(self) -> None:
        """The writer spells the leaf itself rather than importing the sandbox module into
        the app backend's process, so the two spellings are pinned here: a mismatch would
        stage PAT bytes at a name nothing masks."""
        from kiro_crew.apps.builtins.md_notebook import server

        assert server._STAGING_LEAF == sb._MD_NOTEBOOK_STAGING_LEAF

    def test_the_sweep_runs_on_the_macos_launch_path_too(self) -> None:
        """Materialising is Linux-only for a real reason — a Seatbelt deny is a path rule
        that holds for a name that does not exist yet — but that does NOT transfer to an
        orphan already on disk at a name no rule names. The profile denies the leaves and
        the staging dir, never an arbitrary ``*.tmp`` sibling, so skipping macOS would
        leave a pre-upgrade token readable there forever."""
        import inspect

        for fn in (sb.namespace_argv, sb.sandbox_exec_argv):
            assert "_sweep_legacy_md_notebook_temps()" in inspect.getsource(
                fn
            ), f"{fn.__name__} does not sweep legacy md-notebook staging temps"

    def test_the_documents_read_as_absent_to_the_backend(self, crew_home, monkeypatch):
        """The whole materialisation argument: an empty document must mean what
        an absent file means TO THE READER. Pin it against the backend's own
        read functions rather than restating their behavior here."""
        from kiro_crew.apps.builtins.md_notebook import server

        sb._materialize_md_notebook_mask_targets()
        monkeypatch.setattr(server, "_HOME", crew_home / "workspace" / "md-notebook")
        assert server._read_vaults_sync() == []
        assert server._read_settings_sync() == server._default_settings()
        assert server._read_pat_sync() is None

    def test_existing_state_is_left_byte_for_byte_alone(self, crew_home):
        state = crew_home / "workspace" / "md-notebook"
        state.mkdir(parents=True)
        (state / "vaults.json").write_text('[{"id": "real"}]')
        assert sb._materialize_md_notebook_mask_targets()  # creates only the other two
        assert (state / "vaults.json").read_text() == '[{"id": "real"}]'

    def test_a_legacy_sibling_temp_is_swept(self, crew_home):
        """A pre-upgrade writer staged BESIDE the target, so a SIGKILL in that window
        left real PAT bytes at a name no mask covers — not the three leaves, not
        the staging directory. Materialising forward cannot help an artefact on disk, so
        the orphan is removed."""
        state = crew_home / "workspace" / "md-notebook"
        state.mkdir(parents=True)
        orphans = [
            state / "tmpab12cd34.tmp",  # atomic_write's mkstemp(dir=parent)
            state / "vaults.json.deadbeef.tmp",  # git_ops.staged_temp_name
            state / "settings.json.cafe1234.tmp",
        ]
        for o in orphans:
            o.write_text("ghp_leaked_token")

        removed = sb._sweep_legacy_md_notebook_temps()

        for o in orphans:
            assert not o.exists(), f"legacy PAT-bearing temp survived: {o}"
        # The returned list is the caller-visible record of WHICH credentials were exposed,
        # which is what the operator needs in order to know what to rotate — assert it
        # rather than leaving it as bookkeeping nothing reads.
        assert set(removed) == {str(o) for o in orphans}

    def test_the_sweep_keeps_real_state_and_clone_data(self, crew_home):
        """Only ``*.tmp`` DIRECT children are orphans by construction. The state files,
        the staging dir, and the vault clones must all survive."""
        state = crew_home / "workspace" / "md-notebook"
        (state / "vaults").mkdir(parents=True)
        (state / "vaults" / "v1").mkdir()
        (state / "vaults" / "v1" / "note.md.abcd.tmp").write_text("a note temp, not ours")
        (state / "pat").write_text("ghp_real")
        (state / "vaults.json").write_text("[]")

        sb._sweep_legacy_md_notebook_temps()
        sb._materialize_md_notebook_mask_targets()

        assert (state / "pat").read_text() == "ghp_real"
        assert (state / "vaults.json").read_text() == "[]"
        assert (
            state / "vaults" / "v1" / "note.md.abcd.tmp"
        ).exists(), "the sweep reached into a vault clone; it must only touch direct children"

    def test_a_legacy_home_orphan_is_swept_too(self, crew_home, tmp_path):
        """The mask covers BOTH crew-home spellings, so an orphan under an un-migrated or
        rolled-back ``~/.kirocrew`` is exposed exactly like one under the live home. This
        is the opposite requirement from materialising, which is live-home-only because a
        stub in a home nothing reads would be a file nobody opens — a token already written
        to the legacy home stays readable whichever home is live now."""
        legacy = tmp_path / ".kirocrew" / "workspace" / "md-notebook"
        legacy.mkdir(parents=True)
        orphan = legacy / "tmplegacy1.tmp"
        orphan.write_text("ghp_leaked_token")

        sb._sweep_legacy_md_notebook_temps()

        assert not orphan.exists(), "a legacy-home PAT temp survived the sweep"

    def test_a_relocated_home_orphan_is_swept_too(self, tmp_path, monkeypatch):
        """The live home is resolved through ``config_dir()``, which ``KIROCREW_HOME`` can
        move OUT from under ``$HOME`` entirely — so the two roots are not interchangeable
        and neither alone is sufficient. Pins the live-home root independently of the
        ``$HOME``-prefix roots, which the other sweep tests happen to share."""
        relocated = tmp_path / "srv" / "crew"
        state = relocated / "workspace" / "md-notebook"
        state.mkdir(parents=True)
        monkeypatch.setattr(sb, "config_dir", lambda: relocated)
        monkeypatch.setattr(sb.Path, "home", staticmethod(lambda: tmp_path / "elsewhere"))
        orphan = state / "tmprelocated.tmp"
        orphan.write_text("ghp_leaked_token")

        sb._sweep_legacy_md_notebook_temps()

        assert not orphan.exists(), "a relocated-home PAT temp survived the sweep"

    def test_the_sweep_refuses_to_delete_through_a_planted_parent_link(self, crew_home, tmp_path):
        """The sweep UNLINKS, so following a planted link is irreversible deletion in a
        tree the agent chose — strictly worse than the materialiser's create-only version
        of the same hazard. The intermediate components are agent-writable, so the chain
        gets the same #4381 refusal, and the root is skipped rather than swept.

        The planted path resolves COMPLETELY — the victim really does contain an
        ``md-notebook`` directory holding a ``*.tmp`` file — so the open succeeds and the
        chain refusal is the only thing standing between the sweep and the deletion.
        ``O_NOFOLLOW`` cannot help here: it only judges the final component, which is a
        real directory."""
        victim_state = tmp_path / "victim" / "md-notebook"
        victim_state.mkdir(parents=True)
        bystander = victim_state / "unrelated.tmp"
        bystander.write_text("someone else's file")
        # ``workspace`` itself is the planted link, so the state dir resolves inside it.
        (crew_home / "workspace").symlink_to(tmp_path / "victim", target_is_directory=True)

        sb._sweep_legacy_md_notebook_temps()

        assert bystander.exists(), "the sweep deleted through a planted parent link"

    def test_the_sweep_refuses_to_delete_through_a_linked_state_dir(self, crew_home, tmp_path):
        """Same hazard one component deeper, where the LEAF is the link. The
        ``O_NOFOLLOW`` open is what refuses it, and it also pins the directory so a swap
        between the check and the unlink cannot redirect either syscall."""
        victim = tmp_path / "victim2"
        victim.mkdir()
        bystander = victim / "unrelated.tmp"
        bystander.write_text("someone else's file")
        (crew_home / "workspace").mkdir()
        (crew_home / "workspace" / "md-notebook").symlink_to(victim, target_is_directory=True)

        sb._sweep_legacy_md_notebook_temps()

        assert bystander.exists(), "the sweep deleted through a linked state directory"

    def test_the_sweep_spares_an_inflight_ceiling_temp(self, crew_home):
        """``_publish_empty_ceiling`` stages ITS temp in the target's parent — the very
        directory this sweep scans — and publishes with ``os.link``. Two concurrent spawns
        would otherwise let one's sweep unlink the other's temp between the ``mkstemp`` and
        the ``link``, failing that spawn for no reason. Gateway-owned, not an orphan."""
        state = crew_home / "workspace" / "md-notebook"
        state.mkdir(parents=True)
        inflight = state / f"{sb._CEILING_TEMP_PREFIX}abcd1234.tmp"
        inflight.write_bytes(b"")

        sb._sweep_legacy_md_notebook_temps()

        assert inflight.exists(), "the sweep unlinked another spawn's in-flight temp"

    def test_a_non_regular_tmp_is_left_alone_by_the_sweep(self, crew_home):
        """``lstat`` decides, so a link is never followed and a directory is not removed:
        the sweep deletes files, and anything else at such a name is the operator's."""
        state = crew_home / "workspace" / "md-notebook"
        state.mkdir(parents=True)
        (state / "a-dir.tmp").mkdir()

        sb._sweep_legacy_md_notebook_temps()

        assert (state / "a-dir.tmp").is_dir()

    def test_an_unremovable_legacy_temp_refuses_the_spawn(self, crew_home, monkeypatch):
        """Fail-closed like every other branch here: if the orphan cannot be removed,
        launching would hand the agent the PAT this mechanism exists to hide."""
        state = crew_home / "workspace" / "md-notebook"
        state.mkdir(parents=True)
        (state / "tmpstuck.tmp").write_text("ghp_leaked_token")

        def _refuse(path, *a, **k):
            raise OSError(13, "Permission denied")

        monkeypatch.setattr(sb.os, "unlink", _refuse)
        with pytest.raises(sb.SandboxCeilingUnsealable, match="tmpstuck.tmp"):
            sb._sweep_legacy_md_notebook_temps()

    def test_an_absent_data_home_is_not_created(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sb, "config_dir", lambda: tmp_path / "never-made")
        assert sb._materialize_md_notebook_mask_targets() == []
        assert not (tmp_path / "never-made").exists()

    def test_a_failed_publish_refuses_the_spawn(self, crew_home, monkeypatch):
        """Fail-closed: launching anyway would run the agent with a mask the
        launcher silently skips — the exact hole this materialiser closes."""
        monkeypatch.setattr(sb, "_publish_empty_ceiling", lambda *a, **k: False)
        with pytest.raises(sb.SandboxCeilingUnsealable):
            sb._materialize_md_notebook_mask_targets()

    def test_a_planted_intermediate_symlink_refuses_the_spawn(self, crew_home, tmp_path):
        """#4381, materialiser edition: os.makedirs follows a RESOLVING symlink
        planted at ``workspace/md-notebook`` (an agent-writable tree a spawned
        subprocess can symlink at OS level), so the materialised files would
        land at the link's target while the launcher masks the lexical path —
        a mask mounted over an attacker-chosen redirection (server-side GPT
        review, round 7). Refuse the spawn instead."""
        elsewhere = tmp_path / "elsewhere-mask"
        elsewhere.mkdir()
        (crew_home / "workspace").mkdir(parents=True)
        (crew_home / "workspace" / "md-notebook").symlink_to(elsewhere, target_is_directory=True)
        with pytest.raises(sb.SandboxCeilingUnsealable):
            sb._materialize_md_notebook_mask_targets()
        assert list(elsewhere.iterdir()) == [], (
            "the materialiser followed the planted link and created state at "
            f"its target: {list(elsewhere.iterdir())!r}"
        )

    def test_a_resolving_leaf_symlink_refuses_the_spawn(self, crew_home, tmp_path):
        """A RESOLVING link at the leaf is refused too, not only a dangling one:
        mount(2) resolves its target, so a resolving ``pat`` link would put the
        mask on the referent while the lexical name stays an agent-replaceable
        link — swap it after launch and a later PAT write publishes to an
        unmasked name inside the live namespace (server-side GPT review,
        round 9)."""
        real = tmp_path / "somewhere-else-pat"
        real.write_bytes(b"")
        state = crew_home / "workspace" / "md-notebook"
        state.mkdir(parents=True)
        (state / "pat").symlink_to(real)
        with pytest.raises(sb.SandboxCeilingUnsealable):
            sb._materialize_md_notebook_mask_targets()

    def test_a_dangling_leaf_symlink_refuses_the_spawn(self, crew_home, tmp_path):
        """A DANGLING leaf link takes the other route to the same refusal: ``exists()``
        is False for one, so it reaches the publish, where ``os.link`` fails EEXIST on
        the link's own name and the race re-check lstats it as a link."""
        state = crew_home / "workspace" / "md-notebook"
        state.mkdir(parents=True)
        (state / "pat").symlink_to(tmp_path / "nothing-here")
        with pytest.raises(sb.SandboxCeilingUnsealable):
            sb._materialize_md_notebook_mask_targets()

    def test_a_special_file_at_a_leaf_refuses_the_spawn(self, crew_home):
        """An EXISTING target is acceptable only as a regular file: the
        launcher's hiding loops classify with isdir/isfile, and a FIFO at
        ``pat`` matches neither — the mask is silently skipped for the whole
        sandbox (server-side GPT review, round 10)."""
        state = crew_home / "workspace" / "md-notebook"
        state.mkdir(parents=True)
        os.mkfifo(state / "pat")
        with pytest.raises(sb.SandboxCeilingUnsealable):
            sb._materialize_md_notebook_mask_targets()

    def test_a_raced_special_file_refuses_the_spawn(self, crew_home, monkeypatch):
        """A LOST publish race is benign only when the winner clears the same
        regular-file bar the pre-check enforces: an agent racing mkfifo between
        validation and publish must not have its non-file accepted on bare
        existence (server-side GPT review, round 11)."""

        real_publish = sb._publish_empty_ceiling

        def _raced(target, parent, content=b"{}\n"):
            if target.endswith("pat") and not os.path.exists(target):
                os.mkfifo(target)  # the racing writer wins with a FIFO
                return False  # our publish loses (os.link EEXIST -> False)
            return real_publish(target, parent, content=content)

        monkeypatch.setattr(sb, "_publish_empty_ceiling", _raced)
        with pytest.raises(sb.SandboxCeilingUnsealable):
            sb._materialize_md_notebook_mask_targets()

    def test_namespace_argv_materializes_before_the_launcher_runs(self, crew_home):
        """The call site: every Linux spawn gets mount targets before its child
        mounts, so no agent namespace can predate the mask."""
        sb.namespace_argv(["/bin/true"])
        state = crew_home / "workspace" / "md-notebook"
        for name in ("pat", "vaults.json", "settings.json"):
            assert (state / name).is_file(), f"{name} absent after namespace_argv"


class TestStateWritersStageInsideTheMask:
    """The three leaf masks cover exactly three names — a temp staged BESIDE the
    target holds the same bytes (PAT included) at a name no mask covers, and a
    SIGKILL between write and rename leaves it there forever (server-side GPT
    review, F1 on #8778). Every state writer must stage under the whole-directory
    top-level staging mask instead."""

    @pytest.fixture()
    def server(self, tmp_path, monkeypatch):
        from kiro_crew.apps.builtins.md_notebook import server

        monkeypatch.setattr(server, "_HOME", tmp_path / "state")
        return server

    def test_a_crashed_publish_leaves_the_temp_inside_the_mask(self, server, monkeypatch):
        def _boom(tmp, target):
            raise AssertionError("simulated crash at publish time")

        monkeypatch.setattr(server, "replace_with_retry", _boom)
        # Suppress the failure-path unlink so the orphan the crash WOULD leave
        # is observable — this models SIGKILL, which runs no cleanup at all.
        monkeypatch.setattr(server.Path, "unlink", lambda self, *a, **k: None)
        with pytest.raises(AssertionError):
            server._write_pat_sync("ghp_secret")

        state = server._HOME
        orphans_beside_target = list(state.iterdir())
        assert (
            not orphans_beside_target
        ), f"a PAT temp was staged beside the target, outside the mask: {orphans_beside_target!r}"
        staged = list(server._staging_dir().iterdir())
        assert staged, "the temp was not staged under the masked staging dir at all"
        assert staged[0].read_text() == "ghp_secret"
        if os.name == "posix":
            assert os.stat(staged[0]).st_mode & 0o077 == 0

    def test_each_writer_publishes_to_its_target_with_no_residue(self, server):
        server._write_pat_sync("ghp_token")
        server._write_vaults_sync([{"id": "v1"}])
        server._write_settings_sync({"autoSync": True})
        state = server._HOME
        assert (state / "pat").read_text() == "ghp_token"
        assert "v1" in (state / "vaults.json").read_text()
        assert "autoSync" in (state / "settings.json").read_text()
        assert list(server._staging_dir().iterdir()) == [], "a successful write left residue"
        assert {p.name for p in state.iterdir()} == {
            "pat",
            "vaults.json",
            "settings.json",
        }, "a writer left a temp beside its target"

    def test_a_planted_parent_link_refuses_the_write(self, server, tmp_path):
        """The #4381 guard, carried over from atomic_write: the old PAT write
        (atomic_write with restrict_to_owner=True) refused a secret write whose
        parent chain passes through a pre-planted link, because mkdir/mkstemp/
        rename all follow it and the token lands outside the sensitive-path
        fence. Moving the staging off atomic_write must not shed that guard
        (server-side GPT review, round 3 on #8778)."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        # server fixture sets _HOME to tmp_path/"state" without creating it;
        # plant the state dir itself as a link to a foreign directory.
        server._HOME.symlink_to(elsewhere, target_is_directory=True)
        with pytest.raises(OSError):
            server._write_pat_sync("ghp_secret")
        assert list(elsewhere.iterdir()) == [], (
            "the PAT write followed a pre-planted parent link and published "
            f"the token outside the fence: {list(elsewhere.iterdir())!r}"
        )

    def test_a_planted_staging_link_refuses_the_write(self, server, tmp_path):
        """Same guard, one component deeper: the staging dir itself must be a real
        directory, not a link redirecting every temp (PAT bytes included)."""
        elsewhere = tmp_path / "elsewhere-staging"
        elsewhere.mkdir()
        server._HOME.mkdir(parents=True)
        server._staging_dir().symlink_to(elsewhere, target_is_directory=True)
        with pytest.raises(OSError):
            server._write_pat_sync("ghp_secret")
        assert (
            list(elsewhere.iterdir()) == []
        ), "the staged temp followed a pre-planted staging link outside the mask"

    def test_clearing_the_pat_keeps_the_mask_mount_target(self, server):
        """Clearing must atomically empty the file, never unlink it: the inode
        is the sandbox mask's mount target, and a clear landing between the
        launcher's materialize and mount steps would leave that namespace
        maskless for a later PAT save (server-side GPT review, round 5)."""
        server._write_pat_sync("ghp_token")
        assert server._read_pat_sync() == "ghp_token"
        server._write_pat_sync("")  # what api_pat's clear branch calls
        pat_file = server._HOME / "pat"
        assert pat_file.is_file(), "the PAT clear removed the mask's mount target"
        assert pat_file.read_bytes() == b""
        assert server._read_pat_sync() is None, "empty must read as absent"

    def test_the_clear_ROUTE_keeps_the_mask_mount_target(self, server, monkeypatch):
        """Pins the ROUTE, not just the writer. The test above proves an empty write is
        absent-equivalent; this one proves ``api_pat``'s clear branch actually takes it.
        An ``os.unlink`` there would delete the mask's mount target while every
        writer-level assertion above still passed."""
        server._write_pat_sync("ghp_token")
        pat_file = server._HOME / "pat"
        assert pat_file.is_file()

        async def _fake_body(_request):
            return {"pat": ""}

        async def _no_gh():
            return None

        monkeypatch.setattr(server, "json_body", _fake_body)
        monkeypatch.setattr(server, "gh_token", _no_gh)

        response = asyncio.run(server.api_pat(object()))

        assert pat_file.is_file(), "the clear route removed the mask's mount target"
        assert pat_file.read_bytes() == b""
        assert json.loads(response.text)["hasPat"] is False
