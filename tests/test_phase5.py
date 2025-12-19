"""Phase 5: Staleness Callback tests."""

import asyncio

import pytest

import runcue
from runcue.models import WorkState


class TestIsStaleSkipping:
    """Tests for is_stale callback skipping work."""

    async def test_is_stale_skips_work(self):
        """Work is skipped when is_stale returns False."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        executed = []
        stale = True

        @cue.task("extract", uses="api")
        def extract(work):
            executed.append(work.id)
            return {}

        @cue.is_stale
        def is_stale(work):
            return stale

        cue.start()

        # First run: stale=True, should execute
        work_id = await cue.submit("extract", params={})
        await asyncio.sleep(0.1)
        assert work_id in executed

        # Second run: stale=False, should skip
        stale = False
        executed.clear()
        work_id2 = await cue.submit("extract", params={})
        await asyncio.sleep(0.1)
        assert work_id2 not in executed

        await cue.stop()

    async def test_no_is_stale_callback_runs_all(self):
        """Without is_stale callback, all ready work runs."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        executed = []

        @cue.task("task", uses="api")
        def task(work):
            executed.append(work.id)
            return {}

        # No @cue.is_stale registered

        cue.start()
        work_id = await cue.submit("task", params={})
        await asyncio.sleep(0.1)
        await cue.stop()

        assert work_id in executed


class TestIsStaleExceptions:
    """Tests for exception handling in is_stale callback."""

    async def test_is_stale_exception_treated_as_stale(self):
        """Exceptions in is_stale are caught; work runs (treat as stale)."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        executed = []

        @cue.task("task", uses="api")
        def task(work):
            executed.append(work.id)
            return {}

        @cue.is_stale
        def is_stale(work):
            raise RuntimeError("check failed")

        cue.start()
        work_id = await cue.submit("task", params={})
        await asyncio.sleep(0.1)
        await cue.stop()

        # Work should run despite exception (exception = treat as stale)
        assert work_id in executed


class TestOnSkipCallback:
    """Tests for on_skip callback when work is skipped."""

    async def test_on_skip_called(self):
        """on_skip callback fires when work is skipped."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        skipped = []

        @cue.task("task", uses="api")
        def task(work):
            return {}

        @cue.is_stale
        def is_stale(work):
            return False  # Not stale, should skip

        @cue.on_skip
        def on_skip(work):
            skipped.append(work.id)

        cue.start()
        work_id = await cue.submit("task", params={})
        await asyncio.sleep(0.1)
        await cue.stop()

        assert work_id in skipped

    async def test_on_skip_not_called_when_executed(self):
        """on_skip callback does not fire when work is executed."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        skipped = []
        executed = []

        @cue.task("task", uses="api")
        def task(work):
            executed.append(work.id)
            return {}

        @cue.is_stale
        def is_stale(work):
            return True  # Stale, should run

        @cue.on_skip
        def on_skip(work):
            skipped.append(work.id)

        cue.start()
        work_id = await cue.submit("task", params={})
        await asyncio.sleep(0.1)
        await cue.stop()

        assert work_id in executed
        assert work_id not in skipped


class TestSkippedWorkState:
    """Tests for skipped work state and timestamps."""

    async def test_skipped_work_marked_completed(self):
        """Skipped work should have COMPLETED state and completed_at timestamp."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        @cue.task("task", uses="api")
        def task(work):
            return {}

        @cue.is_stale
        def is_stale(work):
            return False  # Not stale, should skip

        cue.start()
        work_id = await cue.submit("task", params={})
        await asyncio.sleep(0.1)
        await cue.stop()

        work = await cue.get(work_id)
        assert work.state == WorkState.COMPLETED
        assert work.completed_at is not None

    async def test_skipped_work_not_started(self):
        """Skipped work should not have started_at timestamp."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        @cue.task("task", uses="api")
        def task(work):
            return {}

        @cue.is_stale
        def is_stale(work):
            return False  # Not stale, should skip

        cue.start()
        work_id = await cue.submit("task", params={})
        await asyncio.sleep(0.1)
        await cue.stop()

        work = await cue.get(work_id)
        # Skipped work was never started
        assert work.started_at is None
        assert work.result is None


class TestIsStalePerWork:
    """Tests for is_stale checking each work unit independently."""

    async def test_is_stale_checked_per_work(self):
        """is_stale is called for each work unit independently."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        stale_keys = {"a"}  # Only "a" is stale
        executed = []

        @cue.task("task", uses="api")
        def task(work):
            executed.append(work.params["key"])
            return {}

        @cue.is_stale
        def is_stale(work):
            return work.params.get("key") in stale_keys

        cue.start()

        id1 = await cue.submit("task", params={"key": "a"})
        id2 = await cue.submit("task", params={"key": "b"})
        await asyncio.sleep(0.1)
        await cue.stop()

        # Only "a" should execute (stale), "b" should be skipped
        assert "a" in executed
        assert "b" not in executed

        work1 = await cue.get(id1)
        work2 = await cue.get(id2)
        assert work1.state == WorkState.COMPLETED
        assert work2.state == WorkState.COMPLETED  # Skipped but marked completed

