#!/usr/bin/env python3
"""
Space News Dashboard

Fetches space news articles, downloads thumbnails, retrieves upcoming
launches, and generates a markdown report.

APIs used:
- Spaceflight News API: https://api.spaceflightnewsapi.net/v4/docs/
- The Space Devs Launch Library 2: https://ll.thespacedevs.com/

Demonstrates:
- Multiple API services with different rate limits
- Image downloading and thumbnail creation
- ICS calendar parsing
- Report generation from multiple data sources
- Complex dependency graph
"""

import asyncio
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

import httpx
from PIL import Image

import runcue

# Configuration
OUTPUT_DIR = Path("output")
ARTICLE_COUNT = 5
THUMBNAIL_SIZE = (200, 150)

# Create output directory
OUTPUT_DIR.mkdir(exist_ok=True)


def main():
    # Auto-fail work that's been waiting too long (e.g., due to failed dependencies)
    # This prevents infinite hangs when upstream tasks fail
    cue = runcue.Cue(pending_timeout=60)  # 60 second timeout

    # Define services with rate limits
    cue.service("spaceflight_news", rate="30/min", concurrent=2)
    cue.service("spacedevs", rate="15/min", concurrent=1)
    cue.service("download", concurrent=3)
    cue.service("local", concurrent=1)

    # --- Task: Fetch news articles ---
    @cue.task("fetch_articles", uses="spaceflight_news")
    async def fetch_articles(work):
        """Fetch latest space news articles."""
        print("  Fetching news articles...", flush=True)
        
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://api.spaceflightnewsapi.net/v4/articles/",
                params={"limit": ARTICLE_COUNT, "ordering": "-published_at"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        
        articles = data.get("results", [])
        
        # Save articles
        articles_file = OUTPUT_DIR / "articles.json"
        articles_file.write_text(json.dumps(articles, indent=2))
        
        # Submit image download tasks
        for i, article in enumerate(articles):
            if article.get("image_url"):
                await cue.submit("download_image", params={
                    "index": i,
                    "url": article["image_url"],
                    "article_id": article["id"],
                })
        
        print(f"  ✓ Got {len(articles)} articles", flush=True)
        return {"count": len(articles)}

    # --- Task: Download article image ---
    @cue.task("download_image", uses="download")
    async def download_image(work):
        """Download an article's image."""
        index = work.params["index"]
        url = work.params["url"]
        
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=30, follow_redirects=True)
            resp.raise_for_status()
        
        # Save original image
        ext = Path(url).suffix.split("?")[0] or ".jpg"  # Handle query strings
        if not ext.startswith("."):
            ext = ".jpg"
        image_path = OUTPUT_DIR / f"image_{index}{ext}"
        image_path.write_bytes(resp.content)
        
        # Submit thumbnail task
        await cue.submit("create_thumbnail", params={
            "index": index,
            "image_path": str(image_path),
        })
        
        print(f"  [article {index}] Downloaded image", flush=True)
        return {"path": str(image_path)}

    # --- Task: Create thumbnail ---
    @cue.task("create_thumbnail", uses="local")
    def create_thumbnail(work):
        """Create a thumbnail from the downloaded image."""
        index = work.params["index"]
        image_path = Path(work.params["image_path"])
        
        # Open and resize
        img = Image.open(image_path)
        img = img.convert("RGB")
        img.thumbnail(THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
        
        # Save thumbnail
        thumb_path = OUTPUT_DIR / f"thumb_{index}.jpg"
        img.save(thumb_path, "JPEG", quality=85)
        
        print(f"  [article {index}] Created thumbnail", flush=True)
        return {"path": str(thumb_path)}

    # --- Task: Fetch launches ---
    @cue.task("fetch_launches", uses="spacedevs")
    async def fetch_launches(work):
        """Fetch upcoming launches as ICS."""
        print("  Fetching upcoming launches...", flush=True)
        
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://ll.thespacedevs.com/launches/latest/feed.ics",
                timeout=15,
            )
            resp.raise_for_status()
        
        ics_content = resp.text
        
        # Save ICS file
        ics_path = OUTPUT_DIR / "launches.ics"
        ics_path.write_text(ics_content)
        
        # Parse launches from ICS
        launches = parse_ics(ics_content)
        
        # Save parsed launches
        launches_file = OUTPUT_DIR / "launches.json"
        launches_file.write_text(json.dumps(launches, indent=2, default=str))
        
        print(f"  ✓ Got {len(launches)} upcoming launches", flush=True)
        return {"count": len(launches)}

    # --- Task: Generate report ---
    @cue.task("generate_report", uses="local")
    def generate_report(work):
        """Generate markdown report from all data."""
        print("  Generating report...", flush=True)
        
        # Load articles
        articles = json.loads((OUTPUT_DIR / "articles.json").read_text())
        
        # Load launches
        launches = json.loads((OUTPUT_DIR / "launches.json").read_text())
        
        # Build markdown
        md = []
        md.append("# 🚀 Space News Dashboard")
        md.append("")
        md.append(f"*Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}*")
        md.append("")
        
        # News section
        md.append("## 📰 Latest News")
        md.append("")
        
        for i, article in enumerate(articles):
            md.append(f"### {article['title']}")
            md.append("")
            
            # Thumbnail
            thumb_path = OUTPUT_DIR / f"thumb_{i}.jpg"
            if thumb_path.exists():
                md.append(f"![{article['title']}](thumb_{i}.jpg)")
                md.append("")
            
            md.append(f"**Source:** {article.get('news_site', 'Unknown')}")
            md.append("")
            
            # Published date
            pub_date = article.get("published_at", "")
            if pub_date:
                try:
                    dt = datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
                    md.append(f"**Published:** {dt.strftime('%B %d, %Y')}")
                except:
                    pass
            md.append("")
            
            md.append(f"{article.get('summary', '')}")
            md.append("")
            md.append(f"[Read more]({article.get('url', '#')})")
            md.append("")
            md.append("---")
            md.append("")
        
        # Launches section
        md.append("## 🛰️ Upcoming Launches")
        md.append("")
        md.append("| Date | Mission | Location |")
        md.append("|------|---------|----------|")
        
        # Show next 10 launches
        for launch in launches[:10]:
            date_str = launch.get("date", "TBD")
            if date_str != "TBD":
                try:
                    dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                    date_str = dt.strftime("%b %d, %Y %H:%M UTC")
                except:
                    pass
            
            summary = launch.get("summary", "Unknown")
            location = launch.get("location", "Unknown")
            
            md.append(f"| {date_str} | {summary} | {location} |")
        
        md.append("")
        md.append("---")
        md.append("")
        md.append("*Data from [Spaceflight News API](https://spaceflightnewsapi.net/) and [The Space Devs](https://thespacedevs.com/)*")
        
        # Write report
        report_path = OUTPUT_DIR / "report.md"
        report_path.write_text("\n".join(md))
        
        print(f"  ✓ Report saved: {report_path}", flush=True)
        return {"path": str(report_path)}

    # --- is_ready: Check dependencies ---
    @cue.is_ready
    def is_ready(work):
        if work.task == "download_image":
            # Need articles fetched first
            return (OUTPUT_DIR / "articles.json").exists()
        
        if work.task == "create_thumbnail":
            # Need image downloaded
            image_path = work.params.get("image_path")
            return image_path and Path(image_path).exists()
        
        if work.task == "generate_report":
            # Need articles and launches
            if not (OUTPUT_DIR / "articles.json").exists():
                return False
            if not (OUTPUT_DIR / "launches.json").exists():
                return False
            
            # Need all thumbnails
            articles = json.loads((OUTPUT_DIR / "articles.json").read_text())
            for i, article in enumerate(articles):
                if article.get("image_url"):
                    if not (OUTPUT_DIR / f"thumb_{i}.jpg").exists():
                        return False
            return True
        
        return True

    # --- is_stale: Check if output exists ---
    @cue.is_stale
    def is_stale(work):
        if work.task == "fetch_articles":
            return not (OUTPUT_DIR / "articles.json").exists()
        
        if work.task == "fetch_launches":
            return not (OUTPUT_DIR / "launches.json").exists()
        
        if work.task == "download_image":
            index = work.params.get("index")
            return not any((OUTPUT_DIR / f"image_{index}{ext}").exists() 
                          for ext in [".jpg", ".jpeg", ".png", ".webp"])
        
        if work.task == "create_thumbnail":
            index = work.params.get("index")
            return not (OUTPUT_DIR / f"thumb_{index}.jpg").exists()
        
        if work.task == "generate_report":
            return not (OUTPUT_DIR / "report.md").exists()
        
        return True

    # --- Callbacks ---
    @cue.on_failure
    def on_failure(work, error):
        print(f"  ✗ {work.task} failed: {error}", flush=True)

    # --- Run ---
    async def run():
        print("\n🚀 Space News Dashboard Generator")
        print(f"   Fetching {ARTICLE_COUNT} articles + upcoming launches\n")
        
        # Clean previous run
        if OUTPUT_DIR.exists():
            shutil.rmtree(OUTPUT_DIR)
        OUTPUT_DIR.mkdir()
        
        cue.start()
        
        # Submit initial tasks (parallel)
        await cue.submit("fetch_articles", params={})
        await cue.submit("fetch_launches", params={})
        
        # Submit report task (will wait for is_ready)
        await cue.submit("generate_report", params={})
        
        # Wait for completion
        while True:
            pending = await cue.list(state=runcue.WorkState.PENDING)
            running = await cue.list(state=runcue.WorkState.RUNNING)
            if not pending and not running:
                break
            await asyncio.sleep(0.1)
        
        await cue.stop()
        
        print(f"\n✓ Done! Open {OUTPUT_DIR / 'report.md'}")

    asyncio.run(run())


def parse_ics(content: str) -> list[dict]:
    """Parse ICS content into launch data."""
    launches = []
    current = {}
    
    for line in content.split("\n"):
        line = line.strip()
        
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            if current:
                launches.append(current)
            current = {}
        elif ":" in line and current is not None:
            key, _, value = line.partition(":")
            
            # Handle folded lines (continuation)
            key = key.split(";")[0]  # Remove parameters like VALUE=DATE
            
            if key == "SUMMARY":
                current["summary"] = value
            elif key == "DTSTART":
                # Parse date
                try:
                    if "T" in value:
                        dt = datetime.strptime(value, "%Y%m%dT%H%M%SZ")
                        current["date"] = dt.isoformat() + "Z"
                    else:
                        current["date"] = value
                except:
                    current["date"] = value
            elif key == "LOCATION":
                current["location"] = value.split("|")[0].strip()
            elif key == "STATUS":
                current["status"] = value
    
    # Sort by date
    def sort_key(x):
        d = x.get("date", "")
        if d and d != "TBD":
            try:
                dt = datetime.fromisoformat(d.replace("Z", "+00:00"))
                # Make naive for comparison
                return dt.replace(tzinfo=None)
            except:
                pass
        return datetime(9999, 12, 31)
    
    launches.sort(key=sort_key)
    return launches


if __name__ == "__main__":
    main()

