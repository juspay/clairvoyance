#!/usr/bin/env python3
"""Switch clairvoyance's .env between the LOCAL and PROD Postgres profiles.

The five POSTGRES_* keys are the ONLY database wiring the app reads, so this
script rewrites exactly those lines in .env — every other line (API keys,
feature flags, comments) is preserved byte-for-byte. On EVERY switch, in BOTH
directions, ENABLE_DISPATCHER and ENABLE_BACKGROUND_TASKS are forced to false:
the same .env carries real Plivo/Exotel/Twilio credentials, and a dev machine
must never auto-dial leads no matter which database it is pointed at.
Re-enable those two flags by hand if you truly need them.

Profiles live next to .env as `.env.db.local` / `.env.db.prod` (chmod 600,
gitignored). Each holds only the five POSTGRES_* lines.

Usage:
    scripts/db_env_switch.py status            # which profile is active (no secrets printed)
    scripts/db_env_switch.py init --as prod    # snapshot current .env DB keys into a profile
    scripts/db_env_switch.py local             # point .env at the local profile
    scripts/db_env_switch.py prod --yes-prod   # point .env at prod (explicit flag required)

The active uvicorn only reads .env at startup — restart it after switching.
Passwords are never printed and never leave the profile files.
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
DB_KEYS = [
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
]
KILL_SWITCHES = {"ENABLE_DISPATCHER": "false", "ENABLE_BACKGROUND_TASKS": "false"}


def profile_path(name: str) -> Path:
    return ROOT / f".env.db.{name}"


def parse_env_lines(path: Path) -> list[str]:
    return path.read_text().splitlines(keepends=False)


def env_values(lines: list[str], keys: list[str]) -> dict[str, str]:
    vals: dict[str, str] = {}
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        if k in keys:
            vals[k] = v
    return vals


def chmod_600(path: Path) -> None:
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def load_profile(name: str) -> dict[str, str]:
    p = profile_path(name)
    if not p.exists():
        sys.exit(
            f"error: profile {p.name} not found — create it with "
            f"`db_env_switch.py init --as {name}` while .env points at {name}"
        )
    vals = env_values(parse_env_lines(p), DB_KEYS)
    missing = [k for k in DB_KEYS if k not in vals]
    if missing:
        sys.exit(f"error: profile {p.name} is missing keys: {', '.join(missing)}")
    return vals


def rewrite_env(new_db: dict[str, str]) -> None:
    """Replace the DB keys + kill switches in .env in place; keep everything else."""
    lines = parse_env_lines(ENV)
    replacements = {**new_db, **KILL_SWITCHES}
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        s = line.strip()
        key = s.partition("=")[0] if ("=" in s and not s.startswith("#")) else None
        if key is not None and key in replacements:
            out.append(f"{key}={replacements[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, val in replacements.items():
        if key not in seen:
            out.append(f"{key}={val}")
    backup = ENV.with_suffix(".bak")
    shutil.copy2(ENV, backup)
    chmod_600(backup)
    ENV.write_text("\n".join(out) + "\n")
    chmod_600(ENV)


def active_profile() -> str:
    """Match current .env against profiles by host/port/db/user (not password)."""
    cur = env_values(parse_env_lines(ENV), DB_KEYS)
    for name in ("local", "prod"):
        p = profile_path(name)
        if not p.exists():
            continue
        prof = env_values(parse_env_lines(p), DB_KEYS)
        if all(cur.get(k) == prof.get(k) for k in DB_KEYS[:4]):
            return name
    return "unknown"


def cmd_status() -> int:
    cur = env_values(parse_env_lines(ENV), DB_KEYS + list(KILL_SWITCHES))
    prof = active_profile()
    print(f"profile:  {prof}")
    print(f"host:     {cur.get('POSTGRES_HOST', '?')}:{cur.get('POSTGRES_PORT', '?')}")
    print(f"database: {cur.get('POSTGRES_DB', '?')}")
    print(f"user:     {cur.get('POSTGRES_USER', '?')}")
    for k in KILL_SWITCHES:
        print(f"{k.lower()}: {cur.get(k, '(unset)')}")
    if prof == "prod":
        print("\n*** .env POINTS AT PRODUCTION — reads only, writes are forbidden ***")
    return 0


def cmd_init(as_name: str) -> int:
    p = profile_path(as_name)
    if p.exists():
        sys.exit(
            f"error: {p.name} already exists — delete it first if you mean to re-snapshot"
        )
    cur = env_values(parse_env_lines(ENV), DB_KEYS)
    missing = [k for k in DB_KEYS if k not in cur]
    if missing:
        sys.exit(f"error: .env is missing keys: {', '.join(missing)}")
    p.write_text("\n".join(f"{k}={cur[k]}" for k in DB_KEYS) + "\n")
    chmod_600(p)
    print(f"snapshotted current .env DB keys into {p.name} (600)")
    return 0


def cmd_switch(name: str, yes_prod: bool) -> int:
    if name == "prod":
        if not yes_prod:
            sys.exit(
                "refusing to switch to PROD without --yes-prod\n"
                "PROD IS READS-ONLY: never run writes, seeds, or migrations against it."
            )
        print("*** SWITCHING .env TO PRODUCTION DATABASE ***")
        print("*** Reads only. Dispatcher + background tasks stay OFF.   ***")
    prof = load_profile(name)
    rewrite_env(prof)
    print(
        f".env now points at the {name.upper()} database "
        f"({prof['POSTGRES_HOST']}:{prof['POSTGRES_PORT']}/{prof['POSTGRES_DB']})"
    )
    print("kill switches forced: ENABLE_DISPATCHER=false ENABLE_BACKGROUND_TASKS=false")
    print("restart uvicorn for this to take effect")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    ini = sub.add_parser("init")
    ini.add_argument("--as", dest="as_name", choices=["local", "prod"], required=True)
    sub.add_parser("local")
    pr = sub.add_parser("prod")
    pr.add_argument("--yes-prod", action="store_true")
    args = ap.parse_args()
    if not ENV.exists():
        sys.exit(f"error: {ENV} not found")
    if args.cmd == "status":
        return cmd_status()
    if args.cmd == "init":
        return cmd_init(args.as_name)
    if args.cmd == "local":
        return cmd_switch("local", yes_prod=False)
    if args.cmd == "prod":
        return cmd_switch("prod", yes_prod=args.yes_prod)
    return 1


if __name__ == "__main__":
    sys.exit(main())
