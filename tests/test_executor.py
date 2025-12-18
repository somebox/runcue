"""Phase 3: Async executor tests."""

import asyncio

import pytest

import runcue
from runcue import WorkState


@pytest.fixture
async def cue():
    """Create a Cue instance and ensure cleanup."""
    c = runcue.Cue(":memory:")
    yield c
    if c._running:
        await c.stop()
    await c.close()


class TestOrchestratorLifecycle:
    """Test start/stop lifecycle."""

    async def test_start_is_nonblocking(self):
        """start() returns immediately without blocking."""
        cue = runcue.Cue(":memory:")
        cue.service("api", rate_limit=(10, 60))
        
        @cue.task("process", services=["api"])
        async def process(work):
            await asyncio.sleep(10)  # Would block if start() waited
            return {}
        
        # This should return immediately
        cue.start()
        assert cue._running is True
        
        await cue.stop()

    async def test_stop_waits_for_active_work(self):
        """stop() waits for running work to complete."""
        cue = runcue.Cue(":memory:")
        cue.service("api", rate_limit=(10, 60))
        
        completed = []
        
        @cue.task("slow", services=["api"])
        async def slow(work):
            await asyncio.sleep(0.1)
            completed.append(work.id)
            return {"done": True}
        
        cue.start()
        work_id = await cue.submit("slow")
        
        # Give it a moment to start
        await asyncio.sleep(0.05)
        
        # Stop should wait for completion
        await cue.stop()
        
        assert work_id in completed

    async def test_double_start_is_safe(self):
        """Calling start() twice doesn't create duplicate loops."""
        cue = runcue.Cue(":memory:")
        cue.service("api", rate_limit=(10, 60))
        
        @cue.task("process", services=["api"])
        async def process(work):
            return {}
        
        cue.start()
        cue.start()  # Should be no-op
        
        assert cue._running is True
        
        await cue.stop()

    async def test_double_stop_is_safe(self):
        """Calling stop() twice doesn't raise."""
        cue = runcue.Cue(":memory:")
        cue.service("api", rate_limit=(10, 60))
        
        @cue.task("process", services=["api"])
        async def process(work):
            return {}
        
        cue.start()
        await cue.stop()
        await cue.stop()  # Should be no-op


class TestWorkExecution:
    """Test work unit execution."""

    async def test_work_executes_and_completes(self):
        """Submitted work is executed and marked completed."""
        cue = runcue.Cue(":memory:")
        cue.service("api", rate_limit=(100, 60))
        
        executed = []
        
        @cue.task("process", services=["api"])
        async def process(work):
            executed.append(work.id)
            return {"processed": True, "target": work.target}
        
        cue.start()
        work_id = await cue.submit("process", target="test.txt")
        
        # Wait for execution
        await asyncio.sleep(0.1)
        await cue.stop()
        
        # Verify executed
        assert work_id in executed
        
        # Verify state updated
        work = await cue.get(work_id)
        assert work.state == WorkState.COMPLETED
        assert work.result == {"processed": True, "target": "test.txt"}
        assert work.started_at is not None
        assert work.completed_at is not None

    async def test_handler_receives_work_unit(self):
        """Handler receives the WorkUnit with all fields."""
        cue = runcue.Cue(":memory:")
        cue.service("api", rate_limit=(100, 60))
        
        received_work = []
        
        @cue.task("inspect", services=["api"])
        async def inspect(work):
            received_work.append(work)
            return {}
        
        cue.start()
        await cue.submit(
            "inspect",
            target="/path/to/file",
            params={"key": "value"},
        )
        
        await asyncio.sleep(0.1)
        await cue.stop()
        
        assert len(received_work) == 1
        work = received_work[0]
        assert work.target == "/path/to/file"
        assert work.params == {"key": "value"}
        assert work.task_type == "inspect"

    async def test_multiple_work_units_execute(self):
        """Multiple submitted work units all execute."""
        cue = runcue.Cue(":memory:")
        cue.service("api", rate_limit=(100, 60))
        
        executed = []
        
        @cue.task("process", services=["api"])
        async def process(work):
            executed.append(work.id)
            return {}
        
        cue.start()
        
        ids = []
        for i in range(5):
            work_id = await cue.submit("process", target=f"file_{i}.txt")
            ids.append(work_id)
        
        # Wait for all to execute (with timeout-based check)
        for _ in range(50):  # Up to 0.5 seconds
            if len(executed) == len(ids):
                break
            await asyncio.sleep(0.01)
        
        await cue.stop()
        
        # All should have executed
        assert set(executed) == set(ids)
        
        # All should be completed
        for work_id in ids:
            work = await cue.get(work_id)
            assert work.state == WorkState.COMPLETED


class TestErrorHandling:
    """Test handler error handling."""

    async def test_handler_exception_marks_failed(self):
        """Handler exception marks work as failed."""
        cue = runcue.Cue(":memory:")
        cue.service("api", rate_limit=(100, 60))
        
        @cue.task("failing", services=["api"])
        async def failing(work):
            raise ValueError("Something went wrong")
        
        cue.start()
        work_id = await cue.submit("failing")
        
        await asyncio.sleep(0.1)
        await cue.stop()
        
        work = await cue.get(work_id)
        assert work.state == WorkState.FAILED
        assert "Something went wrong" in work.error
        assert work.completed_at is not None

    async def test_sync_handler_works(self):
        """Non-async handlers also work."""
        cue = runcue.Cue(":memory:")
        cue.service("api", rate_limit=(100, 60))
        
        @cue.task("sync_task", services=["api"])
        def sync_handler(work):
            return {"sync": True}
        
        cue.start()
        work_id = await cue.submit("sync_task")
        
        await asyncio.sleep(0.1)
        await cue.stop()
        
        work = await cue.get(work_id)
        assert work.state == WorkState.COMPLETED
        assert work.result == {"sync": True}

    async def test_non_dict_result_wrapped(self):
        """Non-dict results are wrapped in a dict."""
        cue = runcue.Cue(":memory:")
        cue.service("api", rate_limit=(100, 60))
        
        @cue.task("returns_string", services=["api"])
        async def returns_string(work):
            return "just a string"
        
        cue.start()
        work_id = await cue.submit("returns_string")
        
        await asyncio.sleep(0.1)
        await cue.stop()
        
        work = await cue.get(work_id)
        assert work.state == WorkState.COMPLETED
        assert work.result == {"result": "just a string"}


class TestFIFOOrdering:
    """Test that work executes in FIFO order (for now)."""

    async def test_fifo_execution_order(self):
        """Work executes in submission order."""
        cue = runcue.Cue(":memory:")
        cue.service("api", rate_limit=(100, 60))
        
        execution_order = []
        
        @cue.task("ordered", services=["api"])
        async def ordered(work):
            execution_order.append(work.target)
            return {}
        
        cue.start()
        
        # Submit in specific order
        for i in range(5):
            await cue.submit("ordered", target=f"item_{i}")
        
        await asyncio.sleep(0.3)
        await cue.stop()
        
        # Should execute in FIFO order
        assert execution_order == ["item_0", "item_1", "item_2", "item_3", "item_4"]

