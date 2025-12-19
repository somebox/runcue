#!/usr/bin/env python3
"""
Mandelbrot Fractal Generator

Generates a Mandelbrot set image by computing tiles in parallel
and stitching them together.

Demonstrates:
- Parallel tile computation with concurrency limits
- Dependency-based aggregation (stitch waits for all tiles)
- Configurable parameters via command line
- Progress tracking as tiles complete

Usage:
    python main.py                           # Default 2048x2048, 256 iterations
    python main.py --size 4096 --iter 512    # Higher resolution
    python main.py --grid 8 --workers 8      # 8x8 tiles, 8 parallel workers
    python main.py --zoom -0.75,0.1,0.01     # Zoom into specific region
"""

import argparse
import asyncio
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

import runcue

# Output directory
OUTPUT_DIR = Path("output")


@dataclass
class FractalConfig:
    """Configuration for fractal generation."""
    size: int = 2048              # Output image size (square)
    iterations: int = 256         # Max iterations per pixel
    grid: int = 4                 # Grid size (grid × grid tiles)
    workers: int = 4              # Max parallel workers
    
    # Region to render (complex plane coordinates)
    center_x: float = -0.5        # Real axis center
    center_y: float = 0.0         # Imaginary axis center
    zoom: float = 1.5             # Zoom level (smaller = more zoomed in)
    
    # Color settings
    colormap: str = "fire"        # Built-in: fire, ocean, neon, electric, plasma, grayscale
    
    @property
    def tile_size(self) -> int:
        return self.size // self.grid
    
    @property
    def x_min(self) -> float:
        return self.center_x - self.zoom
    
    @property
    def x_max(self) -> float:
        return self.center_x + self.zoom
    
    @property
    def y_min(self) -> float:
        return self.center_y - self.zoom
    
    @property
    def y_max(self) -> float:
        return self.center_y + self.zoom


def compute_mandelbrot_tile(
    x_start: float, x_end: float,
    y_start: float, y_end: float,
    width: int, height: int,
    max_iter: int,
) -> np.ndarray:
    """Compute Mandelbrot set for a rectangular region.
    
    Returns an array of iteration counts (0 to max_iter).
    """
    # Create coordinate arrays
    x = np.linspace(x_start, x_end, width)
    y = np.linspace(y_start, y_end, height)
    X, Y = np.meshgrid(x, y)
    C = X + 1j * Y
    
    # Initialize arrays
    Z = np.zeros_like(C)
    M = np.zeros(C.shape, dtype=np.int32)
    
    # Iterate
    for i in range(max_iter):
        mask = np.abs(Z) <= 2
        Z[mask] = Z[mask] ** 2 + C[mask]
        M[mask] = i
    
    return M


def iterations_to_image(iterations: np.ndarray, max_iter: int, colormap: str) -> Image.Image:
    """Convert iteration counts to a colored image."""
    # Normalize to 0-1
    normalized = iterations.astype(np.float64) / max_iter
    
    # Built-in colormaps (no matplotlib needed)
    builtin_maps = {
        "fire": lambda t: (
            np.clip(t * 3, 0, 1),                    # R: rises first
            np.clip(t * 3 - 1, 0, 1),                # G: rises second  
            np.clip(t * 3 - 2, 0, 1),                # B: rises last
        ),
        "ocean": lambda t: (
            np.clip(t * 2 - 1, 0, 1),                # R: late
            np.clip(t * 2 - 0.5, 0, 1),              # G: mid
            np.clip(t * 1.5, 0, 1),                  # B: early
        ),
        "neon": lambda t: (
            (np.sin(t * np.pi * 2) + 1) / 2,         # R: oscillates
            (np.sin(t * np.pi * 2 + 2) + 1) / 2,     # G: phase shifted
            (np.sin(t * np.pi * 2 + 4) + 1) / 2,     # B: phase shifted
        ),
        "electric": lambda t: (
            np.where(t < 0.5, t * 2, 1.0),           # R: rises to 1
            np.where(t < 0.5, 0.0, (t - 0.5) * 2),   # G: rises second half
            1 - t,                                    # B: fades
        ),
        "plasma": lambda t: (
            np.clip(np.sin(t * np.pi) * 1.5, 0, 1),  # R: bulge in middle
            t ** 0.5,                                 # G: sqrt curve
            1 - t ** 2,                               # B: inverse square
        ),
        "grayscale": lambda t: (t, t, t),
    }
    
    # Try built-in first
    if colormap in builtin_maps:
        r, g, b = builtin_maps[colormap](normalized)
        rgb = np.stack([r * 255, g * 255, b * 255], axis=-1).astype(np.uint8)
        return Image.fromarray(rgb)
    
    # Try matplotlib colormap
    try:
        import matplotlib.pyplot as plt
        cmap = plt.get_cmap(colormap)
        colored = cmap(normalized)
        rgb = (colored[:, :, :3] * 255).astype(np.uint8)
        return Image.fromarray(rgb)
    except Exception:
        # Final fallback: use electric
        r, g, b = builtin_maps["electric"](normalized)
        rgb = np.stack([r * 255, g * 255, b * 255], axis=-1).astype(np.uint8)
        return Image.fromarray(rgb)


def main():
    parser = argparse.ArgumentParser(
        description="Generate Mandelbrot fractal using parallel tile computation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                              # Default settings
  python main.py --size 4096 --iter 512       # High resolution
  python main.py --grid 8 --workers 8         # More parallelism
  python main.py --zoom -0.75,0.1,0.01        # Zoom to x,y with scale
  python main.py --colormap viridis           # Different colors
        """,
    )
    parser.add_argument("--size", type=int, default=2048, help="Image size in pixels (default: 2048)")
    parser.add_argument("--iter", type=int, default=256, help="Max iterations (default: 256)")
    parser.add_argument("--grid", type=int, default=4, help="Grid size for tiles (default: 4)")
    parser.add_argument("--workers", type=int, default=4, help="Max parallel workers (default: 4)")
    parser.add_argument("--zoom", type=str, default=None, help="Zoom: center_x,center_y,scale (e.g., -0.75,0.1,0.01)")
    parser.add_argument("--colormap", type=str, default="fire", 
                        help="Colormap: fire, ocean, neon, electric, plasma, grayscale, or matplotlib name")
    
    args = parser.parse_args()
    
    # Build config
    config = FractalConfig(
        size=args.size,
        iterations=args.iter,
        grid=args.grid,
        workers=args.workers,
        colormap=args.colormap,
    )
    
    # Parse zoom if provided
    if args.zoom:
        parts = args.zoom.split(",")
        if len(parts) == 3:
            config.center_x = float(parts[0])
            config.center_y = float(parts[1])
            config.zoom = float(parts[2])
        else:
            parser.error("--zoom must be: center_x,center_y,scale")
    
    run_fractal(config)


class WorkerPool:
    """Track which worker slot is processing each task."""
    
    def __init__(self, num_workers: int):
        self._available = set(range(1, num_workers + 1))
        self._lock = threading.Lock()
    
    def acquire(self) -> int:
        """Get an available worker ID."""
        with self._lock:
            if self._available:
                return self._available.pop()
            return 0  # Fallback if pool exhausted
    
    def release(self, worker_id: int) -> None:
        """Return a worker ID to the pool."""
        with self._lock:
            self._available.add(worker_id)


def run_fractal(config: FractalConfig):
    """Generate fractal using runcue for parallel tile computation."""
    
    # No pending_timeout for batch jobs - work legitimately waits in queue
    # The failure tracking in this example handles actual errors
    cue = runcue.Cue()
    
    # Limit concurrent computation (CPU bound)
    cue.service("compute", concurrent=config.workers)
    cue.service("local", concurrent=1)
    
    # Track progress
    tiles_completed = [0]
    tiles_failed = set()  # Track which tiles failed
    total_tiles = config.grid * config.grid
    start_time = [0.0]
    workers = WorkerPool(config.workers)
    progress_lock = threading.Lock()
    
    # --- Task: Compute a single tile ---
    @cue.task("compute_tile", uses="compute")
    def compute_tile(work):
        """Compute one tile of the Mandelbrot set."""
        worker_id = workers.acquire()
        try:
            row = work.params["row"]
            col = work.params["col"]
            
            # Calculate tile boundaries in complex plane
            tile_width = (config.x_max - config.x_min) / config.grid
            tile_height = (config.y_max - config.y_min) / config.grid
            
            x_start = config.x_min + col * tile_width
            x_end = x_start + tile_width
            y_start = config.y_min + row * tile_height
            y_end = y_start + tile_height
            
            # Compute iterations
            iterations = compute_mandelbrot_tile(
                x_start, x_end,
                y_start, y_end,
                config.tile_size, config.tile_size,
                config.iterations,
            )
            
            # Convert to image and save
            tile_img = iterations_to_image(iterations, config.iterations, config.colormap)
            tile_path = OUTPUT_DIR / f"tile_{row}_{col}.png"
            tile_img.save(tile_path)
            
            with progress_lock:
                tiles_completed[0] += 1
                count = tiles_completed[0]
            elapsed = time.time() - start_time[0]
            pct = count / total_tiles * 100
            print(f"  [{count:2d}/{total_tiles}] W{worker_id} → Tile ({row},{col}) done - {pct:.0f}% ({elapsed:.1f}s)", flush=True)
            
            return {"path": str(tile_path)}
        finally:
            workers.release(worker_id)
    
    # --- Task: Stitch tiles together ---
    @cue.task("stitch_tiles", uses="local")
    def stitch_tiles(work):
        """Combine all tiles into final image."""
        print("  Stitching tiles...", flush=True)
        
        # Create output image
        final = Image.new("RGB", (config.size, config.size))
        
        # Paste each tile
        for row in range(config.grid):
            for col in range(config.grid):
                tile_path = OUTPUT_DIR / f"tile_{row}_{col}.png"
                tile = Image.open(tile_path)
                
                x = col * config.tile_size
                y = row * config.tile_size
                final.paste(tile, (x, y))
        
        # Save final image
        output_path = OUTPUT_DIR / "mandelbrot.png"
        final.save(output_path, "PNG")
        
        elapsed = time.time() - start_time[0]
        print(f"  ✓ Saved: {output_path} ({elapsed:.1f}s total)", flush=True)
        
        return {"path": str(output_path)}
    
    # --- is_ready: Stitch needs all tiles ---
    @cue.is_ready
    def is_ready(work):
        if work.task == "stitch_tiles":
            for row in range(config.grid):
                for col in range(config.grid):
                    if not (OUTPUT_DIR / f"tile_{row}_{col}.png").exists():
                        return False
            return True
        return True
    
    # --- is_stale: Check if output exists ---
    @cue.is_stale
    def is_stale(work):
        if work.task == "compute_tile":
            row = work.params["row"]
            col = work.params["col"]
            return not (OUTPUT_DIR / f"tile_{row}_{col}.png").exists()
        if work.task == "stitch_tiles":
            return not (OUTPUT_DIR / "mandelbrot.png").exists()
        return True
    
    # --- Callbacks ---
    @cue.on_failure
    def on_failure(work, error):
        print(f"  ✗ {work.task} failed: {error}", flush=True)
        if work.task == "compute_tile":
            with progress_lock:
                tiles_failed.add((work.params["row"], work.params["col"]))
    
    # --- Run ---
    async def run():
        print(f"\n🌀 Mandelbrot Fractal Generator")
        print(f"   Size: {config.size}×{config.size}, Iterations: {config.iterations}")
        print(f"   Grid: {config.grid}×{config.grid} ({total_tiles} tiles), Workers: {config.workers}")
        print(f"   Region: x=[{config.x_min:.4f}, {config.x_max:.4f}], y=[{config.y_min:.4f}, {config.y_max:.4f}]")
        print()
        
        # Clean previous run
        if OUTPUT_DIR.exists():
            shutil.rmtree(OUTPUT_DIR)
        OUTPUT_DIR.mkdir()
        
        start_time[0] = time.time()
        cue.start()
        
        # Submit all tile computation tasks
        for row in range(config.grid):
            for col in range(config.grid):
                await cue.submit("compute_tile", params={"row": row, "col": col})
        
        # Submit stitch task (will wait for is_ready)
        await cue.submit("stitch_tiles", params={})
        
        # Wait for completion
        while True:
            pending = await cue.list(state=runcue.WorkState.PENDING)
            running = await cue.list(state=runcue.WorkState.RUNNING)
            
            # Check for failures - abort early if tiles failed
            if tiles_failed:
                # Wait for running tasks to finish
                if not running:
                    await cue.stop()
                    print(f"\n✗ Aborted: {len(tiles_failed)} tile(s) failed", flush=True)
                    for row, col in sorted(tiles_failed):
                        print(f"   - Tile ({row},{col})", flush=True)
                    return
                await asyncio.sleep(0.1)
                continue
            
            if not pending and not running:
                break
            await asyncio.sleep(0.1)
        
        await cue.stop()
        
        # Check for any failures at the end
        failed = await cue.list(state=runcue.WorkState.FAILED)
        if failed:
            print(f"\n✗ Completed with {len(failed)} failure(s)")
            return
        
        print(f"\n✓ Done! Open {OUTPUT_DIR / 'mandelbrot.png'}")
    
    asyncio.run(run())


if __name__ == "__main__":
    main()

