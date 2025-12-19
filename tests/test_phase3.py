"""Phase 3: Rate Limiting & Concurrency tests."""

import asyncio
import time

import pytest

import runcue
from runcue.models import WorkState


class TestConcurrencyLimits:
    """Tests for concurrent work limits."""

    async def test_max_concurrent_respected(self):
        """Concurrent limit prevents too many parallel executions."""
        cue = runcue.Cue()
        cue.service("api", concurrent=2, rate="1000/min")

        running = 0
        max_running = 0
        lock = asyncio.Lock()

        @cue.task("slow", uses="api")
        async def slow(work):
            nonlocal running, max_running
            async with lock:
                running += 1
                max_running = max(max_running, running)
            await asyncio.sleep(0.02)
            async with lock:
                running -= 1
            return {}

        cue.start()
        for _ in range(6):
            await cue.submit("slow", params={})

        # 6 items @ concurrent=2, 20ms each = ~60ms total
        await asyncio.sleep(0.15)
        await cue.stop()

        assert max_running <= 2

    async def test_concurrent_one_is_serial(self):
        """concurrent=1 means only one at a time."""
        cue = runcue.Cue()
        cue.service("serial", concurrent=1)

        execution_order = []
        running = 0
        max_running = 0
        lock = asyncio.Lock()

        @cue.task("task", uses="serial")
        async def task(work):
            nonlocal running, max_running
            async with lock:
                running += 1
                max_running = max(max_running, running)
            execution_order.append(work.params["i"])
            await asyncio.sleep(0.01)
            async with lock:
                running -= 1
            return {}

        cue.start()
        for i in range(4):
            await cue.submit("task", params={"i": i})

        # 4 items @ 10ms serial = ~40ms
        await asyncio.sleep(0.15)
        await cue.stop()

        assert max_running == 1
        assert len(execution_order) == 4

    async def test_no_concurrent_limit_allows_all(self):
        """Without concurrent limit, all work runs in parallel."""
        cue = runcue.Cue()
        cue.service("unlimited")  # No concurrent limit

        running = 0
        max_running = 0
        lock = asyncio.Lock()

        @cue.task("parallel", uses="unlimited")
        async def parallel(work):
            nonlocal running, max_running
            async with lock:
                running += 1
                max_running = max(max_running, running)
            await asyncio.sleep(0.05)
            async with lock:
                running -= 1
            return {}

        cue.start()
        for _ in range(5):
            await cue.submit("parallel", params={})

        await asyncio.sleep(0.03)  # Let all start
        
        # Should have all 5 running in parallel
        assert max_running == 5

        await asyncio.sleep(0.1)
        await cue.stop()


class TestRateLimits:
    """Tests for rate limiting."""

    async def test_rate_limit_throttles(self):
        """Rate limit delays work beyond the limit."""
        cue = runcue.Cue()
        # Only 3 per second - submit 6, should take >1s
        cue.service("api", rate="3/sec", concurrent=100)

        timestamps = []

        @cue.task("record", uses="api")
        def record(work):
            timestamps.append(time.time())
            return {}

        cue.start()
        for _ in range(6):
            await cue.submit("record", params={})

        await asyncio.sleep(1.5)
        await cue.stop()

        assert len(timestamps) == 6
        duration = timestamps[-1] - timestamps[0]
        # First 3 run immediately, wait ~1s, then next 3
        assert duration >= 0.9

    async def test_rate_limit_burst_then_wait(self):
        """First batch runs immediately, then rate limit kicks in."""
        cue = runcue.Cue()
        # 5 per second
        cue.service("api", rate="5/sec", concurrent=100)

        timestamps = []

        @cue.task("record", uses="api")
        def record(work):
            timestamps.append(time.time())
            return {}

        cue.start()
        for _ in range(10):
            await cue.submit("record", params={})

        await asyncio.sleep(1.5)
        await cue.stop()

        assert len(timestamps) == 10
        # First 5 should be nearly instant (burst)
        first_five = timestamps[:5]
        burst_duration = first_five[-1] - first_five[0]
        assert burst_duration < 0.1, "First batch should burst"
        
        # Total should take ~1s (wait for window after first 5)
        total_duration = timestamps[-1] - timestamps[0]
        assert total_duration >= 0.9

    async def test_no_rate_limit_allows_burst(self):
        """Without rate limit, work executes as fast as possible."""
        cue = runcue.Cue()
        cue.service("fast", concurrent=100)  # No rate limit

        timestamps = []

        @cue.task("burst", uses="fast")
        def burst(work):
            timestamps.append(time.time())
            return {}

        cue.start()
        for _ in range(10):
            await cue.submit("burst", params={})

        await asyncio.sleep(0.1)
        await cue.stop()

        assert len(timestamps) == 10
        # All should complete very quickly
        duration = timestamps[-1] - timestamps[0]
        assert duration < 0.1


class TestCombinedLimits:
    """Tests for concurrent + rate limits together."""

    async def test_both_limits_respected(self):
        """Both concurrent and rate limits are enforced."""
        cue = runcue.Cue()
        cue.service("api", rate="100/sec", concurrent=2)

        running = 0
        max_running = 0
        timestamps = []
        lock = asyncio.Lock()

        @cue.task("work", uses="api")
        async def work(work):
            nonlocal running, max_running
            async with lock:
                running += 1
                max_running = max(max_running, running)
                timestamps.append(time.time())
            await asyncio.sleep(0.02)
            async with lock:
                running -= 1
            return {}

        cue.start()
        for _ in range(6):
            await cue.submit("work", params={})

        # 6 items @ concurrent=2, 20ms each = ~60ms
        await asyncio.sleep(0.15)
        await cue.stop()

        # Concurrent limit should be respected
        assert max_running <= 2
        # All work should complete
        assert len(timestamps) == 6


class TestMultipleServices:
    """Tests for multiple services with different limits."""

    async def test_independent_service_limits(self):
        """Each service has independent limits."""
        cue = runcue.Cue()
        cue.service("fast", concurrent=5)
        cue.service("slow", concurrent=1)

        fast_max = 0
        slow_max = 0
        fast_running = 0
        slow_running = 0
        lock = asyncio.Lock()

        @cue.task("fast_task", uses="fast")
        async def fast_task(work):
            nonlocal fast_running, fast_max
            async with lock:
                fast_running += 1
                fast_max = max(fast_max, fast_running)
            await asyncio.sleep(0.02)
            async with lock:
                fast_running -= 1
            return {}

        @cue.task("slow_task", uses="slow")
        async def slow_task(work):
            nonlocal slow_running, slow_max
            async with lock:
                slow_running += 1
                slow_max = max(slow_max, slow_running)
            await asyncio.sleep(0.02)
            async with lock:
                slow_running -= 1
            return {}

        cue.start()
        
        # Submit to both services
        for _ in range(4):
            await cue.submit("fast_task", params={})
            await cue.submit("slow_task", params={})

        # 4 fast in parallel (~20ms), 4 slow serial (~80ms)
        await asyncio.sleep(0.2)
        await cue.stop()

        # Fast service allows 5 concurrent (we submit 4)
        assert fast_max <= 5
        # Slow service allows only 1
        assert slow_max == 1


class TestTaskWithoutService:
    """Tests for tasks without a service."""

    async def test_task_without_service_runs_unlimited(self):
        """Tasks without a service have no limits."""
        cue = runcue.Cue()

        running = 0
        max_running = 0
        lock = asyncio.Lock()

        @cue.task("unlimited")  # No uses= parameter
        async def unlimited(work):
            nonlocal running, max_running
            async with lock:
                running += 1
                max_running = max(max_running, running)
            await asyncio.sleep(0.03)
            async with lock:
                running -= 1
            return {}

        cue.start()
        for _ in range(8):
            await cue.submit("unlimited", params={})

        await asyncio.sleep(0.02)  # Let all start
        
        # Should run all in parallel
        assert max_running == 8

        await asyncio.sleep(0.1)
        await cue.stop()
