"""Tests for basic work execution, error handling, and lifecycle."""

import asyncio
import time

import runcue
from runcue.models import WorkState


class TestBasicExecution:
    """Tests for basic work execution."""

    async def test_work_executes(self):
        """Submitted work gets executed."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min", concurrent=10)

        executed = []

        @cue.task("process", uses="api")
        def process(work):
            executed.append(work.id)
            return {"done": True}

        cue.start()
        work_id = await cue.submit("process", params={})

        await asyncio.sleep(0.1)
        await cue.stop()

        assert work_id in executed

    async def test_async_handler_works(self):
        """Async handlers are properly awaited."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        executed = []

        @cue.task("async_task", uses="api")
        async def async_task(work):
            await asyncio.sleep(0.01)
            executed.append(work.id)
            return {"done": True}

        cue.start()
        work_id = await cue.submit("async_task", params={})

        await asyncio.sleep(0.1)
        await cue.stop()

        assert work_id in executed

    async def test_work_state_transitions(self):
        """Work transitions through PENDING -> RUNNING -> COMPLETED."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        states_seen = []

        @cue.task("track", uses="api")
        async def track(work):
            states_seen.append(work.state)
            await asyncio.sleep(0.05)
            return {}

        work_id = await cue.submit("track", params={})
        
        # Before start: should be PENDING
        work = await cue.get(work_id)
        assert work.state == WorkState.PENDING

        cue.start()
        await asyncio.sleep(0.02)
        
        # During execution: should be RUNNING
        work = await cue.get(work_id)
        assert work.state == WorkState.RUNNING

        await asyncio.sleep(0.1)
        await cue.stop()

        # After completion: should be COMPLETED
        work = await cue.get(work_id)
        assert work.state == WorkState.COMPLETED

    async def test_handler_receives_work_object(self):
        """Handler receives work unit with correct fields."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        received_work = []

        @cue.task("inspect", uses="api")
        def inspect(work):
            received_work.append(work)
            return {}

        cue.start()
        work_id = await cue.submit("inspect", params={"x": 42, "y": "hello"})

        await asyncio.sleep(0.1)
        await cue.stop()

        assert len(received_work) == 1
        work = received_work[0]
        assert work.id == work_id
        assert work.task == "inspect"
        assert work.params == {"x": 42, "y": "hello"}


class TestErrorHandling:
    """Tests for exception handling."""

    async def test_handler_exception_marks_failed(self):
        """Handler exceptions mark work as FAILED."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        @cue.task("failing", uses="api")
        def failing(work):
            raise ValueError("oops")

        cue.start()
        work_id = await cue.submit("failing", params={})

        await asyncio.sleep(0.1)
        await cue.stop()

        work = await cue.get(work_id)
        assert work.state == WorkState.FAILED
        assert "oops" in work.error

    async def test_async_handler_exception_marks_failed(self):
        """Async handler exceptions mark work as FAILED."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        @cue.task("failing_async", uses="api")
        async def failing_async(work):
            await asyncio.sleep(0.01)
            raise RuntimeError("async failure")

        cue.start()
        work_id = await cue.submit("failing_async", params={})

        await asyncio.sleep(0.1)
        await cue.stop()

        work = await cue.get(work_id)
        assert work.state == WorkState.FAILED
        assert "async failure" in work.error


class TestLifecycle:
    """Tests for start/stop lifecycle."""

    async def test_stop_waits_for_running(self):
        """stop() waits for running work to complete."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        completed = []

        @cue.task("slow", uses="api")
        async def slow(work):
            await asyncio.sleep(0.2)
            completed.append(work.id)
            return {}

        cue.start()
        work_id = await cue.submit("slow", params={})
        await asyncio.sleep(0.05)  # Let it start
        await cue.stop()  # Should wait for completion

        assert work_id in completed

    async def test_stop_timeout_abandons(self):
        """stop(timeout) abandons work after timeout."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        @cue.task("very_slow", uses="api")
        async def very_slow(work):
            await asyncio.sleep(10)
            return {}

        cue.start()
        await cue.submit("very_slow", params={})
        await asyncio.sleep(0.05)

        start = time.time()
        await cue.stop(timeout=0.1)
        elapsed = time.time() - start

        assert elapsed < 1  # Didn't wait for the 10s task

    async def test_submit_before_start_queues(self):
        """Work submitted before start() is queued and runs when started."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        executed = []

        @cue.task("task", uses="api")
        def task(work):
            executed.append(work.id)
            return {}

        # Submit before start
        work_id = await cue.submit("task", params={})
        work = await cue.get(work_id)
        assert work.state == WorkState.PENDING
        assert work_id not in executed

        # Now start
        cue.start()
        await asyncio.sleep(0.1)
        await cue.stop()

        assert work_id in executed

    async def test_multiple_work_executes(self):
        """Multiple work units all get executed."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min", concurrent=10)

        executed = []

        @cue.task("process", uses="api")
        def process(work):
            executed.append(work.id)
            return {}

        cue.start()
        
        ids = []
        for i in range(5):
            work_id = await cue.submit("process", params={"i": i})
            ids.append(work_id)

        await asyncio.sleep(0.2)
        await cue.stop()

        assert len(executed) == 5
        for work_id in ids:
            assert work_id in executed


class TestCancel:
    """Tests for cancelling work during execution."""

    async def test_cancel_pending_work_before_start(self):
        """Can cancel pending work before orchestrator starts."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        executed = []

        @cue.task("task", uses="api")
        def task(work):
            executed.append(work.id)
            return {}

        # Submit work but don't start
        work_id = await cue.submit("task", params={})
        
        # Cancel it while still pending
        result = await cue.cancel(work_id)
        assert result is True
        
        work = await cue.get(work_id)
        assert work.state == WorkState.CANCELLED

        # Start and verify it doesn't run
        cue.start()
        await asyncio.sleep(0.1)
        await cue.stop()

        assert work_id not in executed


class TestWorkResult:
    """Tests for work results."""

    async def test_work_result_stored(self):
        """Handler return value is stored in work.result."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        @cue.task("compute", uses="api")
        def compute(work):
            return {"sum": work.params["a"] + work.params["b"]}

        cue.start()
        work_id = await cue.submit("compute", params={"a": 10, "b": 32})

        await asyncio.sleep(0.1)
        await cue.stop()

        work = await cue.get(work_id)
        assert work.state == WorkState.COMPLETED
        assert work.result == {"sum": 42}

    async def test_completed_at_timestamp_set(self):
        """completed_at is set when work finishes."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        @cue.task("task", uses="api")
        def task(work):
            return {}

        cue.start()
        work_id = await cue.submit("task", params={})

        await asyncio.sleep(0.1)
        await cue.stop()

        work = await cue.get(work_id)
        assert work.completed_at is not None
        assert work.completed_at >= work.started_at

