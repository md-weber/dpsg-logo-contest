#!/usr/bin/env python3
"""Wahl-Token-Bögen als PDF (nur Python-Standardbibliothek).

A4, Pfadfinder-Look: grüner Kopf mit Kompassrose, Token-Coupons mit
gestrichelten Schnittlinien, Stamm + laufende Nummer pro Coupon.
12 (2x6), 18 (3x6) oder 24 (3x8) Tokens pro Seite.

Base-14-Fonts (Helvetica, Helvetica-Bold, ZapfDingbats) -> kein Font-Embedding.
Textbreiten werden mit den echten AFM-Metriken berechnet; jeder Text wird
in seinen Container eingepasst (verkleinert bzw. mit ... gekürzt).

Objekt-Layout (fest, ohne Vorwärts-Rätselraten):
  1 Catalog, 2 Pages, 3 Helvetica, 4 Helvetica-Bold, 5 ZapfDingbats, 6 Info,
  dann je Seite i: 7+2i Page-Objekt, 8+2i Content-Stream.
"""
import math
import zlib

PW, PH = 595.28, 841.89          # A4 in pt
MARGIN = 28.0
HEADER_H = 62.0
FOOTER_H = 34.0
# per_page -> (Spalten, Zeilen, max. Schriftgröße Token)
LAYOUTS = {12: (2, 6, 22.0), 18: (3, 6, 16.0), 24: (3, 8, 15.0)}
PER_PAGE_OPTIONS = tuple(sorted(LAYOUTS))

GREEN = (0.039, 0.478, 0.239)
DARK = (0.071, 0.188, 0.125)
INK = (0.11, 0.17, 0.13)
GREY = (0.40, 0.45, 0.42)
CUT = (0.62, 0.66, 0.63)
WHITE = (1.0, 1.0, 1.0)
LIGHT = (0.86, 0.94, 0.88)

F_REG, F_BOLD, F_DING = "F1", "F2", "F3"

# AFM-Breiten (1/1000 em) für ASCII 32..126
_W_REG = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
          556, 556, 556, 556, 556, 556, 556, 556, 556, 556,
          278, 278, 584, 584, 584, 556, 1015,
          667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778, 667, 778,
          722, 667, 611, 722, 667, 944, 667, 667, 611,
          278, 278, 278, 469, 556, 333,
          556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556,
          333, 500, 278, 556, 500, 722, 500, 500, 500,
          334, 260, 334, 584]
_W_BOLD = [278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278,
           556, 556, 556, 556, 556, 556, 556, 556, 556, 556,
           333, 333, 584, 584, 584, 611, 975,
           722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778, 667, 778,
           722, 667, 611, 722, 667, 944, 667, 667, 611,
           333, 278, 333, 584, 556, 333,
           556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611, 611, 611,
           389, 556, 333, 611, 556, 778, 556, 556, 500,
           389, 280, 389, 584]
_X_REG = {"ä": 556, "ö": 556, "ü": 556, "Ä": 667, "Ö": 778, "Ü": 722, "ß": 611, "é": 556,
          "·": 278, "–": 556, "•": 350, "…": 1000, "’": 222, "„": 333, "“": 333}
_X_BOLD = {"ä": 556, "ö": 611, "ü": 611, "Ä": 722, "Ö": 778, "Ü": 722, "ß": 611, "é": 556,
           "·": 278, "–": 556, "•": 350, "…": 1000, "’": 278, "„": 500, "“": 500}


def _cp(s):
    """Auf das darstellbare WinAnsi (cp1252) reduzieren, Rest -> '?'."""
    return str(s).encode("cp1252", "replace").decode("cp1252")


def text_width(s, font, size):
    bold = font == F_BOLD
    table, extra = (_W_BOLD, _X_BOLD) if bold else (_W_REG, _X_REG)
    total = 0
    for ch in _cp(s):
        o = ord(ch)
        total += table[o - 32] if 32 <= o <= 126 else extra.get(ch, 556)
    return total * size / 1000.0


def fit_size(s, font, max_size, width, min_size=6.0):
    w1 = text_width(s, font, 1.0)
    if w1 <= 0:
        return max_size
    return max(min_size, min(max_size, math.floor(width / w1 * 10) / 10))


def ellipsize(s, font, size, width):
    s = _cp(s)
    if text_width(s, font, size) <= width:
        return s
    while s and text_width(s + "…", font, size) > width:
        s = s[:-1]
    return (s.rstrip() + "…") if s else ""


def _pdf_string(s):
    b = _cp(s).encode("cp1252")
    return b"(" + b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


class _Page:
    def __init__(self, boxes):
        self.ops = []
        self.boxes = boxes

    def raw(self, s):
        self.ops.append(s.encode("ascii") if isinstance(s, str) else s)

    def color(self, rgb, stroke=False):
        self.raw("%.3f %.3f %.3f %s" % (rgb[0], rgb[1], rgb[2], "RG" if stroke else "rg"))

    def rect(self, x, y, w, h, fill=None, stroke=None, lw=1.0):
        if fill:
            self.color(fill)
        if stroke:
            self.color(stroke, True)
            self.raw("%.2f w" % lw)
        op = "B" if (fill and stroke) else ("f" if fill else "S")
        self.raw("%.2f %.2f %.2f %.2f re %s" % (x, y, w, h, op))

    def line(self, x1, y1, x2, y2, rgb, lw=0.5, dash=None):
        self.color(rgb, True)
        self.raw("%.2f w" % lw)
        if dash:
            self.raw("[%s] 0 d" % " ".join("%g" % d for d in dash))
        self.raw("%.2f %.2f m %.2f %.2f l S" % (x1, y1, x2, y2))
        if dash:
            self.raw("[] 0 d")

    def text(self, x, y, s, font, size, rgb, align="left", container=None):
        s = _cp(s)
        w = text_width(s, font, size)
        if align == "center":
            x -= w / 2
        elif align == "right":
            x -= w
        self.color(rgb)
        self.raw(b"BT /" + font.encode() + b" %.2f Tf %.2f %.2f Td " % (size, x, y)
                 + _pdf_string(s) + b" Tj ET")
        if container is not None:
            self.boxes.append({"text": s, "x0": x, "x1": x + w, "y": y, "size": size,
                               "c0": container[0], "c1": container[1]})

    def star(self, cx, cy, r_out, r_in, points, rgb, rot=0.0):
        pts = []
        for k in range(points * 2):
            a = math.pi / 2 + rot + k * math.pi / points
            r = r_out if k % 2 == 0 else r_in
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        self.color(rgb)
        self.raw("%.2f %.2f m " % pts[0] + " ".join("%.2f %.2f l" % p for p in pts[1:]) + " h f")

    def compass(self, cx, cy, r, rgb):
        self.star(cx, cy, r, r * 0.22, 4, rgb)
        self.star(cx, cy, r * 0.62, r * 0.18, 4, rgb, rot=math.pi / 4)

    def stream(self):
        return b"\n".join(self.ops)


def _coupon(pg, x0, y0, cw, ch, nr, tok, batch, max_tok, host):
    pad = 5.0
    ix, iy, iw, ih = x0 + pad, y0 + pad, cw - 2 * pad, ch - 2 * pad
    top = iy + ih
    sh = 14.0
    pg.rect(ix, iy, iw, ih, fill=WHITE, stroke=GREEN, lw=0.9)
    pg.rect(ix, top - sh, iw, sh, fill=GREEN)
    pg.compass(ix + 9, top - sh / 2, 4.6, WHITE)
    lab = "WAHL-TOKEN"
    pg.text(ix + 17, top - sh + 4.2, lab, F_BOLD, 7.5, WHITE, container=(ix, ix + iw))
    left_end = ix + 17 + text_width(lab, F_BOLD, 7.5) + 8
    avail = ix + iw - 6 - left_end
    nr_txt = "Nr. %d" % nr
    right = nr_txt
    if batch:
        sep = "  ·  "
        b = ellipsize(batch, F_REG, 7, avail - text_width(sep + nr_txt, F_REG, 7))
        if b:
            right = b + sep + nr_txt
    pg.text(ix + iw - 6, top - sh + 4.2, right, F_REG, 7, WHITE, align="right",
            container=(left_end, ix + iw - 5))
    # Token: so groß wie möglich, aber immer vollständig im Coupon
    size = fit_size(tok, F_BOLD, max_tok, iw - 16)
    area_top, area_bot = top - sh, iy + 20
    ty = (area_top + area_bot) / 2 - size * 0.35
    pg.text(ix + iw / 2, ty, tok, F_BOLD, size, INK, align="center",
            container=(ix + 8, ix + iw - 8))
    l1 = "Abstimmen unter " + host
    pg.text(ix + iw / 2, iy + 11, l1, F_REG, fit_size(l1, F_REG, 7, iw - 12), GREY,
            align="center", container=(ix + 6, ix + iw - 6))
    l2 = "1 Stimme · anonym · nicht weitergeben"
    pg.text(ix + iw / 2, iy + 3.5, l2, F_REG, fit_size(l2, F_REG, 6, iw - 12), GREY,
            align="center", container=(ix + 6, ix + iw - 6))


def _draw_page(pg, entries, page_no, n_pages, per_page, title, subtitle, url):
    cols, rows, max_tok = LAYOUTS[per_page]
    host = url.split("://", 1)[-1].rstrip("/") if url else ""
    # Kopf
    hx, hy, hw = MARGIN, PH - MARGIN - HEADER_H, PW - 2 * MARGIN
    pg.rect(hx, hy, hw, HEADER_H, fill=GREEN)
    pg.rect(hx, hy, hw, 4, fill=DARK)
    pg.compass(hx + hw - 32, hy + HEADER_H / 2 + 2, 20, LIGHT)
    tw = hw - 32 - 60
    pg.text(hx + 16, hy + HEADER_H - 28, title, F_BOLD, fit_size(title, F_BOLD, 20, tw), WHITE,
            container=(hx + 16, hx + 16 + tw))
    sub = ellipsize("%s  ·  Blatt %d/%d" % (subtitle, page_no, n_pages), F_REG, 10, tw)
    pg.text(hx + 16, hy + 14, sub, F_REG, 10, LIGHT, container=(hx + 16, hx + 16 + tw))
    # Raster mit Schnittlinien
    gl, gr = MARGIN, PW - MARGIN
    gt, gb = hy - 12, MARGIN + FOOTER_H
    cw, ch = (gr - gl) / cols, (gt - gb) / rows
    for k in range(cols + 1):
        pg.line(gl + k * cw, gb, gl + k * cw, gt, CUT, 0.5, (3, 3))
    for k in range(rows + 1):
        pg.line(gl, gb + k * ch, gr, gb + k * ch, CUT, 0.5, (3, 3))
    pg.color(CUT)
    pg.raw(b"BT /F3 9 Tf %.2f %.2f Td (\") Tj ET" % (gl + 3, gt + 2.5))  # Schere
    for i, (nr, tok, batch) in enumerate(entries):
        r_i, c_i = divmod(i, cols)
        _coupon(pg, gl + c_i * cw, gt - (r_i + 1) * ch, cw, ch, nr, tok, batch, max_tok, host)
    # Fuß
    fw = PW - 2 * MARGIN
    l1 = ("So geht's: %s öffnen  ·  Lieblingslogo aussuchen  ·  Token eintippen  ·  abstimmen."
          % (host or "die Wettbewerbsseite"))
    pg.text(MARGIN, MARGIN + 19, l1, F_BOLD, fit_size(l1, F_BOLD, 8.5, fw), INK,
            container=(MARGIN, MARGIN + fw))
    l2 = ("Jedes Token gilt für genau eine Stimme und ist anonym – bitte nicht weitergeben. "
          "Groß-/Kleinschreibung ist egal. Entlang der gestrichelten Linien ausschneiden.")
    pg.text(MARGIN, MARGIN + 7, l2, F_REG, fit_size(l2, F_REG, 7.5, fw), GREY,
            container=(MARGIN, MARGIN + fw))


def tokens_pdf(entries, per_page=12, title="Logo-Wettbewerb Bezirk Karlsruhe",
               subtitle="Wahl-Tokens", url="", debug=None):
    """entries: Liste von (nr, token, stamm). Gibt die PDF-Bytes zurück.
    debug: optionale Liste, in die alle Textboxen (für Tests) geschrieben werden."""
    if per_page not in LAYOUTS:
        per_page = 12
    entries = [(int(nr), str(tok), str(batch or "")) for nr, tok, batch in entries]
    pages = [entries[i:i + per_page] for i in range(0, len(entries), per_page)] or [[]]
    boxes = debug if debug is not None else []
    streams = []
    for i, chunk in enumerate(pages, 1):
        pg = _Page(boxes)
        _draw_page(pg, chunk, i, len(pages), per_page, title, subtitle, url)
        streams.append(pg.stream())

    n = len(pages)
    objs = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % (7 + 2 * i) for i in range(n))
           + b"] /Count %d >>" % n,
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        4: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
        5: b"<< /Type /Font /Subtype /Type1 /BaseFont /ZapfDingbats >>",
        6: b"<< /Title " + _pdf_string("%s - %s" % (title, subtitle))
           + b" /Producer (Logo-Wettbewerb Bezirk Karlsruhe) >>",
    }
    res = b"<< /ProcSet [/PDF /Text] /Font << /F1 3 0 R /F2 4 0 R /F3 5 0 R >> >>"
    for i, st in enumerate(streams):
        pnum, cnum = 7 + 2 * i, 8 + 2 * i
        objs[pnum] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.2f %.2f] /Resources " % (PW, PH)
                      + res + b" /Contents %d 0 R >>" % cnum)
        comp = zlib.compress(st, 9)
        objs[cnum] = (b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(comp)
                      + comp + b"\nendstream")

    size = max(objs) + 1
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for num in range(1, size):
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num + objs[num] + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for num in range(1, size):
        out += b"%010d 00000 n \n" % offsets[num]
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info 6 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref)
    return bytes(out)


if __name__ == "__main__":
    demo = [(i + 1, t, "Stamm Daxlanden") for i, t in enumerate(
        ["Halstuch-Laterne-B12F", "Wolf-Zelt-45A4", "Adler-Eule-9869"] * 9)]
    for pp in PER_PAGE_OPTIONS:
        path = "/tmp/wahl-tokens-demo-%d.pdf" % pp
        with open(path, "wb") as f:
            f.write(tokens_pdf(demo, pp, url="http://152.53.190.231:8391"))
        print("geschrieben", path)
