"""Phase 4: Readiness Callback tests."""

import asyncio

import pytest

import runcue
from runcue.models import WorkState


class TestIsReadyBlocking:
    """Tests for is_ready callback blocking work execution."""

    async def test_is_ready_blocks_work(self):
        """Work stays pending until is_ready returns True."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        ready_flag = False

        @cue.task("needs_flag", uses="api")
        def needs_flag(work):
            return {"done": True}

        @cue.is_ready
        def is_ready(work):
            return ready_flag

        cue.start()
        work_id = await cue.submit("needs_flag", params={})
        await asyncio.sleep(0.1)

        work = await cue.get(work_id)
        assert work.state == WorkState.PENDING  # Blocked

        ready_flag = True
        await asyncio.sleep(0.15)

        work = await cue.get(work_id)
        assert work.state == WorkState.COMPLETED  # Now runs

        await cue.stop()

    async def test_no_is_ready_callback_allows_all(self):
        """Without is_ready callback, all work runs immediately."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        @cue.task("task", uses="api")
        def task(work):
            return {}

        # No @cue.is_ready registered

        cue.start()
        work_id = await cue.submit("task", params={})
        await asyncio.sleep(0.1)
        await cue.stop()

        work = await cue.get(work_id)
        assert work.state == WorkState.COMPLETED


class TestIsReadyExceptions:
    """Tests for exception handling in is_ready callback."""

    async def test_is_ready_exception_treated_as_not_ready(self):
        """Exceptions in is_ready are caught; work stays pending."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        @cue.task("task", uses="api")
        def task(work):
            return {}

        @cue.is_ready
        def is_ready(work):
            raise RuntimeError("check failed")

        cue.start()
        work_id = await cue.submit("task", params={})
        await asyncio.sleep(0.1)

        work = await cue.get(work_id)
        assert work.state == WorkState.PENDING  # Not run due to exception

        await cue.stop()


class TestIsReadyPerWork:
    """Tests for is_ready checking each work unit independently."""

    async def test_is_ready_checked_per_work(self):
        """is_ready is called for each work unit independently."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        ready_keys = set()

        @cue.task("task", uses="api")
        def task(work):
            return {}

        @cue.is_ready
        def is_ready(work):
            return work.params.get("key") in ready_keys

        cue.start()

        # Submit two work units
        id1 = await cue.submit("task", params={"key": "a"})
        id2 = await cue.submit("task", params={"key": "b"})
        await asyncio.sleep(0.1)

        # Both should be pending
        assert (await cue.get(id1)).state == WorkState.PENDING
        assert (await cue.get(id2)).state == WorkState.PENDING

        # Make only "a" ready
        ready_keys.add("a")
        await asyncio.sleep(0.15)

        # Only id1 should complete
        assert (await cue.get(id1)).state == WorkState.COMPLETED
        assert (await cue.get(id2)).state == WorkState.PENDING

        await cue.stop()

    async def test_is_ready_with_multiple_tasks(self):
        """is_ready can differentiate by task type."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        executed = []

        @cue.task("always_ready", uses="api")
        def always_ready(work):
            executed.append(("always_ready", work.id))
            return {}

        @cue.task("never_ready", uses="api")
        def never_ready(work):
            executed.append(("never_ready", work.id))
            return {}

        @cue.is_ready
        def is_ready(work):
            if work.task == "always_ready":
                return True
            if work.task == "never_ready":
                return False
            return True

        cue.start()

        id1 = await cue.submit("always_ready", params={})
        id2 = await cue.submit("never_ready", params={})
        await asyncio.sleep(0.1)

        await cue.stop()

        # Only always_ready should have executed
        assert any(task == "always_ready" for task, _ in executed)
        assert not any(task == "never_ready" for task, _ in executed)

        work1 = await cue.get(id1)
        work2 = await cue.get(id2)
        assert work1.state == WorkState.COMPLETED
        assert work2.state == WorkState.PENDING

