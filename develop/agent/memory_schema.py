"""Memory schema — initializes conversation_index in state.db.

This module is a thin wrapper around agent.conversation_index.  The actual
schema, triggers, and FTS5 tables live in conversation_index.py to avoid
duplication.  This module provides the legacy ``initialize_memory_db`` entry
point and the migration helper for moving data from a standalone memory.db
into state.db.

Usage:
    from agent.memory_schema import initialize_memory_db
    initialize_memory_db("/opt/data/state.db")
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


def initialize_memory_db(db_path: str | Path) -> sqlite3.Connection:
    """Create or open state.db with the conversation_index schema.

    Delegates to ConversationIndex.initialize() which creates the table,
    FTS5 virtual table, triggers, and indexes in state.db alongside the
    sessions table.  FOREIGN KEY constraint on session_id referencing
    sessions(id) is enforced.

    Returns a sqlite3 connection to the database.
    """
    from agent.conversation_index import ConversationIndex

    ci = ConversationIndex(db_path)
    ci.initialize()
    conn = ci._get_conn()
    logger.info("conversation_index schema initialized at %s via memory_schema", db_path)
    return conn


def migrate_memory_to_state_db(
    old_db_path: str | Path,
    state_db_path: str | Path,
) -> int:
    """Migrate conversation_index data from a legacy memory.db to state.db.

    Copies all rows from the old conversation_index table into state.db,
    skipping any session_ids that don't exist in sessions (orphans).
    Returns the number of rows migrated.
    """
    old_path = Path(old_db_path)
    state_path = Path(state_db_path)

    if not old_path.exists():
        logger.info("No legacy memory.db at %s — nothing to migrate", old_path)
        return 0

    old_conn = sqlite3.connect(str(old_path))
    old_conn.row_factory = sqlite3.Row
    state_conn = sqlite3.connect(str(state_path))
    state_conn.row_factory = sqlite3.Row

    # Get valid session IDs from state.db
    valid_ids = {
        row[0] for row in
        state_conn.execute("SELECT id FROM sessions").fetchall()
    }

    # Read all rows from old conversation_index
    old_rows = old_conn.execute("SELECT * FROM conversation_index").fetchall()
    migrated = 0
    skipped_orphans = 0

    for row in old_rows:
        row_dict = dict(row)
        sid = row_dict.get("session_id")
        if sid not in valid_ids:
            skipped_orphans += 1
            continue

        # Map old columns — drop auto-increment PK, keep everything else
        row_dict.pop("id", None)
        row_dict.pop("idx_id", None)
        cols = list(row_dict.keys())
        vals = [row_dict[k] for k in cols]
        placeholders = ", ".join("?" * len(cols))
        col_names = ", ".join(cols)

        try:
            state_conn.execute(
                f"INSERT OR IGNORE INTO conversation_index ({col_names}) "
                f"VALUES ({placeholders})",
                vals,
            )
            migrated += 1
        except sqlite3.IntegrityError as e:
            logger.warning("Skipped row session_id=%s: %s", sid, e)

    state_conn.commit()
    old_conn.close()
    state_conn.close()

    if skipped_orphans:
        logger.warning(
            "Migration: %d orphan rows (no matching session) were dropped",
            skipped_orphans,
        )
    logger.info(
        "Migrated %d conversation_index rows from %s to %s",
        migrated, old_path, state_path,
    )
    return migrated
