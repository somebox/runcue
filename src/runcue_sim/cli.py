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


async def run_with_display(config: SimConfig, use_tui: bool = True) -> None:
    """Run simulation with visual display."""
    state = SimulationState()
    runner = SimulationRunner(config, state)
    
    if use_tui and RICH_AVAILABLE:
        # Rich TUI display
        display = SimulatorDisplay(state)
        
        async def update_loop():
            """Background task to refresh display."""
            while True:
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
        print(f"\n🚀 runcue-sim")
        print(f"   Count: {config.count}, Latency: {config.latency_ms}ms, Error: {config.error_rate * 100:.0f}%")
        print()
        
        async def update_loop():
            """Print progress periodically."""
            while True:
                print_simple_stats(state)
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
        print(f"\n📈 Results:")
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
  runcue-sim --count 100 --tui  # Full TUI display (requires rich)
        """,
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
        "--db",
        type=str,
        default=":memory:",
        help="Database path (default: :memory:)",
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
        help="Show detailed logs (debug mode)",
    )
    
    args = parser.parse_args()
    
    # Configure logging early
    configure_logging(verbose=args.verbose)
    
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
        db_path=args.db,
        max_concurrent=args.concurrent,
        rate_limit=rate_limit,
        submit_rate=args.submit_rate,
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
        main_task = asyncio.create_task(run_with_display(config, use_tui=use_tui))
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
