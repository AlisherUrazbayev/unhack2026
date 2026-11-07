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

import send as sms   # "as sms" so it doesn't clash with the send() function below

SHORT_FILE = Path("short_term.json")
LONG_FILE = Path("long_term.json")
STATE_FILE = Path("state.json")

SHORT_THRESHOLD = 0.5
SHORT_COOLDOWN = 20                   # seconds
LONG_THRESHOLD = 0.6
LONG_INTERVAL = 61 * 24 * 3600       # ~2 months, in seconds

# Sample numbers (users subscribed)
USERS = ["647-668-6246"]

MESSAGE_PREFIX = {"short": "Flood alert: ", "long": "Flood outlook: "}


def to_e164(number):
    """'647-668-6246' -> '+16476686246' (Twilio needs this format)."""
    digits = "".join(ch for ch in number if ch.isdigit())
    if len(digits) == 10:
        digits = "1" + digits
    return "+" + digits

# ---------------------------------------------------------------- state

feeds = {
    "short": {"file": SHORT_FILE, "mtime": None, "data": None},
    "long": {"file": LONG_FILE, "mtime": None, "data": None},
}
state = {
    "short": {"last_weight": None, "last_time": 0.0},
    "long": {"last_weight": None, "last_time": 0.0},
}
counts = {
    u: {"short": 0, "long": 0, "received": 0,
        "last": Text("-", style="dim"), "status": Text("idle", style="dim")}
    for u in USERS
}
totals = {"short": 0, "long": 0, "received": 0}
log = deque(maxlen=8)
pending = []   # (future, user, kind) for sends still waiting on a delivery report

def load_state():
    if STATE_FILE.exists():
        state.update(json.loads(STATE_FILE.read_text()))


def save_state():
    STATE_FILE.write_text(json.dumps(state, indent=2))


def read_feed(path):
    data = json.loads(path.read_text())
    if isinstance(data, list):
        data = data[-1]
    return {"description": str(data["description"]), "weight": float(data["weight"])}


def refresh_feeds():
    """Reload a feed only when its file has changed on disk."""
    for feed in feeds.values():
        try:
            mtime = feed["file"].stat().st_mtime
        except FileNotFoundError:
            continue
        if mtime == feed["mtime"]:
            continue
        try:
            feed["data"] = read_feed(feed["file"])
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            continue  # half-written or malformed; try again next tick
        feed["mtime"] = mtime
        log.append(Text.from_markup(
            f"[dim]{now_stamp()}[/] [blue]{feed['file'].name} loaded[/] "
            f"(weight {feed['data']['weight']:.2f})"))


# ---------------------------------------------------------------- decision logic

def should_send_short(weight, st, now):
    if st["last_weight"] is None:      # never sent before
        return True
    big_enough = abs(weight - st["last_weight"]) >= SHORT_THRESHOLD
    cooled = now - st["last_time"] >= SHORT_COOLDOWN
    return big_enough and cooled


def should_send_long(weight, st, now):
    if st["last_weight"] is None:
        return True
    due = now - st["last_time"] >= LONG_INTERVAL
    big_change = abs(weight - st["last_weight"]) >= LONG_THRESHOLD
    return due or big_change


def check_and_send(now):
    # Checked every tick, so a big change that lands during the cooldown
    # still goes out once the cooldown ends.
    for kind, check in (("short", should_send_short), ("long", should_send_long)):
        data = feeds[kind]["data"]
        if data and check(data["weight"], state[kind], now):
            send(kind, data, now)


# ---------------------------------------------------------------- sending

def send(kind, data, now):
    color = weight_color(data["weight"])
    label = "SHORT" if kind == "short" else "LONG"
    text = MESSAGE_PREFIX[kind] + data["description"]

    for u in USERS:   # later: only users near the flood area
        pending.append((sms.send_async(to_e164(u), text), u, kind))
        c = counts[u]
        c[kind] += 1
        totals[kind] += 1
        c["last"] = Text(f"[{label}] {data['description']}", style=color)
        c["status"] = Text("● sending", style="yellow")
        log.append(Text.from_markup(
            f"[dim]{now_stamp()}[/] [{color}]{label:<5}[/] → {u}: {escape(data['description'])}"))

    state[kind]["last_weight"] = data["weight"]
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
        c = counts[u]
        if status == "delivered":
            c["received"] += 1
            totals["received"] += 1
            c["status"] = Text("● delivered", style="green")
            log.append(Text.from_markup(f"[dim]{now_stamp()}[/] [green]✓ delivered[/] → {u}"))
        else:
            c["status"] = Text(f"● {status}", style="red")
            log.append(Text.from_markup(
                f"[dim]{now_stamp()}[/] [red]✗ {status}[/] → {u}"
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
        stat("Received", totals["received"], "green"),
        stat("Users", len(USERS)),
    )
    return grid


def build_feeds(now):
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()

    s, st = feeds["short"]["data"], state["short"]
    if s:
        cooldown = max(0, SHORT_COOLDOWN - (now - st["last_time"]))
        grid.add_row("Short-term", Text.from_markup(
            f"weight [b {weight_color(s['weight'])}]{s['weight']:.2f}[/] · "
            f"last sent {fmt(st['last_weight'])} · cooldown {cooldown:.0f}s"))
    else:
        grid.add_row("Short-term", Text(f"waiting for {SHORT_FILE.name}", style="dim"))

    l, lt = feeds["long"]["data"], state["long"]
    if l:
        next_due = (datetime.fromtimestamp(lt["last_time"] + LONG_INTERVAL).strftime("%Y-%m-%d")
                    if lt["last_weight"] is not None else "now")
        grid.add_row("Long-term", Text.from_markup(
            f"weight [b {weight_color(l['weight'])}]{l['weight']:.2f}[/] · "
            f"last sent {fmt(lt['last_weight'])} · next scheduled {next_due}"))
    else:
        grid.add_row("Long-term", Text(f"waiting for {LONG_FILE.name}", style="dim"))

    return Panel(grid, title="Feeds", border_style="blue")


def build_table():
    table = Table(expand=True, header_style="bold", border_style="dim", title="Nearby users")
    table.add_column("User")
    table.add_column("Short", justify="right")
    table.add_column("Long", justify="right")
    table.add_column("Received", justify="right")
    table.add_column("Last message", ratio=1)
    table.add_column("Status")
    for u in USERS:
        c = counts[u]
        table.add_row(u, str(c["short"]), str(c["long"]), str(c["received"]),
                      c["last"], c["status"])
    return table


def build_log():
    body = Group(*log) if log else Text("Waiting for first send…", style="dim")
    return Panel(body, title="Log", border_style="dim")


def build_screen(now):
    layout = Layout()
    layout.split_column(
        Layout(Text.from_markup(
            "[b]Flood Alert Showcase[/b]  ·  "
            + ("[yellow]DRY RUN[/]" if sms.DRY_RUN else "[green]LIVE SMS[/]")
            + "  ·  Ctrl+C to quit", justify="center"), size=1),
        Layout(build_stats(), size=4),
        Layout(build_feeds(now), size=4),
        Layout(build_table(), ratio=1),
        Layout(build_log(), size=10),
    )
    return layout


# ---------------------------------------------------------------- main loop

def main():
    load_state()
    with Live(build_screen(time.time()), screen=True, refresh_per_second=4) as live:
        try:
            while True:
                now = time.time()
                refresh_feeds()
                check_and_send(now)
                collect_results()
                live.update(build_screen(now))
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
    # Background threads may still be waiting on delivery reports;
    # exit right away instead of waiting up to 2 minutes for them.
    os._exit(0)


if __name__ == "__main__":
    main()
