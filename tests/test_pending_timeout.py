"""Tests for pending_timeout feature."""

import asyncio

import pytest

import runcue


@pytest.mark.asyncio
async def test_pending_timeout_fails_stuck_work():
    """Work pending longer than timeout should fail."""
    cue = runcue.Cue(pending_timeout=0.1)  # 100ms timeout
    
    failures = []
    
    @cue.task("blocked_task")
    def blocked_task(work):
        return {"done": True}
    
    # This will never be ready
    @cue.is_ready
    def is_ready(work):
        return False  # Always blocked
    
    @cue.on_failure
    def on_failure(work, error):
        failures.append((work.task, str(error)))
    
    cue.start()
    await cue.submit("blocked_task", params={})
    
    # Wait for timeout
    await asyncio.sleep(0.3)
    await cue.stop()
    
    # Should have failed due to timeout
    assert len(failures) == 1
    assert failures[0][0] == "blocked_task"
    assert "Pending timeout" in failures[0][1]


@pytest.mark.asyncio
async def test_no_timeout_by_default():
    """Without pending_timeout, work waits indefinitely."""
    cue = runcue.Cue()  # No timeout
    
    failures = []
    ready_flag = False
    
    @cue.task("eventually_ready")
    def eventually_ready(work):
        return {"done": True}
    
    @cue.is_ready
    def is_ready(work):
        return ready_flag
    
    @cue.on_failure
    def on_failure(work, error):
        failures.append((work.task, str(error)))
    
    cue.start()
    await cue.submit("eventually_ready", params={})
    
    # Wait a bit - should NOT timeout
    await asyncio.sleep(0.2)
    
    # No failures yet
    assert len(failures) == 0
    
    # Now make it ready
    ready_flag = True
    await asyncio.sleep(0.1)
    
    await cue.stop()
    
    # Still no failures - it should have run
    assert len(failures) == 0


@pytest.mark.asyncio
async def test_timeout_emits_correct_error():
    """Timeout error should be a TimeoutError with details."""
    cue = runcue.Cue(pending_timeout=0.1)
    
    error_types = []
    
    @cue.task("test_task")
    def test_task(work):
        return {}
    
    @cue.is_ready
    def is_ready(work):
        return False
    
    @cue.on_failure
    def on_failure(work, error):
        error_types.append(type(error).__name__)
    
    cue.start()
    await cue.submit("test_task", params={})
    await asyncio.sleep(0.3)
    await cue.stop()
    
    assert len(error_types) == 1
    assert error_types[0] == "TimeoutError"


@pytest.mark.asyncio
async def test_timeout_only_affects_blocked_work():
    """Work that runs successfully shouldn't be affected by timeout."""
    cue = runcue.Cue(pending_timeout=0.5)
    
    completions = []
    failures = []
    
    @cue.task("quick_task")
    async def quick_task(work):
        await asyncio.sleep(0.05)
        return {"done": True}
    
    @cue.on_complete
    def on_complete(work, result, duration):
        completions.append(work.task)
    
    @cue.on_failure
    def on_failure(work, error):
        failures.append(work.task)
    
    cue.start()
    await cue.submit("quick_task", params={})
    await asyncio.sleep(0.2)
    await cue.stop()
    
    # Should complete, not timeout
    assert len(completions) == 1
    assert len(failures) == 0


@pytest.mark.asyncio
async def test_pending_warning_before_timeout():
    """Test that warning fires before timeout."""
    warnings = []
    failures = []
    
    # Warn at 0.05s, timeout at 0.2s
    cue = runcue.Cue(pending_warn_after=0.05, pending_timeout=0.2)
    
    @cue.task("stuck_task")
    def stuck_handler(work):
        pass  # Never called - task is blocked
    
    @cue.is_ready
    def never_ready(work):
        return False
    
    @cue.on_pending_warning
    def on_warning(work, pending_seconds):
        warnings.append((work.task, pending_seconds))
    
    @cue.on_failure
    def on_fail(work, error):
        failures.append((work.task, type(error).__name__))
    
    cue.start()
    await cue.submit("stuck_task", params={})
    
    # Wait for warning but not timeout
    await asyncio.sleep(0.1)
    assert len(warnings) == 1
    assert warnings[0][0] == "stuck_task"
    assert warnings[0][1] >= 0.05
    assert len(failures) == 0  # Not timed out yet
    
    # Wait for timeout
    await asyncio.sleep(0.15)
    await cue.stop()
    
    # Now should have failed
    assert len(failures) == 1
    assert failures[0][1] == "TimeoutError"


@pytest.mark.asyncio
async def test_warning_only_fires_once():
    """Test that warning only fires once per work item."""
    warnings = []
    
    cue = runcue.Cue(pending_warn_after=0.02)  # No timeout, just warning
    
    @cue.task("blocked")
    def handler(work):
        pass
    
    @cue.is_ready
    def never_ready(work):
        return False
    
    @cue.on_pending_warning
    def on_warning(work, pending_seconds):
        warnings.append(work.task)
    
    cue.start()
    await cue.submit("blocked", params={})
    
    # Wait multiple warn cycles
    await asyncio.sleep(0.1)
    await cue.stop()
    
    # Should only have warned once
    assert len(warnings) == 1


# --- Stall Detection Tests ---

@pytest.mark.asyncio
async def test_stall_warning_when_no_progress():
    """Stall warning should fire when no work completes for stall_warn_after seconds."""
    warnings = []
    
    cue = runcue.Cue(stall_warn_after=0.05)  # Warn after 50ms of no progress
    
    @cue.task("blocked")
    def handler(work):
        pass  # Never called
    
    @cue.is_ready
    def never_ready(work):
        return False  # Work can't start
    
    @cue.on_stall_warning
    def on_stall(seconds, pending_count):
        warnings.append((seconds, pending_count))
    
    cue.start()
    await cue.submit("blocked", params={})
    
    await asyncio.sleep(0.1)
    await cue.stop()
    
    # Should have warned about stall
    assert len(warnings) >= 1
    assert warnings[0][1] == 1  # 1 pending work item


@pytest.mark.asyncio
async def test_no_stall_warning_when_making_progress():
    """No stall warning if work is completing."""
    warnings = []
    completions = []
    
    cue = runcue.Cue(stall_warn_after=0.1)  # Would warn after 100ms
    
    @cue.task("fast")
    def handler(work):
        return "done"
    
    @cue.on_stall_warning
    def on_stall(seconds, pending_count):
        warnings.append((seconds, pending_count))
    
    @cue.on_complete
    def on_complete(work, result, duration):
        completions.append(work.id)
    
    cue.start()
    
    # Submit multiple work items that will complete quickly
    for i in range(5):
        await cue.submit("fast", params={"i": i})
        await asyncio.sleep(0.03)  # Small gaps between submissions
    
    await asyncio.sleep(0.1)
    await cue.stop()
    
    # Work completed, so no stall warning
    assert len(warnings) == 0
    assert len(completions) == 5


@pytest.mark.asyncio
async def test_stall_timeout_fails_all_pending():
    """Stall timeout should fail all pending work."""
    failures = []
    
    cue = runcue.Cue(stall_timeout=0.05)  # Fail after 50ms stall
    
    @cue.task("blocked")
    def handler(work):
        pass
    
    @cue.is_ready
    def never_ready(work):
        return False
    
    @cue.on_failure
    def on_fail(work, error):
        failures.append((work.task, "TimeoutError" in str(type(error).__name__)))
    
    cue.start()
    await cue.submit("blocked", params={"id": 1})
    await cue.submit("blocked", params={"id": 2})
    await cue.submit("blocked", params={"id": 3})
    
    await asyncio.sleep(0.1)
    await cue.stop()
    
    # All 3 should have failed due to stall
    assert len(failures) == 3
    assert all(is_timeout for _, is_timeout in failures)
