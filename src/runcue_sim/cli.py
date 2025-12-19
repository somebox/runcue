#!/usr/bin/env python3
"""
runcue-sim: Interactive simulator for testing runcue.

Usage:
    runcue-sim --count 100 --latency 50
    runcue-sim --count 50 --error-rate 0.1 --duration 30
    runcue-sim --count 100 --concurrent 3 --tui
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from runcue_sim.display import RICH_AVAILABLE, SimulationState, SimulatorDisplay, print_simple_stats
from runcue_sim.runner import SimConfig, SimulationRunner


def configure_logging(verbose: bool = False) -> None:
    """Configure logging for the simulator."""
    # Suppress runcue library logs during TUI mode
    runcue_logger = logging.getLogger("runcue")
    if verbose:
        runcue_logger.setLevel(logging.DEBUG)
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        runcue_logger.addHandler(handler)
    else:
        # Silence library logs - simulator handles its own display
        runcue_logger.setLevel(logging.CRITICAL)


async def run_with_display(config: SimConfig, use_tui: bool = True, verbose: bool = False) -> None:
    """Run simulation with visual display.
    
    Args:
        config: Simulation configuration
        use_tui: Use Rich TUI display (default True)
        verbose: Print event log instead of status updates (implies no-tui)
    """
    state = SimulationState()
    
    # Verbose mode: print each event as it happens
    if verbose:
        from datetime import datetime
        
        # Wrap state.add_event to also print to console
        original_add_event = state.add_event
        
        def logging_add_event(event_type: str, work_id: str, task_type: str | None = None, details: str = "") -> None:
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            task_str = f"[{task_type}]" if task_type else ""
            
            # Color-code by event type
            if event_type == "completed":
                symbol = "✓"
            elif event_type == "failed":
                symbol = "✗"
            elif event_type == "started":
                symbol = "▶"
            elif event_type == "queued":
                symbol = "+"
            elif event_type == "invalidated":
                symbol = "⟳"
            else:
                symbol = "·"
            
            print(f"{ts} {symbol} {event_type:<12} {task_str:<16} {work_id:<20} {details}")
            
            # Also add to state for final stats
            original_add_event(event_type, work_id, task_type, details)
        
        # Patch state.add_event
        state.add_event = logging_add_event  # type: ignore
    
    runner = SimulationRunner(config, state)
    
    if verbose:
        # Verbose log mode - print header then let events stream
        print("\n🚀 runcue-sim [verbose]")
        print(f"   Scenario: {config.scenario}, Count: {config.count}")
        print(f"   Latency: {config.latency_ms}ms ±{int(config.latency_jitter*100)}%, Error: {config.error_rate * 100:.0f}%")
        print()
        print(f"{'TIME':<12} {'':1} {'EVENT':<12} {'TASK':<16} {'WORK_ID':<20} DETAILS")
        print("-" * 80)
        
        try:
            await runner.run()
        except (KeyboardInterrupt, asyncio.CancelledError):
            runner.stop()
        finally:
            await runner.cleanup()
        
        print("-" * 80)
        print_final_summary(state)
    
    elif use_tui and RICH_AVAILABLE:
        # Rich TUI display
        display = SimulatorDisplay(state)
        stall_counter = 0
        last_completed = 0
        stall_timeout_ticks = int(config.stall_timeout * 10) if config.stall_timeout else None
        
        async def update_loop():
            """Background task to refresh display and detect stalls."""
            nonlocal stall_counter, last_completed
            while True:
                # Detect stalls: nothing completing but work queued
                if state.completed == last_completed and state.queued > 0 and state.running == 0:
                    stall_counter += 1
                    # After ~2 seconds of stall, check for blocked work
                    if stall_counter > 20:
                        state.blocked_info = runner.debug_blocked()
                    # Check stall timeout
                    if stall_timeout_ticks and stall_counter >= stall_timeout_ticks:
                        state.add_event("timeout", "system", None, f"Stalled for {config.stall_timeout}s")
                        runner.stop()
                        return
                else:
                    stall_counter = 0
                    state.blocked_info = []
                last_completed = state.completed
                
                display.refresh()
                await asyncio.sleep(0.1)
        
        with display:
            # Start display updates
            update_task = asyncio.create_task(update_loop())
            
            try:
                await runner.run()
            except (KeyboardInterrupt, asyncio.CancelledError):
                runner.stop()
            finally:
                update_task.cancel()
                try:
                    await update_task
                except asyncio.CancelledError:
                    pass
                # Clean up orchestrator resources
                await runner.cleanup()
        
        # Final summary
        print_final_summary(state)
    else:
        # Simple text display
        print("\n🚀 runcue-sim")
        print(f"   Count: {config.count}, Latency: {config.latency_ms}ms, Error: {config.error_rate * 100:.0f}%")
        print()
        
        stall_counter = 0
        last_completed = 0
        stall_timeout_ticks = int(config.stall_timeout * 2) if config.stall_timeout else None  # 0.5s per tick
        
        async def update_loop():
            """Print progress periodically and detect stalls."""
            nonlocal stall_counter, last_completed
            while True:
                print_simple_stats(state)
                
                # Detect stalls
                if state.completed == last_completed and state.queued > 0 and state.running == 0:
                    stall_counter += 1
                    if stall_counter > 4:  # After 2 seconds
                        blocked = runner.debug_blocked()
                        if blocked:
                            print(f"\n⚠️  Stall detected! {len(blocked)} work items blocked:")
                            for item in blocked[:5]:
                                work = item.get("work")
                                task = work.task if work else "?"
                                reason = item.get("reason", "?")
                                details = item.get("details", "")[:60]
                                print(f"    {task}: {reason} - {details}")
                            if len(blocked) > 5:
                                print(f"    ... and {len(blocked) - 5} more")
                            print()
                            stall_counter = 4  # Reset partially to show again after interval
                    # Check stall timeout
                    if stall_timeout_ticks and stall_counter >= stall_timeout_ticks:
                        print(f"\n⏱️  Timeout: Stalled for {config.stall_timeout}s. Stopping.")
                        runner.stop()
                        return
                else:
                    stall_counter = 0
                last_completed = state.completed
                
                await asyncio.sleep(0.5)
        
        update_task = asyncio.create_task(update_loop())
        
        try:
            await runner.run()
        except (KeyboardInterrupt, asyncio.CancelledError):
            runner.stop()
        finally:
            update_task.cancel()
            try:
                await update_task
            except asyncio.CancelledError:
                pass
            # Clean up orchestrator resources
            await runner.cleanup()
        
        print()  # Newline after progress
        print_final_summary(state)


def print_final_summary(state: SimulationState) -> None:
    """Print final summary after simulation."""
    if RICH_AVAILABLE:
        from rich.console import Console
        from rich.table import Table
        
        console = Console()
        console.print()
        
        table = Table(title="Simulation Results", show_header=False, border_style="green")
        table.add_column("Metric", style="dim")
        table.add_column("Value", style="bold")
        
        table.add_row("Submitted", str(state.submitted))
        table.add_row("Completed", f"[green]{state.completed}[/green]")
        table.add_row("Failed", f"[red]{state.failed}[/red]" if state.failed else "0")
        table.add_row("Duration", f"{state.elapsed:.2f}s")
        table.add_row("Throughput", f"{state.throughput:.2f}/s")
        
        console.print(table)
    else:
        print("\n📈 Results:")
        print(f"   Submitted:  {state.submitted}")
        print(f"   Completed:  {state.completed}")
        print(f"   Failed:     {state.failed}")
        print(f"   Duration:   {state.elapsed:.2f}s")
        print(f"   Throughput: {state.throughput:.2f}/s")


def main():
    parser = argparse.ArgumentParser(
        description="runcue simulator - test workloads interactively",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  runcue-sim --count 100 --latency 50
  runcue-sim --count 1000 --latency 10 --concurrent 10
  runcue-sim --count 50 --error-rate 0.2 --duration 30
  runcue-sim --scenario fanout --count 5
  runcue-sim --scenario pipeline --count 10
  runcue-sim --list-scenarios
        """,
    )
    
    # Scenario options
    parser.add_argument(
        "--scenario",
        type=str,
        default="single_queue",
        help="Scenario to run (default: single_queue)",
    )
    parser.add_argument(
        "--list-scenarios",
        action="store_true",
        help="List available scenarios and exit",
    )
    
    parser.add_argument(
        "--count", "-n",
        type=int,
        default=100,
        help="Number of work units to submit (default: 100)",
    )
    parser.add_argument(
        "--latency", "-l",
        type=int,
        default=100,
        help="Base handler latency in ms (default: 100)",
    )
    parser.add_argument(
        "--jitter", "-j",
        type=float,
        default=0.2,
        help="Latency variance as fraction, e.g. 0.2 = ±20%% (default: 0.2)",
    )
    parser.add_argument(
        "--outliers",
        type=float,
        default=0.0,
        help="Chance of outlier (slow) request, 0.0-1.0 (default: 0.0)",
    )
    parser.add_argument(
        "--outlier-mult",
        type=float,
        default=5.0,
        help="Outlier latency multiplier (default: 5.0)",
    )
    parser.add_argument(
        "--error-rate", "-e",
        type=float,
        default=0.0,
        help="Fraction of work that fails, 0.0-1.0 (default: 0.0)",
    )
    parser.add_argument(
        "--duration", "-d",
        type=float,
        default=None,
        help="Maximum duration in seconds (default: run until complete)",
    )
    parser.add_argument(
        "--concurrent", "-c",
        type=int,
        default=5,
        help="Max concurrent work (default: 5)",
    )
    parser.add_argument(
        "--rate-limit", "-r",
        type=str,
        default=None,
        help="Rate limit as 'requests/seconds', e.g. '60/60' (default: none)",
    )
    parser.add_argument(
        "--submit-rate", "-s",
        type=float,
        default=None,
        help="Submit rate (work/second), None = batch (default: batch)",
    )
    parser.add_argument(
        "--tui",
        action="store_true",
        help="Use full TUI display (requires rich)",
    )
    parser.add_argument(
        "--no-tui",
        action="store_true",
        help="Disable TUI, use simple text output",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print event log instead of status updates (no-tui)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Random seed for reproducible behavior (default: random)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Auto-stop if stalled for N seconds (default: none)",
    )
    
    args = parser.parse_args()
    
    # Handle --list-scenarios
    if args.list_scenarios:
        from runcue_sim.scenarios import list_scenarios
        print("\nAvailable scenarios:\n")
        for info in list_scenarios():
            print(f"  {info.name:<15} {info.description}")
        print()
        sys.exit(0)
    
    # Configure logging early
    configure_logging(verbose=args.verbose)
    
    # Set random seed for reproducibility
    if args.seed is not None:
        import random
        random.seed(args.seed)
        if args.verbose:
            print(f"Random seed: {args.seed}")
    
    # Parse rate limit
    rate_limit = None
    if args.rate_limit:
        parts = args.rate_limit.split("/")
        if len(parts) == 2:
            rate_limit = (int(parts[0]), int(parts[1]))
        else:
            parser.error("Rate limit must be 'requests/seconds', e.g. '60/60'")
    
    # Determine TUI usage
    use_tui = args.tui or (RICH_AVAILABLE and not args.no_tui)
    
    if args.tui and not RICH_AVAILABLE:
        print("Warning: --tui requires rich. Install with: pip install runcue[sim]", file=sys.stderr)
        print("Falling back to simple display.", file=sys.stderr)
        use_tui = False
    
    # Build config
    config = SimConfig(
        count=args.count,
        latency_ms=args.latency,
        latency_jitter=args.jitter,
        outlier_chance=args.outliers,
        outlier_multiplier=args.outlier_mult,
        error_rate=args.error_rate,
        duration=args.duration,
        max_concurrent=args.concurrent,
        rate_limit=rate_limit,
        submit_rate=args.submit_rate,
        scenario=args.scenario,
        stall_timeout=args.timeout,
    )
    
    # Run with proper interrupt handling
    async def run_main():
        """Wrapper to handle signals properly."""
        import signal
        
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        
        def handle_signal():
            stop_event.set()
        
        # Set up signal handlers
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, handle_signal)
        
        # Create task for main work
        main_task = asyncio.create_task(run_with_display(config, use_tui=use_tui, verbose=args.verbose))
        stop_task = asyncio.create_task(stop_event.wait())
        
        # Wait for either completion or interrupt
        done, pending = await asyncio.wait(
            [main_task, stop_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        
        # Cancel pending tasks
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        
        # Check if we were interrupted
        if stop_task in done:
            print("\nInterrupted.")
            sys.exit(130)
    
    try:
        asyncio.run(run_main())
    except KeyboardInterrupt:
        # Fallback for immediate interrupts
        print("\nInterrupted.")
        sys.exit(130)


if __name__ == "__main__":
    main()
