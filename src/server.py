"""Flood alert dashboard (functions only, no classes).

Run from the project root:
    uv run --env-file twilio.env python src/server.py        (Ctrl+C to quit)

Data files (lists of {"date", "text", "weight"}):
    data/sample_data_short_term.json   recent changes in the water
    data/sample_data_long_term.json    seasonal outlooks

REPLAY = True  steps through both timelines, one entry every REPLAY_STEP seconds,
               using each entry's own date as the clock. Starts fresh every run.
REPLAY = False watches the files and treats the newest entry as current, using real
               time. Send history is saved in state.json so restarts don't re-send.

Short term: sent when the weight differs from the last SENT weight by >= SHORT_THRESHOLD
            (and SHORT_COOLDOWN has passed since the last short-term send).
Long term:  sent when LONG_INTERVAL has passed since the last long-term send, or when the
            weight differs from the last sent weight by >= LONG_THRESHOLD.

Recipients are everyone opted in through optin.py (data/subscribers.db), read fresh
before every send. Anyone who replies STOP is marked as opted out automatically.
"""

import json
import os
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from rich.console import Group
from rich.layout import Layout
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

import db
import send as sms   # "as sms" so it doesn't clash with the send() function below

ROOT = Path(__file__).resolve().parent.parent      # project root (src/..)
DATA_DIR = ROOT / "data"
SHORT_FILE = DATA_DIR / "sample_data_short_term.json"
LONG_FILE = DATA_DIR / "sample_data_long_term.json"
STATE_FILE = ROOT / "state.json"                   # only used when REPLAY = False

REPLAY = True
REPLAY_STEP = 6                       # real seconds between timeline entries

SHORT_THRESHOLD = 0.3
SHORT_COOLDOWN = 20                   # seconds
LONG_THRESHOLD = 0.6
LONG_INTERVAL = 55 * 24 * 3600        # ~2 months (outlooks arrive every ~2 months)

SUBSCRIBER_REFRESH = 2                # seconds between re-reading the subscriber list


# ---------------------------------------------------------------- state

feeds = {
    "short": {"file": SHORT_FILE, "mtime": None, "entries": [], "index": -1,
              "current": None, "new": False, "decision": Text("-", style="dim")},
    "long": {"file": LONG_FILE, "mtime": None, "entries": [], "index": -1,
             "current": None, "new": False, "decision": Text("-", style="dim")},
}
state = {
    "short": {"last_weight": None, "last_time": 0.0},
    "long": {"last_weight": None, "last_time": 0.0},
}
subscribers = []   # opted-in phones (E.164), refreshed from the database
counts = {}        # per-phone stats, created the first time a phone appears
totals = {"short": 0, "long": 0, "received": 0}
log = deque(maxlen=10)
pending = []   # (future, user, kind) for sends still waiting on a delivery report


def user_counts(phone):
    return counts.setdefault(phone, {
        "short": 0, "long": 0, "received": 0,
        "last": Text("-", style="dim"), "status": Text("subscribed", style="dim")})


def refresh_subscribers():
    """Re-read who is opted in (picks up sign-ups and opt-outs while running)."""
    global subscribers
    try:
        subscribers = db.active_phones()
    except Exception as e:   # e.g. database briefly locked; keep the last list
        log.append(Text(f"{now_stamp()} could not read subscribers: {e}", style="red"))
    for phone in subscribers:
        user_counts(phone)


def load_state():
    if not REPLAY and STATE_FILE.exists():
        state.update(json.loads(STATE_FILE.read_text()))


def save_state():
    if not REPLAY:
        STATE_FILE.write_text(json.dumps(state, indent=2))


def read_entries(path):
    """Load a timeline file -> list of entries sorted by date."""
    raw = json.loads(path.read_text())
    if isinstance(raw, dict):
        raw = [raw]
    entries = [{"date": datetime.fromisoformat(e["date"]),
                "text": str(e["text"]),
                "weight": float(e["weight"])} for e in raw]
    return sorted(entries, key=lambda e: e["date"])


def set_current(feed, index):
    feed["index"] = index
    feed["current"] = feed["entries"][index]
    feed["new"] = True


def load_files():
    """Load both files (live mode: only when they change on disk)."""
    for name, feed in feeds.items():
        try:
            mtime = feed["file"].stat().st_mtime
        except FileNotFoundError:
            continue
        if mtime == feed["mtime"]:
            continue
        try:
            entries = read_entries(feed["file"])
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            continue  # half-written or malformed; try again next tick
        feed["mtime"] = mtime
        feed["entries"] = entries
        log.append(Text.from_markup(
            f"[dim]{now_stamp()}[/] [blue]{feed['file'].name} loaded[/] ({len(entries)} entries)"))
        if not REPLAY and entries:
            newest = len(entries) - 1
            if feed["current"] is None or entries[newest]["date"] != feed["current"]["date"]:
                set_current(feed, newest)


def advance_replay():
    """Move each feed to its next timeline entry."""
    for feed in feeds.values():
        if feed["index"] + 1 < len(feed["entries"]):
            set_current(feed, feed["index"] + 1)


def replay_finished():
    return all(f["index"] >= len(f["entries"]) - 1 and not f["new"] for f in feeds.values())


def clock(feed):
    """Replay: the entry's own date. Live: real time."""
    if REPLAY and feed["current"]:
        return feed["current"]["date"].timestamp()
    return time.time()


# ---------------------------------------------------------------- decision logic

def decide_short(weight, st, now):
    if st["last_weight"] is None:
        return True, "first update"
    change = abs(weight - st["last_weight"])
    if change < SHORT_THRESHOLD:
        return False, f"change {change:.2f} < {SHORT_THRESHOLD}"
    if now - st["last_time"] < SHORT_COOLDOWN:
        return False, "cooldown"
    return True, f"change {change:.2f}"


def decide_long(weight, st, now):
    if st["last_weight"] is None:
        return True, "first outlook"
    days = (now - st["last_time"]) / 86400
    change = abs(weight - st["last_weight"])
    if now - st["last_time"] >= LONG_INTERVAL:
        return True, f"scheduled, {days:.0f} days since last"
    if change >= LONG_THRESHOLD:
        return True, f"big change {change:.2f}"
    return False, f"{days:.0f} days since last, change {change:.2f}"


def check_feeds():
    for kind, decide in (("short", decide_short), ("long", decide_long)):
        feed = feeds[kind]
        entry = feed["current"]
        if entry is None:
            continue
        now = clock(feed)

        if feed["new"]:
            ok, reason = decide(entry["weight"], state[kind], now)
            if ok:
                feed["new"] = False
                feed["decision"] = Text(f"sent · {reason}", style="green")
                send(kind, entry, now)
            elif reason == "cooldown":
                feed["decision"] = Text("waiting for cooldown", style="yellow")  # retried next tick
            else:
                feed["new"] = False
                feed["decision"] = Text(f"skipped · {reason}", style="dim")
                log.append(Text.from_markup(
                    f"[dim]{now_stamp()} SKIP  {kind:<5} weight {entry['weight']:.2f} ({reason})[/]"))

        elif (kind == "long" and not REPLAY and state["long"]["last_weight"] is not None
              and now - state["long"]["last_time"] >= LONG_INTERVAL):
            feed["decision"] = Text("sent · scheduled resend", style="green")
            send(kind, entry, now)


# ---------------------------------------------------------------- sending

def send(kind, entry, now):
    color = weight_color(entry["weight"])
    label = "SHORT" if kind == "short" else "LONG"
    text = entry["text"]
    parts = sms.sms_segments(text)

    refresh_subscribers()   # always send to the latest list
    if not subscribers:
        log.append(Text.from_markup(
            f"[dim]{now_stamp()}[/] [{color}]{label:<5}[/] [dim]no subscribers, nothing sent[/]"))

    for phone in subscribers:   # later: only subscribers near the flood area
        pending.append((sms.send_async(phone, text), phone, kind))
        c = user_counts(phone)
        c[kind] += 1
        totals[kind] += 1
        c["last"] = Text(f"[{label}] {text}", style=color)
        c["status"] = Text("● sending", style="yellow")
        log.append(Text.from_markup(
            f"[dim]{now_stamp()}[/] [{color}]{label:<5}[/] → {db.pretty_phone(phone)}: "
            f"{escape(text)}" + (f" [dim]({parts} SMS)[/]" if parts > 1 else "")))

    state[kind]["last_weight"] = entry["weight"]
    state[kind]["last_time"] = now
    save_state()


def collect_results():
    """Pick up sends that finished in the background and update the table."""
    for item in pending[:]:
        future, u, kind = item
        if not future.done():
            continue
        pending.remove(item)
        message_id, status, error = future.result()
        c = user_counts(u)
        name = db.pretty_phone(u)
        if status == "delivered":
            c["received"] += 1
            totals["received"] += 1
            c["status"] = Text("● delivered", style="green")
            log.append(Text.from_markup(f"[dim]{now_stamp()}[/] [green]✓ delivered[/] → {name}"))
        elif error and error.startswith(str(sms.ERROR_OPTED_OUT)):
            db.unsubscribe(u)   # they replied STOP; keep the database in sync
            c["status"] = Text("● replied STOP", style="red")
            log.append(Text.from_markup(
                f"[dim]{now_stamp()}[/] [red]✗ {name} replied STOP, marked as opted out[/]"))
        else:
            c["status"] = Text(f"● {status}", style="red")
            log.append(Text.from_markup(
                f"[dim]{now_stamp()}[/] [red]✗ {status}[/] → {name}"
                + (f": {escape(error)}" if error else "")))


# ---------------------------------------------------------------- display

def now_stamp():
    return datetime.now().strftime("%H:%M:%S")


def weight_color(w):
    if w >= 0.7:
        return "red"
    if w >= 0.4:
        return "yellow"
    return "green"


def fmt(w):
    return "-" if w is None else f"{w:.2f}"


def stat(label, value, color="cyan"):
    return Panel(Text.from_markup(f"[b]{value}[/b]\n[dim]{label}[/dim]", justify="center"),
                 border_style=color)


def build_stats():
    grid = Table.grid(expand=True)
    for _ in range(4):
        grid.add_column(ratio=1)
    grid.add_row(
        stat("Short-term sent", totals["short"], "yellow"),
        stat("Long-term sent", totals["long"], "magenta"),
        stat("Delivered", totals["received"], "green"),
        stat("Subscribers", len(subscribers)),
    )
    return grid


def build_feeds():
    table = Table(expand=True, box=None, header_style="bold")
    table.add_column("Feed")
    table.add_column("Step", justify="right")
    table.add_column("Date")
    table.add_column("Weight", justify="right")
    table.add_column("Last sent", justify="right")
    table.add_column("Decision", ratio=1)

    for kind, name in (("short", "Short-term"), ("long", "Long-term")):
        feed, st = feeds[kind], state[kind]
        entry = feed["current"]
        if entry is None:
            table.add_row(name, "-", Text(f"waiting for {feed['file'].name}", style="dim"),
                          "-", "-", "")
            continue
        table.add_row(
            name,
            f"{feed['index'] + 1}/{len(feed['entries'])}",
            entry["date"].strftime("%Y-%m-%d %H:%M"),
            Text(f"{entry['weight']:.2f}", style=f"bold {weight_color(entry['weight'])}"),
            fmt(st["last_weight"]),
            feed["decision"],
        )
    return Panel(table, title="Feeds", border_style="blue")


def build_table():
    table = Table(expand=True, header_style="bold", border_style="dim", title="Subscribers")
    table.add_column("Phone")
    table.add_column("Short", justify="right")
    table.add_column("Long", justify="right")
    table.add_column("Delivered", justify="right")
    table.add_column("Last message", ratio=1)
    table.add_column("Status")
    if not counts:
        table.add_row(Text("no one has opted in yet — run optin.py", style="dim"),
                      "", "", "", "", "")
    for phone, c in counts.items():
        active = phone in subscribers
        status = c["status"] if active else Text("opted out", style="dim")
        table.add_row(Text(db.pretty_phone(phone), style="" if active else "dim strike"),
                      str(c["short"]), str(c["long"]), str(c["received"]),
                      c["last"], status)
    return table


def build_log():
    body = Group(*log) if log else Text("Waiting for first update…", style="dim")
    return Panel(body, title="Log", border_style="dim")


def build_header():
    mode = "[yellow]DRY RUN[/]" if sms.DRY_RUN else "[green]LIVE SMS[/]"
    if REPLAY:
        data = "[cyan]replay finished[/]" if replay_finished() else "[cyan]REPLAY[/]"
    else:
        data = "[cyan]LIVE DATA[/]"
    return Text.from_markup(
        f"[b]Flood Alert Showcase[/b]  ·  {mode}  ·  {data}  ·  Ctrl+C to quit",
        justify="center")


def build_screen():
    layout = Layout()
    layout.split_column(
        Layout(build_header(), size=1),
        Layout(build_stats(), size=4),
        Layout(build_feeds(), size=5),
        Layout(build_table(), ratio=1),
        Layout(build_log(), size=12),
    )
    return layout


# ---------------------------------------------------------------- main loop

def main():
    load_state()
    load_files()
    refresh_subscribers()
    next_step = time.time()
    next_refresh = time.time() + SUBSCRIBER_REFRESH
    with Live(build_screen(), screen=True, refresh_per_second=4) as live:
        try:
            while True:
                if time.time() >= next_refresh:
                    refresh_subscribers()
                    next_refresh = time.time() + SUBSCRIBER_REFRESH
                if REPLAY:
                    if time.time() >= next_step:
                        advance_replay()
                        next_step = time.time() + REPLAY_STEP
                else:
                    load_files()
                check_feeds()
                collect_results()
                live.update(build_screen())
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
    # Background threads may still be waiting on delivery reports;
    # exit right away instead of waiting up to 2 minutes for them.
    os._exit(0)


if __name__ == "__main__":
    main()
