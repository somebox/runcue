"""Phase 0: Basic import and instantiation tests."""

import runcue


def test_import():
    """Verify runcue can be imported and has Cue class."""
    assert hasattr(runcue, "Cue")


def test_cue_instantiation():
    """Verify Cue can be instantiated with in-memory database."""
    cue = runcue.Cue(":memory:")
    assert cue is not None
    assert cue.db_path == ":memory:"


def test_service_registration():
    """Verify services can be registered."""
    cue = runcue.Cue(":memory:")
    cue.service("openai", rate_limit=(60, 60), max_concurrent=5)
    
    assert "openai" in cue._services
    assert cue._services["openai"]["rate_limit"] == 60
    assert cue._services["openai"]["rate_window"] == 60
    assert cue._services["openai"]["max_concurrent"] == 5


def test_task_registration():
    """Verify task types can be registered via decorator."""
    cue = runcue.Cue(":memory:")
    cue.service("api", rate_limit=(10, 60))
    
    @cue.task("extract", services=["api"], retry=3)
    async def extract_handler(work):
        return {"extracted": True}
    
    task = cue.get_task("extract")
    assert task is not None
    assert task.services == ["api"]
    assert task.retry_max_attempts == 3
    assert task.handler is extract_handler

