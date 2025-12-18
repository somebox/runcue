"""Phase 1: Data models and persistence tests."""

import os
import tempfile

import pytest

import runcue
from runcue import WorkState


class TestServicePersistence:
    """Test service registration and persistence."""

    async def test_service_persisted_to_db(self):
        """Services are persisted when work is submitted."""
        cue = runcue.Cue(":memory:")
        cue.service("openai", rate_limit=(60, 60), max_concurrent=5)
        
        @cue.task("extract", services=["openai"])
        async def extract(work):
            return {"done": True}
        
        # Submit triggers persistence
        await cue.submit("extract", target="test")
        
        # Verify service in database
        service = await cue.get_service("openai")
        assert service is not None
        assert service["name"] == "openai"
        assert service["rate_limit"] == 60
        assert service["rate_window"] == 60
        assert service["max_concurrent"] == 5
        
        await cue.close()

    async def test_list_services(self):
        """Can list all registered services."""
        cue = runcue.Cue(":memory:")
        cue.service("openai", rate_limit=(60, 60), max_concurrent=5)
        cue.service("s3", rate_limit=(100, 60), max_concurrent=10)
        
        @cue.task("process", services=["openai"])
        async def process(work):
            return {}
        
        await cue.submit("process")
        
        services = await cue.list_services()
        assert len(services) == 2
        names = {s["name"] for s in services}
        assert names == {"openai", "s3"}
        
        await cue.close()


class TestTaskRegistration:
    """Test task type registration."""

    def test_task_registered_with_decorator(self):
        """Task types are registered via decorator."""
        cue = runcue.Cue(":memory:")
        cue.service("api", rate_limit=(10, 60))
        
        @cue.task("extract", services=["api"], retry=3)
        async def extract_handler(work):
            return {"extracted": True}
        
        task = cue.get_task("extract")
        assert task is not None
        assert task.name == "extract"
        assert task.services == ["api"]
        assert task.retry_max_attempts == 3
        assert task.handler is extract_handler

    def test_task_retry_policy_dict(self):
        """Retry policy can be specified as dict."""
        cue = runcue.Cue(":memory:")
        
        @cue.task("flaky", retry={"max_attempts": 5, "backoff": "linear", "base_delay": 2.0})
        async def flaky(work):
            return {}
        
        task = cue.get_task("flaky")
        assert task.retry_max_attempts == 5
        assert task.retry_backoff == "linear"
        assert task.retry_base_delay == 2.0


class TestWorkUnitPersistence:
    """Test work unit submission and retrieval."""

    async def test_submit_returns_id(self):
        """Submit returns a work unit ID."""
        cue = runcue.Cue(":memory:")
        cue.service("api", rate_limit=(10, 60))
        
        @cue.task("process", services=["api"])
        async def process(work):
            return {}
        
        work_id = await cue.submit("process", target="test.txt")
        
        assert work_id is not None
        assert len(work_id) > 0
        
        await cue.close()

    async def test_get_work_unit(self):
        """Can retrieve submitted work unit."""
        cue = runcue.Cue(":memory:")
        cue.service("api", rate_limit=(10, 60))
        
        @cue.task("process", services=["api"])
        async def process(work):
            return {}
        
        work_id = await cue.submit(
            "process",
            target="/path/to/file.txt",
            params={"key": "value"},
        )
        
        work = await cue.get(work_id)
        
        assert work is not None
        assert work.id == work_id
        assert work.task_type == "process"
        assert work.target == "/path/to/file.txt"
        assert work.params == {"key": "value"}
        assert work.state == WorkState.QUEUED
        
        await cue.close()

    async def test_list_work_units_by_state(self):
        """Can list work units filtered by state."""
        cue = runcue.Cue(":memory:")
        cue.service("api", rate_limit=(100, 60))
        
        @cue.task("process", services=["api"])
        async def process(work):
            return {}
        
        # Submit several
        for i in range(5):
            await cue.submit("process", target=f"file_{i}.txt")
        
        queued = await cue.list(state="queued")
        assert len(queued) == 5
        
        running = await cue.list(state="running")
        assert len(running) == 0
        
        await cue.close()

    async def test_work_unit_persistence_across_restart(self):
        """Work units persist across Cue restarts."""
        # Use a temp file for the database
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        
        try:
            # First instance - submit work
            cue1 = runcue.Cue(db_path)
            cue1.service("api", rate_limit=(10, 60))
            
            @cue1.task("process", services=["api"])
            async def process1(work):
                return {}
            
            work_id = await cue1.submit("process", target="persist_test.txt")
            await cue1.close()
            
            # Second instance - retrieve work
            cue2 = runcue.Cue(db_path)
            
            @cue2.task("process", services=["api"])
            async def process2(work):
                return {}
            
            work = await cue2.get(work_id)
            
            assert work is not None
            assert work.id == work_id
            assert work.target == "persist_test.txt"
            assert work.state == WorkState.QUEUED
            
            await cue2.close()
        finally:
            os.unlink(db_path)

    async def test_idempotency_key_returns_existing(self):
        """Duplicate idempotency key returns existing work ID."""
        cue = runcue.Cue(":memory:")
        cue.service("api", rate_limit=(10, 60))
        
        @cue.task("process", services=["api"])
        async def process(work):
            return {}
        
        id1 = await cue.submit(
            "process",
            target="file_a.txt",
            idempotency_key="unique-key-123",
        )
        id2 = await cue.submit(
            "process",
            target="file_b.txt",  # Different target
            idempotency_key="unique-key-123",  # Same key
        )
        
        assert id1 == id2
        
        # Only one work unit exists
        all_work = await cue.list()
        assert len(all_work) == 1
        assert all_work[0].target == "file_a.txt"  # First one wins
        
        await cue.close()

    async def test_submit_unknown_task_raises(self):
        """Submitting unknown task type raises ValueError."""
        cue = runcue.Cue(":memory:")
        
        with pytest.raises(ValueError, match="Unknown task type"):
            await cue.submit("nonexistent", target="test")
        
        await cue.close()

    async def test_work_unit_with_dependencies(self):
        """Work units can specify dependencies."""
        cue = runcue.Cue(":memory:")
        cue.service("api", rate_limit=(10, 60))
        
        @cue.task("step", services=["api"])
        async def step(work):
            return {}
        
        id_a = await cue.submit("step", target="step_a")
        id_b = await cue.submit("step", target="step_b", depends_on=[id_a])
        
        work_b = await cue.get(id_b)
        assert work_b.depends_on == [id_a]
        
        await cue.close()


class TestWorkUnitModel:
    """Test WorkUnit dataclass serialization."""

    def test_work_unit_to_row_and_back(self):
        """WorkUnit can round-trip through row format."""
        from runcue.models import WorkUnit, WorkState
        import time
        
        original = WorkUnit(
            id="test-123",
            task_type="process",
            target="/path/to/file",
            params={"key": "value", "nested": {"a": 1}},
            state=WorkState.QUEUED,
            created_at=time.time(),
            depends_on=["dep-1", "dep-2"],
        )
        
        row = original.to_row()
        restored = WorkUnit.from_row(row)
        
        assert restored.id == original.id
        assert restored.task_type == original.task_type
        assert restored.target == original.target
        assert restored.params == original.params
        assert restored.state == original.state
        assert restored.depends_on == original.depends_on

