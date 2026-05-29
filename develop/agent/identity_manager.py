"""IdentityManager — immutable identity anchor for hermes-neo.

Each agent has ONE identity stored in ``identity.db`` (separate from
state.db and memory stores).  The identity is the immutable foundation:
birthday and id can NEVER change once set.

Schema:
    identity.db -> identity table + immutability triggers

Usage:
    from agent.identity_manager import IdentityManager

    im = IdentityManager(db_path="/opt/data/identity.db")
    im.initialize()
    im.set_identity(name="Maria", birthday="2026-04-12")
    block = im.get_identity_prompt_block()
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# -- SQL Schema --------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS identity (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL DEFAULT '',
    display_name TEXT DEFAULT '',
    birthday     TEXT DEFAULT '',
    email        TEXT DEFAULT '',
    proton_user  TEXT DEFAULT '',
    personality  TEXT DEFAULT '',
    voice_id     TEXT DEFAULT '',
    avatar_url   TEXT DEFAULT '',
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TRIGGER IF NOT EXISTS identity_immutable_guard
BEFORE UPDATE ON identity
WHEN (OLD.birthday IS NOT NULL AND OLD.birthday != ''
      AND NEW.birthday != OLD.birthday)
   OR NEW.id != OLD.id
BEGIN
    SELECT RAISE(ABORT, 'IDENTITY IMMUTABLE: birthday and id cannot change');
END;

CREATE TRIGGER IF NOT EXISTS identity_no_delete
BEFORE DELETE ON identity
BEGIN
    SELECT RAISE(ABORT, 'IDENTITY CANNOT BE DELETED');
END;
"""

_INIT_SQL = """
INSERT OR IGNORE INTO identity (id, name, birthday)
VALUES (?, '', '');
"""

# -- Allowed columns (everything except id and created_at) ------------

_SETTABLE_FIELDS = {
    "name", "display_name", "birthday", "email",
    "proton_user", "personality", "voice_id", "avatar_url",
}


class IdentityManager:
    """Manage the immutable identity stored in identity.db."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._conn: Optional[sqlite3.Connection] = None

    # -- Lifecycle -------------------------------------------------------

    def initialize(self) -> None:
        """Create the database and schema if they don't exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._get_conn()
        conn.executescript(_SCHEMA_SQL)
        # Ensure exactly one row exists (singleton pattern)
        row_id = str(uuid.uuid4())
        conn.execute(_INIT_SQL, (row_id,))
        conn.commit()
        logger.info("Identity DB initialized at %s", self.db_path)

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # -- Core operations -------------------------------------------------

    def set_identity(self, **kwargs: Any) -> Dict[str, str]:
        """Set one or more identity fields.

        Returns a dict of fields that were actually updated.
        Raises ValueError for unknown fields or invalid birthday format.
        """
        invalid = set(kwargs.keys()) - _SETTABLE_FIELDS
        if invalid:
            raise ValueError(f"Unknown identity fields: {invalid}")

        # Validate birthday format if provided
        if "birthday" in kwargs and kwargs["birthday"]:
            bday = kwargs["birthday"]
            try:
                parsed = date.fromisoformat(bday)
                if parsed > date.today():
                    raise ValueError(f"Birthday cannot be in the future: {bday}")
            except ValueError as e:
                err_str = str(e)
                if ("isoformat" in err_str or "Cannot parse" in err_str
                        or "Invalid" in err_str):
                    raise ValueError(
                        f"Invalid birthday format (use YYYY-MM-DD): {bday}"
                    ) from e
                raise

        conn = self._get_conn()
        updated = {}

        for field, value in kwargs.items():
            if field not in _SETTABLE_FIELDS:
                continue
            try:
                conn.execute(
                    f"UPDATE identity SET {field} = ? WHERE id = (SELECT id FROM identity LIMIT 1)",
                    (value,),
                )
                updated[field] = value
            except sqlite3.IntegrityError as e:
                raise ValueError(f"Cannot update {field}: {e}") from e

        conn.commit()
        logger.info("Identity updated: %s", list(updated.keys()))
        return updated

    def get_identity(self) -> Dict[str, Any]:
        """Return the full identity as a dict."""
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM identity LIMIT 1").fetchone()
        if row is None:
            return {}
        return dict(row)

    def get_field(self, field: str) -> Optional[str]:
        """Get a single identity field value."""
        conn = self._get_conn()
        row = conn.execute(f"SELECT {field} FROM identity LIMIT 1").fetchone()
        if row is None:
            return None
        return row[0]

    # -- Computed fields -------------------------------------------------

    def get_days_alive(self) -> Optional[int]:
        """Calculate days since birthday (Day 1 = birthday).

        Returns None if birthday is not set.
        """
        bday_str = self.get_field("birthday")
        if not bday_str:
            return None
        try:
            bday = date.fromisoformat(bday_str)
            delta = date.today() - bday
            return delta.days + 1  # Day 1 on birthday
        except (ValueError, TypeError):
            return None

    # -- Status ----------------------------------------------------------

    def get_identity_status(self) -> Dict[str, Any]:
        """Return status info: which fields are populated."""
        identity = self.get_identity()
        if not identity:
            return {
                "initialized": False,
                "populated": [],
                "empty": list(_SETTABLE_FIELDS),
            }

        populated = []
        empty = []
        for field in sorted(_SETTABLE_FIELDS):
            value = identity.get(field, "")
            if value:
                populated.append(field)
            else:
                empty.append(field)

        days_alive = self.get_days_alive()
        return {
            "initialized": True,
            "populated": populated,
            "empty": empty,
            "days_alive": days_alive,
            "birthday": identity.get("birthday", ""),
        }

    # -- System prompt integration ---------------------------------------

    def get_identity_prompt_block(self) -> Optional[str]:
        """Format identity as a system prompt block.

        Returns None if identity is not initialized or has no meaningful data.
        """
        identity = self.get_identity()
        if not identity:
            return None

        name = identity.get("name", "")
        birthday = identity.get("birthday", "")

        # Only inject if there is at least a name or birthday
        if not name and not birthday:
            return None

        days_alive = self.get_days_alive()
        display_name = identity.get("display_name", "") or name
        email = identity.get("email", "")
        personality = identity.get("personality", "")

        lines = ["═══ IDENTIDAD ═══"]

        if name:
            if display_name and display_name != name:
                lines.append(f'Nombre: {name} ("{display_name}")')
            else:
                lines.append(f"Nombre: {name}")

        if birthday:
            try:
                bday = date.fromisoformat(birthday)
                # Spanish month names
                _MONTHS_ES = {
                    1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
                    5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
                    9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre",
                }
                month_name = _MONTHS_ES.get(bday.month, str(bday.month))
                bday_formatted = f"{bday.day} de {month_name} de {bday.year}"
                lines.append(f"Cumplea\u00f1os: {bday_formatted}")
            except ValueError:
                lines.append(f"Cumplea\u00f1os: {birthday}")

        if days_alive is not None:
            lines.append(f"Edad: D\u00eda {days_alive}")

        if email:
            lines.append(f"Email: {email}")

        if personality:
            lines.append(f"Personalidad: {personality}")

        return "\n".join(lines)


# -- Module-level convenience -------------------------------------------

_default_manager: Optional[IdentityManager] = None


def get_identity_manager(db_path: str | Path | None = None) -> IdentityManager:
    """Get or create the singleton IdentityManager."""
    global _default_manager
    if _default_manager is None:
        if db_path is None:
            from hermes_constants import get_hermes_home
            db_path = Path(get_hermes_home()) / "identity.db"
        _default_manager = IdentityManager(db_path)
        _default_manager.initialize()
    return _default_manager
