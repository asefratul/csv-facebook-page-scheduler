"""Settings for the draft/idea features, read from config.json (see the README). Everything has a neutral default."""
import copy
import json
import os
from pathlib import Path

DEFAULTS = {
    "brand_name": "Your Brand",
    "language": "English",
    "model": "claude-sonnet-5-5",
    "brand_guidelines": "brand-guidelines.md",
    "default_link": "",
    "disclaimer": "",
    "hashtags": "",
    "required_script": "",  # optional regex every text must match, e.g. "[\u0600-\u06ff]" to require Arabic letters
    "blocked_patterns": [],  # extra regexes that block a draft or idea (added to the built-in claim list)
    "kinds": {"tip": "Tips", "myth": "Myth vs fact", "activity": "Ideas to try"},
    "cadence": {"post_hour": 12, "spacing_days": 4, "lead_days": 3, "queue_below": 2,
                "ideas_per_batch": 5, "max_pending": 4},
    "card_style": {"background": "#f8fafc", "accent": "#2563eb", "text": "#0f172a",
                   "font_family": "'Helvetica Neue', Arial, 'Noto Sans', sans-serif",
                   "cover_subtitle": "", "last_slide_note": ""},
}


def _merge(base, extra):
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict) and key != "kinds":
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def load(path=None):
    """The merged settings dict. A missing file means all defaults; a broken file is an error."""
    path = Path(path or os.environ.get("SCHEDULER_CONFIG") or "config.json")
    cfg = copy.deepcopy(DEFAULTS)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise ValueError(f"{path} is not valid JSON: {e}") from None
        if not isinstance(data, dict):
            raise ValueError(f"{path} must contain a JSON object")
        _merge(cfg, data)
    if not cfg["kinds"]:
        raise ValueError("config 'kinds' must list at least one post kind")
    return cfg


def guidelines(cfg):
    """Text of the brand guidelines file; falls back to the shipped example template."""
    for name in (cfg["brand_guidelines"], "brand-guidelines.example.md"):
        p = Path(name)
        if p.is_file():
            if name != cfg["brand_guidelines"]:
                print(f"NOTE: {cfg['brand_guidelines']} not found, using the example template {name}. "
                      "Copy it to your own file and edit it.")
            return p.read_text(encoding="utf-8")
    raise FileNotFoundError(f"{cfg['brand_guidelines']} not found")
