#!/usr/bin/env python3
"""
Dog Collage Generator

Fetches 9 random dog images from random.dog API, downloads them,
and combines into a 3x3 collage.

Demonstrates:
- Rate limiting (API calls)
- Dependencies (download waits for URL)
- Aggregation (collage waits for all downloads)
- Artifact-based scheduling
"""

import asyncio
import json
import shutil
from pathlib import Path

import httpx
from PIL import Image

import runcue

# Configuration
OUTPUT_DIR = Path("output")
GRID_SIZE = 3  # 3x3 grid
TOTAL_IMAGES = GRID_SIZE * GRID_SIZE
COLLAGE_WIDTH = 1024

# Create output directory
OUTPUT_DIR.mkdir(exist_ok=True)


def main():
    cue = runcue.Cue()

    # Rate limit the random.dog API (be nice!)
    cue.service("random_dog_api", rate="10/min", concurrent=2)
    cue.service("download", concurrent=3)
    cue.service("local", concurrent=1)

    # --- Task: Fetch random dog URL ---
    @cue.task("fetch_url", uses="random_dog_api")
    async def fetch_url(work):
        """Call random.dog API to get a random image URL."""
        slot = work.params["slot"]
        
        # Keep trying until we get an image (not video/gif)
        max_attempts = 10
        for _attempt in range(max_attempts):
            async with httpx.AsyncClient() as client:
                resp = await client.get("https://random.dog/woof.json", timeout=10)
                resp.raise_for_status()
                data = resp.json()
            
            url = data["url"]
            
            # Skip videos/gifs - we want images
            if url.endswith((".mp4", ".webm", ".gif")):
                print(f"  [slot {slot}] Skipped video: {url[:40]}...", flush=True)
                continue
            
            # Got an image!
            break
        else:
            raise RuntimeError(f"Couldn't get image URL after {max_attempts} attempts")
        
        # Save URL to artifact
        url_file = OUTPUT_DIR / f"url_{slot}.json"
        url_file.write_text(json.dumps({"url": url, "slot": slot}))
        
        # Submit download task
        await cue.submit("download_image", params={"slot": slot})
        
        print(f"  [slot {slot}] Got URL: {url[:50]}...", flush=True)
        return {"url": url}

    # --- Task: Download image ---
    @cue.task("download_image", uses="download")
    async def download_image(work):
        """Download the image from the URL."""
        slot = work.params["slot"]
        
        # Read URL from artifact
        url_file = OUTPUT_DIR / f"url_{slot}.json"
        data = json.loads(url_file.read_text())
        url = data["url"]
        
        # Download image
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=30, follow_redirects=True)
            resp.raise_for_status()
        
        # Save image
        ext = Path(url).suffix or ".jpg"
        image_file = OUTPUT_DIR / f"dog_{slot}{ext}"
        image_file.write_bytes(resp.content)
        
        print(f"  [slot {slot}] Downloaded: {image_file.name}", flush=True)
        return {"path": str(image_file)}

    # --- Task: Create collage ---
    @cue.task("create_collage", uses="local")
    def create_collage(work):
        """Combine all images into a 3x3 collage."""
        print("  Creating collage...", flush=True)
        
        # Find all downloaded images
        images = []
        for slot in range(TOTAL_IMAGES):
            # Find the image file (could be .jpg, .png, etc.)
            for img_file in OUTPUT_DIR.glob(f"dog_{slot}.*"):
                if img_file.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                    images.append((slot, img_file))
                    break
        
        if len(images) != TOTAL_IMAGES:
            raise RuntimeError(f"Expected {TOTAL_IMAGES} images, found {len(images)}")
        
        # Sort by slot
        images.sort(key=lambda x: x[0])
        
        # Calculate cell size
        cell_width = COLLAGE_WIDTH // GRID_SIZE
        cell_height = cell_width  # Square cells
        
        # Create collage canvas
        collage = Image.new("RGB", (COLLAGE_WIDTH, cell_height * GRID_SIZE), "white")
        
        # Place each image
        for slot, img_path in images:
            img = Image.open(img_path)
            
            # Resize to fit cell (maintain aspect ratio, crop to square)
            img = img.convert("RGB")
            
            # Crop to square from center
            w, h = img.size
            min_dim = min(w, h)
            left = (w - min_dim) // 2
            top = (h - min_dim) // 2
            img = img.crop((left, top, left + min_dim, top + min_dim))
            
            # Resize to cell size
            img = img.resize((cell_width, cell_height), Image.Resampling.LANCZOS)
            
            # Calculate position
            row = slot // GRID_SIZE
            col = slot % GRID_SIZE
            x = col * cell_width
            y = row * cell_height
            
            collage.paste(img, (x, y))
        
        # Save collage
        output_path = OUTPUT_DIR / "collage.jpg"
        collage.save(output_path, "JPEG", quality=90)
        
        print(f"  ✓ Collage saved: {output_path}", flush=True)
        return {"path": str(output_path)}

    # --- is_ready: Check if dependencies exist ---
    @cue.is_ready
    def is_ready(work):
        if work.task == "download_image":
            # Need URL artifact
            slot = work.params["slot"]
            return (OUTPUT_DIR / f"url_{slot}.json").exists()
        
        if work.task == "create_collage":
            # Need all images downloaded
            for slot in range(TOTAL_IMAGES):
                found = any(
                    (OUTPUT_DIR / f"dog_{slot}{ext}").exists()
                    for ext in [".jpg", ".jpeg", ".png", ".webp"]
                )
                if not found:
                    return False
            return True
        
        return True

    # --- is_stale: Check if output already exists ---
    @cue.is_stale
    def is_stale(work):
        if work.task == "fetch_url":
            slot = work.params["slot"]
            return not (OUTPUT_DIR / f"url_{slot}.json").exists()
        
        if work.task == "download_image":
            slot = work.params["slot"]
            return not any(
                (OUTPUT_DIR / f"dog_{slot}{ext}").exists()
                for ext in [".jpg", ".jpeg", ".png", ".webp"]
            )
        
        if work.task == "create_collage":
            return not (OUTPUT_DIR / "collage.jpg").exists()
        
        return True

    # --- Callbacks for logging ---
    @cue.on_complete
    def on_complete(work, result, duration):
        pass  # Already printing in tasks

    @cue.on_failure
    def on_failure(work, error):
        print(f"  ✗ {work.task} failed: {error}")

    # --- Run ---
    async def run():
        print("\n🐕 Dog Collage Generator")
        print(f"   Fetching {TOTAL_IMAGES} random dog images...\n")
        
        # Clean previous run
        if OUTPUT_DIR.exists():
            shutil.rmtree(OUTPUT_DIR)
        OUTPUT_DIR.mkdir()
        
        cue.start()
        
        # Submit URL fetch tasks
        for slot in range(TOTAL_IMAGES):
            await cue.submit("fetch_url", params={"slot": slot})
        
        # Submit collage task (will wait for is_ready)
        await cue.submit("create_collage", params={})
        
        # Wait for completion
        while True:
            pending = await cue.list(state=runcue.WorkState.PENDING)
            running = await cue.list(state=runcue.WorkState.RUNNING)
            if not pending and not running:
                break
            await asyncio.sleep(0.1)
        
        await cue.stop()
        
        print(f"\n✓ Done! Open {OUTPUT_DIR / 'collage.jpg'}")

    asyncio.run(run())


if __name__ == "__main__":
    main()

