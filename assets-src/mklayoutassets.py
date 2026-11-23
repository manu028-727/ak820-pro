#!/usr/bin/env python3
"""The extra sizes a layout can carry (the screen editor's resizable elements).

Writes hostagent/layout-assets/: glyph strips (one row of cells, white on
black, monochrome, no markers) and resized icons, plus index.json naming each
size and its cell. ak820layout.py packs the ones a layout uses into its slot,
so the board needs nothing provisioned for them.

  clock-<h>.png  Iosevka Regular, '0'..':' (11 cells) cropped to the digits'
                 ink rows, the colon centred on them: the clock's own face
                 at other sizes (the shipped one is the 30 ppem, 15x22).
  text-<h>.png   Iosevka Medium, ASCII 0x20..0x7E: the song lines.
  digit-<h>.png  Iosevka Medium, '1'..'3': the connection row's slot number.
  os-<name>-<s>.png, conn-<mode>-<h>.png: the 24 px icons resized.

Needs Pillow and the two TTFs (assets-src/FONTS.md has the download):
  python3 assets-src/mklayoutassets.py /path/Iosevka-Regular.ttf /path/Iosevka-Medium.ttf
"""
import json, os, sys
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "hostagent", "layout-assets")
SRC = os.path.join(ROOT, "assets-src", "font-backup")
SRC2 = os.path.join(ROOT, "assets-src", "current")

CLOCK_PPEM = [18, 22, 26, 36, 42, 48, 56]          # 30 is the shipped face
TEXT_PPEM = [16, 24, 28]                           # 20 is the shipped FONT_STATUS
OS_SIZES = [16, 20, 32, 40, 48]                    # 24 shipped
CONN_SIZES = [16, 20, 32]                          # 24 shipped; 40 runs off the panel with its digit


def mask(font, ch):
    """The glyph's monochrome ink at the baseline origin: {(x, y)} with y from the baseline."""
    l, t, r, b = font.getbbox(ch, anchor="ls")
    if r <= l or b <= t:
        return set(), 0
    im = Image.new("L", (r - l, b - t), 0)
    ImageDraw.Draw(im).text((-l, -t), ch, font=font, fill=255, anchor="ls")
    px = im.load()
    return {(x + l, y + t) for y in range(b - t) for x in range(r - l) if px[x, y] >= 128}, font.getlength(ch)


def strip(font, chars, cw, top, ch, nudge=None):
    img = Image.new("RGB", (cw * len(chars), ch), (0, 0, 0))
    px = img.load()
    for i, c in enumerate(chars):
        ink, adv = mask(font, c)
        ox = i * cw + max(0, int(round((cw - adv) / 2)))
        dy = (nudge or {}).get(c, 0)
        for x, y in ink:
            X, Y = ox + x, y - top + dy
            if i * cw <= X < (i + 1) * cw and 0 <= Y < ch:
                px[X, Y] = (255, 255, 255)
    return img


def clock(path, ppem):
    f = ImageFont.truetype(path, ppem, layout_engine=ImageFont.Layout.BASIC)
    chars = "0123456789:"
    cw = int(round(f.getlength("0")))
    ys = [y for c in "0123456789" for _, y in mask(f, c)[0]]
    top, bot = min(ys), max(ys)
    cy = (top + bot) / 2
    cys = [y for _, y in mask(f, ":")[0]]
    nudge = {":": int(round(cy - (min(cys) + max(cys)) / 2))}   # the colon on the digits' centre
    h = bot - top + 1
    return strip(f, chars, cw, top, h, nudge), cw, h


def text(path, ppem, chars):
    f = ImageFont.truetype(path, ppem, layout_engine=ImageFont.Layout.BASIC)
    cw = int(round(f.getlength("M")))
    ys = [y for c in map(chr, range(0x20, 0x7F)) for _, y in mask(f, c)[0]]
    top, bot = min(ys), max(ys)
    h = bot - top + 1
    return strip(f, chars, cw, top, h), cw, h, -top   # -top: the baseline row in the cell


def resize(src, w, h):
    im = Image.open(src).convert("RGBA")
    bg = Image.new("RGBA", im.size, (0, 0, 0, 255))
    im = Image.alpha_composite(bg, im).convert("RGB")
    return im.resize((w, h), Image.BOX if w < im.width else Image.NEAREST)


def main():
    reg, med = sys.argv[1:3]
    os.makedirs(OUT, exist_ok=True)
    idx = {"clock": [], "text": [], "digit": [], "os": [], "conn": []}
    for p in CLOCK_PPEM:
        im, cw, h = clock(reg, p)
        n = f"clock-{h}.png"; im.save(os.path.join(OUT, n))
        idx["clock"].append({"h": h, "cw": cw, "first": 0x30, "count": 11, "file": n, "ppem": p})
    full = "".join(map(chr, range(0x20, 0x7F)))
    for p in TEXT_PPEM:
        im, cw, h, base = text(med, p, full)
        n = f"text-{h}.png"; im.save(os.path.join(OUT, n))
        idx["text"].append({"h": h, "cw": cw, "first": 0x20, "count": 95, "file": n, "ppem": p, "baseline": base})
    # The shipped 20 ppem face (its hand-fixed P), as a layout face for the song lines.
    at = Image.open(os.path.join(SRC2, "Iosevka-Medium-20.png")).convert("RGB")
    apx = at.load()
    for x in range(at.width):
        for y in range(at.height):
            if apx[x, y] == (255, 0, 255):
                apx[x, y] = (0, 0, 0)
    at.save(os.path.join(OUT, "text-23.png"))
    idx["text"].append({"h": 23, "cw": 10, "first": 0x20, "count": 95, "file": "text-23.png", "ppem": 20})
    idx["text"].sort(key=lambda e: e["h"])
    for s in CONN_SIZES:
        # The slot digit beside a row s tall: the face whose cell is at most s.
        p = max(q for q in range(8, 60) if text(med, q, "1")[2] <= s - 1)
        im, cw, h, base = text(med, p, "123")
        n = f"digit-{s}.png"; im.save(os.path.join(OUT, n))
        idx["digit"].append({"for": s, "h": h, "cw": cw, "first": 0x31, "count": 3, "file": n, "ppem": p})
    for name, f in (("apple", "apple_icon_24x24.png"), ("windows", "windows_icon_24x24.png"),
                    ("linux", "windows_linux_icon_24x24.png")):
        src = os.path.join(SRC, f) if os.path.exists(os.path.join(SRC, f)) else os.path.join(SRC2, f)
        for s in OS_SIZES:
            n = f"os-{name}-{s}.png"; resize(src, s, s).save(os.path.join(OUT, n))
    idx["os"] = OS_SIZES
    for mode in ("cable", "bt", "24g"):
        src = os.path.join(SRC, f"conn_row_{mode}.png")
        for s in CONN_SIZES:
            w = int(round(79 * s / 24))
            n = f"conn-{mode}-{s}.png"; resize(src, w, s).save(os.path.join(OUT, n))
        idx["conn"] = [{"h": s, "w": int(round(79 * s / 24))} for s in CONN_SIZES]
    json.dump(idx, open(os.path.join(OUT, "index.json"), "w"), indent=1)
    print(json.dumps({k: [e["h"] if isinstance(e, dict) and "h" in e else e for e in v] for k, v in idx.items()}))


if __name__ == "__main__":
    main()
