"""Automatic checks run on every generated draft and idea (a failure aborts, so no bad PR is opened)."""
import re
import struct
from pathlib import Path

from . import config
from .plan import parse_date

MOJIBAKE = ("�", "à¦", "à§", "Ã")
CARD_SIZE = (1080, 1080)

# Words that make a post medical/financial advice or a promise. Matching is deliberately blunt: a hit blocks the
# draft (or drops the idea) and a human can rephrase. Add more with "blocked_patterns" in config.json.
CLAIMS = re.compile(
    r"\b(cure[sd]?|diagnos\w*|treat(?:s|ed|ment)?|guarantee\w*|medicine|medication|dosage|"
    r"risk[- ]free|get rich)\b|100\s*%",
    re.IGNORECASE)
URL = re.compile(r"https?://|www\.|\b[a-z0-9-]+\.(?:com|org|net|io|app|co|me|ly)\b/?\S*", re.IGNORECASE)


def png_size(path):
    with open(path, "rb") as f:
        head = f.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path} is not a PNG")
    return struct.unpack(">II", head[16:24])


def check_claims(label, text, cfg=None):
    cfg = cfg or config.load()
    m = CLAIMS.search(text)
    for pattern in cfg["blocked_patterns"]:
        m = m or re.search(pattern, text, re.IGNORECASE)
    return [f"{label}: contains {m.group(0)!r} (no claims, advice or guarantees)"] if m else []


def check_text(label, text, cfg=None, allow_empty=False):
    cfg = cfg or config.load()
    errs = []
    if not text.strip():
        return [] if allow_empty else [f"{label}: empty"]
    if cfg["required_script"] and not re.search(cfg["required_script"], text):
        errs.append(f"{label}: does not contain the required script/characters ({cfg['language']})")
    if any(m in text for m in MOJIBAKE):
        errs.append(f"{label}: looks garbled")
    return errs


def check_draft(rows, now, root=".", cfg=None):
    """rows: the new content.csv rows (dicts). Returns a list of error strings (empty = OK)."""
    cfg = cfg or config.load()
    root = Path(root)
    errs = []
    for r in rows:
        kind = r["post_type"]
        errs += check_text(f"{kind} caption", r["post_content"], cfg)
        errs += check_text(f"{kind} first comment", r["post_comment"], cfg, allow_empty=True)
        d = parse_date(r["post_date"])
        if d is None or d <= now:
            errs.append(f"{kind}: post_date {r['post_date']!r} is not in the future")
        media = [m for m in r["media_url"].split("|") if m]
        if not media:
            errs.append(f"{kind}: no media")
        for m in media:
            p = root / m
            if not p.is_file():
                errs.append(f"{m}: file missing")
            elif kind in ("image", "images") and png_size(p) != CARD_SIZE:
                errs.append(f"{m}: expected {CARD_SIZE[0]}x{CARD_SIZE[1]}, got {png_size(p)}")
    return errs
