"""Phase 6: Event Callbacks tests."""

import asyncio

import pytest

import runcue
from runcue.models import WorkState


class TestOnCompleteCallback:
    """Tests for on_complete callback."""

    async def test_on_complete_called(self):
        """on_complete callback fires after successful completion."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        completions = []

        @cue.task("task", uses="api")
        def task(work):
            return {"x": 1}

        @cue.on_complete
        def on_complete(work, result, duration):
            completions.append((work.id, result, duration))

        cue.start()
        work_id = await cue.submit("task", params={})
        await asyncio.sleep(0.1)
        await cue.stop()

        assert len(completions) == 1
        assert completions[0][0] == work_id
        assert completions[0][1] == {"x": 1}
        assert completions[0][2] >= 0  # duration is non-negative

    async def test_on_complete_duration_accurate(self):
        """on_complete receives accurate duration."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        completions = []

        @cue.task("slow", uses="api")
        async def slow(work):
            await asyncio.sleep(0.1)
            return {}

        @cue.on_complete
        def on_complete(work, result, duration):
            completions.append(duration)

        cue.start()
        await cue.submit("slow", params={})
        await asyncio.sleep(0.2)
        await cue.stop()

        assert len(completions) == 1
        # Duration should be at least 0.1s
        assert completions[0] >= 0.1

    async def test_on_complete_exception_doesnt_affect_work(self):
        """Exceptions in on_complete don't affect work state."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        @cue.task("task", uses="api")
        def task(work):
            return {"done": True}

        @cue.on_complete
        def on_complete(work, result, duration):
            raise RuntimeError("callback error")

        cue.start()
        work_id = await cue.submit("task", params={})
        await asyncio.sleep(0.1)
        await cue.stop()

        work = await cue.get(work_id)
        assert work.state == WorkState.COMPLETED
        assert work.result == {"done": True}


class TestOnFailureCallback:
    """Tests for on_failure callback."""

    async def test_on_failure_called(self):
        """on_failure callback fires after handler exception."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        failures = []

        @cue.task("task", uses="api")
        def task(work):
            raise ValueError("boom")

        @cue.on_failure
        def on_failure(work, error):
            failures.append((work.id, str(error)))

        cue.start()
        work_id = await cue.submit("task", params={})
        await asyncio.sleep(0.1)
        await cue.stop()

        assert len(failures) == 1
        assert failures[0][0] == work_id
        assert "boom" in failures[0][1]

    async def test_on_failure_receives_exception(self):
        """on_failure receives the actual exception object."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        errors = []

        @cue.task("task", uses="api")
        def task(work):
            raise ValueError("specific error")

        @cue.on_failure
        def on_failure(work, error):
            errors.append(error)

        cue.start()
        await cue.submit("task", params={})
        await asyncio.sleep(0.1)
        await cue.stop()

        assert len(errors) == 1
        assert isinstance(errors[0], ValueError)
        assert str(errors[0]) == "specific error"

    async def test_on_failure_exception_doesnt_affect_work(self):
        """Exceptions in on_failure don't affect work state."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        @cue.task("task", uses="api")
        def task(work):
            raise ValueError("original error")

        @cue.on_failure
        def on_failure(work, error):
            raise RuntimeError("callback error")

        cue.start()
        work_id = await cue.submit("task", params={})
        await asyncio.sleep(0.1)
        await cue.stop()

        work = await cue.get(work_id)
        assert work.state == WorkState.FAILED
        assert "original error" in work.error


class TestOnStartCallback:
    """Tests for on_start callback."""

    async def test_on_start_called(self):
        """on_start callback fires when work begins executing."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        starts = []

        @cue.task("task", uses="api")
        def task(work):
            return {}

        @cue.on_start
        def on_start(work):
            starts.append(work.id)

        cue.start()
        work_id = await cue.submit("task", params={})
        await asyncio.sleep(0.1)
        await cue.stop()

        assert work_id in starts

    async def test_on_start_before_handler(self):
        """on_start fires before handler executes."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        events = []

        @cue.task("task", uses="api")
        def task(work):
            events.append("handler")
            return {}

        @cue.on_start
        def on_start(work):
            events.append("start")

        cue.start()
        await cue.submit("task", params={})
        await asyncio.sleep(0.1)
        await cue.stop()

        assert events == ["start", "handler"]

    async def test_on_start_exception_doesnt_prevent_execution(self):
        """Exceptions in on_start don't prevent work execution."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        executed = []

        @cue.task("task", uses="api")
        def task(work):
            executed.append(work.id)
            return {}

        @cue.on_start
        def on_start(work):
            raise RuntimeError("callback error")

        cue.start()
        work_id = await cue.submit("task", params={})
        await asyncio.sleep(0.1)
        await cue.stop()

        # Work should still execute despite callback error
        assert work_id in executed


class TestCallbackCombinations:
    """Tests for multiple callbacks working together."""

    async def test_all_callbacks_fire(self):
        """All registered callbacks fire in correct order."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        events = []

        @cue.task("task", uses="api")
        def task(work):
            events.append("handler")
            return {"done": True}

        @cue.on_start
        def on_start(work):
            events.append("on_start")

        @cue.on_complete
        def on_complete(work, result, duration):
            events.append("on_complete")

        cue.start()
        await cue.submit("task", params={})
        await asyncio.sleep(0.1)
        await cue.stop()

        assert events == ["on_start", "handler", "on_complete"]

    async def test_failure_callbacks_order(self):
        """on_start and on_failure fire in correct order on failure."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        events = []

        @cue.task("task", uses="api")
        def task(work):
            events.append("handler")
            raise ValueError("boom")

        @cue.on_start
        def on_start(work):
            events.append("on_start")

        @cue.on_failure
        def on_failure(work, error):
            events.append("on_failure")

        @cue.on_complete
        def on_complete(work, result, duration):
            events.append("on_complete")

        cue.start()
        await cue.submit("task", params={})
        await asyncio.sleep(0.1)
        await cue.stop()

        assert events == ["on_start", "handler", "on_failure"]
        assert "on_complete" not in events


class TestNoCallbacksRegistered:
    """Tests for when no callbacks are registered."""

    async def test_no_callbacks_work_still_completes(self):
        """Work completes normally without any callbacks registered."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        @cue.task("task", uses="api")
        def task(work):
            return {"result": 42}

        # No callbacks registered

        cue.start()
        work_id = await cue.submit("task", params={})
        await asyncio.sleep(0.1)
        await cue.stop()

        work = await cue.get(work_id)
        assert work.state == WorkState.COMPLETED
        assert work.result == {"result": 42}

    async def test_no_callbacks_work_still_fails(self):
        """Work fails normally without any callbacks registered."""
        cue = runcue.Cue()
        cue.service("api", rate="100/min")

        @cue.task("task", uses="api")
        def task(work):
            raise ValueError("error")

        # No callbacks registered

        cue.start()
        work_id = await cue.submit("task", params={})
        await asyncio.sleep(0.1)
        await cue.stop()

        work = await cue.get(work_id)
        assert work.state == WorkState.FAILED
        assert "error" in work.error

