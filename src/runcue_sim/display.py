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
    retrying: int = 0
    
    # Timing
    start_time: float = 0.0
    elapsed: float = 0.0
    
    # Services
    services: dict[str, ServiceStatus] = field(default_factory=dict)
    
    # Recent events (most recent first)
    events: list[EventRecord] = field(default_factory=list)
    max_events: int = 10
    
    # Config display
    target_count: int = 0
    latency_ms: int = 0
    latency_jitter: float = 0.2
    outlier_chance: float = 0.0
    error_rate: float = 0.0
    
    # Status flags
    backpressure: bool = False
    paused: bool = False
    
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
    """Rich-based TUI display for the simulator.
    
    Matches the design spec mockup with:
    - Queue stats panel
    - Services panel with progress bars
    - Recent events log
    - Controls footer
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
        """Build the main display layout matching design spec."""
        s = self.state
        
        # Build sections
        sections = []
        
        # Queue section
        sections.append(self._build_queue_section())
        
        # Services section
        sections.append(self._build_services_section())
        
        # Events section
        sections.append(self._build_events_section())
        
        # Controls section
        sections.append(self._build_controls_section())
        
        # Combine into layout
        layout = Layout()
        layout.split_column(
            Layout(name="queue", size=4),
            Layout(name="services", size=3 + len(s.services)),
            Layout(name="events", size=6),
            Layout(name="controls", size=3),
        )
        
        layout["queue"].update(sections[0])
        layout["services"].update(sections[1])
        layout["events"].update(sections[2])
        layout["controls"].update(sections[3])
        
        return Panel(
            layout,
            title="[bold cyan]runcue-sim[/bold cyan]",
            border_style="cyan",
        )
    
    def _build_queue_section(self) -> Panel:
        """Build queue stats panel matching design spec."""
        s = self.state
        
        # Main stats line
        stats = Table.grid(expand=True, padding=(0, 2))
        stats.add_column(justify="left")
        stats.add_column(justify="left")
        stats.add_column(justify="left")
        stats.add_column(justify="left")
        
        stats.add_row(
            f"[dim]Queued:[/dim] [bold]{s.queued:,}[/bold]",
            f"[dim]Running:[/dim] [bold yellow]{s.running}[/bold yellow]",
            f"[dim]Completed:[/dim] [bold green]{s.completed:,}[/bold green]",
            f"[dim]Failed:[/dim] [bold red]{s.failed}[/bold red]",
        )
        
        # Secondary stats line
        stats2 = Table.grid(expand=True, padding=(0, 2))
        stats2.add_column(justify="left")
        stats2.add_column(justify="left")
        stats2.add_column(justify="left")
        
        bp_status = "[red]ON[/red]" if s.backpressure else "[green]OFF[/green]"
        pct = s.progress * 100
        
        stats2.add_row(
            f"[dim]Backpressure:[/dim] {bp_status}",
            f"[dim]Progress:[/dim] [bold]{pct:.0f}%[/bold]",
            f"[dim]Throughput:[/dim] [bold]{s.throughput:.1f}/s[/bold]",
        )
        
        # Combine
        content = Table.grid(expand=True)
        content.add_row(stats)
        content.add_row(stats2)
        
        return Panel(content, title="[bold]Queue[/bold]", border_style="blue")
    
    def _build_services_section(self) -> Panel:
        """Build services panel with progress bars."""
        s = self.state
        
        table = Table(box=None, expand=True, padding=(0, 1), show_header=False)
        table.add_column("Service", width=12)
        table.add_column("Concurrent", width=24)
        table.add_column("Rate", width=12, justify="center")
        table.add_column("Status", width=10, justify="center")
        
        for name, svc in s.services.items():
            # Concurrent bar
            if svc.max_concurrent:
                pct = svc.current_concurrent / svc.max_concurrent
                bar = self._progress_bar(pct, 10)
                concurrent = f"{bar} {svc.current_concurrent}/{svc.max_concurrent} concurrent"
            else:
                concurrent = "[dim]—[/dim]"
            
            # Rate display
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
            
            table.add_row(f"[bold]{name}[/bold]", concurrent, rate, status)
        
        if not s.services:
            table.add_row("[dim]No services configured[/dim]", "", "", "")
        
        return Panel(table, title="[bold]Services[/bold]", border_style="blue")
    
    def _build_events_section(self) -> Panel:
        """Build recent events panel."""
        s = self.state
        
        table = Table(box=None, expand=True, padding=(0, 1), show_header=False)
        table.add_column("Time", width=10, style="dim")
        table.add_column("Event", width=14)
        table.add_column("ID", width=10)
        table.add_column("Task", width=12)
        table.add_column("Details")
        
        for event in s.events[:5]:
            time_str = event.timestamp.strftime("%H:%M:%S")
            
            # Style by event type
            event_styles = {
                "completed": "green",
                "failed": "red",
                "started": "yellow",
                "retrying": "magenta",
                "queued": "dim",
                "rate_limited": "cyan",
            }
            style = event_styles.get(event.event_type, "white")
            
            work_short = event.work_id[:8] if event.work_id else ""
            task = event.task_type or ""
            details = event.details[:25] if event.details else ""
            
            table.add_row(
                time_str,
                f"[{style}]{event.event_type}[/{style}]",
                work_short,
                task,
                details,
            )
        
        if not s.events:
            table.add_row("[dim]No events yet[/dim]", "", "", "", "")
        
        return Panel(table, title="[bold]Recent Events[/bold]", border_style="blue")
    
    def _build_controls_section(self) -> Panel:
        """Build controls/config footer."""
        s = self.state
        
        # Config info
        text = Text()
        text.append("Latency: ", style="dim")
        text.append(f"{s.latency_ms}ms", style="bold")
        if s.latency_jitter > 0:
            text.append(f" ±{s.latency_jitter*100:.0f}%", style="dim")
        if s.outlier_chance > 0:
            text.append(f"  Outliers: ", style="dim")
            text.append(f"{s.outlier_chance*100:.0f}%", style="bold yellow")
        text.append("  Error: ", style="dim")
        text.append(f"{s.error_rate*100:.0f}%", style="bold red" if s.error_rate > 0 else "bold")
        text.append("  Target: ", style="dim")
        text.append(f"{s.target_count:,}", style="bold")
        text.append("    Ctrl+C to stop", style="dim")
        
        return Panel(text, title="[bold]Config[/bold]", border_style="dim")
    
    @staticmethod
    def _progress_bar(pct: float, width: int = 10) -> str:
        """Create a mini progress bar."""
        pct = min(1.0, max(0.0, pct))
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
