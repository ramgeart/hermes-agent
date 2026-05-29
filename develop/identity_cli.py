#!/usr/bin/env python3
"""CLI for IdentityManager — hermes identity subcommand.

Usage:
    python identity_cli.py init
    python identity_cli.py set --name "Maria" --birthday "2026-04-12"
    python identity_cli.py show
    python identity_cli.py status
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _get_manager():
    """Get IdentityManager with default db path."""
    try:
        from hermes_constants import get_hermes_home
        db_path = Path(get_hermes_home()) / "identity.db"
    except ImportError:
        db_path = Path.home() / ".hermes" / "identity.db"
    from agent.identity_manager import IdentityManager
    im = IdentityManager(db_path)
    im.initialize()
    return im


def cmd_init(args):
    """Initialize the identity database."""
    im = _get_manager()
    status = im.get_identity_status()
    print(f"\n  Identity DB initialized at: {im.db_path}")
    print(f"  Status: {'configured' if status['populated'] else 'empty'}")
    if status.get("days_alive"):
        print(f"  Day: {status['days_alive']}")
    print()
    im.close()


def cmd_set(args):
    """Set identity fields."""
    im = _get_manager()

    fields = {}
    for field in ("name", "display_name", "birthday", "email",
                   "proton_user", "personality", "voice_id", "avatar_url"):
        value = getattr(args, field, None)
        if value is not None:
            fields[field] = value

    if not fields:
        print("\n  Nothing to set. Use --name, --birthday, etc.")
        print("  Example: hermes identity set --name 'Maria' --birthday '2026-04-12'\n")
        im.close()
        return

    try:
        updated = im.set_identity(**fields)
        print("\n  Identity updated:")
        for k, v in updated.items():
            print(f"    {k}: {v}")

        # Show days_alive if birthday was set
        if "birthday" in updated:
            days = im.get_days_alive()
            if days:
                print(f"    days_alive: Day {days}")
        print()
    except ValueError as e:
        print(f"\n  Error: {e}\n", file=sys.stderr)
        sys.exit(1)
    finally:
        im.close()


def cmd_show(args):
    """Show the full identity."""
    im = _get_manager()
    identity = im.get_identity()

    if not identity or not any(identity.get(f) for f in
                                ("name", "birthday", "email", "personality")):
        print("\n  Identity not configured yet.")
        print("  Run: hermes identity set --name 'Name' --birthday 'YYYY-MM-DD'\n")
        im.close()
        return

    days = im.get_days_alive()
    prompt_block = im.get_identity_prompt_block()

    print()
    print("  ═══ IDENTITY ═══")
    for field in ("name", "display_name", "birthday", "email",
                   "proton_user", "personality", "voice_id", "avatar_url"):
        value = identity.get(field, "")
        if value:
            label = field.replace("_", " ").title()
            print(f"  {label}: {value}")

    if days:
        print(f"  Days Alive: Day {days}")
    print(f"  Created: {identity.get('created_at', '?')}")
    print()

    if args.prompt_block:
        print("  --- System Prompt Block ---")
        print(prompt_block)
        print()
    im.close()


def cmd_status(args):
    """Show identity status."""
    im = _get_manager()
    status = im.get_identity_status()

    print()
    if not status["initialized"]:
        print("  Identity: NOT INITIALIZED")
        print("  Run: hermes identity init\n")
        im.close()
        return

    print(f"  Initialized: yes")
    print(f"  Birthday: {status.get('birthday') or '(not set)'}")
    if status.get("days_alive"):
        print(f"  Day: {status['days_alive']}")

    print(f"\n  Populated ({len(status['populated'])}):")
    for f in status["populated"]:
        print(f"    + {f}")

    print(f"\n  Empty ({len(status['empty'])}):")
    for f in status["empty"]:
        print(f"    - {f}")
    print()
    im.close()


def main():
    parser = argparse.ArgumentParser(
        prog="hermes identity",
        description="Manage the agent's immutable identity",
    )
    sub = parser.add_subparsers(dest="command")

    # init
    sub.add_parser("init", help="Initialize the identity database")

    # set
    set_parser = sub.add_parser("set", help="Set identity fields")
    set_parser.add_argument("--name", help="Agent name")
    set_parser.add_argument("--display-name", dest="display_name", help="Display name")
    set_parser.add_argument("--birthday", help="Birthday (YYYY-MM-DD)")
    set_parser.add_argument("--email", help="Email address")
    set_parser.add_argument("--proton-user", dest="proton_user", help="Proton username")
    set_parser.add_argument("--personality", help="Personality traits")
    set_parser.add_argument("--voice-id", dest="voice_id", help="TTS voice ID")
    set_parser.add_argument("--avatar-url", dest="avatar_url", help="Avatar URL")

    # show
    show_parser = sub.add_parser("show", help="Show full identity")
    show_parser.add_argument("--prompt-block", action="store_true", default=True,
                              help="Also show system prompt block")

    # status
    sub.add_parser("status", help="Show identity field status")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    commands = {
        "init": cmd_init,
        "set": cmd_set,
        "show": cmd_show,
        "status": cmd_status,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
