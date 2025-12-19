"""Dynamic scenario - Complex dependency graph with rebuilds.

Demonstrates dynamic dependencies, cross-pipeline dependencies,
and re-processing when artifacts are invalidated.

Four pipelines:
- API: External API calls producing artifacts
- Local: Local processing (some depend on API artifacts)
- Publisher: Assembles documents from collected artifacts
- Checker: Validates docs, may invalidate artifacts triggering rebuilds
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import TYPE_CHECKING

from runcue_sim.scenarios import Scenario, ScenarioInfo

if TYPE_CHECKING:
    import runcue
    from runcue_sim.display import SimulationState
    from runcue_sim.runner import SimConfig


class DynamicScenario(Scenario):
    """Dynamic dependency graph with potential rebuilds.
    
    Simulates a document assembly system:
    - API tasks fetch external data
    - Local tasks process data (some need API results)
    - Publisher assembles docs when all artifacts ready
    - Checker validates and may trigger rebuilds
    
    Demonstrates:
    - Dynamic is_ready with variable dependencies
    - Cross-pipeline dependencies
    - Re-submission for invalidated artifacts
    - Complex concurrent workloads
    """
    
    @property
    def info(self) -> ScenarioInfo:
        return ScenarioInfo(
            name="dynamic",
            description="Dynamic dependencies with rebuild cycles",
        )
    
    def setup(self, cue: runcue.Cue, config: SimConfig, state: SimulationState) -> None:
        """Configure api, local, publisher, and checker services."""
        from runcue_sim.display import ServiceStatus
        
        # Artifact tracking
        api_artifacts: dict[str, bool] = {}      # artifact_id -> valid
        local_artifacts: dict[str, bool] = {}    # artifact_id -> valid
        published_docs: dict[str, bool] = {}     # doc_id -> published
        
        # Document requirements: doc_id -> {api: [ids], local: [ids]}
        doc_requirements: dict[str, dict[str, list[str]]] = {}
        
        # Local task dependencies: local_id -> api_id or None
        local_deps: dict[str, str | None] = {}
        
        # Build rate string
        rate_str = None
        if config.rate_limit:
            count, seconds = config.rate_limit
            if seconds == 1:
                rate_str = f"{count}/sec"
            elif seconds == 60:
                rate_str = f"{count}/min"
            else:
                rate_str = f"{int(count * 60 / seconds)}/min"
        
        # Register services
        cue.service("api", concurrent=config.max_concurrent, rate=rate_str)
        cue.service("local", concurrent=config.max_concurrent * 2)
        cue.service("publisher", concurrent=2)  # Limited assembly capacity
        cue.service("checker", concurrent=1)     # Sequential checking
        
        # Initialize service display
        now = time.time()
        state.services["api"] = ServiceStatus(
            name="api",
            max_concurrent=config.max_concurrent,
            rate_limit=config.rate_limit[0] if config.rate_limit else None,
            rate_window=config.rate_limit[1] if config.rate_limit else None,
            start_time=now,
        )
        state.services["local"] = ServiceStatus(
            name="local", max_concurrent=config.max_concurrent * 2, start_time=now
        )
        state.services["publisher"] = ServiceStatus(
            name="publisher", max_concurrent=2, start_time=now
        )
        state.services["checker"] = ServiceStatus(
            name="checker", max_concurrent=1, start_time=now
        )
        
        # Store references
        self._cue = cue
        self._state = state
        self._config = config
        self._doc_requirements = doc_requirements
        self._api_artifacts = api_artifacts
        self._local_artifacts = local_artifacts
        self._local_deps = local_deps
        
        # --- API task (external calls) ---
        @cue.task("api_fetch", uses="api")
        async def api_handler(work):
            artifact_id = work.params["artifact_id"]
            
            # API latency with jitter
            base_latency = config.latency_ms / 1000.0
            if base_latency > 0:
                jitter = config.latency_jitter
                actual = base_latency * random.uniform(1 - jitter, 1 + jitter)
                
                # Occasional slow responses
                if config.outlier_chance > 0 and random.random() < config.outlier_chance:
                    actual *= config.outlier_multiplier
                
                await asyncio.sleep(actual)
            
            # Simulate errors
            if random.random() < config.error_rate:
                raise RuntimeError(f"API error fetching {artifact_id}")
            
            # Mark artifact as valid
            api_artifacts[artifact_id] = True
            return {"artifact_id": artifact_id, "type": "api"}
        
        # --- Local task (fast processing, may depend on API) ---
        @cue.task("local_process", uses="local")
        async def local_handler(work):
            artifact_id = work.params["artifact_id"]
            
            # Fast local processing (20% of API latency)
            base_latency = config.latency_ms / 1000.0 * 0.2
            if base_latency > 0:
                await asyncio.sleep(base_latency * random.uniform(0.8, 1.2))
            
            # Lower error rate for local
            if random.random() < config.error_rate * 0.3:
                raise RuntimeError(f"Local processing error for {artifact_id}")
            
            # Mark artifact as valid
            local_artifacts[artifact_id] = True
            return {"artifact_id": artifact_id, "type": "local"}
        
        # --- Publisher task (assembles documents) ---
        @cue.task("publish", uses="publisher")
        async def publish_handler(work):
            doc_id = work.params["doc_id"]
            
            # Assembly takes moderate time
            base_latency = config.latency_ms / 1000.0 * 0.5
            if base_latency > 0:
                await asyncio.sleep(base_latency * random.uniform(0.8, 1.2))
            
            # Mark as published
            published_docs[doc_id] = True
            
            # Submit checker task
            await cue.submit("check", params={"doc_id": doc_id})
            state.submitted += 1
            state.queued += 1
            
            return {"doc_id": doc_id, "status": "published"}
        
        # --- Checker task (validates and may invalidate) ---
        @cue.task("check", uses="checker")
        async def check_handler(work):
            doc_id = work.params["doc_id"]
            reqs = doc_requirements.get(doc_id, {"api": [], "local": []})
            
            # Checking takes time
            base_latency = config.latency_ms / 1000.0 * 0.3
            if base_latency > 0:
                await asyncio.sleep(base_latency * random.uniform(0.8, 1.2))
            
            # 20% chance to invalidate some API artifacts
            invalidate_chance = 0.2
            if random.random() < invalidate_chance and reqs["api"]:
                # Pick 1-2 API artifacts to invalidate
                num_to_invalidate = min(random.randint(1, 2), len(reqs["api"]))
                to_invalidate = random.sample(reqs["api"], num_to_invalidate)
                
                for artifact_id in to_invalidate:
                    api_artifacts[artifact_id] = False
                    state.add_event("invalidated", artifact_id, "api_fetch", "checker")
                    
                    # Re-submit API task
                    await cue.submit("api_fetch", params={"artifact_id": artifact_id})
                    state.submitted += 1
                    state.queued += 1
                
                # Mark doc as unpublished and re-queue publish
                published_docs[doc_id] = False
                await cue.submit("publish", params={"doc_id": doc_id})
                state.submitted += 1
                state.queued += 1
                
                return {"doc_id": doc_id, "status": "rebuild_required", "invalidated": to_invalidate}
            
            return {"doc_id": doc_id, "status": "verified"}
        
        # --- is_ready callback ---
        @cue.is_ready
        def is_ready(work):
            if work.task == "local_process":
                # Check if this local task depends on an API artifact
                artifact_id = work.params.get("artifact_id")
                dep = local_deps.get(artifact_id)
                if dep:
                    return api_artifacts.get(dep, False)
                return True
            
            if work.task == "publish":
                # All required artifacts must be valid
                doc_id = work.params.get("doc_id")
                reqs = doc_requirements.get(doc_id, {"api": [], "local": []})
                
                for aid in reqs["api"]:
                    if not api_artifacts.get(aid, False):
                        return False
                for lid in reqs["local"]:
                    if not local_artifacts.get(lid, False):
                        return False
                return True
            
            if work.task == "check":
                # Doc must be published
                doc_id = work.params.get("doc_id")
                return published_docs.get(doc_id, False)
            
            return True
        
        # --- State tracking callbacks ---
        task_to_service = {
            "api_fetch": "api",
            "local_process": "local",
            "publish": "publisher",
            "check": "checker",
        }
        
        @cue.on_start
        def on_start(work):
            state.running += 1
            if state.queued > 0:
                state.queued -= 1
            svc = state.services.get(task_to_service.get(work.task, "api"))
            if svc:
                svc.current_concurrent += 1
            state.add_event("started", work.id, work.task, work.params.get("artifact_id") or work.params.get("doc_id", ""))
        
        @cue.on_complete
        def on_complete(work, result, duration):
            state.running = max(0, state.running - 1)
            state.completed += 1
            svc = state.services.get(task_to_service.get(work.task, "api"))
            if svc:
                svc.current_concurrent = max(0, svc.current_concurrent - 1)
                svc.total_completed += 1
            
            # Show rebuild info in event
            detail = f"{int(duration*1000)}ms"
            if isinstance(result, dict) and result.get("status") == "rebuild_required":
                detail = f"rebuild: {result.get('invalidated', [])}"
            state.add_event("completed", work.id, work.task, detail)
        
        @cue.on_failure
        def on_failure(work, error):
            state.running = max(0, state.running - 1)
            state.failed += 1
            svc = state.services.get(task_to_service.get(work.task, "api"))
            if svc:
                svc.current_concurrent = max(0, svc.current_concurrent - 1)
                svc.total_failed += 1
            state.add_event("failed", work.id, work.task, str(error)[:40])
    
    async def submit_workload(self, cue: runcue.Cue, config: SimConfig, state: SimulationState) -> None:
        """Submit initial work: API tasks, local tasks, and publish jobs.
        
        Creates a dependency graph where:
        - Each doc requires 2-4 API artifacts and 1-3 local artifacts
        - 30% of local tasks depend on an API artifact (from the needed set)
        """
        num_docs = config.count
        
        # Create artifact pools
        num_api_artifacts = max(num_docs * 2, 10)
        num_local_artifacts = max(num_docs * 2, 8)
        
        api_ids = [f"api_{i:03d}" for i in range(num_api_artifacts)]
        local_ids = [f"local_{i:03d}" for i in range(num_local_artifacts)]
        
        # Create document requirements FIRST so we know which artifacts are needed
        needed_api: set[str] = set()
        needed_local: set[str] = set()
        
        for i in range(num_docs):
            doc_id = f"doc_{i:03d}"
            
            # Each doc needs 2-4 API artifacts
            num_api_needed = random.randint(2, min(4, len(api_ids)))
            doc_api = random.sample(api_ids, num_api_needed)
            
            # Each doc needs 1-3 local artifacts
            num_local_needed = random.randint(1, min(3, len(local_ids)))
            doc_local = random.sample(local_ids, num_local_needed)
            
            self._doc_requirements[doc_id] = {"api": doc_api, "local": doc_local}
            needed_api.update(doc_api)
            needed_local.update(doc_local)
        
        # Initialize ONLY needed artifacts as invalid
        for aid in needed_api:
            self._api_artifacts[aid] = False
        for lid in needed_local:
            self._local_artifacts[lid] = False
        
        # Assign local dependencies ONLY to API artifacts that will be submitted
        # (30% of local tasks depend on a needed API artifact)
        needed_api_list = list(needed_api)
        for lid in needed_local:
            if random.random() < 0.3 and needed_api_list:
                self._local_deps[lid] = random.choice(needed_api_list)
        
        # Submit all API tasks
        for aid in needed_api:
            await cue.submit("api_fetch", params={"artifact_id": aid})
            state.submitted += 1
            state.queued += 1
            state.add_event("queued", f"api_{aid}", "api_fetch", aid)
            
            if config.submit_rate:
                await asyncio.sleep(1.0 / config.submit_rate)
        
        # Submit all local tasks
        for lid in needed_local:
            await cue.submit("local_process", params={"artifact_id": lid})
            state.submitted += 1
            state.queued += 1
            
            dep = self._local_deps.get(lid)
            detail = f"{lid} (needs {dep})" if dep else lid
            state.add_event("queued", f"local_{lid}", "local_process", detail)
            
            if config.submit_rate:
                await asyncio.sleep(1.0 / config.submit_rate)
        
        # Submit all publish jobs (will wait for is_ready)
        for doc_id in self._doc_requirements:
            await cue.submit("publish", params={"doc_id": doc_id})
            state.submitted += 1
            state.queued += 1
            
            reqs = self._doc_requirements[doc_id]
            state.add_event("queued", f"pub_{doc_id}", "publish", f"needs {len(reqs['api'])}api+{len(reqs['local'])}local")
            
            if config.submit_rate:
                await asyncio.sleep(1.0 / config.submit_rate)

