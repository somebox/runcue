"""Phase 8: Polish - Integration tests and edge cases."""

import asyncio

import pytest

import runcue


class TestFullPipelineIntegration:
    """Integration tests for complete workflows."""

    async def test_full_pipeline(self):
        """Integration test: submit → is_ready → is_stale → execute → callbacks."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min", concurrent=2)

        artifacts = {}
        events = []

        @cue.task("produce", uses="api")
        def produce(work):
            artifacts[work.params["key"]] = "data"
            return {"key": work.params["key"]}

        @cue.task("consume", uses="api")
        def consume(work):
            return {"value": artifacts[work.params["key"]]}

        @cue.is_ready
        def is_ready(work):
            if work.task == "consume":
                return work.params["key"] in artifacts
            return True

        @cue.is_stale
        def is_stale(work):
            if work.task == "produce":
                return work.params["key"] not in artifacts
            return True

        @cue.on_complete
        def on_complete(work, result, duration):
            events.append(("complete", work.task))

        cue.start()

        # Submit consumer first (will wait for producer)
        await cue.submit("consume", params={"key": "x"})
        await asyncio.sleep(0.1)
        assert len(events) == 0  # Blocked by is_ready

        # Submit producer
        await cue.submit("produce", params={"key": "x"})
        await asyncio.sleep(0.3)

        assert ("complete", "produce") in events
        assert ("complete", "consume") in events

        await cue.stop()

    async def test_producer_consumer_chain(self):
        """Test chain: A produces → B consumes A and produces → C consumes B."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min", concurrent=2)

        artifacts = {}
        order = []

        @cue.task("step_a", uses="api")
        def step_a(work):
            artifacts["a_output"] = "from_a"
            order.append("a")
            return {}

        @cue.task("step_b", uses="api")
        def step_b(work):
            artifacts["b_output"] = artifacts["a_output"] + "_from_b"
            order.append("b")
            return {}

        @cue.task("step_c", uses="api")
        def step_c(work):
            order.append("c")
            return {"final": artifacts["b_output"]}

        @cue.is_ready
        def is_ready(work):
            if work.task == "step_b":
                return "a_output" in artifacts
            if work.task == "step_c":
                return "b_output" in artifacts
            return True

        cue.start()

        # Submit in reverse order
        await cue.submit("step_c", params={})
        await cue.submit("step_b", params={})
        await cue.submit("step_a", params={})

        await asyncio.sleep(0.5)
        await cue.stop()

        # Should execute in correct order despite reverse submission
        assert order == ["a", "b", "c"]

    async def test_skip_then_execute(self):
        """Test work is skipped first, then executed when stale."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        artifacts = {"output": "cached"}
        executed = []
        skipped = []

        @cue.task("process", uses="api")
        def process(work):
            executed.append(work.id)
            return {}

        @cue.is_stale
        def is_stale(work):
            return "output" not in artifacts

        @cue.on_skip
        def on_skip(work):
            skipped.append(work.id)

        cue.start()

        # First submission: should skip (artifact exists)
        id1 = await cue.submit("process", params={})
        await asyncio.sleep(0.1)
        assert id1 in skipped
        assert id1 not in executed

        # Remove artifact, make it stale
        del artifacts["output"]

        # Second submission: should execute (artifact missing)
        id2 = await cue.submit("process", params={})
        await asyncio.sleep(0.1)
        assert id2 in executed
        assert id2 not in skipped

        await cue.stop()


class TestEdgeCases:
    """Edge case tests."""

    async def test_empty_queue_no_errors(self):
        """Starting with empty queue doesn't cause errors."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        @cue.task("task", uses="api")
        def task(work):
            return {}

        cue.start()
        # No work submitted
        await asyncio.sleep(0.1)
        await cue.stop()
        # Should not raise

    async def test_rapid_start_stop(self):
        """Rapid start/stop cycles don't cause errors."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        @cue.task("task", uses="api")
        def task(work):
            return {}

        # Multiple rapid cycles
        for _ in range(3):
            cue.start()
            await asyncio.sleep(0.01)
            await cue.stop()
        # Should not raise

    async def test_stop_without_start(self):
        """Calling stop() without start() is safe."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        @cue.task("task", uses="api")
        def task(work):
            return {}

        # Stop without start
        await cue.stop()
        # Should not raise

    async def test_double_start(self):
        """Calling start() twice is idempotent."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        executed = []

        @cue.task("task", uses="api")
        def task(work):
            executed.append(work.id)
            return {}

        cue.start()
        cue.start()  # Second start should be no-op

        work_id = await cue.submit("task", params={})
        await asyncio.sleep(0.1)
        await cue.stop()

        # Work should execute exactly once
        assert executed.count(work_id) == 1

    async def test_submit_many_at_once(self):
        """Submitting many work items at once works."""
        cue = runcue.Cue()
        cue.service("api", rate="1000/min", concurrent=10)

        executed = []

        @cue.task("task", uses="api")
        def task(work):
            executed.append(work.id)
            return {}

        cue.start()

        # Submit 50 items rapidly
        work_ids = []
        for i in range(50):
            work_id = await cue.submit("task", params={"i": i})
            work_ids.append(work_id)

        await asyncio.sleep(0.5)
        await cue.stop()

        # All should complete
        assert len(executed) == 50
        for work_id in work_ids:
            assert work_id in executed

    async def test_get_nonexistent_work(self):
        """Getting nonexistent work returns None."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        @cue.task("task", uses="api")
        def task(work):
            return {}

        result = await cue.get("nonexistent_id")
        assert result is None

    async def test_cancel_nonexistent_work(self):
        """Cancelling nonexistent work returns False."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        @cue.task("task", uses="api")
        def task(work):
            return {}

        result = await cue.cancel("nonexistent_id")
        assert result is False


class TestErrorMessages:
    """Tests for error messages."""

    async def test_unknown_task_error_message(self):
        """Submitting unknown task gives clear error."""
        cue = runcue.Cue()

        with pytest.raises(ValueError, match="Unknown task"):
            await cue.submit("nonexistent", params={})

    def test_unknown_service_error_message(self):
        """Task using unknown service gives clear error."""
        cue = runcue.Cue()

        with pytest.raises(ValueError, match="Unknown service"):
            @cue.task("task", uses="nonexistent")
            def task(work):
                return {}

    def test_invalid_rate_format_error(self):
        """Invalid rate format gives clear error."""
        cue = runcue.Cue()

        with pytest.raises(ValueError, match="Invalid rate format"):
            cue.service("bad", rate="invalid")

    def test_unknown_rate_unit_error(self):
        """Unknown rate unit gives clear error."""
        cue = runcue.Cue()

        with pytest.raises(ValueError, match="Unknown rate unit"):
            cue.service("bad", rate="60/fortnight")


class TestAllCallbacksTogether:
    """Test all features working together."""

    async def test_complete_scenario(self):
        """Test with all features: services, tasks, ready, stale, callbacks, priority."""
        cue = runcue.Cue()
        cue.service("fast", concurrent=2)
        cue.service("slow", concurrent=1, rate="100/min")

        artifacts = {}
        events = []

        @cue.task("fetch", uses="slow")
        def fetch(work):
            key = work.params["key"]
            artifacts[key] = f"data_{key}"
            return {"key": key}

        @cue.task("process", uses="fast")
        def process(work):
            key = work.params["key"]
            return {"result": artifacts[key].upper()}

        @cue.is_ready
        def is_ready(work):
            if work.task == "process":
                return work.params["key"] in artifacts
            return True

        @cue.is_stale
        def is_stale(work):
            if work.task == "fetch":
                return work.params["key"] not in artifacts
            return True

        @cue.priority
        def prioritize(ctx):
            # Fetch has higher priority than process
            if ctx.work.task == "fetch":
                return 0.9
            return 0.5

        @cue.on_start
        def on_start(work):
            events.append(("start", work.task, work.params.get("key")))

        @cue.on_complete
        def on_complete(work, result, duration):
            events.append(("complete", work.task, work.params.get("key")))

        @cue.on_skip
        def on_skip(work):
            events.append(("skip", work.task, work.params.get("key")))

        cue.start()

        # Submit process first (will wait), then fetch
        await cue.submit("process", params={"key": "a"})
        await cue.submit("fetch", params={"key": "a"})

        await asyncio.sleep(0.3)

        # Submit duplicate fetch (should skip)
        await cue.submit("fetch", params={"key": "a"})

        await asyncio.sleep(0.2)
        await cue.stop()

        # Verify fetch completed before process could run
        fetch_complete_idx = next(
            i for i, e in enumerate(events)
            if e == ("complete", "fetch", "a")
        )
        process_start_idx = next(
            i for i, e in enumerate(events)
            if e == ("start", "process", "a")
        )
        assert fetch_complete_idx < process_start_idx

        # Verify duplicate fetch was skipped
        assert ("skip", "fetch", "a") in events

