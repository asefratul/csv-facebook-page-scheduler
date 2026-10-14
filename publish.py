#!/usr/bin/env python3
"""Publish the posts that are due from content.csv to a Facebook Page.

content.csv columns (header row required; header case and spaces are ignored):
    post_type      text | link | image | images | reel   (blank = text)
    post_content   the post text (caption for image posts)
    post_comment   optional first comment; blank = no comment
    post_date      YYYY-MM-DD HH:MM (24h, POST_TIMEZONE) or just YYYY-MM-DD
                   (a date alone means DEFAULT_POST_TIME on that day)
    media_url      optional column. link: a URL. image: a URL or a file path inside this repo
                   (for example media/play.png). images: 2-10 of those, separated by | (or new lines).
                   reel: a video (.mp4/.mov) in this repo or a public URL.
                   Also accepted as media_path / media_file.

Several rows may share a day. Each run publishes every row whose time has passed (within the
last CATCHUP_HOURS) and that is not yet listed in posted.csv, oldest first. content.csv is never
modified; published rows are recorded in posted.csv.

Environment:
    FB_PAGE_ID, FB_ACCESS_TOKEN   required unless DRY_RUN is set
    DRY_RUN=true                  print what would be posted, publish and write nothing
    POST_AS_OF                    pretend "now" is this time: 'YYYY-MM-DD HH:MM', or 'YYYY-MM-DD'
                                  (= the end of that day). For previews and catching up.
    POST_TIMEZONE                 IANA name such as Asia/Dhaka or America/New_York; default UTC
    DEFAULT_POST_TIME             HH:MM for date-only rows, default 19:00
    CATCHUP_HOURS                 how late a run may still publish a due row, default 6
    FB_GRAPH_VERSION              Graph API version (default below)
"""
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

GRAPH_VERSION = os.getenv("FB_GRAPH_VERSION", "v25.0")
GRAPH_URL = f"https://graph.facebook.com/{GRAPH_VERSION}"
SCHEDULE_FILE = os.getenv("SCHEDULE_FILE", "content.csv")
LOG_FILE = os.getenv("POSTED_LOG_FILE", "posted.csv")
LOG_FIELDS = ["scheduled_for", "facebook_post_id", "posted_at_utc"]
TIMEZONE = os.getenv("POST_TIMEZONE") or "UTC"
DEFAULT_POST_TIME = os.getenv("DEFAULT_POST_TIME") or "19:00"
CATCHUP_HOURS = float(os.getenv("CATCHUP_HOURS", "6"))
REQUIRED_COLUMNS = {"post_type", "post_content", "post_comment", "post_date"}
HEADER_ALIASES = {"media_url": "media", "media_path": "media", "media_file": "media"}
POST_TYPES = {"text", "link", "image", "images", "reel"}
MAX_IMAGES = 10
VIDEO_EXTENSIONS = (".mp4", ".mov")
MAX_VIDEO_BYTES = 100 * 1024 * 1024  # GitHub rejects files over 100 MB
REEL_POLL_SECONDS = 10
REEL_WAIT_SECONDS = 300
WHEN_FORMATS = (
    ("%Y-%m-%d %H:%M:%S", True),
    ("%Y-%m-%d %H:%M", True),
    ("%Y-%m-%dT%H:%M", True),
    ("%Y-%m-%d", False),
)
UPCOMING_WARNING = 3  # warn when fewer than this many future posts are scheduled
MISSED_LOOKBACK_DAYS = 7
TIMEOUT = 30
UPLOAD_TIMEOUT = 120
VIDEO_UPLOAD_TIMEOUT = (30, 300)  # (connect, read) seconds


class MediaError(Exception):
    """Something failed before the post could go live, so it is safe to retry on the next run."""


def fail(message):
    print(f"::error::{message}")
    sys.exit(1)


def warn(message):
    print(f"::warning::{message}")


def get_tz():
    try:
        return ZoneInfo(TIMEZONE)
    except Exception:
        fail(f"Unknown POST_TIMEZONE '{TIMEZONE}'.")


def default_time():
    try:
        hour, minute = (int(part) for part in DEFAULT_POST_TIME.split(":"))
        datetime(2000, 1, 1, hour, minute)
        return hour, minute
    except ValueError:
        fail(f"DEFAULT_POST_TIME '{DEFAULT_POST_TIME}' must be HH:MM (24h).")


def parse_when(text, tz):
    text = text.strip()
    for fmt, has_time in WHEN_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if not has_time:
            hour, minute = default_time()
            dt = dt.replace(hour=hour, minute=minute)
        return dt.replace(second=0, microsecond=0, tzinfo=tz)
    raise ValueError(text)


def normalize_header(name):
    name = re.sub(r"[\s\-]+", "_", (name or "").strip().lower())
    return HEADER_ALIASES.get(name, name)


def is_url(value):
    return bool(re.match(r"^https?://", value, re.I))


def split_media(value):
    """The items of a multi-image media cell: separated by | or new lines, blanks ignored."""
    return [part.strip() for part in re.split(r"[|\n]", value) if part.strip()]


def local_media_path(value):
    """A safe path inside the repo, or None if the value is absolute or escapes the repo."""
    norm = os.path.normpath(value.replace("\\", "/"))
    if os.path.isabs(norm) or norm == ".." or norm.startswith(".." + os.sep):
        return None
    return norm


# Signs that a spreadsheet export mangled the text (wrong encoding): the replacement character, the
# Apple-logo character left by Mac Roman, common UTF-8-read-as-Latin-1 sequences, or mostly '?'.
GARBLED = re.compile("[\ufffd\uf8ff]|\u00f0\u0178|\u00e0\u00a6|\u00c3[\u0080-\u00bf]")


def looks_garbled(text):
    if GARBLED.search(text):
        return True
    letters = [c for c in text if not c.isspace()]
    return len(letters) >= 6 and text.count("?") >= 3 and text.count("?") / len(letters) > 0.3


def check_image(media):
    """Problem with one image reference (a URL or a repo path), or None."""
    if is_url(media):
        return None
    path = local_media_path(media)
    if path is None:
        return f"media path '{media}' must be relative and inside the repo"
    if not os.path.isfile(path):
        return f"image file '{media}' was not found in the repo"
    return None


def validate(row):
    kind, media = row["type"], row["media"]
    for field, label in (("content", "post_content"), ("comment", "post_comment")):
        if looks_garbled(row[field]):
            return f"{label} looks garbled (encoding problem). Re-export the sheet as CSV UTF-8 and check emoji and non-English text"
    if kind not in POST_TYPES:
        return f"post_type '{kind}' must be one of: {', '.join(sorted(POST_TYPES))}"
    if kind in {"text", "link"} and not row["content"]:
        return "post_content is empty"
    if kind == "link" and not is_url(media):
        return "media_url must be a full http(s) link for 'link' posts"
    if kind == "image":
        if not media:
            return "media_url is required for 'image' posts (a URL or a file path in the repo)"
        return check_image(media)
    if kind == "images":
        items = split_media(media)
        if not 2 <= len(items) <= MAX_IMAGES:
            return (f"'images' posts need 2 to {MAX_IMAGES} images in media_url, separated by | "
                    f"(found {len(items)}); use 'image' for a single picture")
        for item in items:
            problem = check_image(item)
            if problem:
                return problem
    if kind == "reel":
        if not media:
            return "media_url is required for 'reel' posts (a video file in the repo or a public URL)"
        if not is_url(media):
            path = local_media_path(media)
            if path is None:
                return f"media path '{media}' must be relative and inside the repo"
            if not path.lower().endswith(VIDEO_EXTENSIONS):
                return f"reel video '{media}' must be an .mp4 or .mov file"
            if not os.path.isfile(path):
                return f"reel video '{media}' was not found in the repo"
            if os.path.getsize(path) > MAX_VIDEO_BYTES:
                return f"reel video '{media}' is over {MAX_VIDEO_BYTES // (1024 * 1024)} MB"
    return None


def parse_row(line, raw, tz):
    def get(key):
        value = raw.get(key)
        return value.strip() if isinstance(value, str) else ""

    row = {
        "line": line,
        "type": (get("post_type") or "text").lower(),
        "content": get("post_content"),
        "comment": get("post_comment"),
        "media": get("media"),
        "when": None,
        "key": None,
        "error": None,
    }
    try:
        row["when"] = parse_when(get("post_date"), tz)
    except ValueError:
        row["error"] = f"post_date '{get('post_date')}' must look like 2026-10-08 or 2026-10-08 09:00"
        return row
    row["error"] = validate(row)
    return row


def load_schedule(tz):
    if not os.path.exists(SCHEDULE_FILE):
        fail(f"{SCHEDULE_FILE} not found.")
    with open(SCHEDULE_FILE, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            fail(f"{SCHEDULE_FILE} is empty - it needs a header row.")
        reader.fieldnames = [normalize_header(h) for h in reader.fieldnames]
        missing = REQUIRED_COLUMNS - set(reader.fieldnames)
        if missing:
            fail(f"{SCHEDULE_FILE} is missing column(s): {', '.join(sorted(missing))}")
        rows = []
        for line, raw in enumerate(reader, start=2):
            if not any(isinstance(v, str) and v.strip() for v in raw.values()):
                continue  # blank line
            rows.append(parse_row(line, raw, tz))
    # A row's identity is its scheduled minute (+ #2, #3 ... if several rows share it).
    seen = {}
    for row in rows:
        if row["when"]:
            base = row["when"].strftime("%Y-%m-%dT%H:%M")
            seen[base] = seen.get(base, 0) + 1
            row["key"] = base if seen[base] == 1 else f"{base}#{seen[base]}"
    return rows


def load_posted_keys():
    if not os.path.exists(LOG_FILE):
        return set()
    with open(LOG_FILE, "r", newline="", encoding="utf-8") as f:
        return {(r.get("scheduled_for") or "").strip() for r in csv.DictReader(f)}


def log_post(key, post_id):
    is_new = not os.path.exists(LOG_FILE) or os.path.getsize(LOG_FILE) == 0
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        if is_new:
            writer.writerow(LOG_FIELDS)
        writer.writerow([key, post_id, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")])


def resolve_now(tz):
    """Return (now, whole_day). A bare-date POST_AS_OF means 'the whole of that day'."""
    override = os.getenv("POST_AS_OF", "").strip()
    if not override:
        return datetime.now(tz).replace(second=0, microsecond=0), False
    try:
        when = parse_when(override, tz)
    except ValueError:
        fail(f"POST_AS_OF '{override}' must be 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD'.")
    if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", override):
        return when.replace(hour=23, minute=59), True
    return when, False


def graph_post(path, data, token, files=None, timeout=TIMEOUT):
    response = requests.post(
        f"{GRAPH_URL}/{path}",
        data={**data, "access_token": token},
        files=files,
        timeout=timeout,
    )
    return parse_json(response)


def parse_json(response):
    try:
        body = response.json()
    except ValueError:
        return {"error": {"message": f"Non-JSON response (HTTP {response.status_code})"}}
    return body if isinstance(body, dict) else {"error": {"message": f"Unexpected response: {body!r}"[:200]}}


def describe_error(body):
    if isinstance(body, dict) and "error" not in body and body:
        return f"unexpected response: {str(body)[:300]}"
    error = body.get("error", {}) if isinstance(body, dict) else {}
    return f"{error.get('message', 'unknown error')} (code {error.get('code')}, subcode {error.get('error_subcode')})"


def publish_images(page_id, token, row):
    """Upload each image unpublished, then publish one post with all of them attached.

    Failures while uploading raise MediaError: nothing is public yet (the unpublished photos are
    invisible), so the row is retried on the next run. Only the final feed call can be UNCERTAIN.
    """
    photo_ids = []
    for media in split_media(row["media"]):
        try:
            if is_url(media):
                body = graph_post(f"{page_id}/photos", {"url": media, "published": "false"}, token)
            else:
                with open(local_media_path(media), "rb") as image:
                    body = graph_post(f"{page_id}/photos", {"published": "false"}, token,
                                      files={"source": image}, timeout=UPLOAD_TIMEOUT)
        except (requests.RequestException, OSError) as exc:
            raise MediaError(f"uploading '{media}' failed ({type(exc).__name__}: {exc})")
        if not body.get("id"):
            raise MediaError(f"Facebook rejected '{media}': {describe_error(body)}")
        photo_ids.append(body["id"])
    data = {"message": row["content"]}
    for index, photo_id in enumerate(photo_ids):
        data[f"attached_media[{index}]"] = json.dumps({"media_fbid": photo_id})
    body = graph_post(f"{page_id}/feed", data, token)
    return body.get("id"), body


def publish_row(page_id, token, row):
    """Publish a row. Returns (post_id or None, response body)."""
    if row["type"] == "images":
        return publish_images(page_id, token, row)
    if row["type"] == "image":
        data = {"caption": row["content"]} if row["content"] else {}
        if is_url(row["media"]):
            body = graph_post(f"{page_id}/photos", {**data, "url": row["media"]}, token)
        else:
            with open(local_media_path(row["media"]), "rb") as image:
                body = graph_post(f"{page_id}/photos", data, token, files={"source": image}, timeout=UPLOAD_TIMEOUT)
        return body.get("post_id") or body.get("id"), body
    data = {"message": row["content"]}
    if row["type"] == "link":
        data["link"] = row["media"]
    body = graph_post(f"{page_id}/feed", data, token)
    return body.get("id"), body


def publish_reel(page_id, token, row):
    """Upload and publish a Reel. Returns (video_id or None, body).

    Anything that fails before the final 'finish' call raises MediaError: nothing is public yet, so
    the row is simply retried on the next run. Only the finish call can leave the outcome unknown.
    """
    try:
        start = graph_post(f"{page_id}/video_reels", {"upload_phase": "start"}, token)
    except requests.RequestException as exc:
        raise MediaError(f"could not start the reel upload ({type(exc).__name__})")
    video_id = start.get("video_id")
    if not video_id:
        raise MediaError(f"Facebook refused to start the reel: {describe_error(start)}")
    upload_url = start.get("upload_url") or f"https://rupload.facebook.com/video-upload/{GRAPH_VERSION}/{video_id}"
    headers = {"Authorization": f"OAuth {token}"}
    try:
        if is_url(row["media"]):
            response = requests.post(upload_url, headers={**headers, "file_url": row["media"]},
                                     timeout=VIDEO_UPLOAD_TIMEOUT)
        else:
            path = local_media_path(row["media"])
            size = os.path.getsize(path)
            with open(path, "rb") as video:
                response = requests.post(
                    upload_url,
                    headers={**headers, "offset": "0", "file_size": str(size),
                             "Content-Type": "application/octet-stream"},
                    data=video,
                    timeout=VIDEO_UPLOAD_TIMEOUT,
                )
    except (requests.RequestException, OSError) as exc:
        raise MediaError(f"the video upload failed ({type(exc).__name__}: {exc})")
    result = parse_json(response)
    if not result.get("success"):
        raise MediaError(f"Facebook rejected the video upload: {describe_error(result)}")
    data = {"upload_phase": "finish", "video_id": video_id, "video_state": "PUBLISHED"}
    if row["content"]:
        data["description"] = row["content"]
    body = graph_post(f"{page_id}/video_reels", data, token)
    return (video_id if body.get("success") else None), body


def reel_state(status):
    """Classify a video's status object as ('published' | 'failed' | 'pending', detail)."""
    status = status if isinstance(status, dict) else {}
    phases = {}
    for name in ("uploading_phase", "processing_phase", "publishing_phase"):
        phase = status.get(name)
        phases[name] = phase if isinstance(phase, dict) else {}
    for name, phase in phases.items():
        if str(phase.get("status", "")).lower() in {"error", "failed"} or phase.get("errors"):
            return "failed", f"{name}: {phase.get('errors') or phase.get('status')}"
    if str(status.get("video_status", "")).lower() in {"error", "failed"}:
        return "failed", f"video_status: {status.get('video_status')}"
    publishing = phases["publishing_phase"]
    if str(publishing.get("status", "")).lower() == "complete" or \
            str(publishing.get("publish_status", "")).lower() == "published":
        return "published", ""
    return "pending", ""


def wait_for_reel(video_id, token):
    """Poll until the Reel is published, failed, or REEL_WAIT_SECONDS pass."""
    deadline = time.monotonic() + REEL_WAIT_SECONDS
    state, detail = "pending", ""
    while True:
        try:
            response = requests.get(f"{GRAPH_URL}/{video_id}", params={"fields": "status", "access_token": token},
                                    timeout=TIMEOUT)
            state, detail = reel_state(parse_json(response).get("status"))
        except requests.RequestException:
            state, detail = "pending", ""
        if state != "pending" or time.monotonic() >= deadline:
            return state, detail
        time.sleep(REEL_POLL_SECONDS)


def process_row(page_id, token, row):
    """Publish one due row and its comment. Returns the number of failures (0 or 1)."""
    label = f"{row['key']} (line {row['line']})"
    try:
        if row["type"] == "reel":
            post_id, body = publish_reel(page_id, token, row)
        else:
            post_id, body = publish_row(page_id, token, row)
    except MediaError as exc:
        print(f"::error::{label}: {exc}. Nothing was published; it will be retried on the next run "
              "while it is inside the catch-up window.")
        return 1
    except requests.RequestException as exc:  # must come before OSError: it is a subclass
        log_post(row["key"], "UNCERTAIN")
        print(f"::error::{label}: request failed ({type(exc).__name__}). The post may have gone live, so it "
              f"is marked UNCERTAIN in {LOG_FILE} and will NOT be retried. Check the Page; to retry, delete "
              "that line from the log.")
        return 1
    except OSError as exc:
        print(f"::error::{label}: could not read the media file ({exc}).")
        return 1
    if not post_id:
        print(f"::error::{label}: Facebook rejected the post: {describe_error(body)}. "
              "It will be retried on the next run while it is inside the catch-up window.")
        return 1
    print(f"Post successful! {label} -> ID: {post_id}")

    # Record it straight away so a later failure can never cause a repost.
    log_post(row["key"], post_id)

    if row["type"] == "reel":
        state, detail = wait_for_reel(post_id, token)
        if state == "failed":
            print(f"::error::Reel {post_id} was accepted but Facebook failed to process it ({detail}). Check the "
                  f"video against the Reel specs. To retry, delete its line from {LOG_FILE}.")
            return 1
        if state == "pending":
            warn(f"Reel {post_id} is still processing after {REEL_WAIT_SECONDS}s, so its comment was not posted. "
                 "Check the Page; Facebook usually finishes on its own.")
            if row["comment"]:
                print("Comment text to add manually:\n" + row["comment"])
                return 1
            return 0

    if not row["comment"]:
        print("No comment for this post - skipped.")
        return 0
    try:
        body = graph_post(f"{post_id}/comments", {"message": row["comment"]}, token)
    except requests.RequestException as exc:
        body = {"error": {"message": f"request failed ({type(exc).__name__})"}}
    if body.get("id"):
        print(f"First comment posted! ID: {body['id']}")
        return 0
    print(f"::error::Post {post_id} is live but its first comment failed: {describe_error(body)}")
    print("Comment text to add manually:\n" + row["comment"])
    return 1


def main():
    dry_run = os.getenv("DRY_RUN", "").strip().lower() in {"1", "true", "yes"} or "--dry-run" in sys.argv[1:]
    page_id = os.getenv("FB_PAGE_ID")
    token = os.getenv("FB_ACCESS_TOKEN")
    if not dry_run and not (page_id and token):
        fail("FB_PAGE_ID and FB_ACCESS_TOKEN must be set.")

    tz = get_tz()
    now, whole_day = resolve_now(tz)
    rows = load_schedule(tz)
    posted = load_posted_keys()
    window_start = now.replace(hour=0, minute=0) if whole_day else now - timedelta(hours=CATCHUP_HOURS)
    print(f"Now: {now.strftime('%Y-%m-%d %H:%M')} ({TIMEZONE}) | {len(rows)} row(s) in {SCHEDULE_FILE} | "
          f"{len(posted)} already posted")

    # Problems worth fixing early: unreadable dates, and invalid rows that are still in the future.
    for r in rows:
        if r["error"] and (r["when"] is None or r["when"] > now):
            warn(f"{SCHEDULE_FILE} line {r['line']}: {r['error']}")

    upcoming = [r for r in rows if r["when"] and r["when"] > now and not r["error"] and r["key"] not in posted]
    if len(upcoming) < UPCOMING_WARNING:
        warn(f"Only {len(upcoming)} post(s) scheduled after now - add more rows to {SCHEDULE_FILE}.")

    lookback = now - timedelta(days=MISSED_LOOKBACK_DAYS)
    missed = [r for r in rows if r["when"] and lookback <= r["when"] < window_start
              and not r["error"] and r["key"] not in posted]
    if missed:
        print(f"Note: {len(missed)} earlier row(s) in the last {MISSED_LOOKBACK_DAYS} days were never posted "
              f"and are outside the {CATCHUP_HOURS:g}h catch-up window (use POST_AS_OF to publish them).")

    due = sorted(
        (r for r in rows if r["when"] and window_start <= r["when"] <= now and r["key"] not in posted),
        key=lambda r: (r["when"], r["line"]),
    )
    if not due:
        print("Nothing is due - skipping.")
        return 0

    failures = 0
    for row in due:
        if row["error"]:
            print(f"::error::{SCHEDULE_FILE} line {row['line']} ({row['key']}): {row['error']}")
            failures += 1
            continue
        if dry_run:
            print(f"\nDRY RUN - would publish a '{row['type']}' post scheduled for {row['key']}:\n")
            print(row["content"] or "(no text)")
            if row["media"]:
                print(f"\nMedia/link: {row['media']}" if row["type"] != "images"
                      else "\nImages:\n  " + "\n  ".join(split_media(row["media"])))
                if row["type"] == "reel" and not is_url(row["media"]):
                    print(f"Video size: {os.path.getsize(row['media']) / (1024 * 1024):.1f} MB")
            print(f"\nFirst comment: {row['comment']}" if row["comment"] else "\n(no first comment - would be skipped)")
            continue
        failures += process_row(page_id, token, row)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
