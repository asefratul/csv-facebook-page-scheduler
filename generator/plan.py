"""Backlog (content-plan.csv), scheduling slots and appending rows to content.csv."""
import csv
import io
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

PLAN_FIELDS = ["kind", "link", "status", "generated_on", "idea"]


def timezone_name():
    return os.environ.get("POST_TIMEZONE") or "UTC"  # the same setting publish.py uses


def now_local():
    """Naive datetime in the posting time zone."""
    return datetime.now(ZoneInfo(timezone_name())).replace(tzinfo=None)


def parse_date(s):
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            d = datetime.strptime(s, fmt)
            return d.replace(hour=12) if fmt == "%Y-%m-%d" else d
        except ValueError:
            pass
    return None


def read_rows(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def future_count(rows, now):
    return sum(1 for r in rows if (d := parse_date(r.get("post_date"))) and d > now)


def next_slots(rows, now, spacing_days=4, lead_days=3, hour=12, n=1):
    """First slot: >= `lead_days` x 24 h from now (time to review) and >= `spacing_days` after the last post."""
    dates = [d for r in rows if (d := parse_date(r.get("post_date")))]
    earliest = now + timedelta(days=lead_days)
    if dates:
        earliest = max(earliest, max(dates) + timedelta(days=spacing_days))
    first = earliest.replace(hour=hour, minute=0, second=0, microsecond=0)
    if first < earliest:
        first += timedelta(days=1)
    return [(first + timedelta(days=spacing_days * i)).strftime("%Y-%m-%d %H:%M") for i in range(n)]


def append_rows(path, new_rows):
    """Append rows to content.csv without re-serialising it (keeps BOM and line endings byte-for-byte)."""
    with open(path, "rb") as f:
        raw = f.read()
    crlf = b"\r\n" in raw
    eol = "\r\n" if crlf else "\n"
    header = next(csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline="")))
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator=eol)
    for r in new_rows:
        unknown = set(r) - set(header)
        if unknown:
            raise ValueError(f"content.csv has no column(s) {sorted(unknown)}")
        w.writerow([r.get(h, "") for h in header])
    sep = b"" if raw.endswith(b"\n") or not raw else eol.encode()
    with open(path, "wb") as f:
        f.write(raw + sep + buf.getvalue().encode("utf-8"))


def read_plan(path):
    return read_rows(path)


def write_plan(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=PLAN_FIELDS, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in PLAN_FIELDS})


def next_pending(plan):
    for r in plan:
        if r.get("status", "").strip() == "pending":
            return r
    return None
