"""Tests for work operations: submit, get, list, cancel."""

import pytest

import runcue
from runcue.models import WorkState


class TestSubmit:
    """Tests for work submission."""

    async def test_submit_returns_id(self):
        """Submit returns a non-empty work ID."""
        cue = runcue.Cue()
        cue.service("api", rate="60/min")

        @cue.task("process", uses="api")
        def process(work):
            return {}

        work_id = await cue.submit("process", params={"x": 1})
        assert work_id is not None
        assert len(work_id) > 0

    async def test_submit_unknown_task_raises(self):
        """Submit raises ValueError for unregistered task."""
        cue = runcue.Cue()
        with pytest.raises(ValueError, match="Unknown task"):
            await cue.submit("nonexistent", params={})

    async def test_submit_without_params(self):
        """Submit works with no params."""
        cue = runcue.Cue()
        cue.service("api", rate="60/min")

        @cue.task("simple", uses="api")
        def simple(work):
            return {}

        work_id = await cue.submit("simple")
        work = await cue.get(work_id)
        assert work.params == {}


class TestGet:
    """Tests for retrieving work by ID."""

    async def test_get_returns_work(self):
        """Get returns work unit with correct fields."""
        cue = runcue.Cue()
        cue.service("api", rate="60/min")

        @cue.task("process", uses="api")
        def process(work):
            return {}

        work_id = await cue.submit("process", params={"x": 1})
        work = await cue.get(work_id)

        assert work is not None
        assert work.id == work_id
        assert work.task == "process"
        assert work.params == {"x": 1}
        assert work.state == WorkState.PENDING
        assert work.created_at > 0

    async def test_get_unknown_returns_none(self):
        """Get returns None for unknown work ID."""
        cue = runcue.Cue()
        work = await cue.get("nonexistent")
        assert work is None


class TestList:
    """Tests for listing work units."""

    async def test_list_all(self):
        """List returns all submitted work."""
        cue = runcue.Cue()
        cue.service("api", rate="60/min")

        @cue.task("process", uses="api")
        def process(work):
            return {}

        await cue.submit("process", params={"x": 1})
        await cue.submit("process", params={"x": 2})

        all_work = await cue.list()
        assert len(all_work) == 2

    async def test_list_filters_by_state(self):
        """List filters by work state."""
        cue = runcue.Cue()
        cue.service("api", rate="60/min")

        @cue.task("process", uses="api")
        def process(work):
            return {}

        await cue.submit("process", params={"x": 1})
        await cue.submit("process", params={"x": 2})

        pending = await cue.list(state=WorkState.PENDING)
        assert len(pending) == 2

        running = await cue.list(state=WorkState.RUNNING)
        assert len(running) == 0

    async def test_list_filters_by_task(self):
        """List filters by task type."""
        cue = runcue.Cue()
        cue.service("api", rate="60/min")

        @cue.task("task_a", uses="api")
        def task_a(work):
            return {}

        @cue.task("task_b", uses="api")
        def task_b(work):
            return {}

        await cue.submit("task_a", params={})
        await cue.submit("task_a", params={})
        await cue.submit("task_b", params={})

        task_a_work = await cue.list(task="task_a")
        assert len(task_a_work) == 2

        task_b_work = await cue.list(task="task_b")
        assert len(task_b_work) == 1

    async def test_list_respects_limit(self):
        """List respects the limit parameter."""
        cue = runcue.Cue()
        cue.service("api", rate="60/min")

        @cue.task("process", uses="api")
        def process(work):
            return {}

        for i in range(10):
            await cue.submit("process", params={"i": i})

        limited = await cue.list(limit=3)
        assert len(limited) == 3


class TestCancel:
    """Tests for cancelling work."""

    async def test_cancel_pending_work(self):
        """Cancel marks pending work as cancelled."""
        cue = runcue.Cue()
        cue.service("api", rate="60/min")

        @cue.task("process", uses="api")
        def process(work):
            return {}

        work_id = await cue.submit("process", params={})
        result = await cue.cancel(work_id)

        assert result is True
        work = await cue.get(work_id)
        assert work.state == WorkState.CANCELLED
        assert work.completed_at is not None

    async def test_cancel_unknown_returns_false(self):
        """Cancel returns False for unknown work ID."""
        cue = runcue.Cue()
        result = await cue.cancel("nonexistent")
        assert result is False

    async def test_cancel_removes_from_queue(self):
        """Cancelled work is removed from pending queue."""
        cue = runcue.Cue()
        cue.service("api", rate="60/min")

        @cue.task("process", uses="api")
        def process(work):
            return {}

        work_id = await cue.submit("process", params={})
        await cue.cancel(work_id)

        pending = await cue.list(state=WorkState.PENDING)
        assert len(pending) == 0

        cancelled = await cue.list(state=WorkState.CANCELLED)
        assert len(cancelled) == 1


class TestTaskValidation:
    """Tests for task registration validation."""

    def test_task_unknown_service_raises(self):
        """Registering task with unknown service raises ValueError."""
        cue = runcue.Cue()
        with pytest.raises(ValueError, match="Unknown service"):

            @cue.task("bad", uses="nonexistent")
            def bad(work):
                pass

    def test_task_with_valid_service_succeeds(self):
        """Registering task with valid service works."""
        cue = runcue.Cue()
        cue.service("api", rate="60/min")

        @cue.task("good", uses="api")
        def good(work):
            return {}

        task = cue.get_task("good")
        assert task is not None
        assert task.service == "api"

    def test_task_without_service_succeeds(self):
        """Registering task without service works."""
        cue = runcue.Cue()

        @cue.task("standalone")
        def standalone(work):
            return {}

        task = cue.get_task("standalone")
        assert task is not None
        assert task.service is None

