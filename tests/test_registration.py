"""Tests for import, instantiation, and service/task/callback registration."""

import runcue


def test_import():
    """Verify runcue can be imported and has Cue class."""
    assert hasattr(runcue, "Cue")


def test_cue_instantiation():
    """Verify Cue can be instantiated."""
    cue = runcue.Cue()
    assert cue is not None


def test_service_registration():
    """Verify services can be registered with rate string."""
    cue = runcue.Cue()
    cue.service("openai", rate="60/min", concurrent=5)
    
    assert "openai" in cue._services
    assert cue._services["openai"]["rate_limit"] == 60
    assert cue._services["openai"]["rate_window"] == 60
    assert cue._services["openai"]["concurrent"] == 5


def test_service_rate_formats():
    """Verify various rate string formats are parsed correctly."""
    cue = runcue.Cue()
    
    cue.service("per_sec", rate="10/sec")
    assert cue._services["per_sec"]["rate_limit"] == 10
    assert cue._services["per_sec"]["rate_window"] == 1
    
    cue.service("per_min", rate="60/min")
    assert cue._services["per_min"]["rate_limit"] == 60
    assert cue._services["per_min"]["rate_window"] == 60
    
    cue.service("per_hour", rate="1000/hour")
    assert cue._services["per_hour"]["rate_limit"] == 1000
    assert cue._services["per_hour"]["rate_window"] == 3600


def test_service_no_rate():
    """Verify services can be registered without rate limits."""
    cue = runcue.Cue()
    cue.service("local", concurrent=4)
    
    assert cue._services["local"]["rate_limit"] is None
    assert cue._services["local"]["concurrent"] == 4


def test_task_registration():
    """Verify task types can be registered via decorator."""
    cue = runcue.Cue()
    cue.service("api", rate="60/min")
    
    @cue.task("extract", uses="api", retry=3)
    def extract_handler(work):
        return {"extracted": True}
    
    task = cue.get_task("extract")
    assert task is not None
    assert task.service == "api"
    assert task.retry == 3
    assert task.handler is extract_handler


def test_is_ready_registration():
    """Verify is_ready callback can be registered."""
    cue = runcue.Cue()
    
    @cue.is_ready
    def check_ready(work):
        return True
    
    assert cue._is_ready_callback is check_ready


def test_is_stale_registration():
    """Verify is_stale callback can be registered."""
    cue = runcue.Cue()
    
    @cue.is_stale
    def check_stale(work):
        return True
    
    assert cue._is_stale_callback is check_stale


def test_priority_registration():
    """Verify priority callback can be registered."""
    cue = runcue.Cue()
    
    @cue.priority
    def prioritize(ctx):
        return 0.5
    
    assert cue._priority_callback is prioritize


def test_on_complete_registration():
    """Verify on_complete callback can be registered."""
    cue = runcue.Cue()
    
    @cue.on_complete
    def on_complete(work, result, duration):
        pass
    
    assert cue._on_complete_callback is on_complete


def test_on_failure_registration():
    """Verify on_failure callback can be registered."""
    cue = runcue.Cue()
    
    @cue.on_failure
    def on_failure(work, error):
        pass
    
    assert cue._on_failure_callback is on_failure


def test_on_skip_registration():
    """Verify on_skip callback can be registered."""
    cue = runcue.Cue()
    
    @cue.on_skip
    def on_skip(work):
        pass
    
    assert cue._on_skip_callback is on_skip


def test_on_start_registration():
    """Verify on_start callback can be registered."""
    cue = runcue.Cue()
    
    @cue.on_start
    def on_start(work):
        pass
    
    assert cue._on_start_callback is on_start

