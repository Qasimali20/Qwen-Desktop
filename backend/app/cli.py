"""Admin CLI.

  python -m app.cli create-user <username> [--admin] [--name "Display Name"] [--password PW]
  python -m app.cli reset-password <username> [--password PW]
  python -m app.cli list-users
"""
from __future__ import annotations

import argparse
import getpass
import secrets
import sys

from .auth import MIN_PASSWORD, hash_password
from .config import get_settings
from .db import Database


def _password(given: str | None) -> tuple[str, bool]:
    if given:
        return given, False
    if sys.stdin.isatty():
        pw = getpass.getpass("Password (blank = generate): ")
        if pw:
            return pw, False
    return secrets.token_urlsafe(12), True


def main() -> None:
    ap = argparse.ArgumentParser(prog="qwen-desktop")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-user")
    c.add_argument("username")
    c.add_argument("--admin", action="store_true")
    c.add_argument("--name")
    c.add_argument("--password")
    r = sub.add_parser("reset-password")
    r.add_argument("username")
    r.add_argument("--password")
    sub.add_parser("list-users")
    args = ap.parse_args()

    db = Database(get_settings().data_dir / "qwen-desktop.db")
    if args.cmd == "list-users":
        for u in db.list_users():
            print(f"{u['username']:<20} {u['role']:<6} {'disabled' if u['disabled'] else 'active'}")
        return

    pw, generated = _password(args.password)
    if len(pw) < MIN_PASSWORD:
        sys.exit(f"Password must be at least {MIN_PASSWORD} characters")

    if args.cmd == "create-user":
        if db.get_user_auth(args.username):
            sys.exit(f"User {args.username!r} already exists")
        first = db.count_users() == 0
        user = db.create_user(args.username, args.name or args.username, hash_password(pw),
                              "admin" if args.admin or first else "user")
        if first:
            db.claim_orphans(user["id"])  # existing chats/projects belong to the first account
        print(f"Created {user['role']} {user['username']!r}" + (" (claimed existing data)" if first else ""))
    else:
        u = db.get_user_auth(args.username)
        if not u:
            sys.exit(f"No user {args.username!r}")
        db.update_user(u["id"], {"password_hash": hash_password(pw)})
        db.delete_user_sessions(u["id"])
        print(f"Password reset for {args.username!r}")
    if generated:
        print(f"Generated password: {pw}")


if __name__ == "__main__":
    main()
