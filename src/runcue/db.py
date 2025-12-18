"""Database operations for runcue."""

from __future__ import annotations

import aiosqlite

SCHEMA_VERSION = 1

SCHEMA = """
-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

-- Services: rate-limited or capacity-limited resources
CREATE TABLE IF NOT EXISTS services (
    name TEXT PRIMARY KEY,
    max_concurrent INTEGER,
    rate_limit INTEGER,
    rate_window INTEGER,
    circuit_state TEXT DEFAULT 'closed',
    circuit_threshold INTEGER DEFAULT 5,
    circuit_timeout INTEGER DEFAULT 300,
    circuit_failures INTEGER DEFAULT 0,
    circuit_last_failure REAL
);

-- Work units: the queue
CREATE TABLE IF NOT EXISTS work_units (
    id TEXT PRIMARY KEY,
    task_type TEXT NOT NULL,
    target TEXT,
    params TEXT,  -- JSON
    state TEXT DEFAULT 'queued',
    created_at REAL NOT NULL,
    started_at REAL,
    completed_at REAL,
    result TEXT,  -- JSON
    error TEXT,
    attempt INTEGER DEFAULT 1,
    max_attempts INTEGER DEFAULT 1,
    next_retry_at REAL,
    depends_on TEXT,  -- JSON array of work unit IDs
    idempotency_key TEXT,
    timeout REAL DEFAULT 300,
    dependency_timeout REAL,
    leased_by TEXT,
    lease_expires_at REAL
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_work_units_state ON work_units(state);
CREATE INDEX IF NOT EXISTS idx_work_units_task_type ON work_units(task_type);
CREATE INDEX IF NOT EXISTS idx_work_units_idempotency ON work_units(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_work_units_next_retry ON work_units(next_retry_at);

-- Service usage log for rate limiting
CREATE TABLE IF NOT EXISTS service_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service TEXT NOT NULL,
    work_unit_id TEXT NOT NULL,
    started_at REAL NOT NULL,
    completed_at REAL
);

CREATE INDEX IF NOT EXISTS idx_service_log_service_time ON service_log(service, started_at);
"""


async def init_db(db_path: str) -> aiosqlite.Connection:
    """
    Initialize database connection and schema.
    
    Args:
        db_path: Path to SQLite file or ":memory:" for in-memory.
        
    Returns:
        Open database connection.
    """
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    
    # Enable WAL mode for better concurrency
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    
    # Check if schema exists
    async with conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ) as cursor:
        exists = await cursor.fetchone()
    
    if not exists:
        # Fresh database - create schema
        await conn.executescript(SCHEMA)
        await conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        await conn.commit()
    
    return conn


async def save_service(conn: aiosqlite.Connection, service: dict) -> None:
    """Insert or update a service."""
    await conn.execute(
        """
        INSERT INTO services (
            name, max_concurrent, rate_limit, rate_window,
            circuit_state, circuit_threshold, circuit_timeout,
            circuit_failures, circuit_last_failure
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            max_concurrent = excluded.max_concurrent,
            rate_limit = excluded.rate_limit,
            rate_window = excluded.rate_window,
            circuit_threshold = excluded.circuit_threshold,
            circuit_timeout = excluded.circuit_timeout
        """,
        (
            service["name"],
            service.get("max_concurrent"),
            service.get("rate_limit"),
            service.get("rate_window"),
            service.get("circuit_state", "closed"),
            service.get("circuit_threshold", 5),
            service.get("circuit_timeout", 300),
            service.get("circuit_failures", 0),
            service.get("circuit_last_failure"),
        ),
    )
    await conn.commit()


async def get_service(conn: aiosqlite.Connection, name: str) -> dict | None:
    """Get a service by name."""
    async with conn.execute("SELECT * FROM services WHERE name = ?", (name,)) as cursor:
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_all_services(conn: aiosqlite.Connection) -> list[dict]:
    """Get all registered services."""
    async with conn.execute("SELECT * FROM services") as cursor:
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def save_work_unit(conn: aiosqlite.Connection, work: dict) -> None:
    """Insert or update a work unit."""
    columns = list(work.keys())
    placeholders = ", ".join(["?"] * len(columns))
    column_names = ", ".join(columns)
    
    await conn.execute(
        f"INSERT OR REPLACE INTO work_units ({column_names}) VALUES ({placeholders})",
        list(work.values()),
    )
    await conn.commit()


async def get_work_unit(conn: aiosqlite.Connection, work_id: str) -> dict | None:
    """Get a work unit by ID."""
    async with conn.execute("SELECT * FROM work_units WHERE id = ?", (work_id,)) as cursor:
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_work_by_idempotency_key(
    conn: aiosqlite.Connection, key: str
) -> dict | None:
    """Find work unit by idempotency key."""
    async with conn.execute(
        "SELECT * FROM work_units WHERE idempotency_key = ?", (key,)
    ) as cursor:
        row = await cursor.fetchone()
        return dict(row) if row else None


async def list_work_units(
    conn: aiosqlite.Connection,
    state: str | None = None,
    task_type: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """List work units with optional filters."""
    query = "SELECT * FROM work_units WHERE 1=1"
    params: list = []
    
    if state:
        query += " AND state = ?"
        params.append(state)
    if task_type:
        query += " AND task_type = ?"
        params.append(task_type)
    
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    
    async with conn.execute(query, params) as cursor:
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

