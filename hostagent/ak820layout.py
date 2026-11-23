#!/usr/bin/env python3
"""ak820layout.py -- the AK820 Pro's dashboard layout: check, pack, upload.

The screen widget editor's host side (Manuel's SCREEN-EDITOR-SCOPE.md, M2-M4).
A layout is a JSON file: where the six built-in elements sit (and which of
four are hidden), plus up to eight custom widgets -- PNG images and text
labels. This tool checks it against the firmware's own rules, packs it into
the package format the firmware reads (graphics/layout_store.h in the
firmware), and writes it to the slot the board is NOT using, verifies it,
then asks the board to switch. A failed upload leaves the old layout up.

    ak820layout.py check  my.json          # the rules, and what it would cost
    ak820layout.py pack   my.json -o x.bin # the package, for a look
    ak820layout.py info                    # the board's active layout
    ak820layout.py upload my.json          # write it and switch to it
    ak820layout.py reset                   # back to the built-in default
    ak820layout.py default > my.json       # today's dashboard, as a JSON file
    ak820layout.py serve                   # the drag-and-drop editor, in your browser

The layout file:

    {
      "elements": {
        "os":     {"x": 0,  "y": 0},
        "conn":   {"x": 29, "y": 0},
        "text":   {"x": 0, "y": 27,  "w": 128},
        "clock":  {"x": 0, "y": 58,  "w": 128},
        "lock":   {"x": 0, "y": 83,  "w": 128, "hidden": false},
        "status": {"x": 0, "y": 105, "w": 128, "hidden": false}
      },
      "widgets": [
        {"type": "image", "x": 0, "y": 83, "src": "tux.png"},
        {"type": "label", "x": 30, "y": 90, "text": "hello", "font": "small"}
      ]
    }

Elements go anywhere, in any order: the only rules are that everything is on
the screen and nothing overlaps (the lock row may share its blank last row
with the battery row's blank first). The four bands -- song text, clock, lock
row, battery row -- also take a width "w"; a narrower band draws smaller (the
clock steps down a face, the lock row goes compact, text is cut). Minimum
widths: text 32, clock 72, lock row 40, battery row 52.

An element left out keeps its default place. Any element can be hidden
("hidden": true); a hidden song text takes the keyboard's messages with it
(connection words, alerts, readouts), since they show in that band. Image paths are
relative to the JSON file. Transparent pixels become black, the panel's
background.

Linux only for the upload, through the kernel's hidraw node (no
dependencies, like ak820-linux-agent.py); check, pack and serve run
anywhere. The Linux agent is paused around an upload, as flash.sh does.
"""
import argparse, glob, json, os, select, struct, subprocess, sys, time, zlib

PANEL = 128

# --- the firmware's geometry (display.c; keep in step) ------------------------
CONN_ICON_W, CONN_NUM_W, CONN_ICON_H = 79, 12, 24
CONN_W = CONN_ICON_W + 1 + CONN_NUM_W + 1
OS_W = OS_H = 24
TEXT_H, CLOCK_BAND_H, LOCK_TXT_H, STATUS_H = 31, 23, 23, 23
TEXT_LINE_H, SONG_LARGE_H, TEXT_FONT_DY = 15, 14, 9
LOCK_LABEL_INK_END, LOCK_PAD_H, FONT_STATUS_ROW0_BLANK = 18, 16, 1
FONTS = {"small": (0, 6, 14), "large": (1, 10, 23)}   # name -> (p0, advance, height)
DEFAULT = {"os": {"x": 0, "y": 0}, "conn": {"x": 29, "y": 0},
           "text": {"x": 0, "y": 27, "w": 128}, "clock": {"x": 0, "y": 58, "w": 128},
           "lock": {"x": 0, "y": 83, "w": 128}, "status": {"x": 0, "y": 105, "w": 128}}
BANDS = ("text", "clock", "lock", "status")
BAND_H = {"text": 31, "clock": 23, "lock": 23, "status": 23}           # display_layout.h's DL_*_H
MIN_W = {"text": 32, "clock": 72, "lock": 40, "status": 52}            # ...and DL_*_MIN_W
NAMES = {"os": "the OS logo", "conn": "the connection icons", "text": "the song text",
         "clock": "the clock", "lock": "the lock row", "status": "the battery row"}
KIND = {"os": 1, "conn": 2, "text": 3, "clock": 4, "lock": 5, "status": 6, "image": 16, "label": 17, "anim": 18,
        "font": 19, "icon": 20}
FRAMES_MAX = 255
HIDEABLE = ("os", "conn", "text", "clock", "lock", "status")
CUSTOM_MAX, RECORDS_MAX, META_MAX = 8, 32, 4096
SLOTS = (0xE00000, 0xF00000)
SLOT_SIZE = 0x100000
MAGIC, VERSION = 0x314C4B41, 1

# --- the board ---------------------------------------------------------------
VID, PID = 0x0C45, 0x8009
SYSFS = os.environ.get("AK820_SYSFS", "/sys/class/hidraw")
DEV = os.environ.get("AK820_DEV", "/dev")
REPORT = 32
SET_VALUE, FLASH_CHANNEL = 0x07, 0x11
FC_ERASE, FC_WRITE_BEGIN, FC_WRITE_DATA, FC_WRITE_END = 0x02, 0x03, 0x04, 0x05
FC_CRC32, FC_CRC_NEXT, FC_LAYOUT_RELOAD, FC_LAYOUT_INFO = 0x06, 0x09, 0x0A, 0x0B
FS_OK, FS_BUSY, FS_REFUSED, FS_BADARG, FS_MORE = 0, 1, 2, 3, 4
CHUNK = 27          # bytes a write report carries (ak820ctl's CHUNK)
AGENT_UNIT = "ak820pro-agent"
ERRORS = {0: "ok", 1: "no layout in either slot", 2: "a malformed header", 3: "a CRC mismatch",
          4: "a record the firmware refused", 5: "a placement the firmware refused"}


class LayoutError(Exception):
    pass


# --- sizes (Manuel, 11-23: every element resizable) ---------------------------
# The faces and icons a layout carries at other sizes than the board's own:
# hostagent/layout-assets/ (made by assets-src/mklayoutassets.py). An element's
# "size" is a height in pixels; None or the default is the board's own.
ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "layout-assets")
DEFAULT_SIZE = {"os": 24, "conn": 24, "text": None, "clock": 22}
DLI = {"apple": 0, "windows": 1, "linux": 2, "cable": 3, "bt": 4, "24g": 5}   # layout_store.h's DLI_*
_assets_index = None


def assets_index():
    global _assets_index
    if _assets_index is None:
        _assets_index = json.load(open(os.path.join(ASSETS_DIR, "index.json")))
    return _assets_index


def sizes_for(k):
    """The sizes element k can take, the default among them (None: the menu's face)."""
    ix = assets_index()
    if k == "os":
        return sorted([24] + ix["os"])
    if k == "conn":
        return sorted([24] + [c["h"] for c in ix["conn"]])
    if k == "clock":
        return sorted([22] + [c["h"] for c in ix["clock"]])
    if k == "text":
        return [None] + [c["h"] for c in ix["text"]]
    return []


def _face(kind, h):
    for e in assets_index()[kind]:
        if e.get("h") == h or e.get("for") == h:
            return e
    raise LayoutError(f"no {kind} face {h} px tall")


def face_data(e):
    """A strip PNG -> the firmware's glyph tiles: each cell row by row, one after another."""
    w, h, px = decode_png(os.path.join(ASSETS_DIR, e["file"]))
    cw = e["cw"]
    out = b""
    for i in range(e["count"]):
        out += rgb565([px[y * w + i * cw + x] for y in range(h) for x in range(cw)])
    return out


def icon_data(name):
    w, h, px = decode_png(os.path.join(ASSETS_DIR, name))
    return w, h, rgb565(px)


def element_size(els, k):
    """(w, h) of what element k draws at its size, as display_layout_rect() says."""
    e, s = els[k], els[k].get("size")
    if k == "os":
        return (s or 24, s or 24)
    if k == "conn":
        if not s or s == 24:
            return CONN_W, CONN_ICON_H
        c = next(c for c in assets_index()["conn"] if c["h"] == s)
        return c["w"] + 1 + _face("digit", s)["cw"] + 1, s
    if k == "text":
        return e["w"], (2 * (s + 1) + 1 if s else TEXT_H)
    if k == "clock":
        return e["w"], (s + 1 if s and s != 22 else CLOCK_BAND_H)
    return e["w"], BAND_H[k]


def size_records(els):
    """The fonts and icons the sizes need, and each built-in's p0: ([(rec, data)], {element: p0})."""
    recs, p0, fonts = [], {}, []

    def font(e):
        fonts.append(e)
        recs.append(([KIND["font"], 0, 0, 0, e["cw"], e["h"], e["first"], e["count"]], face_data(e)))
        return len(fonts)            # its place among the fonts, plus one

    s = els["clock"].get("size")
    if s and s != 22:
        p0["clock"] = font(_face("clock", s))
    s = els["text"].get("size")
    if s:
        p0["text"] = font(_face("text", s))
    s = els["os"].get("size")
    if s and s != 24:
        for name in ("apple", "windows", "linux"):
            w, h, d = icon_data(f"os-{name}-{s}.png")
            recs.append(([KIND["icon"], 0, 0, 0, w, h, DLI[name], 0], d))
    s = els["conn"].get("size")
    if s and s != 24:
        for name in ("cable", "bt", "24g"):
            w, h, d = icon_data(f"conn-{name}-{s}.png")
            recs.append(([KIND["icon"], 0, 0, 0, w, h, DLI[name], 0], d))
        p0["conn"] = font(_face("digit", s))
    return recs, p0


# --- PNG, stdlib only ----------------------------------------------------------

def decode_png(path):
    """-> (w, h, [(r, g, b, a)]): 8-bit grey, grey+alpha, RGB, RGBA and
    palette (1-8 bit) PNGs, not interlaced."""
    blob = open(path, "rb").read()
    if blob[:8] != b"\x89PNG\r\n\x1a\n":
        raise LayoutError(f"{path}: not a PNG")
    pos, idat, plte, trns = 8, b"", None, None
    w = h = depth = ctype = None
    while pos < len(blob):
        n, typ = struct.unpack(">I4s", blob[pos:pos + 8])
        data = blob[pos + 8:pos + 8 + n]
        pos += 12 + n
        if typ == b"IHDR":
            w, h, depth, ctype, _, _, inter = struct.unpack(">IIBBBBB", data)
            if inter:
                raise LayoutError(f"{path}: interlaced PNGs are not supported; re-save it without interlacing")
        elif typ == b"PLTE":
            plte = data
        elif typ == b"tRNS":
            trns = data
        elif typ == b"IDAT":
            idat += data
        elif typ == b"IEND":
            break
    chans = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
    if chans is None or (ctype != 3 and depth != 8) or (ctype == 3 and depth not in (1, 2, 4, 8)):
        raise LayoutError(f"{path}: PNG colour type {ctype} at {depth} bits is not supported (use 8-bit RGB or RGBA)")
    raw = zlib.decompress(idat)
    bpp = max(1, chans * depth // 8)
    stride = (w * chans * depth + 7) // 8
    rows, prev, p = [], bytearray(stride), 0
    for _ in range(h):
        f, line = raw[p], bytearray(raw[p + 1:p + 1 + stride])
        p += 1 + stride
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if f == 1:
                line[i] = (line[i] + a) & 255
            elif f == 2:
                line[i] = (line[i] + b) & 255
            elif f == 3:
                line[i] = (line[i] + ((a + b) >> 1)) & 255
            elif f == 4:
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(line)
        prev = line
    px = []
    for line in rows:
        if ctype == 3:
            per = 8 // depth
            for x in range(w):
                byte = line[x // per]
                idx = (byte >> (8 - depth * (x % per + 1))) & ((1 << depth) - 1)
                r, g, b = plte[3 * idx:3 * idx + 3]
                a = trns[idx] if trns and idx < len(trns) else 255
                px.append((r, g, b, a))
        else:
            for x in range(w):
                v = line[x * chans:(x + 1) * chans]
                if ctype == 0:
                    px.append((v[0], v[0], v[0], 255))
                elif ctype == 4:
                    px.append((v[0], v[0], v[0], v[1]))
                elif ctype == 2:
                    px.append((v[0], v[1], v[2], 255))
                else:
                    px.append(tuple(v))
    return w, h, px


def fit_size(w, h, box=PANEL):
    """A picture's size shrunk (never grown) to fit box x box, aspect kept."""
    if w <= box and h <= box:
        return w, h
    k = min(box / w, box / h)
    return max(1, round(w * k)), max(1, round(h * k))


def _pil():
    try:
        from PIL import Image  # optional: much faster for big GIFs; the stdlib path is the fallback
        return Image
    except ImportError:
        return None


def picture_info(path):
    """-> (w, h, frames) of a PNG or GIF."""
    st = os.stat(path)
    return _decoded(path, st.st_mtime_ns, st.st_size)[:3]


_cache = {}


def _decoded(path, mtime, size):
    key = (os.path.abspath(path), mtime, size)
    if key not in _cache:
        Image = _pil()
        if Image is not None:
            frames = []
            with Image.open(path) as im:
                n = getattr(im, "n_frames", 1)
                for k in range(n):
                    im.seek(k)
                    frames.append((im.info.get("duration", 0) // 10, list(_flat(im.convert("RGBA")))))
                w, h = im.size
        elif path.lower().endswith(".gif"):
            w, h, frames = decode_gif(path)
        else:
            w, h, px = decode_png(path)
            frames = [(0, px)]
        if len(_cache) > 16:
            _cache.clear()
        _cache[key] = (w, h, len(frames), frames)
    return _cache[key]


def _flat(im):
    get = getattr(im, "get_flattened_data", None) or im.getdata
    return get()


def load_frames(path, tw, th):
    """-> [(delay_cs, [(r, g, b, 255)])]: every frame over black, resampled to
    tw x th (an area average shrinking, nearest pixel growing, as Pillow's BOX
    and NEAREST). What the editor previews is this same call."""
    st = os.stat(path)
    key = (os.path.abspath(path), st.st_mtime_ns, st.st_size, tw, th)
    if key in _cache:
        return _cache[key]
    sw, sh, _, frames = _decoded(path, st.st_mtime_ns, st.st_size)
    Image = _pil()
    out = []
    for d, px in frames:
        flat = [(r * a // 255, g * a // 255, b * a // 255) for r, g, b, a in px]
        if (tw, th) == (sw, sh):
            res = flat
        elif Image is not None:
            im = Image.new("RGB", (sw, sh))
            im.putdata(flat)
            im = im.resize((tw, th), Image.BOX if tw <= sw and th <= sh else Image.NEAREST)
            res = list(_flat(im))
        else:
            res = _resample(flat, sw, sh, tw, th)
        out.append((d, [(r, g, b, 255) for r, g, b in res]))
    _cache[key] = out
    return out


def _resample(px, sw, sh, tw, th):
    xs = [(x * sw // tw, max(x * sw // tw + 1, (x + 1) * sw // tw)) for x in range(tw)]
    ys = [(y * sh // th, max(y * sh // th + 1, (y + 1) * sh // th)) for y in range(th)]
    out = []
    for y0, y1 in ys:
        for x0, x1 in xs:
            n = (y1 - y0) * (x1 - x0)
            r = g = b = 0
            for yy in range(y0, y1):
                row = yy * sw
                for xx in range(x0, x1):
                    p = px[row + xx]
                    r += p[0]; g += p[1]; b += p[2]
            out.append(((r + n // 2) // n, (g + n // 2) // n, (b + n // 2) // n))
    return out


def rgb565(px):
    """Over black, LOW byte first: what the DMA from the external flash wants
    (mkraw.py's emit_flash swaps its big-endian raws the same way; written
    big-endian, red and blue came out swapped, orange as blue)."""
    out = bytearray()
    for r, g, b, a in px:
        r, g, b = r * a // 255, g * a // 255, b * a // 255
        v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        out += struct.pack("<H", v)
    return bytes(out)


def decode_gif(path):
    """-> (w, h, [(delay_cs, [(r, g, b, a)])]): every frame composited whole,
    as a browser shows it (disposal 0-2, transparency, interlace). Stdlib only."""
    blob = open(path, "rb").read()
    if blob[:6] not in (b"GIF87a", b"GIF89a"):
        raise LayoutError(f"{path}: not a GIF")
    w, h, flags = struct.unpack_from("<HHB", blob, 6)
    pos = 13
    gct = None
    if flags & 0x80:
        n = 2 << (flags & 7)
        gct = [tuple(blob[pos + 3 * i:pos + 3 * i + 3]) for i in range(n)]
        pos += 3 * n
    canvas = [(0, 0, 0, 0)] * (w * h)
    frames, delay, trans, disposal = [], 0, None, 0
    while pos < len(blob):
        tag = blob[pos]; pos += 1
        if tag == 0x3B:
            break
        if tag == 0x21:
            label = blob[pos]; pos += 1
            if label == 0xF9 and blob[pos] >= 4:
                pf, delay, ti = struct.unpack_from("<BHB", blob, pos + 1)
                disposal = (pf >> 2) & 7
                trans = ti if pf & 1 else None
            while blob[pos]:
                pos += blob[pos] + 1
            pos += 1
            continue
        if tag != 0x2C:
            raise LayoutError(f"{path}: a malformed GIF")
        fx, fy, fw, fh, lf = struct.unpack_from("<HHHHB", blob, pos); pos += 9
        ct = gct
        if lf & 0x80:
            n = 2 << (lf & 7)
            ct = [tuple(blob[pos + 3 * i:pos + 3 * i + 3]) for i in range(n)]
            pos += 3 * n
        if ct is None:
            raise LayoutError(f"{path}: a GIF frame without a palette")
        minsize = blob[pos]; pos += 1
        data = bytearray()
        while blob[pos]:
            data += blob[pos + 1:pos + 1 + blob[pos]]
            pos += blob[pos] + 1
        pos += 1
        idx = _lzw(data, minsize, fw * fh)
        rows = list(range(fh))
        if lf & 0x40:   # interlaced
            rows = list(range(0, fh, 8)) + list(range(4, fh, 8)) + list(range(2, fh, 4)) + list(range(1, fh, 2))
        prev = list(canvas) if disposal == 3 else None
        for k, ry in enumerate(rows):
            Y = fy + ry
            if Y >= h:
                continue
            for X in range(fw):
                i = k * fw + X
                if i >= len(idx) or fx + X >= w:
                    continue
                c = idx[i]
                if c == trans or c >= len(ct):
                    continue
                canvas[Y * w + fx + X] = ct[c] + (255,)
        frames.append((delay, list(canvas)))
        if disposal == 2:
            for ry in range(fh):
                for X in range(fw):
                    if fy + ry < h and fx + X < w:
                        canvas[(fy + ry) * w + fx + X] = (0, 0, 0, 0)
        elif disposal == 3 and prev is not None:
            canvas = prev
        delay, trans, disposal = 0, None, 0
    if not frames:
        raise LayoutError(f"{path}: a GIF with no frames")
    return w, h, frames


def _lzw(data, minsize, npix):
    clear, end = 1 << minsize, (1 << minsize) + 1
    size, nxt = minsize + 1, end + 1
    table = [bytes([i]) for i in range(clear)] + [b"", b""]
    out, prev = bytearray(), None
    bits = int.from_bytes(data, "little")
    pos, total = 0, len(data) * 8
    while pos + size <= total and len(out) < npix:
        code = (bits >> pos) & ((1 << size) - 1)
        pos += size
        if code == clear:
            size, nxt = minsize + 1, end + 1
            del table[end + 1:]
            prev = None
            continue
        if code == end:
            break
        if prev is None:
            entry = table[code]
        elif code < len(table):
            entry = table[code]
            table.append(prev + entry[:1])
        else:
            entry = prev + prev[:1]
            table.append(entry)
        out += entry
        prev = entry
        if len(table) == (1 << size) and size < 12:
            size += 1
    return list(out[:npix])


# --- the rules -------------------------------------------------------------------

def _int(v, what, lo=0, hi=255):
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        raise LayoutError(f"{what} must be a whole number from {lo} to {hi}, not {v!r}")
    return v


def resolve(doc, base_dir=".", strict=True):
    """The layout file -> (elements, widgets) with every default filled in and
    every image loaded. Raises LayoutError naming the first problem."""
    if not isinstance(doc, dict):
        raise LayoutError("the layout must be a JSON object")
    els = {k: dict(v) for k, v in DEFAULT.items()}
    for k, v in (doc.get("elements") or {}).items():
        if k not in DEFAULT:
            raise LayoutError(f"unknown element {k!r} (known: {', '.join(DEFAULT)})")
        if not isinstance(v, dict):
            raise LayoutError(f"element {k!r} must be an object")
        for key, val in v.items():
            if key == "hidden":
                if k not in HIDEABLE and val:
                    raise LayoutError(f"{k!r} cannot be hidden")
                els[k]["hidden"] = bool(val)
            elif key in ("x", "y"):
                els[k][key] = _int(val, f"{k}.{key}", 0, PANEL - 1)
            elif key == "w" and k in BANDS:
                els[k]["w"] = _int(val, f"{k}.w", 1, PANEL)
            elif key == "size" and k in DEFAULT_SIZE:
                if val is not None and val not in sizes_for(k):
                    raise LayoutError(f"{NAMES[k]} comes in {', '.join(str(v) for v in sizes_for(k) if v)} px, not {val!r}")
                els[k]["size"] = None if val == DEFAULT_SIZE[k] else val
            else:
                raise LayoutError(f"element {k!r}: unknown field {key!r}")
    widgets = []
    for i, w in enumerate(doc.get("widgets") or []):
        t = w.get("type")
        name = f"widget {i + 1} ({t})"
        x, y = _int(w.get("x"), f"{name} x", 0, PANEL - 1), _int(w.get("y"), f"{name} y", 0, PANEL - 1)
        if t == "image":
            src = w.get("src")
            if not src:
                raise LayoutError(f"{name}: no src")
            path = src if os.path.isabs(src) else os.path.join(base_dir, src)
            sw, sh, nframes = picture_info(path)
            if nframes > FRAMES_MAX:
                raise LayoutError(f"{name}: {nframes} frames; the keyboard plays at most {FRAMES_MAX}")
            # Its size on the screen: "w"/"h" if given, else its own, shrunk to fit 128x128.
            fw, fh = fit_size(sw, sh)
            iw = _int(w.get("w", fw), f"{name} w", 1, PANEL)
            ih = _int(w.get("h", fh), f"{name} h", 1, PANEL)
            frames = load_frames(path, iw, ih)
            if len(frames) == 1:
                widgets.append({"type": "image", "x": x, "y": y, "w": iw, "h": ih, "data": rgb565(frames[0][1]), "src": src})
            else:
                # The delay table (10 ms units, as the GIF's own), padded to even, then the frames.
                # Under 2 (20 ms) is "as fast as possible", which browsers show at 100 ms: so does the keyboard.
                delays = bytes(min(255, d if d >= 2 else 10) for d, _ in frames)
                data = delays + bytes(len(delays) & 1) + b"".join(rgb565(f) for _, f in frames)
                widgets.append({"type": "anim", "x": x, "y": y, "w": iw, "h": ih, "data": data, "src": src,
                                "frames": len(frames)})
        elif t == "label":
            text = w.get("text", "")
            font = w.get("font", "small")
            if font not in FONTS:
                raise LayoutError(f"{name}: font must be one of {', '.join(FONTS)}")
            if not text or any(not 32 <= ord(c) < 127 for c in text):
                raise LayoutError(f"{name}: text must be printable ASCII, at least one character")
            p0, adv, fh = FONTS[font]
            widgets.append({"type": "label", "x": x, "y": y, "w": adv * len(text), "h": fh,
                            "text": text, "font": font})
        else:
            raise LayoutError(f"{name}: type must be image or label")
    if len(widgets) > CUSTOM_MAX:
        raise LayoutError(f"{len(widgets)} widgets: the firmware draws at most {CUSTOM_MAX}")
    if strict:
        check(els, widgets)
    return els, widgets


def builtin_rects(els):
    """What each visible built-in owns, as display_layout_rect() says."""
    r = {}
    for k in ("os", "conn") + BANDS:
        if not els[k].get("hidden"):
            r[k] = (els[k]["x"], els[k]["y"]) + element_size(els, k)
    return r


def all_rects(els):
    """Every built-in's rect, hidden or not (the bounds apply to all)."""
    shown = {k: dict(v, hidden=False) for k, v in els.items()}
    return builtin_rects(shown)


def meets(a, b):
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def collisions(els, widgets=()):
    """Every problem with the placement, as (what, why) pairs: what the editor
    outlines in red. display_layout_valid() and place_ok(), rule for rule."""
    out = []
    for k, (x, y, w, h) in all_rects(els).items():
        if x + w > PANEL or y + h > PANEL:
            out.append(((k,), f"{NAMES[k]} runs off the screen"))
    for b in BANDS:
        if els[b]["w"] < MIN_W[b]:
            out.append(((b,), f"{NAMES[b]} must be at least {MIN_W[b]} pixels wide"))
    owned = builtin_rects(els)
    keys = list(owned)
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            ra, rb = owned[a], owned[b]
            # The lock row's last row may sit on the battery row's first: both blank.
            if a == "lock" and b == "status" and ra[1] + ra[3] - FONT_STATUS_ROW0_BLANK == rb[1]:
                ra = (ra[0], ra[1], ra[2], ra[3] - FONT_STATUS_ROW0_BLANK)
            if meets(ra, rb):
                out.append(((a, b), f"{NAMES[a]} and {NAMES[b]} overlap"))
    for i, w in enumerate(widgets):
        r = (w["x"], w["y"], w["w"], w["h"])
        if w["x"] + w["w"] > PANEL or w["y"] + w["h"] > PANEL:
            out.append(((i,), f"widget {i + 1} ({w['type']}) runs off the screen"))
        for k2, br in owned.items():
            if meets(r, br):
                out.append(((i, k2), f"widget {i + 1} ({w['type']}) overlaps {NAMES[k2]}"))
        for j in range(i):
            v = widgets[j]
            if meets(r, (v["x"], v["y"], v["w"], v["h"])):
                out.append(((i, j), f"widget {i + 1} overlaps widget {j + 1}"))
    return out


def check(els, widgets):
    probs = collisions(els, widgets)
    if probs:
        raise LayoutError(probs[0][1])


# --- the package (graphics/layout_store.h) --------------------------------------

def pack(els, widgets, seq=1):
    recs, strings, blobs = [], b"", []
    sized, p0 = size_records(els)
    for name in ("os", "conn", "text", "clock", "lock", "status"):
        e = els[name]
        flags = 1 if e.get("hidden") else 0
        recs.append([KIND[name], flags, e.get("x", 0), e["y"], e.get("w", 0), 0, p0.get(name, 0), 0, 0])
    for r, data in sized:
        recs.append(r + [("b", len(blobs))])
        blobs.append(data)
    for w in widgets:
        if w["type"] == "label":
            recs.append([KIND["label"], 0, w["x"], w["y"], 0, 0, FONTS[w["font"]][0], len(w["text"]), ("s", len(strings))])
            strings += w["text"].encode("ascii")
        else:
            kind = KIND["anim"] if w["type"] == "anim" else KIND["image"]
            recs.append([kind, 0, w["x"], w["y"], w["w"], w["h"], w.get("frames", 0), 0, ("b", len(blobs))])
            blobs.append(w["data"])
    if len(recs) > RECORDS_MAX:
        raise LayoutError("too many records")
    meta_len = 32 + 16 * len(recs) + len(strings)
    if meta_len > META_MAX:
        raise LayoutError("the labels' text is too long")
    first_data = (meta_len + 1) & ~1
    data_off, offs = first_data, []
    for b in blobs:
        offs.append(data_off)
        data_off += len(b)
    total = data_off
    if total > SLOT_SIZE:
        raise LayoutError(f"the images take {total} bytes; a slot holds {SLOT_SIZE}")
    out = bytearray(32)
    for r in recs:
        ref = r[8]
        off = 0
        if isinstance(ref, tuple):
            off = 32 + 16 * len(recs) + ref[1] if ref[0] == "s" else offs[ref[1]]
        out += struct.pack("<8BII", *r[:8], off, 0)
    out += strings
    out += bytes(first_data - len(out))
    for b in blobs:
        out += b
    assert len(out) == total, (len(out), total)
    struct.pack_into("<IBBHII", out, 0, MAGIC, VERSION, len(recs), meta_len, seq & 0xFFFFFFFF, total)
    crc = zlib.crc32(bytes(out[20:meta_len]), zlib.crc32(bytes(out[:16])))
    struct.pack_into("<I", out, 16, crc)
    return bytes(out)


def load(path):
    with open(path) as f:
        doc = json.load(f)
    return resolve(doc, os.path.dirname(os.path.abspath(path)))


# --- the board's flash channel ----------------------------------------------------

def find_hidraw():
    want = "HID_ID=0003:%08X:%08X" % (VID, PID)
    hits = []
    for node in sorted(glob.glob(os.path.join(SYSFS, "hidraw*"))):
        try:
            with open(os.path.join(node, "device", "uevent")) as f:
                if want not in f.read().upper().split("\n"):
                    continue
            with open(os.path.join(node, "device", "report_descriptor"), "rb") as f:
                if f.read(3) != b"\x06\x60\xff":
                    continue
        except OSError:
            continue
        hits.append(os.path.join(DEV, os.path.basename(node)))
    if not hits:
        raise LayoutError("the keyboard was not found on USB (is the cable in?)")
    if len(hits) > 1:
        raise LayoutError("more than one AK820 Pro found; unplug one")
    return hits[0]


class Board:
    def __init__(self):
        path = find_hidraw()
        try:
            self.fd = os.open(path, os.O_RDWR)
        except PermissionError:
            raise LayoutError(f"no permission on {path}: install hostagent/60-ak820pro.rules (setup.sh does)")

    def close(self):
        os.close(self.fd)

    def cmd(self, c, payload=b"", timeout=1.0, busy_s=5.0):
        """One flash-channel command; re-sent while the chip is busy. Only a
        reply naming this command is taken."""
        pkt = bytes([SET_VALUE, FLASH_CHANNEL, c]) + bytes(payload)
        pkt += bytes(REPORT - len(pkt))
        end = time.monotonic() + busy_s
        while True:
            os.write(self.fd, b"\x00" + pkt)
            r = self._reply(pkt[:3], timeout)
            if r is None:
                raise LayoutError("the keyboard did not answer (another program holding it? the agent is paused for uploads)")
            if r[3] == FS_BUSY and time.monotonic() < end:
                time.sleep(0.002)
                continue
            return r

    def _reply(self, header, timeout):
        end = time.monotonic() + timeout
        while (left := end - time.monotonic()) > 0:
            if not select.select([self.fd], [], [], left)[0]:
                break
            r = os.read(self.fd, 64)
            if r[:3] == header:
                return r
            if r[:1] == b"\xff" and r[1:3] == header[1:3]:
                raise LayoutError("the firmware does not know this command: flash the screen-layout firmware first")
        return None

    def info(self):
        r = self.cmd(FC_LAYOUT_INFO)
        if r[3] != FS_OK:
            raise LayoutError(f"layout info failed (status {r[3]})")
        d = r[4:16]
        return {"version": d[0], "loading": bool(d[1]), "active": d[2], "seq": struct.unpack("<I", d[3:7])[0],
                "error": d[7], "widgets": d[8], "slots": (d[9] << 16, d[10] << 16), "max_widgets": d[11]}

    def erase(self, addr):
        r = self.cmd(FC_ERASE, addr.to_bytes(3, "big"), busy_s=10.0)
        if r[3] != FS_OK:
            raise LayoutError(f"erase at {addr:#x} refused (status {r[3]})")

    def write(self, addr, data, progress=None):
        for a in range(addr & ~0xFFF, addr + len(data), 0x1000):
            self.erase(a)
        r = self.cmd(FC_WRITE_BEGIN, addr.to_bytes(3, "big"))
        if r[3] != FS_OK:
            raise LayoutError(f"write begin refused (status {r[3]})")
        for i in range(0, len(data), CHUNK):
            part = data[i:i + CHUNK]
            r = self.cmd(FC_WRITE_DATA, bytes([len(part)]) + part)
            if r[3] != FS_OK:
                raise LayoutError(f"write refused at byte {i} (status {r[3]})")
            if progress:
                progress(i + len(part), len(data))
        r = self.cmd(FC_WRITE_END)
        if r[3] != FS_OK:
            raise LayoutError(f"write end refused (status {r[3]})")

    def crc(self, addr, n):
        r = self.cmd(FC_CRC32, addr.to_bytes(3, "big") + n.to_bytes(3, "big"))
        while r[3] == FS_MORE:
            r = self.cmd(FC_CRC_NEXT)
        if r[3] != FS_OK:
            raise LayoutError(f"CRC failed (status {r[3]})")
        return int.from_bytes(r[4:8], "big")

    def reload(self, wait=10.0):
        r = self.cmd(FC_LAYOUT_RELOAD)
        if r[3] != FS_OK:
            raise LayoutError("the keyboard is already loading a layout; try again")
        end = time.monotonic() + wait
        while time.monotonic() < end:
            i = self.info()
            if not i["loading"]:
                return i
            time.sleep(0.05)
        raise LayoutError("the keyboard did not finish loading the layout")


class AgentPaused:
    """Stop the Linux agent around an upload, as flash.sh does: its reports
    would otherwise share the interface with ours."""
    def __enter__(self):
        self.was = False
        try:
            r = subprocess.run(["systemctl", "--user", "is-active", AGENT_UNIT], capture_output=True, text=True)
            if r.stdout.strip() == "active":
                subprocess.run(["systemctl", "--user", "stop", AGENT_UNIT], check=True)
                self.was = True
        except (OSError, subprocess.CalledProcessError):
            pass
        return self

    def __exit__(self, *a):
        if self.was:
            subprocess.run(["systemctl", "--user", "start", AGENT_UNIT])


def describe(i):
    where = {0: "slot A", 1: "slot B", 0xFF: "the built-in default"}.get(i["active"], "?")
    s = f"active: {where}"
    if i["active"] != 0xFF:
        s += f", seq {i['seq']}, {i['widgets']} custom widget(s)"
    if i["error"]:
        s += f"; last load refused: {ERRORS.get(i['error'], i['error'])}"
    return s


def upload(els, widgets, progress=None, log=print):
    with AgentPaused():
        b = Board()
        try:
            cur = b.info()
            target = 1 if cur["active"] == 0 else 0
            seq = (cur["seq"] + 1) if cur["active"] != 0xFF else 1
            blob = pack(els, widgets, seq)
            addr = SLOTS[target]
            log(f"writing {len(blob)} bytes to slot {'AB'[target]} (about {len(blob) / 2700 + 1:.0f} s)")
            b.write(addr, blob, progress)
            got = b.crc(addr, len(blob))
            want = zlib.crc32(blob)
            if got != want:
                raise LayoutError(f"verify failed: the keyboard read {got:#010x}, expected {want:#010x}; the old layout stays")
            info = b.reload()
            if info["active"] != target or info["seq"] != seq:
                raise LayoutError("the keyboard did not take the layout: " + describe(info))
            log("done: " + describe(info))
            return info
        finally:
            b.close()


def reset(log=print):
    with AgentPaused():
        b = Board()
        try:
            for a in SLOTS:
                b.erase(a)
            info = b.reload()
            log("done: " + describe(info))
            return info
        finally:
            b.close()


def default_doc():
    return {"elements": {k: dict(v) for k, v in DEFAULT.items()}, "widgets": []}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "upload"):
        sub.add_parser(name).add_argument("layout")
    p = sub.add_parser("pack")
    p.add_argument("layout")
    p.add_argument("-o", "--out", required=True)
    p.add_argument("--seq", type=int, default=1)
    sub.add_parser("info")
    sub.add_parser("reset")
    sub.add_parser("default")
    p = sub.add_parser("serve")
    p.add_argument("--port", type=int, default=8820)
    p.add_argument("--dir", default=".", help="where layouts and images are read and saved")
    p.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    try:
        if a.cmd == "check":
            els, widgets = load(a.layout)
            size = len(pack(els, widgets))
            print(f"ok: {len(widgets)} widget(s), {size} bytes, about {size / 2700 + 1:.0f} s to upload")
        elif a.cmd == "pack":
            els, widgets = load(a.layout)
            open(a.out, "wb").write(pack(els, widgets, a.seq))
        elif a.cmd == "info":
            b = Board()
            try:
                print(describe(b.info()))
            finally:
                b.close()
        elif a.cmd == "upload":
            els, widgets = load(a.layout)
            upload(els, widgets, progress=lambda d, n: print(f"\r{d * 100 // n:3d}%", end="", file=sys.stderr, flush=True))
        elif a.cmd == "reset":
            reset()
        elif a.cmd == "default":
            json.dump(default_doc(), sys.stdout, indent=2)
            print()
        elif a.cmd == "serve":
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import ak820layout_editor
            ak820layout_editor.serve(a.port, os.path.abspath(a.dir), open_browser=not a.no_browser)
    except LayoutError as e:
        raise SystemExit(f"ak820layout: {e}")


if __name__ == "__main__":
    main()
