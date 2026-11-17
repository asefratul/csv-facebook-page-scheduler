"""Brand-neutral card images: HTML templates + Playwright screenshots (1080x1080 PNG).

Colours, font and texts come from "card_style" in config.json. To change the look completely, edit `_page` /
`card_pages` here, or replace this module (the rest of the code only calls `card_pages` and `screenshot_all`).
Reels/video rendering is intentionally not included: add your own and put the file path in a `reel` row.
"""
import html
import os
import re
from pathlib import Path

from . import config

FIT_JS = """() => {
  let over = false;
  document.querySelectorAll('[data-fit]').forEach(el => {
    const min = +el.dataset.min || 24;
    let fs = parseFloat(getComputedStyle(el).fontSize);
    while (el.scrollHeight > el.clientHeight + 1 && fs > min) { fs -= 2; el.style.fontSize = fs + 'px'; }
    if (el.scrollHeight > el.clientHeight + 1) over = true;
  });
  return {over};
}"""

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
html,body{width:1080px;height:1080px;overflow:hidden}
body{font-family:var(--font);color:var(--text);background:var(--bg)}
.sq{width:1080px;height:1080px;padding:64px;display:flex;flex-direction:column}
.chip{align-self:flex-start;background:var(--accent);color:#fff;font-weight:700;font-size:30px;
 padding:14px 30px;border-radius:999px}
.cover h1{font-size:96px;line-height:1.15;margin-top:48px;font-weight:800;overflow-wrap:anywhere}
.cover .sub{font-size:44px;font-weight:600;color:var(--accent);margin-top:40px}
.head{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:28px}
.head h2{font-size:58px;font-weight:800;line-height:1.15}
.head .pg{font-size:32px;font-weight:700;color:var(--accent)}
.body{flex:1;min-height:0;overflow:hidden;font-size:44px;line-height:1.3;display:flex;flex-direction:column;gap:22px}
.it{display:flex;gap:22px;align-items:baseline}
.dot{flex:none;background:var(--accent);color:#fff;border-radius:50%;width:56px;height:56px;font-size:30px;
 font-weight:700;display:flex;align-items:center;justify-content:center}
.note{font-size:26px;opacity:.7;margin-top:18px}
.brand{margin-top:auto;padding-top:24px;font-size:34px;font-weight:800;color:var(--accent)}
"""


def esc(s):
    return html.escape(s, quote=False)


def _page(body, style):
    vars_ = (f"--bg:{style['background']};--accent:{style['accent']};--text:{style['text']};"
             f"--font:{style['font_family']}")
    return (f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head>"
            f"<body style=\"{html.escape(vars_, quote=True)}\">{body}</body></html>")


def card_pages(chip, title, slides, cfg=None):
    """Return [(name, html)]: a cover plus one slide per entry of `slides` ([{heading, lines}])."""
    cfg = cfg or config.load()
    style, brand = cfg["card_style"], esc(cfg["brand_name"])
    sub = f'<div class="sub">{esc(style["cover_subtitle"])}</div>' if style["cover_subtitle"] else ""
    cover = (f'<div class="sq cover"><div class="chip">{esc(chip)}</div><h1>{esc(title)}</h1>{sub}'
             f'<div class="brand">{brand}</div></div>')
    pages = [("1", _page(cover, style))]
    total = len(slides) + 1
    for n, sl in enumerate(slides, start=2):
        its = "".join(f'<div class="it"><span class="dot">{k}</span><span>{esc(x)}</span></div>'
                      for k, x in enumerate(sl["lines"], start=1))
        note = style["last_slide_note"] or cfg["disclaimer"]
        foot = f'<div class="note">{esc(note)}</div>' if n == total and note else ""
        body = (f'<div class="sq"><div class="head"><h2>{esc(sl["heading"])}</h2><div class="pg">{n}/{total}</div></div>'
                f'<div class="body" data-fit data-min="28">{its}</div>{foot}<div class="brand">{brand}</div></div>')
        pages.append((str(n), _page(body, style)))
    return pages


def screenshot_all(jobs, workdir):
    """jobs: [(out_png_path, w, h, html)]. Raises if text overflows even at the minimum size."""
    from playwright.sync_api import sync_playwright

    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    tmp = workdir / "page.html"
    with sync_playwright() as p:
        # CI uses the runner's preinstalled Chrome (BROWSER_CHANNEL=chrome) so it needs no download.
        browser = p.chromium.launch(channel=os.environ.get("BROWSER_CHANNEL") or None)
        page = browser.new_page(device_scale_factor=1)
        for out, w, h, doc in jobs:
            tmp.write_text(doc, encoding="utf-8")
            page.set_viewport_size({"width": w, "height": h})
            page.goto(tmp.as_uri())
            page.evaluate("document.fonts.ready")
            if page.evaluate(FIT_JS)["over"]:
                raise RuntimeError(f"Text does not fit in {Path(out).name} even at the smallest size")
            page.screenshot(path=str(out), clip={"x": 0, "y": 0, "width": w, "height": h})
        browser.close()
