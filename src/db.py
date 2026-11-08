"""Shared subscriber database (SQLite, functions only).

Used by both apps:
    optin.py   writes opt-ins / opt-outs
    server.py  reads who is opted in before each send

The database file lives at data/subscribers.db (created automatically).
Both apps find it from this file's location, so it works from any folder.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent      # project root (src/..)
DB_PATH = ROOT / "data" / "subscribers.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscribers (
    phone       TEXT PRIMARY KEY,   -- E.164, e.g. +16476686246
    postal_code TEXT,               -- for "nearby" filtering later
    opted_in    INTEGER NOT NULL,   -- 1 = in, 0 = out
    updated_at  TEXT NOT NULL       -- last opt-in/opt-out time (consent record)
)
"""


# ---------------------------------------------------------------- connection

def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # lets one app read while the other writes
    conn.execute(SCHEMA)
    return conn


def run(sql, params=()):
    """Run one statement in its own short connection and return the rows."""
    conn = connect()
    try:
        with conn:                      # commits on success
            return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- phone / postal helpers

def digits_of(number):
    return "".join(ch for ch in number if ch.isdigit())


def is_valid_phone(number):
    """North American numbers: 10 digits, or 11 starting with 1."""
    d = digits_of(number)
    return len(d) == 10 or (len(d) == 11 and d.startswith("1"))


def to_e164(number):
    """'647-668-6246' -> '+16476686246' (Twilio needs this format)."""
    d = digits_of(number)
    if len(d) == 10:
        d = "1" + d
    return "+" + d


def pretty_phone(e164):
    """'+16476686246' -> '647-668-6246' for display."""
    d = digits_of(e164)[-10:]
    return f"{d[:3]}-{d[3:6]}-{d[6:]}" if len(d) == 10 else e164


def clean_postal(code):
    """'r3c4t3' -> 'R3C 4T3'. Empty input -> None."""
    code = "".join((code or "").split()).upper()
    if not code:
        return None
    return f"{code[:3]} {code[3:]}" if len(code) == 6 else code


# ---------------------------------------------------------------- subscribers

def get_subscriber(phone):
    rows = run("SELECT * FROM subscribers WHERE phone = ?", (phone,))
    return dict(rows[0]) if rows else None


def subscribe(phone, postal_code=None):
    """Opt a number in. Returns True if this is a new or renewed opt-in,
    False if the number was already opted in."""
    existing = get_subscriber(phone)
    run("""
        INSERT INTO subscribers (phone, postal_code, opted_in, updated_at)
        VALUES (?, ?, 1, ?)
        ON CONFLICT(phone) DO UPDATE SET
            opted_in    = 1,
            postal_code = COALESCE(excluded.postal_code, subscribers.postal_code),
            updated_at  = CASE WHEN subscribers.opted_in = 1
                               THEN subscribers.updated_at ELSE excluded.updated_at END
    """, (phone, postal_code, now_iso()))
    return not (existing and existing["opted_in"])


def unsubscribe(phone):
    """Opt a number out (the row is kept as a record). Returns True if it was opted in."""
    existing = get_subscriber(phone)
    if not existing or not existing["opted_in"]:
        return False
    run("UPDATE subscribers SET opted_in = 0, updated_at = ? WHERE phone = ?",
        (now_iso(), phone))
    return True


def active_phones():
    """All opted-in numbers, oldest sign-up first."""
    rows = run("SELECT phone FROM subscribers WHERE opted_in = 1 ORDER BY updated_at")
    return [r["phone"] for r in rows]


def count_active():
    return run("SELECT COUNT(*) AS n FROM subscribers WHERE opted_in = 1")[0]["n"]
