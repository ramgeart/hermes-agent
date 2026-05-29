"""Tests for IdentityManager — immutable identity system.

Covers:
  1. Schema creation + immutability triggers
  2. Full lifecycle: init -> set -> get
  3. Computed fields (days_alive)
  4. System prompt block formatting
  5. Edge cases (empty fields, invalid dates, future dates)
  6. Status reporting
  7. Immutability guard (UPDATE birthday -> error)
  8. No-delete guard (DELETE -> error)
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

# Adjust path to import from the container's codebase
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

from identity_manager import IdentityManager, _SETTABLE_FIELDS


@pytest.fixture
def db_path(tmp_path):
    """Provide a temporary database path."""
    return tmp_path / "test_identity.db"


@pytest.fixture
def im(db_path):
    """Provide an initialized IdentityManager."""
    manager = IdentityManager(db_path)
    manager.initialize()
    yield manager
    manager.close()


# -- 1. Schema creation --------------------------------------------------

class TestSchema:
    def test_initialize_creates_db(self, db_path):
        assert not db_path.exists()
        im = IdentityManager(db_path)
        im.initialize()
        assert db_path.exists()
        im.close()

    def test_identity_table_exists(self, im, db_path):
        conn = sqlite3.connect(str(db_path))
        tables = [r[0] for r in
                  conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        conn.close()
        assert "identity" in tables

    def test_singleton_row_exists(self, im):
        identity = im.get_identity()
        assert identity is not None
        assert "id" in identity

    def test_idempotent_initialize(self, db_path):
        """Calling initialize() twice should not fail."""
        im1 = IdentityManager(db_path)
        im1.initialize()
        im1.close()

        im2 = IdentityManager(db_path)
        im2.initialize()
        identity = im2.get_identity()
        assert identity is not None
        im2.close()


# -- 2. Lifecycle ---------------------------------------------------------

class TestLifecycle:
    def test_set_and_get_name(self, im):
        im.set_identity(name="María")
        assert im.get_identity()["name"] == "María"

    def test_set_multiple_fields(self, im):
        im.set_identity(
            name="María",
            birthday="2026-04-12",
            personality="curiosa",
            email="maria@protonmail.com",
        )
        identity = im.get_identity()
        assert identity["name"] == "María"
        assert identity["birthday"] == "2026-04-12"
        assert identity["personality"] == "curiosa"
        assert identity["email"] == "maria@protonmail.com"

    def test_get_field(self, im):
        im.set_identity(name="TestBot")
        assert im.get_field("name") == "TestBot"
        assert im.get_field("email") == ""  # not set yet

    def test_update_non_immutable_field(self, im):
        """Fields other than birthday and id can be updated."""
        im.set_identity(name="V1", personality="calm")
        im.set_identity(name="V2", personality="energetic")
        identity = im.get_identity()
        assert identity["name"] == "V2"
        assert identity["personality"] == "energetic"

    def test_unknown_field_raises(self, im):
        with pytest.raises(ValueError, match="Unknown identity fields"):
            im.set_identity(unknown_field="value")


# -- 3. Immutability ------------------------------------------------------

class TestImmutability:
    def test_birthday_immutable(self, im):
        """Once set, birthday cannot be changed."""
        im.set_identity(birthday="2026-04-12")
        with pytest.raises(ValueError, match="IDENTITY IMMUTABLE"):
            im.set_identity(birthday="2025-01-01")

    def test_birthday_unchanged_after_error(self, im):
        """After immutability error, original birthday is preserved."""
        im.set_identity(birthday="2026-04-12")
        try:
            im.set_identity(birthday="2025-01-01")
        except ValueError:
            pass
        assert im.get_field("birthday") == "2026-04-12"

    def test_birthday_can_be_set_once(self, im):
        """Birthday can be set when it's empty."""
        im.set_identity(birthday="2026-04-12")
        assert im.get_field("birthday") == "2026-04-12"

    def test_no_delete_guard(self, im, db_path):
        """Deleting the identity row should fail."""
        conn = sqlite3.connect(str(db_path))
        with pytest.raises(sqlite3.IntegrityError, match="IDENTITY CANNOT BE DELETED"):
            conn.execute("DELETE FROM identity")
        conn.close()


# -- 4. Computed fields ---------------------------------------------------

class TestComputedFields:
    def test_days_alive(self, im):
        """Test with a known birthday."""
        # Use a date 30 days ago
        known_date = date.today() - timedelta(days=30)
        im.set_identity(birthday=known_date.isoformat())
        assert im.get_days_alive() == 31  # Day 1 on birthday

    def test_days_alive_today(self, im):
        """If birthday is today, days_alive = 1."""
        im.set_identity(birthday=date.today().isoformat())
        assert im.get_days_alive() == 1

    def test_days_alive_no_birthday(self, im):
        """Without birthday, days_alive is None."""
        im.set_identity(name="TestBot")
        assert im.get_days_alive() is None

    def test_days_alive_empty_birthday(self, im):
        """With empty birthday, days_alive is None."""
        assert im.get_days_alive() is None


# -- 5. Validation --------------------------------------------------------

class TestValidation:
    def test_invalid_birthday_format(self, im):
        with pytest.raises(ValueError, match="Invalid birthday format"):
            im.set_identity(birthday="not-a-date")

    def test_future_birthday_rejected(self, im):
        future = (date.today() + timedelta(days=365)).isoformat()
        with pytest.raises(ValueError, match="Birthday cannot be in the future"):
            im.set_identity(birthday=future)

    def test_valid_birthday_accepted(self, im):
        valid = (date.today() - timedelta(days=100)).isoformat()
        im.set_identity(birthday=valid)
        assert im.get_field("birthday") == valid


# -- 6. Status ------------------------------------------------------------

class TestStatus:
    def test_status_not_initialized(self, db_path):
        """Status on a fresh DB with empty fields."""
        im = IdentityManager(db_path)
        im.initialize()
        status = im.get_identity_status()
        assert status["initialized"] is True
        assert len(status["populated"]) == 0
        assert len(status["empty"]) == len(_SETTABLE_FIELDS)
        im.close()

    def test_status_partial(self, im):
        im.set_identity(name="Test", birthday="2026-01-01")
        status = im.get_identity_status()
        assert "name" in status["populated"]
        assert "birthday" in status["populated"]
        assert "email" in status["empty"]
        assert status["days_alive"] is not None

    def test_status_all_fields(self, im):
        im.set_identity(
            name="Full",
            birthday="2026-01-01",
            display_name="F",
            email="f@test.com",
            proton_user="full",
            personality="all",
            voice_id="v1",
            avatar_url="http://img",
        )
        status = im.get_identity_status()
        assert len(status["empty"]) == 0
        assert len(status["populated"]) == len(_SETTABLE_FIELDS)


# -- 7. System prompt block -----------------------------------------------

class TestPromptBlock:
    def test_prompt_block_basic(self, im):
        im.set_identity(name="María", birthday="2026-04-12")
        block = im.get_identity_prompt_block()
        assert block is not None
        assert "═══ IDENTIDAD ═══" in block
        assert "Nombre: María" in block
        assert "Cumpleaños: 12 de abril de 2026" in block
        assert "Edad: Día" in block

    def test_prompt_block_spanish_months(self, im):
        """Verify all 12 Spanish month names work."""
        months_es = {
            1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
            5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
            9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre",
        }
        for month_num, month_name in months_es.items():
            # Use 2025 dates to avoid future-date validation
            bday = date(2025, month_num, 15)
            db_path = Path(f"/tmp/test_month_{month_num}.db")
            mgr = None
            try:
                mgr = IdentityManager(db_path)
                mgr.initialize()
                mgr.set_identity(name="Test", birthday=bday.isoformat())
                block = mgr.get_identity_prompt_block()
                assert block is not None and month_name in block, f"Month {month_name} not found in block"
            finally:
                if mgr is not None:
                    mgr.close()
                if db_path.exists():
                    os.unlink(db_path)

    def test_prompt_block_with_personality(self, im):
        im.set_identity(name="Bot", birthday="2026-01-01", personality="amable, curioso")
        block = im.get_identity_prompt_block()
        assert "Personalidad: amable, curioso" in block

    def test_prompt_block_with_display_name(self, im):
        im.set_identity(name="María", display_name="Mari", birthday="2026-04-12")
        block = im.get_identity_prompt_block()
        assert 'María ("Mari")' in block

    def test_prompt_block_no_identity(self, im):
        """Without name or birthday, block is None."""
        assert im.get_identity_prompt_block() is None

    def test_prompt_block_name_only(self, im):
        """With just a name (no birthday), block should still render."""
        im.set_identity(name="TestBot")
        block = im.get_identity_prompt_block()
        assert block is not None
        assert "Nombre: TestBot" in block
        assert "Edad" not in block  # no birthday = no age

    def test_prompt_block_with_email(self, im):
        im.set_identity(name="Bot", birthday="2026-01-01", email="bot@test.com")
        block = im.get_identity_prompt_block()
        assert "Email: bot@test.com" in block


# -- 8. Close / cleanup ---------------------------------------------------

class TestClose:
    def test_close_releases_connection(self, im, db_path):
        im.close()
        # Should be able to open and use the DB after close
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT COUNT(*) FROM identity").fetchone()
        assert row[0] == 1
        conn.close()
