"""Rich-based display for runcue-sim.

This module provides visual output for the simulator using Rich library.
It's decoupled from the simulation logic - it just renders data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import Console

# Try to import Rich, provide fallback info if not available
try:
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
    from rich.table import Table
    from rich.text import Text

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False


@dataclass
class ServiceStatus:
    """Status of a service for display."""

    name: str
    max_concurrent: int | None = None
    current_concurrent: int = 0
    rate_limit: int | None = None
    rate_window: int | None = None
    current_rate: int = 0
    circuit_state: str = "closed"


@dataclass
class EventRecord:
    """A recent event for display."""

    timestamp: datetime
    event_type: str
    work_id: str
    task_type: str | None = None
    details: str = ""


@dataclass
class SimulationState:
    """Current state of the simulation for display.
    
    This is the data contract between the runner and display.
    The runner updates this; the display renders it.
    """

    # Queue stats
    submitted: int = 0
    queued: int = 0
    running: int = 0
    completed: int = 0
    failed: int = 0
    
    # Timing
    start_time: float = 0.0
    elapsed: float = 0.0
    
    # Services
    services: dict[str, ServiceStatus] = field(default_factory=dict)
    
    # Recent events (most recent first)
    events: list[EventRecord] = field(default_factory=list)
    max_events: int = 10
    
    # Config
    target_count: int = 0
    latency_ms: int = 0
    error_rate: float = 0.0
    
    @property
    def throughput(self) -> float:
        """Work units completed per second."""
        if self.elapsed > 0:
            return self.completed / self.elapsed
        return 0.0
    
    @property
    def progress(self) -> float:
        """Fraction complete (0.0 to 1.0)."""
        if self.submitted > 0:
            return (self.completed + self.failed) / self.submitted
        return 0.0
    
    def add_event(self, event_type: str, work_id: str, task_type: str | None = None, details: str = "") -> None:
        """Add an event to the display log."""
        self.events.insert(0, EventRecord(
            timestamp=datetime.now(),
            event_type=event_type,
            work_id=work_id,
            task_type=task_type,
            details=details,
        ))
        # Trim to max
        if len(self.events) > self.max_events:
            self.events = self.events[:self.max_events]


class SimulatorDisplay:
    """Rich-based display for the simulator.
    
    Usage:
        state = SimulationState()
        display = SimulatorDisplay(state)
        
        with display:
            # Update state in your loop
            state.completed += 1
            display.refresh()
    """
    
    def __init__(self, state: SimulationState, console: Console | None = None):
        if not RICH_AVAILABLE:
            raise ImportError(
                "Rich is required for the simulator display. "
                "Install with: pip install runcue[sim]"
            )
        
        self.state = state
        self.console = console or Console()
        self._live: Live | None = None
    
    def __enter__(self) -> "SimulatorDisplay":
        self._live = Live(
            self._build_layout(),
            console=self.console,
            refresh_per_second=10,
            screen=False,
        )
        self._live.__enter__()
        return self
    
    def __exit__(self, *args) -> None:
        if self._live:
            self._live.__exit__(*args)
            self._live = None
    
    def refresh(self) -> None:
        """Update the display with current state."""
        if self._live:
            self._live.update(self._build_layout())
    
    def _build_layout(self) -> Panel:
        """Build the main display layout."""
        layout = Layout()
        
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="footer", size=3),
        )
        
        layout["body"].split_column(
            Layout(name="queue", size=5),
            Layout(name="services", size=8),
            Layout(name="events"),
        )
        
        layout["header"].update(self._build_header())
        layout["queue"].update(self._build_queue_panel())
        layout["services"].update(self._build_services_panel())
        layout["events"].update(self._build_events_panel())
        layout["footer"].update(self._build_footer())
        
        return Panel(
            layout,
            title="[bold cyan]runcue-sim[/bold cyan]",
            border_style="cyan",
        )
    
    def _build_header(self) -> Panel:
        """Build the header with config info."""
        s = self.state
        text = Text()
        text.append("Target: ", style="dim")
        text.append(f"{s.target_count}", style="bold")
        text.append("  Latency: ", style="dim")
        text.append(f"{s.latency_ms}ms", style="bold")
        text.append("  Error rate: ", style="dim")
        text.append(f"{s.error_rate * 100:.0f}%", style="bold red" if s.error_rate > 0 else "bold")
        
        return Panel(text, border_style="dim")
    
    def _build_queue_panel(self) -> Panel:
        """Build the queue status panel."""
        s = self.state
        
        # Progress bar
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=40),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeElapsedColumn(),
            expand=True,
        )
        task = progress.add_task("Processing", total=s.submitted or 1, completed=s.completed + s.failed)
        
        # Stats line
        stats = Table.grid(expand=True)
        stats.add_column(justify="center")
        stats.add_column(justify="center")
        stats.add_column(justify="center")
        stats.add_column(justify="center")
        stats.add_column(justify="center")
        
        stats.add_row(
            f"[dim]Queued:[/dim] [bold]{s.queued}[/bold]",
            f"[dim]Running:[/dim] [bold yellow]{s.running}[/bold yellow]",
            f"[dim]Completed:[/dim] [bold green]{s.completed}[/bold green]",
            f"[dim]Failed:[/dim] [bold red]{s.failed}[/bold red]",
            f"[dim]Throughput:[/dim] [bold]{s.throughput:.1f}/s[/bold]",
        )
        
        # Combine
        table = Table.grid(expand=True)
        table.add_row(progress)
        table.add_row(stats)
        
        return Panel(table, title="[bold]Queue[/bold]", border_style="blue")
    
    def _build_services_panel(self) -> Panel:
        """Build the services status panel."""
        table = Table(expand=True, box=None, padding=(0, 1))
        table.add_column("Service", style="bold")
        table.add_column("Concurrent", justify="center")
        table.add_column("Rate", justify="center")
        table.add_column("Status", justify="center")
        
        for name, svc in self.state.services.items():
            # Concurrent bar
            if svc.max_concurrent:
                pct = svc.current_concurrent / svc.max_concurrent
                bar = self._mini_bar(pct, 10)
                concurrent = f"{bar} {svc.current_concurrent}/{svc.max_concurrent}"
            else:
                concurrent = "[dim]—[/dim]"
            
            # Rate
            if svc.rate_limit and svc.rate_window:
                rate = f"{svc.current_rate}/{svc.rate_limit}"
            else:
                rate = "[dim]—[/dim]"
            
            # Status indicator
            if svc.circuit_state == "closed":
                status = "[green]● OK[/green]"
            elif svc.circuit_state == "open":
                status = "[red]● OPEN[/red]"
            else:
                status = "[yellow]◐ HALF[/yellow]"
            
            table.add_row(name, concurrent, rate, status)
        
        if not self.state.services:
            table.add_row("[dim]No services configured[/dim]", "", "", "")
        
        return Panel(table, title="[bold]Services[/bold]", border_style="blue")
    
    def _build_events_panel(self) -> Panel:
        """Build the recent events panel."""
        table = Table(expand=True, box=None, padding=(0, 1))
        table.add_column("Time", style="dim", width=10)
        table.add_column("Event", width=15)
        table.add_column("Work ID", width=12)
        table.add_column("Details")
        
        for event in self.state.events[:8]:
            time_str = event.timestamp.strftime("%H:%M:%S")
            
            # Color by event type
            if event.event_type == "completed":
                event_style = "green"
            elif event.event_type == "failed":
                event_style = "red"
            elif event.event_type == "started":
                event_style = "yellow"
            else:
                event_style = "dim"
            
            work_short = event.work_id[:8] if event.work_id else ""
            details = event.details or event.task_type or ""
            
            table.add_row(
                time_str,
                f"[{event_style}]{event.event_type}[/{event_style}]",
                work_short,
                details[:30],
            )
        
        if not self.state.events:
            table.add_row("[dim]No events yet[/dim]", "", "", "")
        
        return Panel(table, title="[bold]Recent Events[/bold]", border_style="blue")
    
    def _build_footer(self) -> Panel:
        """Build the footer with controls hint."""
        return Panel(
            "[dim]Press Ctrl+C to stop[/dim]",
            border_style="dim",
        )
    
    @staticmethod
    def _mini_bar(pct: float, width: int = 10) -> str:
        """Create a mini progress bar."""
        filled = int(pct * width)
        empty = width - filled
        
        if pct >= 0.9:
            color = "red"
        elif pct >= 0.7:
            color = "yellow"
        else:
            color = "green"
        
        return f"[{color}]{'█' * filled}{'░' * empty}[/{color}]"


def print_simple_stats(state: SimulationState) -> None:
    """Print simple stats without Rich (fallback)."""
    s = state
    done = s.completed + s.failed
    pct = (done / s.submitted * 100) if s.submitted > 0 else 0
    
    print(
        f"\r[{done}/{s.submitted}] "
        f"Q:{s.queued} R:{s.running} ✓:{s.completed} ✗:{s.failed} "
        f"({pct:.0f}%) {s.throughput:.1f}/s",
        end="",
        flush=True,
    )

