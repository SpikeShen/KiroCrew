"""Tests for _remove_slot_for_history_key in handlers.py."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from typing import Any, Collection
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from kiro_crew import cron as cron_module
from kiro_crew.cron import (
    CronService,
    CronStoreBusy,
    cron_job_id_from_session_key,
    cron_owner_matches,
    cron_session_key_is_stable,
)
from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.handlers import (
    _remove_slot_for_history_key,
    api_session_delete,
    api_sessions_clear,
)
from kiro_crew.dashboard.handlers.sessions import _CRON_RELEASE_ATTEMPTS


def _make_state(slots: dict) -> MagicMock:
    state = MagicMock()
    state._slots = dict(slots)
    state.push_slots_update = MagicMock()
    state.sessions = MagicMock()
    state.sessions.destroy = AsyncMock()
    return state


def _make_slot(key: str, running: bool = False) -> MagicMock:
    slot = MagicMock()
    slot.key = key
    slot.running = running
    # A real slot is unbound unless its conversation lives on another session.
    # Left unset, a bare MagicMock hands back a truthy Mock as the session key,
    # so the teardown would target something that is not a key at all.
    slot.linked_session_key = ""
    if running:
        async def _hang():
            await asyncio.sleep(999)
        slot.task = asyncio.ensure_future(_hang())
    else:
        slot.task = None
    return slot


class TestRemoveSlotForHistoryKey:
    @pytest.mark.asyncio
    async def test_exact_key_match(self):
        slot = _make_slot("dashboard_chat-1-100")
        state = _make_state({"dashboard_chat-1-100": slot})
        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")
        assert "dashboard_chat-1-100" not in state._slots

    @pytest.mark.asyncio
    async def test_stripped_key_match(self):
        slot = _make_slot("chat-1-100")
        state = _make_state({"chat-1-100": slot})
        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")
        assert "chat-1-100" not in state._slots

    @pytest.mark.asyncio
    async def test_colon_prefix_stripped(self):
        slot = _make_slot("chat-2-200")
        state = _make_state({"chat-2-200": slot})
        await _remove_slot_for_history_key(state, "dashboard:chat-2-200")
        assert "chat-2-200" not in state._slots

    @pytest.mark.asyncio
    async def test_no_match_is_noop(self):
        state = _make_state({"chat-9-999": _make_slot("chat-9-999")})
        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")
        assert "chat-9-999" in state._slots
        state.sessions.destroy.assert_not_called()

    @pytest.mark.asyncio
    async def test_running_task_cancelled(self):
        slot = _make_slot("dashboard_chat-1-100", running=True)
        state = _make_state({"dashboard_chat-1-100": slot})
        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")
        assert slot.task.cancelled()
        state.sessions.destroy.assert_awaited_once_with("dashboard:chat-1-100")

    @pytest.mark.asyncio
    async def test_pending_question_cancelled_before_running_task(self):
        """History deletion must not leave a DashboardState-owned question
        future alive after its slot task and provider have been destroyed."""
        slot = _make_slot("dashboard_chat-1-100", running=True)
        state = _make_state({"dashboard_chat-1-100": slot})
        task_was_done: list[bool] = []

        def cancel_questions(slot_key: str) -> int:
            assert slot_key == slot.key
            task_was_done.append(slot.task.done())
            return 1

        state.cancel_questions_for_slot = MagicMock(side_effect=cancel_questions)

        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")

        state.cancel_questions_for_slot.assert_called_once_with(slot.key)
        assert task_was_done == [False]
        assert slot.task.cancelled()

    @pytest.mark.asyncio
    async def test_non_running_task_not_cancelled(self):
        slot = _make_slot("dashboard_chat-1-100", running=False)
        state = _make_state({"dashboard_chat-1-100": slot})
        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")
        assert slot.task is None
        state.sessions.destroy.assert_awaited_once_with("dashboard:chat-1-100")

    @pytest.mark.asyncio
    async def test_stacked_dashboard_prefix(self):
        slot = _make_slot("chat-3-300")
        state = _make_state({"chat-3-300": slot})
        await _remove_slot_for_history_key(state, "dashboard_dashboard_chat-3-300")
        assert "chat-3-300" not in state._slots

    @pytest.mark.asyncio
    async def test_batch_clear_removes_multiple_slots(self):
        """Verify batch clear removes matched slots and leaves unmatched."""
        slot_a = _make_slot("chat-1-100")
        slot_b = _make_slot("chat-2-200", running=True)
        slot_c = _make_slot("chat-9-999")
        state = _make_state({
            "chat-1-100": slot_a,
            "chat-2-200": slot_b,
            "chat-9-999": slot_c,
        })
        # Simulate batch clear for two keys (one matched, one running)
        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")
        await _remove_slot_for_history_key(state, "dashboard_chat-2-200")
        assert "chat-1-100" not in state._slots
        assert "chat-2-200" not in state._slots
        assert "chat-9-999" in state._slots  # unmatched stays
        assert state.sessions.destroy.await_count == 2

    @pytest.mark.asyncio
    async def test_reverse_prefix_lookup(self):
        """History key 'chat-1-100' finds slot stored as 'dashboard_chat-1-100'."""
        slot = _make_slot("dashboard_chat-1-100")
        state = _make_state({"dashboard_chat-1-100": slot})
        await _remove_slot_for_history_key(state, "chat-1-100")
        assert "dashboard_chat-1-100" not in state._slots
        state.sessions.destroy.assert_awaited_once_with("dashboard:chat-1-100")

    @pytest.mark.asyncio
    async def test_sessions_remove_exception_does_not_propagate(self):
        slot = _make_slot("dashboard_chat-1-100")
        state = _make_state({"dashboard_chat-1-100": slot})
        state.sessions.destroy = AsyncMock(side_effect=RuntimeError("already gone"))
        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")
        assert "dashboard_chat-1-100" not in state._slots


class TestChannelSlotTeardown:
    """Deleting a channel history must tear down the CHANNEL's session.

    A channel-born slot runs the channel's own session, so a key derived from
    the history key names a session that does not exist: the provider survives
    the delete and its next inbound message recreates the transcript the user
    just removed.
    """

    @pytest.mark.asyncio
    async def test_destroys_the_slots_own_session_not_a_derived_key(self):
        slot = _make_slot("slack_1785370133.085469")
        slot.linked_session_key = "slack:1785370133.085469"
        state = _make_state({"slack_1785370133.085469": slot})

        await _remove_slot_for_history_key(state, "slack_1785370133.085469")

        state.sessions.destroy.assert_awaited_once_with("slack:1785370133.085469")
        assert "slack_1785370133.085469" not in state._slots


class TestPermanentDeleteReleasesCronOwnership:
    """A permanently deleted session must not strand the jobs it scheduled.

    ``cron_add`` stamps the creating session's key on the job, and the MCP
    ownership gate is equality on that key -- so once the session is gone for
    good the row is manageable by nobody. Release collapses that invisible state
    into the documented ownerless one the CLI and Schedule page already manage.
    """

    def _service(self, tmp_path):
        return CronService(base_dir=tmp_path)

    @pytest.mark.asyncio
    async def test_releases_the_deleted_sessions_job(self, tmp_path):
        crons = self._service(tmp_path)
        job = crons.add_job("repro", "ping", every_secs=3600, session_key="dashboard:chat-1-100")
        state = _make_state({})
        state.crons = crons

        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")

        reloaded = CronService(base_dir=tmp_path).get_job(job.id)
        assert reloaded is not None
        assert reloaded.session_key == ""

    @pytest.mark.asyncio
    async def test_released_job_keeps_its_schedule_and_stays_enabled(self, tmp_path):
        crons = self._service(tmp_path)
        job = crons.add_job(
            "repro", "ping", cron_expr="0 9 * * 1-5", session_key="dashboard:chat-1-100"
        )
        state = _make_state({})
        state.crons = crons

        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")

        reloaded = CronService(base_dir=tmp_path).get_job(job.id)
        assert reloaded is not None
        assert reloaded.enabled is True
        assert reloaded.schedule.cron_expr == "0 9 * * 1-5"
        assert reloaded.message == "ping"

    @pytest.mark.asyncio
    async def test_leaves_another_live_sessions_job_owned(self, tmp_path):
        crons = self._service(tmp_path)
        mine = crons.add_job("mine", "ping", every_secs=3600, session_key="dashboard:chat-1-100")
        theirs = crons.add_job(
            "theirs", "ping", every_secs=3600, session_key="dashboard:chat-9-999"
        )
        state = _make_state({})
        state.crons = crons

        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")

        reloaded = CronService(base_dir=tmp_path)
        assert reloaded.get_job(mine.id).session_key == ""
        assert reloaded.get_job(theirs.id).session_key == "dashboard:chat-9-999"

    @pytest.mark.asyncio
    async def test_leaves_an_ownerless_job_untouched(self, tmp_path):
        crons = self._service(tmp_path)
        job = crons.add_job("cli-made", "ping", every_secs=3600)
        state = _make_state({})
        state.crons = crons

        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")

        reloaded = CronService(base_dir=tmp_path).get_job(job.id)
        assert reloaded is not None
        assert reloaded.session_key == ""

    @pytest.mark.asyncio
    async def test_releases_a_channel_sessions_job_by_its_own_key(self, tmp_path):
        crons = self._service(tmp_path)
        job = crons.add_job(
            "channel", "ping", every_secs=3600, session_key="slack:1785370133.085469"
        )
        slot = _make_slot("slack_1785370133.085469")
        slot.linked_session_key = "slack:1785370133.085469"
        state = _make_state({"slack_1785370133.085469": slot})
        state.crons = crons

        await _remove_slot_for_history_key(state, "slack_1785370133.085469")

        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == ""

    @pytest.mark.asyncio
    async def test_a_released_job_is_then_adoptable(self, tmp_path):
        crons = self._service(tmp_path)
        job = crons.add_job("repro", "ping", every_secs=3600, session_key="dashboard:chat-1-100")
        state = _make_state({})
        state.crons = crons

        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")

        adopter = CronService(base_dir=tmp_path)
        assert adopter.get_job(job.id).session_key == ""
        assert adopter.adopt_job(job.id, "dashboard:chat-2-200") is True
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == (
            "dashboard:chat-2-200"
        )

    @pytest.mark.asyncio
    async def test_a_missing_cron_service_does_not_break_the_delete(self, tmp_path):
        state = _make_state({"dashboard_chat-1-100": _make_slot("dashboard_chat-1-100")})
        state.crons = None

        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")

        assert "dashboard_chat-1-100" not in state._slots

    def test_tab_close_does_not_reach_the_permanent_delete_funnel(self):
        """Closing a tab archives a session, so it must not release anything."""
        source = inspect.getsource(chat_handlers.api_chat_slot_delete)
        assert "_remove_slot_for_history_key" not in source


class TestReleaseIsOwnerConditioned:
    """The release must clear the owner it was told about, not whoever owns the
    job by the time the write lands.

    Releasing by iterating a snapshot and calling ``adopt_job(id, "")``
    unconditionally is a lost-update race: another surface (the CLI's
    ``cron adopt``, a cron-injected slot stamping its own key) can hand the job
    to a DIFFERENT session in the gap, and the unconditional write then unbinds
    a job from a session that legitimately owns it. The store's
    ``release_jobs_owned_by`` selects AFTER its in-lock reload, making the
    release a compare-and-clear.
    """

    class _StaleRow:
        """What a pre-lock snapshot hands back: the owner as of the last cache
        refresh, which a cross-process write has already superseded."""

        def __init__(self, job_id: str, session_key: str) -> None:
            self.id = job_id
            self.session_key = session_key

    @pytest.mark.asyncio
    async def test_a_job_re_adopted_since_the_snapshot_keeps_its_new_owner(
        self, tmp_path, monkeypatch
    ):
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("repro", "ping", every_secs=3600, session_key="dashboard:chat-1-100")
        # Another surface re-owns the job through its OWN service instance, so
        # the write is on disk while this instance's cache still names the old
        # owner -- the staleness any pre-lock snapshot is subject to.
        other = CronService(base_dir=tmp_path)
        assert other.adopt_job(job.id, "dashboard:chat-2-200") is True
        monkeypatch.setattr(
            crons,
            "list_jobs",
            lambda include_disabled=False: [self._StaleRow(job.id, "dashboard:chat-1-100")],
        )
        state = _make_state({})
        state.crons = crons

        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")

        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == (
            "dashboard:chat-2-200"
        )

    def test_a_failed_save_leaves_the_cache_agreeing_with_disk(self, tmp_path, monkeypatch):
        """An unwritable store must not release the job in memory only.

        ``_save`` serializes the job list, so the release has to mutate before
        persisting -- but if that write fails, memory saying "ownerless" while
        disk still names the owner makes every in-process ownership decision read
        a state that never happened, and the old owner comes back on restart.
        """
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("repro", "ping", every_secs=3600, session_key="dashboard:chat-1-100")

        def _boom() -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(crons, "_save", _boom)

        with pytest.raises(OSError):
            crons._release_jobs_owned_by_locked({"dashboard:chat-1-100"})

        assert crons.get_job(job.id).session_key == "dashboard:chat-1-100"
        monkeypatch.undo()
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == (
            "dashboard:chat-1-100"
        )


class TestALiveCronPrincipalKeepsItsJobs:
    """A ``cron:<job id>`` owner is not retired by deleting a transcript.

    A cron's result conversation is linked to that key, so it reaches the funnel's
    owner candidates — but every future run of the job presents the same key, so
    releasing the jobs that cron created would leave a LIVE owner unable to list,
    update or remove its own work.
    """

    @pytest.mark.asyncio
    async def test_a_still_scheduled_cron_keeps_owning_the_jobs_it_created(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        owner = crons.add_job("nightly", "check the queue", every_secs=3600)
        # The nightly job's own turn scheduled a follow-up, stamped with the
        # session key a cron run presents.
        created = crons.add_job(
            "follow-up", "ping", every_secs=3600, session_key=f"cron:{owner.id}"
        )
        # Deleting the nightly job's RESULT transcript, whose slot is linked to
        # that same key.
        slot = _make_slot(f"cron-{owner.id}")
        slot.linked_session_key = f"cron:{owner.id}"
        state = _make_state({f"cron-{owner.id}": slot})
        state.crons = crons

        await _remove_slot_for_history_key(
            state, f"cron_{owner.id}", exact_owner_keys=(f"cron:{owner.id}",)
        )

        assert CronService(base_dir=tmp_path).get_job(created.id).session_key == (
            f"cron:{owner.id}"
        )

    @pytest.mark.asyncio
    async def test_a_retired_crons_key_is_still_released(self, tmp_path):
        """Once the owning job is gone its key can never be presented again."""
        crons = CronService(base_dir=tmp_path)
        stranded = crons.add_job(
            "follow-up", "ping", every_secs=3600, session_key="cron:deadbeef"
        )
        state = _make_state({})
        state.crons = crons

        await _remove_slot_for_history_key(
            state, "cron_deadbeef", exact_owner_keys=("cron:deadbeef",)
        )

        assert CronService(base_dir=tmp_path).get_job(stranded.id).session_key == ""

    @pytest.mark.asyncio
    async def test_a_cron_the_cli_deleted_is_dead_even_while_the_cache_lists_it(self, tmp_path):
        """Liveness must be read from the store, not from the cache.

        ``list_jobs`` is cache-only with up to one timer-poll interval of
        cross-process staleness. A CLI ``cron remove`` inside that window leaves
        this service still listing the job, so a cached liveness check calls the
        dead principal live and skips releasing its children — and nothing re-runs
        the delete funnel, so they strand with no warning.
        """
        crons = CronService(base_dir=tmp_path)
        owner = crons.add_job("nightly", "check the queue", every_secs=3600)
        child = crons.add_job(
            "follow-up", "ping", every_secs=3600, session_key=f"cron:{owner.id}"
        )
        # Another process (the CLI) removes the owning job. This service's cache
        # has not observed it.
        cli = CronService(base_dir=tmp_path)
        assert cli.remove_job(owner.id, actor="cli", source="cli") is True
        assert {j.id for j in crons.list_jobs(include_disabled=True)} == {owner.id, child.id}

        state = _make_state({})
        state.crons = crons

        await _remove_slot_for_history_key(
            state, f"cron_{owner.id}", exact_owner_keys=(f"cron:{owner.id}",)
        )

        assert CronService(base_dir=tmp_path).get_job(child.id).session_key == ""

    def test_per_run_execution_keys_resolve_to_their_job(self):
        """``cron:<job id>:<run id>`` names the job, not a principal of its own."""
        assert cron_job_id_from_session_key("cron:abc123") == "abc123"
        assert cron_job_id_from_session_key("cron:abc123:run-7") == "abc123"
        assert cron_job_id_from_session_key("dashboard:chat-1-100") == ""
        assert cron_job_id_from_session_key("slack:1785370133.085469") == ""

    def test_the_release_path_shares_the_stores_one_key_parser(self):
        """A second parser in cron.py could drift from the MCP surface's reading."""
        source = inspect.getsource(cron_module)
        assert source.count("def cron_job_id_from_session_key") == 1
        assert "def _cron_job_id_from_session_key" not in source


class TestOwnerSpellingsOfOneCronPrincipal:
    """A cron principal is stamped under several spellings, all naming one job.

    ``build_cron_session_context`` mints ``cron:<job>`` for a persistent job and
    ``cron:<job>:<run id>`` for a stateless one, and the sequential-agent path
    mints ``cron:<job>:<agent>``. A job the run creates carries whichever the run
    presented, so a release holding only the two-segment form must still reach it.
    """

    def test_matcher_folds_cron_spellings_and_keeps_others_exact(self):
        assert cron_owner_matches("cron:P", "cron:P") is True
        assert cron_owner_matches("cron:P:agentA", "cron:P") is True
        assert cron_owner_matches("cron:P:1f0e-uuid", "cron:P") is True
        assert cron_owner_matches("cron:P", "cron:P:agentA") is True
        assert cron_owner_matches("cron:P:agentA", "cron:P:agentB") is True
        # Different principals, and cron vs non-cron, never fold together.
        assert cron_owner_matches("cron:P", "cron:Q") is False
        assert cron_owner_matches("cron:P", "dashboard:chat-1-100") is False
        assert cron_owner_matches("dashboard:chat-1-100", "cron:P") is False
        # Non-cron owners keep ONE spelling each and compare exactly.
        assert cron_owner_matches("dashboard:chat-1-100", "dashboard:chat-1-100") is True
        assert cron_owner_matches("dashboard:chat-1-100", "dashboard:chat-1-101") is False

    @pytest.mark.asyncio
    async def test_an_agent_sequence_child_is_released_with_its_retired_parent(self, tmp_path):
        """``cron:<parent>:<agent>`` is a DURABLE spelling of the parent's key."""
        crons = CronService(base_dir=tmp_path)
        child = crons.add_job(
            "follow-up", "ping", every_secs=3600, session_key="cron:deadbeef:researcher"
        )
        state = _make_state({})
        state.crons = crons

        # The delete funnel only ever holds the two-segment form.
        await _remove_slot_for_history_key(
            state, "cron_deadbeef", exact_owner_keys=("cron:deadbeef",)
        )

        assert CronService(base_dir=tmp_path).get_job(child.id).session_key == ""

    @pytest.mark.asyncio
    async def test_a_stateless_run_child_is_released_with_its_retired_parent(self, tmp_path):
        """``cron:<parent>:<run id>`` is the ephemeral spelling of the same key."""
        crons = CronService(base_dir=tmp_path)
        child = crons.add_job(
            "follow-up",
            "ping",
            every_secs=3600,
            session_key="cron:deadbeef:3f2a1c88-0000-4000-8000-000000000001",
        )
        state = _make_state({})
        state.crons = crons

        await _remove_slot_for_history_key(
            state, "cron_deadbeef", exact_owner_keys=("cron:deadbeef",)
        )

        assert CronService(base_dir=tmp_path).get_job(child.id).session_key == ""

    @pytest.mark.asyncio
    async def test_a_live_parent_keeps_its_three_segment_children_owned(self, tmp_path):
        """Spelling tolerance must not outrun the liveness check."""
        crons = CronService(base_dir=tmp_path)
        owner = crons.add_job("nightly", "check the queue", every_secs=3600)
        child = crons.add_job(
            "follow-up", "ping", every_secs=3600, session_key=f"cron:{owner.id}:researcher"
        )
        state = _make_state({})
        state.crons = crons

        await _remove_slot_for_history_key(
            state, f"cron_{owner.id}", exact_owner_keys=(f"cron:{owner.id}",)
        )

        assert CronService(base_dir=tmp_path).get_job(child.id).session_key == (
            f"cron:{owner.id}:researcher"
        )

    @pytest.mark.asyncio
    async def test_another_crons_children_are_left_alone(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        # Both parents must really exist: the add guard refuses to stamp an owner
        # whose cron is absent (see TestAChildCannotBeBornUnderADeadParent), and
        # the survivor here has to be a LIVE, stable principal to stay owned.
        mine_parent = crons.add_job(
            "mine-parent", "sweep", every_secs=3600, agent_sequence=["agentA", "agentB"]
        )
        theirs_parent = crons.add_job(
            "theirs-parent", "sweep", every_secs=3600, agent_sequence=["agentA", "agentB"]
        )
        mine = crons.add_job(
            "mine", "ping", every_secs=3600, session_key=f"cron:{mine_parent.id}:agentA"
        )
        theirs = crons.add_job(
            "theirs", "ping", every_secs=3600, session_key=f"cron:{theirs_parent.id}:agentA"
        )
        assert crons.remove_job(mine_parent.id, actor="cli", source="cli") is True
        state = _make_state({})
        state.crons = crons

        await _remove_slot_for_history_key(
            state, f"cron_{mine_parent.id}", exact_owner_keys=(f"cron:{mine_parent.id}",)
        )

        reloaded = CronService(base_dir=tmp_path)
        assert reloaded.get_job(mine.id).session_key == ""
        assert reloaded.get_job(theirs.id).session_key == f"cron:{theirs_parent.id}:agentA"

    @pytest.mark.asyncio
    async def test_the_stranded_warning_names_three_segment_children(self, caplog):
        """A release that cannot land must still name the child it could not free."""
        crons = _BusyCrons("76ef369f", "cron:deadbeef:researcher")
        state = _make_state({})
        state.crons = crons

        with caplog.at_level(logging.WARNING):
            await _remove_slot_for_history_key(
                state, "cron_deadbeef", exact_owner_keys=("cron:deadbeef",)
            )

        assert crons.release_attempts == _CRON_RELEASE_ATTEMPTS
        assert "76ef369f" in caplog.text
        assert "FAILED" in caplog.text


class TestRemovingACronReleasesItsChildren:
    """Removing a cron retires its principal, so its children must be released.

    The history-delete funnel deliberately SKIPS a live cron owner, so a
    transcript deleted BEFORE the cron is removed leaves nothing behind to notice
    later — the removal itself is the only place that can close the gap. Without
    the cascade the child keeps firing under a key no session can present:
    `cron_list` omits it, `cron_update` / `cron_remove` answer "job not found".
    """

    def _parent_and_child(self, tmp_path, child_owner_suffix: str = ""):
        crons = CronService(base_dir=tmp_path)
        parent = crons.add_job("nightly", "check the queue", every_secs=3600)
        child = crons.add_job(
            "follow-up",
            "ping",
            every_secs=3600,
            session_key=f"cron:{parent.id}{child_owner_suffix}",
        )
        return crons, parent, child

    @pytest.mark.asyncio
    async def test_transcript_delete_then_parent_removal_releases_the_child(self, tmp_path):
        """The exact ordering the funnel cannot cover on its own."""
        crons, parent, child = self._parent_and_child(tmp_path)
        state = _make_state({})
        state.crons = crons

        # Step 1: the cron's own transcript is deleted while the cron is LIVE.
        # The funnel correctly leaves the child owned.
        await _remove_slot_for_history_key(
            state, f"cron_{parent.id}", exact_owner_keys=(f"cron:{parent.id}",)
        )
        assert CronService(base_dir=tmp_path).get_job(child.id).session_key == (
            f"cron:{parent.id}"
        )

        # Step 2: the cron is removed. Its principal is now retired.
        assert crons.remove_job(parent.id, actor="cli", source="cli") is True

        assert CronService(base_dir=tmp_path).get_job(child.id).session_key == ""

    def test_remove_job_releases_a_three_segment_child(self, tmp_path):
        crons, parent, child = self._parent_and_child(tmp_path, ":researcher")

        assert crons.remove_job(parent.id, actor="cli", source="cli") is True

        assert CronService(base_dir=tmp_path).get_job(child.id).session_key == ""

    def test_batch_removal_releases_children(self, tmp_path):
        """`cron_remove_all` lands in the batch core, not the single-job one."""
        crons, parent, child = self._parent_and_child(tmp_path)
        removed, missing = crons.remove_jobs_sync([parent.id], actor="mcp", source="mcp")

        assert removed == [parent.id] and missing == []
        assert CronService(base_dir=tmp_path).get_job(child.id).session_key == ""

    def test_app_owner_teardown_releases_children(self, tmp_path):
        """An app's cron can own jobs outside the app; uninstall retires it."""
        crons = CronService(base_dir=tmp_path)
        parent = crons.add_job("app-nightly", "sweep", every_secs=3600, created_by="app:demo")
        child = crons.add_job(
            "follow-up", "ping", every_secs=3600, session_key=f"cron:{parent.id}"
        )

        assert crons.remove_jobs_by_owner_sync("app:demo") == [parent.id]

        assert CronService(base_dir=tmp_path).get_job(child.id).session_key == ""

    def test_deferred_one_shot_removal_releases_children(self, tmp_path):
        """A Done()/delete_after_run self-removal deferred to a tick still cascades."""
        crons, parent, child = self._parent_and_child(tmp_path)
        crons.defer_removal(parent.id)

        with crons._file_lock():
            crons._sync()
            drained = crons._drain_pending_removals_locked()

        assert drained == [parent.id]
        assert CronService(base_dir=tmp_path).get_job(child.id).session_key == ""

    def test_removal_leaves_an_unrelated_jobs_owner_alone(self, tmp_path):
        crons, parent, child = self._parent_and_child(tmp_path)
        other = crons.add_job(
            "theirs", "ping", every_secs=3600, session_key="dashboard:chat-9-999"
        )
        sibling_cron = crons.add_job("sibling", "tick", every_secs=3600)
        sibling_child = crons.add_job(
            "sibling-follow-up", "ping", every_secs=3600, session_key=f"cron:{sibling_cron.id}"
        )

        assert crons.remove_job(parent.id, actor="cli", source="cli") is True

        reloaded = CronService(base_dir=tmp_path)
        assert reloaded.get_job(child.id).session_key == ""
        assert reloaded.get_job(other.id).session_key == "dashboard:chat-9-999"
        assert reloaded.get_job(sibling_child.id).session_key == f"cron:{sibling_cron.id}"

    def test_a_failed_save_rolls_the_child_release_back(self, tmp_path, monkeypatch):
        crons, parent, child = self._parent_and_child(tmp_path)

        def _boom() -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(crons, "_save", _boom)

        with pytest.raises(OSError):
            crons.remove_job(parent.id, actor="cli", source="cli")

        assert crons.get_job(child.id).session_key == f"cron:{parent.id}"
        monkeypatch.undo()
        reloaded = CronService(base_dir=tmp_path)
        assert reloaded.get_job(parent.id) is not None
        assert reloaded.get_job(child.id).session_key == f"cron:{parent.id}"


class TestOnlyASTABLELiveKeyRetainsOwnership:
    """Existence of the parent row is not enough to keep a child owned.

    A stateless job mints ``cron:<job id>:<uuid4>`` fresh per fire, so the key its
    last run stamped on a child is ALREADY unpresentable while the parent is still
    scheduled. Retaining that ownership strands the child exactly as a removed
    parent would, and the shape of the key cannot tell you which case you are in:
    a durable sequential-agent key and an ephemeral per-run key are both three
    segments.
    """

    @pytest.mark.asyncio
    async def test_a_stateless_parents_child_is_released(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        parent = crons.add_job(
            "stateless-nightly", "sweep", every_secs=3600, persistent_session=False
        )
        assert cron_session_key_is_stable(parent) is False
        child = crons.add_job(
            "follow-up",
            "ping",
            every_secs=3600,
            session_key=f"cron:{parent.id}:3f2a1c88-0000-4000-8000-000000000001",
        )
        state = _make_state({})
        state.crons = crons

        await _remove_slot_for_history_key(
            state, f"cron_{parent.id}", exact_owner_keys=(f"cron:{parent.id}",)
        )

        assert CronService(base_dir=tmp_path).get_job(child.id).session_key == ""

    @pytest.mark.asyncio
    async def test_a_persistent_parents_child_stays_owned(self, tmp_path):
        """The positive control: a stable key is still presentable, so keep it."""
        crons = CronService(base_dir=tmp_path)
        parent = crons.add_job(
            "persistent-nightly", "sweep", every_secs=3600, persistent_session=True
        )
        assert cron_session_key_is_stable(parent) is True
        child = crons.add_job(
            "follow-up", "ping", every_secs=3600, session_key=f"cron:{parent.id}"
        )
        state = _make_state({})
        state.crons = crons

        await _remove_slot_for_history_key(
            state, f"cron_{parent.id}", exact_owner_keys=(f"cron:{parent.id}",)
        )

        assert CronService(base_dir=tmp_path).get_job(child.id).session_key == (
            f"cron:{parent.id}"
        )

    @pytest.mark.asyncio
    async def test_an_agent_sequence_parents_child_stays_owned(self, tmp_path):
        """A sequential-agent key is three segments but DURABLE, so keep it."""
        crons = CronService(base_dir=tmp_path)
        parent = crons.add_job(
            "seq-nightly",
            "sweep",
            every_secs=3600,
            persistent_session=False,
            agent_sequence=["researcher", "writer"],
        )
        assert cron_session_key_is_stable(parent) is True
        child = crons.add_job(
            "follow-up", "ping", every_secs=3600, session_key=f"cron:{parent.id}:researcher"
        )
        state = _make_state({})
        state.crons = crons

        await _remove_slot_for_history_key(
            state, f"cron_{parent.id}", exact_owner_keys=(f"cron:{parent.id}",)
        )

        assert CronService(base_dir=tmp_path).get_job(child.id).session_key == (
            f"cron:{parent.id}:researcher"
        )


class TestAChildCannotBeBornUnderADeadParent:
    """The add side closes the window the removal cascade cannot see.

    The cascade only releases children that existed when it scanned. A run of the
    parent still in flight can call ``cron_add`` between that scan and its own
    teardown, so the guard has to live in the locked ADD transaction — resolved
    against the same reloaded store, which is what makes the two cover each other.
    """

    def test_add_drops_an_owner_whose_cron_is_gone(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        parent = crons.add_job("nightly", "sweep", every_secs=3600)
        assert crons.remove_job(parent.id, actor="cli", source="cli") is True

        child = crons.add_job(
            "late-follow-up", "ping", every_secs=3600, session_key=f"cron:{parent.id}"
        )

        assert CronService(base_dir=tmp_path).get_job(child.id).session_key == ""

    def test_add_drops_a_three_segment_owner_whose_cron_is_gone(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        parent = crons.add_job("nightly", "sweep", every_secs=3600)
        assert crons.remove_job(parent.id, actor="cli", source="cli") is True

        child = crons.add_job(
            "late-follow-up",
            "ping",
            every_secs=3600,
            session_key=f"cron:{parent.id}:researcher",
        )

        assert CronService(base_dir=tmp_path).get_job(child.id).session_key == ""

    def test_add_keeps_an_owner_whose_cron_is_alive(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        parent = crons.add_job("nightly", "sweep", every_secs=3600)

        child = crons.add_job(
            "follow-up", "ping", every_secs=3600, session_key=f"cron:{parent.id}"
        )

        assert CronService(base_dir=tmp_path).get_job(child.id).session_key == (
            f"cron:{parent.id}"
        )

    def test_add_leaves_non_cron_owners_alone(self, tmp_path):
        """A dashboard or channel owner names no cron row to look up."""
        crons = CronService(base_dir=tmp_path)

        job = crons.add_job(
            "from-chat", "ping", every_secs=3600, session_key="dashboard:chat-1-100"
        )

        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == (
            "dashboard:chat-1-100"
        )


class _BusyCrons:
    """A cron store whose lock never frees, on both release surfaces.

    Answers ``CronStoreBusy`` for the batch release AND for a per-id
    ``adopt_job``, so the test discriminates on the funnel's BEHAVIOUR (does it
    retry, does it report) rather than on which method it happens to call.
    """

    def __init__(self, job_id: str, session_key: str, *, busy_for: int | None = None) -> None:
        self._job_id = job_id
        self._session_key = session_key
        self._busy_for = busy_for
        self.release_attempts = 0
        self.adopt_attempts = 0

    def list_jobs(self, include_disabled: bool = False) -> list[Any]:
        return [TestReleaseIsOwnerConditioned._StaleRow(self._job_id, self._session_key)]

    async def release_jobs_owned_by(self, owner_keys: Collection[str]) -> list[str]:
        self.release_attempts += 1
        if self._busy_for is not None and self.release_attempts > self._busy_for:
            self._session_key = ""
            return [self._job_id]
        raise CronStoreBusy("cron store lock held")

    def adopt_job(self, job_id: str, session_key: str) -> bool:
        self.adopt_attempts += 1
        raise CronStoreBusy("cron store lock held")


class TestReleaseSurvivesAndSurfacesLockContention:
    """``CronStoreBusy`` is ordinary lock contention, not a reason to give up.

    Nothing re-runs this funnel and the deleted session can never present its
    key again, so a dropped release strands the job permanently -- the exact
    state the release exists to prevent. Contention is retried, and a release
    that still will not land is reported loudly instead of swallowed.
    """

    @pytest.mark.asyncio
    async def test_transient_contention_is_retried_until_it_lands(self, caplog):
        crons = _BusyCrons("76ef369f", "dashboard:chat-1-100", busy_for=2)
        state = _make_state({})
        state.crons = crons

        with caplog.at_level(logging.WARNING):
            await _remove_slot_for_history_key(state, "dashboard_chat-1-100")

        assert crons.release_attempts == 3
        assert crons._session_key == ""
        assert "FAILED" not in caplog.text

    @pytest.mark.asyncio
    async def test_sustained_contention_is_surfaced_not_swallowed(self, caplog):
        crons = _BusyCrons("76ef369f", "dashboard:chat-1-100")
        state = _make_state({})
        state.crons = crons

        with caplog.at_level(logging.WARNING):
            await _remove_slot_for_history_key(state, "dashboard_chat-1-100")

        assert crons.release_attempts == 3
        # The warning has to name what is stranded and who owns it -- an operator
        # cannot release a job by hand from a message that names neither.
        assert "76ef369f" in caplog.text
        assert "dashboard:chat-1-100" in caplog.text
        assert "FAILED" in caplog.text


class _FakeTranscriptStore:
    """A conversation log whose metadata dies with the transcript.

    ``get_metadata`` answers ``{}`` once the row is unlinked, exactly as the real
    store does when the file is gone -- so a funnel that reads
    ``linked_session_key`` after the delete gets nothing, which is the defect
    these tests guard.
    """

    def __init__(self, key: str, linked_session_key: str) -> None:
        self._meta: dict[str, dict] = {key: {"linked_session_key": linked_session_key}}
        self.calls: list[str] = []

    def get_metadata(self, key: str) -> dict:
        self.calls.append(f"get_metadata:{key}")
        return dict(self._meta.get(key, {}))

    def delete_session(self, key: str, *, skip_pinned: bool = False) -> bool:
        self.calls.append(f"delete_session:{key}")
        existed = key in self._meta
        self._meta.pop(key, None)
        return existed


class TestSlotlessChannelSessionReleasesItsExactOwnerKey:
    """A channel session's cron is owned under the channel's EXACT key.

    After a gateway restart such a session has no live slot, so the funnel's
    ``effective_session_key`` branch never runs and the derived candidates are
    only the folded transcript name and a ``dashboard:`` spelling of it --
    neither of which a channel job is stamped with. The exact key survives only
    in the transcript's ``linked_session_key``, which the delete itself destroys,
    so it has to be read first.
    """

    @pytest.mark.asyncio
    async def test_delete_releases_the_cron_and_reads_the_key_before_unlinking(self, tmp_path):
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        log = _FakeTranscriptStore(history_key, channel_key)
        # Post-restart: the transcript is on disk, the slot is not.
        state = _make_state({})
        state.crons = crons
        state.conversation_log = log
        request = MagicMock(spec=web.Request)
        request.app = {"state": state}
        request.match_info = {"key": history_key}

        resp = await api_session_delete(request)

        assert json.loads(resp.body.decode("utf-8")) == {"ok": True}
        assert log.calls.index(f"get_metadata:{history_key}") < log.calls.index(
            f"delete_session:{history_key}"
        )
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == ""

    @pytest.mark.asyncio
    async def test_bulk_clear_releases_the_same_cron(self, tmp_path):
        channel_key = "slack:1785370133.085469"
        history_key = "slack_1785370133.085469"
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job("channel", "ping", every_secs=3600, session_key=channel_key)
        log = _FakeTranscriptStore(history_key, channel_key)
        log.list_sessions = lambda: [{"key": history_key}]  # type: ignore[method-assign]
        log.get_metadata_status = lambda key: (dict(log._meta.get(key, {})), True)  # type: ignore[method-assign]
        state = _make_state({})
        state.crons = crons
        state.conversation_log = log
        request = MagicMock(spec=web.Request)
        request.app = {"state": state}

        resp = await api_sessions_clear(request)

        assert json.loads(resp.body.decode("utf-8"))["cleared"] == 1
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == ""
