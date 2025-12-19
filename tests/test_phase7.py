"""Phase 7: Priority Callback tests."""

import asyncio
import time

import runcue
from runcue.models import PriorityContext


class TestPriorityOrdering:
    """Tests for priority-based work ordering."""

    async def test_priority_ordering(self):
        """Higher priority work runs first."""
        cue = runcue.Cue()
        cue.service("api", concurrent=1, rate="100/min")

        order = []

        @cue.task("task", uses="api")
        async def task(work):
            order.append(work.params["name"])
            await asyncio.sleep(0.01)
            return {}

        @cue.priority
        def prioritize(ctx):
            return ctx.work.params.get("priority", 0.5)

        cue.start()

        await cue.submit("task", params={"name": "low", "priority": 0.1})
        await cue.submit("task", params={"name": "high", "priority": 0.9})
        await cue.submit("task", params={"name": "med", "priority": 0.5})

        await asyncio.sleep(0.5)
        await cue.stop()

        assert order[0] == "high"

    async def test_priority_stable_for_equal_values(self):
        """Work with equal priority maintains FIFO order."""
        cue = runcue.Cue()
        cue.service("api", concurrent=1, rate="100/min")

        order = []

        @cue.task("task", uses="api")
        async def task(work):
            order.append(work.params["name"])
            await asyncio.sleep(0.01)
            return {}

        @cue.priority
        def prioritize(ctx):
            return 0.5  # Same priority for all

        cue.start()

        # Submit in order: a, b, c
        await cue.submit("task", params={"name": "a"})
        await cue.submit("task", params={"name": "b"})
        await cue.submit("task", params={"name": "c"})

        await asyncio.sleep(0.5)
        await cue.stop()

        # Should maintain FIFO order
        assert order == ["a", "b", "c"]


class TestPriorityContext:
    """Tests for PriorityContext data passed to callback."""

    async def test_priority_context_has_wait_time(self):
        """PriorityContext includes wait_time."""
        cue = runcue.Cue()
        cue.service("api", concurrent=1, rate="100/min")

        wait_times = []

        @cue.task("task", uses="api")
        async def task(work):
            await asyncio.sleep(0.1)
            return {}

        @cue.priority
        def prioritize(ctx):
            wait_times.append(ctx.wait_time)
            return 0.5

        cue.start()
        await cue.submit("task", params={})
        await cue.submit("task", params={})

        await asyncio.sleep(0.5)
        await cue.stop()

        # Second task should have waited longer (checked multiple times)
        assert len(wait_times) >= 2

    async def test_priority_context_has_queue_depth(self):
        """PriorityContext includes queue_depth."""
        cue = runcue.Cue()
        cue.service("api", concurrent=1, rate="100/min")

        queue_depths = []

        @cue.task("task", uses="api")
        async def task(work):
            await asyncio.sleep(0.05)
            return {}

        @cue.priority
        def prioritize(ctx):
            queue_depths.append(ctx.queue_depth)
            return 0.5

        cue.start()
        
        # Submit 3 tasks
        await cue.submit("task", params={})
        await cue.submit("task", params={})
        await cue.submit("task", params={})

        await asyncio.sleep(0.4)
        await cue.stop()

        # Queue depth should decrease as work is processed
        # First check should see all 3, later checks see fewer
        assert 3 in queue_depths

    async def test_priority_context_has_work(self):
        """PriorityContext includes the work unit."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        seen_params = []

        @cue.task("task", uses="api")
        def task(work):
            return {}

        @cue.priority
        def prioritize(ctx):
            seen_params.append(ctx.work.params.get("key"))
            return 0.5

        cue.start()
        await cue.submit("task", params={"key": "value1"})
        await cue.submit("task", params={"key": "value2"})
        await asyncio.sleep(0.1)
        await cue.stop()

        assert "value1" in seen_params
        assert "value2" in seen_params


class TestNoPriorityCallback:
    """Tests for default behavior without priority callback."""

    async def test_no_priority_callback_uses_fifo(self):
        """Without priority callback, work runs in FIFO order."""
        cue = runcue.Cue()
        cue.service("api", concurrent=1, rate="100/min")

        order = []

        @cue.task("task", uses="api")
        async def task(work):
            order.append(work.params["name"])
            await asyncio.sleep(0.01)
            return {}

        # No @cue.priority registered

        cue.start()

        await cue.submit("task", params={"name": "first"})
        await cue.submit("task", params={"name": "second"})
        await cue.submit("task", params={"name": "third"})

        await asyncio.sleep(0.3)
        await cue.stop()

        # Default FIFO-like ordering (with starvation prevention)
        # First should definitely be first since it's oldest
        assert order[0] == "first"


class TestPriorityExceptions:
    """Tests for exception handling in priority callback."""

    async def test_priority_exception_uses_default(self):
        """Exceptions in priority callback use default priority."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        executed = []

        @cue.task("task", uses="api")
        def task(work):
            executed.append(work.id)
            return {}

        @cue.priority
        def prioritize(ctx):
            raise RuntimeError("callback error")

        cue.start()
        work_id = await cue.submit("task", params={})
        await asyncio.sleep(0.1)
        await cue.stop()

        # Work should still execute despite callback error
        assert work_id in executed


class TestPriorityClamping:
    """Tests for priority value clamping."""

    async def test_priority_clamped_to_range(self):
        """Priority values are clamped to 0.0-1.0."""
        cue = runcue.Cue()
        cue.service("api", concurrent=1, rate="100/min")

        order = []

        @cue.task("task", uses="api")
        async def task(work):
            order.append(work.params["name"])
            await asyncio.sleep(0.01)
            return {}

        @cue.priority
        def prioritize(ctx):
            # Return values outside 0-1 range
            return ctx.work.params.get("priority", 0.5)

        cue.start()

        # Even with extreme values, ordering should work
        await cue.submit("task", params={"name": "negative", "priority": -10})
        await cue.submit("task", params={"name": "very_high", "priority": 100})
        await cue.submit("task", params={"name": "normal", "priority": 0.5})

        await asyncio.sleep(0.3)
        await cue.stop()

        # very_high (clamped to 1.0) should run first
        assert order[0] == "very_high"


class TestPriorityModel:
    """Tests for PriorityContext model."""

    def test_priority_context_fields(self):
        """PriorityContext has expected fields."""
        from runcue.models import WorkUnit
        
        work = WorkUnit(
            id="test123",
            task="mytask",
            params={"key": "value"},
            created_at=time.time(),
        )
        
        ctx = PriorityContext(
            work=work,
            wait_time=1.5,
            queue_depth=10,
        )
        
        assert ctx.work.id == "test123"
        assert ctx.wait_time == 1.5
        assert ctx.queue_depth == 10

    def test_priority_context_exported(self):
        """PriorityContext is exported from runcue module."""
        import runcue
        assert hasattr(runcue, "PriorityContext")
        assert runcue.PriorityContext is PriorityContext

