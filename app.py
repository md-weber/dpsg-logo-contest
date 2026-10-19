#!/usr/bin/env python3
"""Logo-Wettbewerb Bezirk Karlsruhe — v3.

Nur Python-Standardbibliothek (http.server + sqlite3), keine Abhängigkeiten.

Öffentlich:
  /            Galerie, Einreichen, Abstimmen (mit Wahl-Token)
  /ergebnis    Ergebnis (erst sichtbar, wenn im Admin freigegeben)
  /hilfe       Kurzinfo, Datenschutz, Anleitung für Admins
Admin (/admin, Passwort):
  Phasen steuern, Tokens pro Stamm erzeugen, Token-PDFs (12/18/24 pro A4),
  Vorschläge moderieren, Ergebnis + CSV-Export, Runde zurücksetzen,
  alles löschen (DSGVO), Passwort ändern.

Anonymität: Eine Stimme speichert nur den Vorschlag + Tag. Welches Token für
welchen Vorschlag gestimmt hat, wird nirgends gespeichert.

Start:  python3 app.py   (PORT, PUBLIC_URL, LOGO_DATA_DIR per Umgebung)
"""
import csv
import hashlib
import hmac
import html
import io
import os
import re
import secrets
import sqlite3
import threading
import time
import traceback
import unicodedata
import uuid
from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import pfadi_pdf

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.environ.get("LOGO_DATA_DIR", BASE))
UPLOAD_DIR = os.path.join(DATA_DIR, "static", "uploads")
DB_PATH = os.path.join(DATA_DIR, "contest.db")
HASH_PATH = os.path.join(DATA_DIR, "admin.hash")
INITIAL_PW_PATH = os.path.join(DATA_DIR, "admin-initial-password.txt")
PORT = int(os.environ.get("PORT", "8391"))
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://152.53.190.231:8391").rstrip("/")

MAX_UPLOAD = 8 * 1024 * 1024
MAX_FORM = 64 * 1024
MAX_DRAIN = 32 * 1024 * 1024
SESSION_TTL = 12 * 3600
MAX_TOKENS_PER_REQUEST = 500

MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".svg": "image/svg+xml"}
UPLOAD_RE = re.compile(r"^[0-9a-f]{32}\.(png|jpg|jpeg|svg)$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Pfadfinder-Wörter: kurz, ohne Umlaute (leichter zu tippen, auch für Wölflinge)
WORDS = [
    "Adler", "Wolf", "Falke", "Biber", "Fuchs", "Reh", "Hirsch", "Eule", "Dachs", "Otter",
    "Luchs", "Rabe", "Specht", "Igel", "Pfad", "Spur", "Lager", "Feuer", "Funke", "Glut",
    "Kompass", "Stern", "Wald", "Wiese", "Fluss", "Bach", "Fels", "Zelt", "Jurte", "Kothe",
    "Kluft", "Rudel", "Meute", "Halstuch", "Lied", "Laterne", "Knoten", "Lilie", "Fahrt",
    "Weg", "Appell", "Kanu", "Karte", "Berg", "Mond", "Sonne", "Quelle", "Gipfel",
]
DEFAULT_SETTINGS = {"uploads_open": "1", "voting_open": "1", "show_results": "0"}


# ---------------------------------------------------------------- helpers

def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def clean(s, maxlen):
    s = " ".join(str(s or "").split())
    return s if len(s) <= maxlen else None


_UMLAUT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def norm_token(s):
    """Tolerant gegenüber Groß/klein, Leerzeichen, fehlenden/anderen Bindestrichen,
    Umlauten und O/0- bzw. I/l/1-Verwechslung im 4-stelligen Code am Ende."""
    s = unicodedata.normalize("NFKC", s or "").lower().translate(_UMLAUT)
    s = re.sub(r"[^a-z0-9]", "", s)
    if len(s) >= 4:
        s = s[:-4] + s[-4:].translate(str.maketrans("oil", "011"))
    return s


def same_person(a, b):
    return " ".join(a.casefold().split()) == " ".join(b.casefold().split())


def slug(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower() or "alle"


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    except BaseException:
        c.rollback()
        raise
    finally:
        c.close()


def _cols(c, table):
    return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}


def init_db():
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    with conn() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript("""
          CREATE TABLE IF NOT EXISTS proposals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uploader_name TEXT NOT NULL, uploader_email TEXT, title TEXT,
            filename TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')));
          CREATE TABLE IF NOT EXISTS votes(
            id INTEGER PRIMARY KEY AUTOINCREMENT, proposal_id INTEGER NOT NULL, created_at TEXT);
          CREATE TABLE IF NOT EXISTS tokens(
            tok TEXT PRIMARY KEY, norm TEXT, batch TEXT NOT NULL DEFAULT '',
            used INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT (datetime('now')));
          CREATE TABLE IF NOT EXISTS admin_sessions(sid TEXT PRIMARY KEY, created_at REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        # --- Migration aus v1/v2 ---
        if "title" not in _cols(c, "proposals"):
            c.execute("ALTER TABLE proposals ADD COLUMN title TEXT")
        tcols = _cols(c, "tokens")
        if "norm" not in tcols:
            c.execute("ALTER TABLE tokens ADD COLUMN norm TEXT")
        if "batch" not in tcols:
            c.execute("ALTER TABLE tokens ADD COLUMN batch TEXT NOT NULL DEFAULT ''")
        for col in ("used_by", "used_at"):          # Verknüpfung Token<->Stimme entfernen
            if col in tcols:
                c.execute(f"UPDATE tokens SET {col}=NULL")
                c.execute(f"ALTER TABLE tokens DROP COLUMN {col}")
        if _cols(c, "votes") - {"id", "proposal_id", "created_at"}:   # alte Spalten (Token/Name)
            c.executescript("""
              CREATE TABLE votes_new(id INTEGER PRIMARY KEY AUTOINCREMENT,
                                     proposal_id INTEGER NOT NULL, created_at TEXT);
              INSERT INTO votes_new(proposal_id, created_at) SELECT proposal_id, date(created_at) FROM votes;
              DROP TABLE votes; ALTER TABLE votes_new RENAME TO votes;
            """)
        for r in c.execute("SELECT tok FROM tokens WHERE norm IS NULL").fetchall():
            c.execute("UPDATE tokens SET norm=? WHERE tok=?", (norm_token(r["tok"]), r["tok"]))
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS tokens_norm ON tokens(norm)")
        c.execute("CREATE INDEX IF NOT EXISTS votes_pid ON votes(proposal_id)")
        # Sitzungen: Tabelle sauber mit REAL-Prinzip neu anlegen (alte v2-Spalte war TEXT ->
        # floats wurden als Text gespeichert und die Gültigkeitsprüfung schlug fehl).
        scols = _cols(c, "admin_sessions")
        if "created_at" not in scols or scols != {"sid", "created_at"} or \
                c.execute("PRAGMA table_info(admin_sessions)").fetchall()[1][2] != "REAL":
            c.execute("DROP TABLE IF EXISTS admin_sessions")
            c.execute("CREATE TABLE admin_sessions(sid TEXT PRIMARY KEY, created_at REAL NOT NULL)")
        else:
            # nur abgelaufene raus
            c.execute("DELETE FROM admin_sessions WHERE created_at<?", (time.time() - SESSION_TTL,))
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))


def get_settings():
    with conn() as c:
        d = dict(DEFAULT_SETTINGS)
        d.update({r["key"]: r["value"] for r in c.execute("SELECT key,value FROM settings")})
    return {k: v == "1" for k, v in d.items()}


def make_token():
    return "%s-%s-%s" % (secrets.choice(WORDS), secrets.choice(WORDS), secrets.token_hex(2).upper())


# ---------------------------------------------------------------- password / sessions

def _write_private(path, text):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def hash_pw(pw, salt=None, it=240_000):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), it).hex()
    return f"pbkdf2_sha256${it}${salt}${h}"


def set_pw(pw):
    _write_private(HASH_PATH, hash_pw(pw))


def check_pw(pw):
    """-> (ok, needs_upgrade)"""
    try:
        with open(HASH_PATH) as f:
            stored = f.read().strip()
    except OSError:
        return False, False
    if stored.startswith("pbkdf2_sha256$"):
        _, it, salt, h = stored.split("$")
        return hmac.compare_digest(hash_pw(pw, salt, int(it)).split("$")[3], h), False
    ok = hmac.compare_digest(hashlib.sha256(pw.encode()).hexdigest(), stored)   # v2-Format
    return ok, ok


def ensure_admin_hash():
    if os.path.exists(HASH_PATH):
        return None
    pw = secrets.token_urlsafe(12)
    set_pw(pw)
    _write_private(INITIAL_PW_PATH, pw + "\n")
    print(f"[!] Erstes Admin-Passwort steht in {INITIAL_PW_PATH} — nach dem Login ändern.", flush=True)
    return pw


def new_session():
    sid = secrets.token_urlsafe(32)
    with conn() as c:
        c.execute("DELETE FROM admin_sessions WHERE created_at<?", (time.time() - SESSION_TTL,))
        c.execute("INSERT INTO admin_sessions(sid,created_at) VALUES(?,?)", (sid, time.time()))
    return sid


def valid_session(sid):
    with conn() as c:
        r = c.execute("SELECT created_at FROM admin_sessions WHERE sid=?", (sid,)).fetchone()
    return bool(r) and isinstance(r["created_at"], float) and time.time() - r["created_at"] < SESSION_TTL


def csrf_for(sid):
    return hashlib.sha256(("csrf:" + sid).encode()).hexdigest()[:40]


class Limiter:
    """Zählt Fehlversuche je IP nur im Arbeitsspeicher (nichts wird gespeichert)."""

    def __init__(self, limit, window):
        self.limit, self.window, self.hits, self.lock = limit, window, {}, threading.Lock()

    def blocked(self, key):
        now = time.time()
        with self.lock:
            q = [t for t in self.hits.get(key, []) if now - t < self.window]
            if q:
                self.hits[key] = q
            else:
                self.hits.pop(key, None)
            return len(q) >= self.limit

    def fail(self, key):
        with self.lock:
            self.hits.setdefault(key, []).append(time.time())


LOGIN_LIMIT = Limiter(5, 900)
VOTE_LIMIT = Limiter(30, 900)


# ---------------------------------------------------------------- form parsing

def parse_multipart(body, boundary):
    fields, files = {}, {}
    for piece in (b"\r\n" + body).split(b"\r\n--" + boundary)[1:]:
        if piece.startswith(b"--"):
            break
        if piece.startswith(b"\r\n"):
            piece = piece[2:]
        head, sep, content = piece.partition(b"\r\n\r\n")
        if not sep:
            continue
        cd = next((l for l in head.decode("utf-8", "replace").split("\r\n")
                   if l.lower().startswith("content-disposition:")), "")
        nm = re.search(r';\s*name="([^"]*)"', cd)
        fn = re.search(r';\s*filename="([^"]*)"', cd)
        if not nm:
            continue
        if fn:
            files[nm.group(1)] = (fn.group(1), content)
        else:
            fields[nm.group(1)] = content.decode("utf-8", "replace")
    return fields, files


def parse_form(ctype, body):
    low = (ctype or "").lower()
    if low.startswith("multipart/form-data"):
        m = re.search(r'boundary=(?:"([^"]+)"|([^;\s]+))', ctype, re.I)
        return parse_multipart(body, (m.group(1) or m.group(2)).encode("latin-1")) if m else ({}, {})
    qs = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
    return {k: v[0] for k, v in qs.items()}, {}


def sniff_image(data):
    """-> ('.png'|'.jpg'|'.svg', None) oder (None, fehlercode)"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", None
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg", None
    text = data.decode("utf-8", "replace")
    if re.search(r"<svg[\s>]", text, re.I):
        bad = (r"<\s*script", r"<\s*foreignobject", r"<\s*iframe", r"<\s*embed", r"<\s*object",
               r"\bon[a-z]+\s*=", r"javascript\s*:", r"<!entity")
        if any(re.search(p, text, re.I) for p in bad):
            return None, "err_svg"
        return ".svg", None
    return None, "err_type"


# ---------------------------------------------------------------- messages

MSG = {
    "upload_ok": ("ok", "Danke! Dein Vorschlag ist eingereicht und jetzt sichtbar. 🎉"),
    "vote_ok": ("ok", "Danke für deine Stimme! ✅ Dein Token ist jetzt verbraucht."),
    "err_name": ("warn", "Bitte gib deinen Namen an (max. 80 Zeichen)."),
    "err_email": ("warn", "Die E-Mail-Adresse sieht nicht gültig aus."),
    "err_title": ("warn", "Der Titel ist zu lang (max. 120 Zeichen)."),
    "err_file": ("warn", "Bitte wähle eine Bilddatei aus."),
    "err_type": ("warn", "Nur echte JPG-, PNG- oder SVG-Dateien sind erlaubt."),
    "err_svg": ("warn", "Diese SVG-Datei enthält Skripte oder aktive Inhalte und wurde aus "
                        "Sicherheitsgründen abgelehnt. Bitte als PNG exportieren."),
    "err_size": ("warn", "Die Datei ist zu groß (max. 8 MB)."),
    "err_uploads_closed": ("warn", "Die Einreichungsphase ist beendet."),
    "err_voting_closed": ("warn", "Die Abstimmung ist gerade nicht geöffnet."),
    "err_token_missing": ("warn", "Bitte gib dein Wahl-Token ein."),
    "err_token_invalid": ("warn", "Dieses Wahl-Token gibt es nicht. Bitte Schreibweise prüfen – "
                                  "Groß-/Kleinschreibung und Leerzeichen sind egal."),
    "err_token_used": ("warn", "Dieses Wahl-Token wurde bereits benutzt. Jedes Token gilt für genau eine Stimme."),
    "err_self_vote": ("warn", "Für deinen eigenen Vorschlag kannst du nicht stimmen – dein Token ist noch gültig."),
    "err_proposal": ("warn", "Diesen Vorschlag gibt es nicht (mehr)."),
    "err_rate": ("warn", "Zu viele Fehlversuche. Bitte warte 15 Minuten."),
    "err_request": ("warn", "Ungültige Anfrage."),
    "login_fail": ("warn", "Falsches Passwort."),
    "login_rate": ("warn", "Zu viele Fehlversuche. Bitte warte 15 Minuten."),
    "logged_out": ("ok", "Abgemeldet."),
    "csrf": ("warn", "Sitzung abgelaufen – bitte erneut versuchen."),
    "gen_ok": ("ok", "{n} Wahl-Tokens erzeugt."),
    "err_count": ("warn", "Anzahl muss zwischen 1 und 500 liegen."),
    "err_batch": ("warn", "Bitte einen Stamm-/Gruppennamen angeben (max. 40 Zeichen)."),
    "settings_ok": ("ok", "Einstellungen gespeichert."),
    "proposal_deleted": ("ok", "Vorschlag gelöscht."),
    "batch_deleted": ("ok", "{n} ungenutzte Tokens gelöscht."),
    "reset_ok": ("ok", "Runde zurückgesetzt: alle Stimmen und Tokens gelöscht, Vorschläge bleiben."),
    "wipe_ok": ("ok", "Alle Daten gelöscht (Vorschläge, Bilder, Stimmen, Tokens)."),
    "err_wipe": ("warn", "Zur Bestätigung bitte LÖSCHEN eintippen."),
    "pw_ok": ("ok", "Passwort geändert. Andere Sitzungen wurden abgemeldet."),
    "pw_wrong": ("warn", "Das aktuelle Passwort ist falsch."),
    "pw_short": ("warn", "Das neue Passwort muss mindestens 10 Zeichen haben."),
    "pw_mismatch": ("warn", "Die neuen Passwörter stimmen nicht überein."),
    "pdf_empty": ("warn", "Für diese Auswahl gibt es keine offenen Tokens."),
}


def flash(q):
    key = (q.get("m") or [""])[0]
    if key not in MSG:
        return ""
    kind, text = MSG[key]
    n = (q.get("n") or ["0"])[0]
    text = text.replace("{n}", str(int(n)) if n.isdigit() else "0")
    return f"<div class={kind} role=status>{esc(text)}</div>"


# ---------------------------------------------------------------- HTML

CSS = """
:root{--green:#0a7a3d;--dark:#123020;--card:#fff;--bg:#e8f3ea;--warn:#8a2b2b}
*{box-sizing:border-box}
body{margin:0;font-family:'Segoe UI',system-ui,sans-serif;background:linear-gradient(180deg,#d9eee0,var(--bg));color:#1c2b22;min-height:100vh}
.wrap{max-width:1020px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:32px;margin:.2em 0;color:var(--dark)}.lily{font-size:42px;margin:0}
h2{color:var(--dark);margin-top:.2em}h3{margin:.1em 0 .3em;color:var(--dark)}
.sub{color:#3c5646}.meta,.small{color:#5b7464;font-size:14px}.small{font-size:13px}
.grid2{display:grid;grid-template-columns:1.1fr .9fr;gap:18px;margin:18px 0}
.card{background:var(--card);border:1px solid #cfe2d4;border-radius:14px;padding:20px;box-shadow:0 2px 8px rgba(18,48,32,.06);margin-bottom:18px}
.danger{border-color:#e7b4b4}
.prop{display:flex;gap:18px}
.thumb{flex:0 0 200px;height:170px;display:flex;align-items:center;justify-content:center;background:#f4faf5;border-radius:10px;overflow:hidden;border:1px solid #e0ece2}
.thumb img{max-width:100%;max-height:100%;object-fit:contain}
.propinfo{flex:1;min-width:0}.pnum{margin:0;font-size:13px;color:#5b7464;font-weight:600}
.votes{font-size:17px;font-weight:600;color:var(--green);margin:.4em 0}
label{display:block;font-size:14px;margin:10px 0 4px;color:#3c5646}
input[type=text],input[type=email],input[type=file],input[type=password],input[type=number],input:not([type]){width:100%;padding:10px;border:1px solid #c4d8ca;border-radius:8px;font-size:15px;margin-top:4px;background:#fbfefb}
button,a.btn{display:inline-block;margin-top:12px;padding:11px 16px;border:0;border-radius:8px;background:var(--green);color:#fff;font-size:15px;font-weight:600;cursor:pointer;text-decoration:none}
button.full{width:100%}button.red{background:#a33}button.grey,a.btn.grey{background:#567}
a.btn.small{padding:7px 11px;font-size:13px;margin-top:4px}
.warn{background:#fdecec;border:1px solid #f2c4c4;color:var(--warn);padding:12px;border-radius:8px;margin:12px 0}
.ok{background:#e8f5ec;border:1px solid #bcdcc4;color:#0a7a3d;padding:12px;border-radius:8px;margin:12px 0}
.toklist{font-family:ui-monospace,Consolas,monospace;font-size:14px;line-height:1.6;background:#0f2418;color:#b9e6c8;padding:14px;border-radius:10px;white-space:pre-wrap;max-height:320px;overflow:auto}
.topnav{display:flex;gap:16px;justify-content:flex-end;flex-wrap:wrap;margin-bottom:10px;font-size:14px}
a{color:#0a7a3d}
.badges{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}
.badge{padding:5px 11px;border-radius:999px;font-size:13px;font-weight:600;background:#e2efe5;color:#2c5a3b}
.badge.off{background:#f1e4e4;color:#7a3a3a}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:7px 8px;border-bottom:1px solid #e3efe6;text-align:left;vertical-align:middle}
.tthumb img{max-height:48px;max-width:80px;object-fit:contain}
details summary{cursor:pointer;color:#2c5a3b;font-size:14px;margin-top:8px}
.inline{display:inline}.inline button{margin-top:0;padding:6px 10px;font-size:13px}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end}.row>*{flex:1;min-width:160px}
.check{display:flex;gap:8px;align-items:center;font-size:15px;color:#1c2b22}
@media(max-width:760px){.grid2{grid-template-columns:1fr}}
@media(max-width:620px){.prop{flex-direction:column}.thumb{flex:none;width:100%;height:220px}h1{font-size:26px}}
"""

FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E"
           "%3Ctext y='.9em' font-size='90'%3E%E2%9A%9C%3C/text%3E%3C/svg%3E")


def page(body, title="Logo-Wettbewerb Bezirk Karlsruhe", admin=False):
    nav = ("<a href=/>Abstimmung</a><a href=/ergebnis>Ergebnis</a>"
           "<a href=/hilfe>Hilfe &amp; Datenschutz</a><a href=/admin>Admin</a>")
    if admin:
        nav += "<a href=/admin/logout>Abmelden</a>"
    return ("<!doctype html><html lang=de><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<link rel=icon href=\"{FAVICON}\"><title>{esc(title)}</title><style>{CSS}</style></head>"
            f"<body><div class=wrap><nav class=topnav>{nav}</nav>{body}</div></body></html>")


def hero(sub):
    return f"<p class=lily>⚜️</p><h1>Logo-Wettbewerb Bezirk Karlsruhe</h1><p class=sub>{sub}</p>"


def ranking(c):
    return c.execute("SELECT p.*, (SELECT COUNT(*) FROM votes v WHERE v.proposal_id=p.id) AS n "
                     "FROM proposals p ORDER BY n DESC, p.id ASC").fetchall()


# ---------------------------------------------------------------- handler

class App(BaseHTTPRequestHandler):
    server_version = "LogoContest/3"
    sys_version = ""

    def setup(self):
        super().setup()
        self.out_cookies = []     # pro Anfrage (v2-Bug: war klassenweit geteilt!)
        self.head_only = False

    def log_message(self, fmt, *args):
        pass                     # keine Zugriffslogs -> keine IP-Adressen gespeichert

    # ---- basics
    def ip(self):
        return self.client_address[0]

    def cookies(self):
        c = SimpleCookie()
        try:
            c.load(self.headers.get("Cookie", ""))
        except Exception:
            return {}
        return {k: m.value for k, m in c.items()}

    def session(self):
        sid = self.cookies().get("adm")
        return sid if sid and valid_session(sid) else None

    def send(self, body, code=200, ctype="text/html; charset=utf-8", headers=None, cache=False):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'; "
                             "script-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("Cache-Control", "public, max-age=3600" if cache else "no-store")
        for ck in self.out_cookies:
            self.send_header("Set-Cookie", ck)
        self.out_cookies = []
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not self.head_only:
            self.wfile.write(b)

    def redirect(self, location):
        self.send("", 303, headers={"Location": location})

    def go(self, path, m, n=None, anchor=""):
        params = {"m": m}
        if n is not None:
            params["n"] = int(n)
        self.redirect(f"{path}?{urlencode(params)}{anchor}")

    def not_found(self):
        self.send(page(hero("") + "<div class=card><h2>Seite nicht gefunden</h2>"
                       "<p><a href=/>Zur Abstimmung</a></p></div>"), 404)

    # ---- dispatch
    def do_HEAD(self):
        self.head_only = True
        self.do_GET()

    def do_GET(self):
        self.out_cookies = []
        try:
            self._get()
        except Exception:
            traceback.print_exc()
            self.send(page("<div class=warn>Interner Fehler. Bitte später erneut versuchen.</div>"), 500)

    def do_POST(self):
        self.out_cookies = []
        try:
            self._post()
        except Exception:
            traceback.print_exc()
            self.send(page("<div class=warn>Interner Fehler. Bitte später erneut versuchen.</div>"), 500)

    def _get(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        if p in ("/", "/index.html"):
            return self.send(self.gallery(q))
        if p == "/ergebnis":
            return self.send(self.results())
        if p == "/hilfe":
            return self.send(self.hilfe())
        if p.startswith("/static/uploads/"):
            return self.serve_upload(p[len("/static/uploads/"):])
        if p == "/admin":
            sid = self.session()
            return self.send(self.admin(sid, q) if sid else self.login_page(q))
        if p == "/admin/logout":
            sid = self.cookies().get("adm")
            if sid:
                with conn() as c:
                    c.execute("DELETE FROM admin_sessions WHERE sid=?", (sid,))
            self.out_cookies.append("adm=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
            return self.go("/admin", "logged_out")
        if p in ("/admin/pdf", "/admin/export.csv"):
            if not self.session():
                return self.redirect("/admin")
            return self.admin_pdf(q) if p == "/admin/pdf" else self.admin_csv()
        return self.not_found()

    def _post(self):
        p = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self.send("Length Required", 411, "text/plain; charset=utf-8")
        limit = MAX_UPLOAD + 256 * 1024 if p == "/upload" else MAX_FORM
        if length > limit:
            if length <= MAX_DRAIN:
                left = length
                while left > 0:
                    chunk = self.rfile.read(min(65536, left))
                    if not chunk:
                        break
                    left -= len(chunk)
                return self.go("/", "err_size") if p == "/upload" else self.go("/", "err_request")
            self.close_connection = True
            return self.send("Payload Too Large", 413, "text/plain; charset=utf-8")
        body = self.rfile.read(length) if length else b""
        fields, files = parse_form(self.headers.get("Content-Type", ""), body)

        if p == "/upload":
            return self.do_upload(fields, files)
        if p == "/vote":
            return self.do_vote(fields)
        if p == "/admin/login":
            return self.admin_login(fields)
        if p.startswith("/admin/"):
            sid = self.session()
            if not sid:
                return self.redirect("/admin")
            if not hmac.compare_digest(fields.get("csrf", ""), csrf_for(sid)):
                return self.go("/admin", "csrf")
            action = {
                "/admin/settings": lambda: self.admin_settings(fields),
                "/admin/generate": lambda: self.admin_generate(fields),
                "/admin/delete_proposal": lambda: self.admin_delete_proposal(fields),
                "/admin/delete_batch": lambda: self.admin_delete_batch(fields),
                "/admin/reset": self.admin_reset,
                "/admin/wipe": lambda: self.admin_wipe(fields),
                "/admin/password": lambda: self.admin_password(fields, sid),
            }.get(p)
            return action() if action else self.not_found()
        return self.not_found()

    # ---- uploads (sandboxed)
    def serve_upload(self, name):
        if not UPLOAD_RE.match(name):
            return self.not_found()
        fp = os.path.join(UPLOAD_DIR, name)
        if not os.path.isfile(fp):
            return self.not_found()
        with open(fp, "rb") as f:
            data = f.read()
        self.send(data, ctype=MIME[os.path.splitext(name)[1]], cache=True, headers={
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox"})

    # ---- public pages
    def gallery(self, q):
        s = get_settings()
        with conn() as c:
            order = "n DESC, p.id ASC" if s["show_results"] else "p.id ASC"
            props = c.execute("SELECT p.*, (SELECT COUNT(*) FROM votes v WHERE v.proposal_id=p.id) AS n "
                              f"FROM proposals p ORDER BY {order}").fetchall()
        badges = ("<div class=badges>"
                  f"<span class='badge{'' if s['uploads_open'] else ' off'}'>📤 Einreichen: "
                  f"{'offen' if s['uploads_open'] else 'geschlossen'}</span>"
                  f"<span class='badge{'' if s['voting_open'] else ' off'}'>🗳️ Abstimmung: "
                  f"{'offen' if s['voting_open'] else 'geschlossen'}</span></div>")
        if s["uploads_open"]:
            upload = ("<section class=card><h2>📤 Vorschlag einreichen</h2>"
                      "<form method=post action=/upload enctype=multipart/form-data>"
                      "<label>Dein Name * <span class=small>(wird angezeigt)</span>"
                      "<input name=name required maxlength=80 placeholder='z.B. Lena M.'></label>"
                      "<label>Titel / Idee (optional)<input name=title maxlength=120 placeholder='z.B. Lilie mit Rheinwelle'></label>"
                      "<label>E-Mail (optional, nur für das Orga-Team)<input name=email type=email maxlength=120></label>"
                      "<label>Bild (JPG, PNG oder SVG, max. 8 MB)<input name=image type=file required "
                      "accept='.jpg,.jpeg,.png,.svg,image/png,image/jpeg,image/svg+xml'></label>"
                      "<button class=full>Hochladen</button></form></section>")
        else:
            upload = "<section class=card><h2>📤 Einreichen</h2><p>Die Einreichungsphase ist beendet.</p></section>"
        info = ("<section class=card><h2>ℹ️ So funktioniert's</h2><ul>"
                "<li>Jedes Mitglied kann einen Logo-Vorschlag einreichen.</li>"
                "<li>Abgestimmt wird mit deinem <b>Wahl-Token</b> – das bekommst du von deiner Leitung.</li>"
                "<li>Ein Token = <b>eine Stimme</b>. Die Stimme ist anonym.</li>"
                "<li>Für den eigenen Vorschlag kann man nicht stimmen.</li></ul>"
                f"<p class=meta>{len(props)} Vorschläge · <a href=/hilfe>Mehr Infos &amp; Datenschutz</a></p></section>")
        cards = "".join(self.card(pr, s) for pr in props) or \
            "<p class=meta>Noch keine Vorschläge – sei die erste Person! 🚀</p>"
        return page(hero("Neues Logo für unseren Bezirk – reiche deine Idee ein oder stimm für deinen Favoriten.")
                    + badges + flash(q) + f"<div class=grid2>{upload}{info}</div>"
                    + "<h2>🗳️ Vorschläge</h2>" + cards)

    def card(self, pr, s):
        pid, src = pr["id"], f"/static/uploads/{esc(pr['filename'])}"
        title = f"<h3>{esc(pr['title'])}</h3>" if pr["title"] else ""
        votes = (f"<p class=votes>⭐ {pr['n']} Stimme{'' if pr['n'] == 1 else 'n'}</p>"
                 if s["show_results"] else "")
        if s["voting_open"]:
            form = (f"<form method=post action=/vote onsubmit=\"return confirm('Deine Stimme für Vorschlag #{pid} "
                    f"abgeben? Dein Token ist danach verbraucht.')\">"
                    f"<input type=hidden name=proposal_id value={pid}>"
                    "<label>Dein Wahl-Token *<input name=token required autocomplete=off autocapitalize=none "
                    "spellcheck=false placeholder='z.B. Wolf-Zelt-7B3F'></label>"
                    "<details><summary>Ich habe selbst einen Vorschlag eingereicht</summary>"
                    "<label>Dein Name (wie beim Einreichen)<input name=voter_name maxlength=80></label>"
                    "<p class=small>So verhindern wir Stimmen für den eigenen Vorschlag. "
                    "Der Name wird nicht gespeichert.</p></details>"
                    f"<button>Für #{pid} abstimmen</button></form>")
        else:
            form = "<p class=meta>Die Abstimmung ist gerade nicht geöffnet.</p>"
        return (f"<article class='card prop' id=p{pid}><a class=thumb href=\"{src}\" target=_blank rel=noopener "
                f"title='Groß ansehen'><img src=\"{src}\" alt=\"Vorschlag #{pid}\" loading=lazy></a>"
                f"<div class=propinfo><p class=pnum>Vorschlag #{pid}</p>{title}"
                f"<p>von <b>{esc(pr['uploader_name'])}</b></p>{votes}{form}</div></article>")

    def results(self):
        s = get_settings()
        is_admin = bool(self.session())
        if not (s["show_results"] or is_admin):
            return page(hero("Ergebnis") + "<div class=card><p>Das Ergebnis wird nach Ende der Abstimmung "
                        "hier veröffentlicht.</p><p><a href=/>Zur Abstimmung</a></p></div>")
        with conn() as c:
            rows = ranking(c)
        total = sum(r["n"] for r in rows)
        trs = ""
        for r in rows:
            rank = 1 + sum(1 for x in rows if x["n"] > r["n"])
            pct = f"{100 * r['n'] / total:.0f} %" if total else "–"
            trs += (f"<tr><td><b>{rank}.</b></td><td class=tthumb><img src=\"/static/uploads/{esc(r['filename'])}\" alt=''>"
                    f"</td><td>#{r['id']} {esc(r['title'] or '')}<br><span class=small>von {esc(r['uploader_name'])}"
                    f"</span></td><td><b>{r['n']}</b></td><td>{pct}</td></tr>")
        note = "" if s["show_results"] else "<div class=warn>Nur für Admins sichtbar – noch nicht veröffentlicht.</div>"
        return page(hero("Ergebnis") + note + f"<div class=card><p class=meta>{total} abgegebene Stimmen</p>"
                    "<table><tr><th>Platz</th><th></th><th>Vorschlag</th><th>Stimmen</th><th>Anteil</th></tr>"
                    f"{trs}</table></div>", admin=is_admin)

    def hilfe(self):
        return page(hero("Kurzinfo, Datenschutz und Anleitung für Admins – zum Teilen in der Gruppe.") + """
<div class=card><h2>🎯 Worum geht's?</h2>
<p>Der Pfadfinder-Bezirk Karlsruhe sucht ein <b>neues Logo</b>. Jedes Mitglied kann einen Entwurf
(JPG, PNG oder SVG) einreichen. Danach stimmen alle Mitglieder ab – es gewinnt der Vorschlag mit den meisten Stimmen.</p></div>

<div class=card><h2>🔑 Abstimmen mit dem Wahl-Token</h2><ul>
<li>Jedes Mitglied bekommt von seiner Leitung ein <b>Wahl-Token</b> aus Pfadfinder-Wörtern, z.B. <code>Wolf-Zelt-7B3F</code>.</li>
<li>Ein Token gilt für <b>genau eine Stimme</b>. Groß-/Kleinschreibung und Leerzeichen sind beim Eintippen egal.</li>
<li>Die Stimme ist <b>anonym</b>: Gespeichert wird nur, <i>dass</i> ein Token verbraucht ist, und getrennt davon
  eine Stimme für den Vorschlag. Welches Token für welchen Vorschlag gestimmt hat, wird nirgends gespeichert –
  auch das Orga-Team kann es nicht nachvollziehen.</li>
<li>Für den eigenen Vorschlag kann man nicht stimmen (Abgleich über den Namen, der dafür nicht gespeichert wird).</li></ul></div>

<div class=card><h2>💾 Welche Daten werden verarbeitet?</h2><table>
<tr><th>Beim Einreichen</th><td>Name (öffentlich angezeigt), optional Titel, optional E-Mail (nur für das Orga-Team,
  z.B. für Rückfragen oder die Gewinner-Nachricht) und das Bild.</td></tr>
<tr><th>Beim Abstimmen</th><td>Das Token wird als „verbraucht“ markiert; die Stimme wird ohne Bezug zum Token gezählt
  (nur mit Tagesdatum).</td></tr>
<tr><th>Nicht gespeichert</th><td>Keine Konten, keine Tracking-Cookies, keine IP-Adressen, keine Zugriffsprotokolle.
  Zum Schutz vor Missbrauch werden Fehlversuche höchstens 15 Minuten im Arbeitsspeicher gezählt.</td></tr></table></div>

<div class=card><h2>🧭 Datenschutz</h2><ul>
<li><b>Datenminimierung</b> (Art. 5 DSGVO): Es wird nur erhoben, was für den Wettbewerb nötig ist.</li>
<li><b>Hosting in Deutschland</b> auf einem Server eines Mitglieds – keine US-Dienste, keine Weitergabe an Dritte.</li>
<li><b>Löschung:</b> Nach Abschluss und Bekanntgabe des Ergebnisses werden alle Bilder und Daten gelöscht.
  Einzelne Einträge entfernen wir auf Wunsch sofort – melde dich bei deiner Leitung.</li></ul></div>

<div class=card><h2>👩‍💻 Anleitung für Admins</h2><ol>
<li><b>Anmelden</b> unter <a href=/admin>/admin</a>. Beim ersten Mal das Passwort ändern (ganz unten im Admin-Bereich).</li>
<li><b>Tokens pro Stamm erzeugen:</b> Stammname + Anzahl Mitglieder eintragen (bis 500 auf einmal).
  Pro Stamm entsteht eine eigene Liste mit fortlaufender Nummer.</li>
<li><b>PDF drucken:</b> Beim Stamm „PDF 12 / 18 / 24“ wählen (Tokens pro A4-Seite). Das PDF enthält nur noch
  ungenutzte Tokens, die Nummern bleiben gleich. Ausschneiden und an die Mitglieder verteilen.</li>
<li><b>Bogen verloren?</b> „Ungenutzte löschen“ beim Stamm macht alle noch nicht benutzten Tokens dieses Stamms
  ungültig – danach neue erzeugen.</li>
<li><b>Phasen steuern:</b> Einreichen und Abstimmung lassen sich getrennt öffnen/schließen.
  Der Zwischenstand bleibt verborgen, bis „Ergebnis veröffentlichen“ eingeschaltet wird.</li>
<li><b>Moderation:</b> Unpassende Vorschläge in der Vorschlagsliste löschen (am besten vor Beginn der Abstimmung).</li>
<li><b>Ergebnis:</b> Rangliste unter <a href=/ergebnis>/ergebnis</a> (für Admins immer sichtbar) und als CSV-Export.</li>
<li><b>Zum Schluss:</b> Nach der Bekanntgabe „Alles löschen“ – damit ist das Löschversprechen erfüllt.</li></ol></div>
""")

    # ---- actions: public
    def do_upload(self, f, files):
        if not get_settings()["uploads_open"]:
            return self.go("/", "err_uploads_closed")
        name = clean(f.get("name"), 80)
        if not name:
            return self.go("/", "err_name")
        email = clean(f.get("email"), 120)
        if email is None or (email and not EMAIL_RE.match(email)):
            return self.go("/", "err_email")
        title = clean(f.get("title"), 120)
        if title is None:
            return self.go("/", "err_title")
        img = files.get("image")
        if not img or not img[1]:
            return self.go("/", "err_file")
        data = img[1]
        if len(data) > MAX_UPLOAD:
            return self.go("/", "err_size")
        ext, err = sniff_image(data)
        if err:
            return self.go("/", err)
        fn = uuid.uuid4().hex + ext
        tmp = os.path.join(UPLOAD_DIR, fn + ".tmp")
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, os.path.join(UPLOAD_DIR, fn))
        with conn() as c:
            pid = c.execute("INSERT INTO proposals(uploader_name,uploader_email,title,filename) VALUES(?,?,?,?)",
                            (name, email or None, title or None, fn)).lastrowid
        self.go("/", "upload_ok", anchor=f"#p{pid}")

    def do_vote(self, f):
        ip = self.ip()
        if VOTE_LIMIT.blocked(ip):
            return self.go("/", "err_rate")
        if not get_settings()["voting_open"]:
            return self.go("/", "err_voting_closed")
        try:
            pid = int(f.get("proposal_id", ""))
        except ValueError:
            return self.go("/", "err_proposal")
        n = norm_token(f.get("token", ""))
        if not n:
            return self.go("/", "err_token_missing", anchor=f"#p{pid}")
        vname = clean(f.get("voter_name"), 80) or ""
        with conn() as c:
            pr = c.execute("SELECT uploader_name FROM proposals WHERE id=?", (pid,)).fetchone()
            if not pr:
                return self.go("/", "err_proposal")
            if vname and same_person(vname, pr["uploader_name"]):
                return self.go("/", "err_self_vote", anchor=f"#p{pid}")
            # atomar: nur EINE Anfrage kann ein Token von 0 auf 1 setzen
            cur = c.execute("UPDATE tokens SET used=1 WHERE norm=? AND used=0", (n,))
            if cur.rowcount != 1:
                if c.execute("SELECT 1 FROM tokens WHERE norm=?", (n,)).fetchone() is None:
                    VOTE_LIMIT.fail(ip)
                    return self.go("/", "err_token_invalid", anchor=f"#p{pid}")
                return self.go("/", "err_token_used", anchor=f"#p{pid}")
            c.execute("INSERT INTO votes(proposal_id, created_at) VALUES(?, date('now'))", (pid,))
        self.go("/", "vote_ok")

    # ---- admin pages
    def login_page(self, q):
        return page(hero("Admin-Bereich") + flash(q) +
                    "<div class=card style='max-width:420px'><form method=post action=/admin/login>"
                    "<label>Passwort<input type=password name=password required autofocus "
                    "autocomplete=current-password></label><button class=full>Anmelden</button></form></div>")

    def admin(self, sid, q):
        s = get_settings()
        csrf = f"<input type=hidden name=csrf value=\"{csrf_for(sid)}\">"
        with conn() as c:
            rows = ranking(c)
            batches = c.execute("SELECT batch, COUNT(*) total, SUM(used) used FROM tokens "
                                "GROUP BY batch ORDER BY batch COLLATE NOCASE").fetchall()
            open_by_batch = {b["batch"]: [r["tok"] for r in c.execute(
                "SELECT tok FROM tokens WHERE batch=? AND used=0 ORDER BY rowid", (b["batch"],))] for b in batches}
            nvotes = c.execute("SELECT COUNT(*) FROM votes").fetchone()[0]
        ttotal = sum(b["total"] for b in batches)
        tused = sum(b["used"] or 0 for b in batches)

        warn = ""
        if os.path.exists(INITIAL_PW_PATH):
            warn += "<div class=warn>Bitte das Start-Passwort ändern (ganz unten).</div>"
        if PUBLIC_URL.startswith("http://"):
            warn += ("<div class=warn>Hinweis: Die Seite läuft noch ohne HTTPS. Passwort und Tokens werden "
                     "unverschlüsselt übertragen – für einen PoC ok, für den echten Einsatz eine Domain mit TLS einrichten.</div>")

        def chk(key, label):
            return (f"<label class=check><input type=checkbox name={key} value=1"
                    f"{' checked' if s[key] else ''}> {label}</label>")

        status = (f"<div class=card><h2>⚙️ Phasen</h2><form method=post action=/admin/settings>{csrf}"
                  + chk("uploads_open", "Einreichen geöffnet")
                  + chk("voting_open", "Abstimmung geöffnet")
                  + chk("show_results", "Ergebnis veröffentlichen (Stimmenzahlen öffentlich sichtbar)")
                  + "<button>Speichern</button></form>"
                  f"<p class=meta>{len(rows)} Vorschläge · {nvotes} Stimmen · Tokens: {tused} von {ttotal} benutzt"
                  f"{f' ({100 * tused / ttotal:.0f} %)' if ttotal else ''}</p></div>")

        known = "".join(f"<option value=\"{esc(b['batch'])}\">" for b in batches if b["batch"])
        gen = (f"<div class=card id=batches><h2>🔑 Wahl-Tokens</h2>"
               f"<form method=post action=/admin/generate>{csrf}<div class=row>"
               "<label>Stamm / Gruppe<input name=batch required maxlength=40 list=batchlist "
               f"placeholder='z.B. Stamm Daxlanden'></label><datalist id=batchlist>{known}</datalist>"
               f"<label>Anzahl<input type=number name=count value=30 min=1 max={MAX_TOKENS_PER_REQUEST}></label>"
               "<div><button>Tokens erzeugen</button></div></div></form>")
        if batches:
            gen += "<table><tr><th>Stamm</th><th>Benutzt</th><th>Offen</th><th>PDF (Tokens/Seite)</th><th></th></tr>"
            for b in batches:
                name = b["batch"] or "(ohne Stamm)"
                open_t = open_by_batch[b["batch"]]
                links = " ".join(
                    f"<a class='btn small' href=\"/admin/pdf?{esc(urlencode({'batch': b['batch'], 'n': k}))}\">{k}</a>"
                    for k in pfadi_pdf.PER_PAGE_OPTIONS) if open_t else "<span class=small>–</span>"
                delete = (f"<form method=post action=/admin/delete_batch class=inline onsubmit=\"return confirm("
                          f"'Alle {len(open_t)} ungenutzten Tokens dieses Stamms ungültig machen?')\">{csrf}"
                          f"<input type=hidden name=batch value=\"{esc(b['batch'])}\"><button class=red>Ungenutzte löschen</button></form>"
                          if open_t else "")
                gen += (f"<tr><td><b>{esc(name)}</b><details><summary>Offene Tokens anzeigen</summary>"
                        f"<div class=toklist>{esc(chr(10).join(open_t)) or '–'}</div></details></td>"
                        f"<td>{b['used'] or 0} / {b['total']}</td><td>{len(open_t)}</td><td>{links}</td><td>{delete}</td></tr>")
            gen += "</table>"
            gen += ("<p class=small>Alle Stämme in einem PDF: " + " ".join(
                f"<a class='btn small grey' href=/admin/pdf?n={k}>{k}</a>" for k in pfadi_pdf.PER_PAGE_OPTIONS) + "</p>")
        gen += ("<p class=small>Das PDF enthält nur ungenutzte Tokens; die Nummer je Stamm bleibt beim Nachdrucken gleich. "
                "Welches Token für welchen Vorschlag gestimmt hat, wird nicht gespeichert.</p></div>")

        props = ""
        for r in sorted(rows, key=lambda x: x["id"]):
            props += (f"<tr><td>#{r['id']}</td><td class=tthumb><a href=\"/static/uploads/{esc(r['filename'])}\" "
                      f"target=_blank rel=noopener><img src=\"/static/uploads/{esc(r['filename'])}\" alt=''></a></td>"
                      f"<td>{esc(r['title'] or '')}<br><span class=small>von {esc(r['uploader_name'])}</span></td>"
                      f"<td class=small>{esc(r['uploader_email'] or '–')}</td><td>{r['n']}</td>"
                      f"<td><form method=post action=/admin/delete_proposal class=inline onsubmit=\"return confirm("
                      f"'Vorschlag #{r['id']} endgültig löschen? Bereits abgegebene Stimmen dafür gehen verloren.')\">"
                      f"{csrf}<input type=hidden name=id value={r['id']}><button class=red>Löschen</button></form></td></tr>")
        props_card = ("<div class=card id=proposals><h2>🖼️ Vorschläge &amp; Ergebnis</h2>"
                      + ("<table><tr><th>#</th><th></th><th>Titel / Name</th><th>E-Mail</th><th>Stimmen</th><th></th></tr>"
                         + props + "</table>" if props else "<p class=meta>Noch keine Vorschläge.</p>")
                      + "<p><a class='btn small' href=/ergebnis>Rangliste ansehen</a> "
                        "<a class='btn small grey' href=/admin/export.csv>CSV-Export</a></p></div>")

        danger = (f"<div class='card danger'><h2>⚠️ Runde beenden / Daten löschen</h2>"
                  f"<form method=post action=/admin/reset onsubmit=\"return confirm('Alle Stimmen und alle Tokens löschen? "
                  f"Vorschläge bleiben erhalten.')\">{csrf}<p class=small>Neue Wahlrunde: löscht alle Stimmen und Tokens, "
                  f"Vorschläge bleiben.</p><button class=red>Runde zurücksetzen</button></form><hr>"
                  f"<form method=post action=/admin/wipe>{csrf}<p class=small>Nach Bekanntgabe des Ergebnisses (DSGVO): "
                  f"löscht <b>alles</b> – Vorschläge, Bilder, Stimmen, Tokens.</p>"
                  f"<label>Zur Bestätigung LÖSCHEN eintippen<input name=confirm autocomplete=off></label>"
                  f"<button class=red>Alles löschen</button></form></div>")

        pw = (f"<div class=card><h2>🔒 Passwort ändern</h2><form method=post action=/admin/password>{csrf}<div class=row>"
              "<label>Aktuelles Passwort<input type=password name=current required autocomplete=current-password></label>"
              "<label>Neues Passwort (min. 10 Zeichen)<input type=password name=new required minlength=10 autocomplete=new-password></label>"
              "<label>Wiederholen<input type=password name=new2 required minlength=10 autocomplete=new-password></label>"
              "</div><button>Ändern</button></form></div>")

        return page("<h1>🛠️ Admin-Bereich</h1>" + flash(q) + warn + status + gen + props_card + danger + pw,
                    title="Admin – Logo-Wettbewerb", admin=True)

    # ---- actions: admin
    def admin_login(self, f):
        ip = self.ip()
        if LOGIN_LIMIT.blocked(ip):
            return self.go("/admin", "login_rate")
        pw = f.get("password", "")
        ok, upgrade = check_pw(pw)
        if not ok:
            LOGIN_LIMIT.fail(ip)
            return self.go("/admin", "login_fail")
        if upgrade:
            set_pw(pw)        # altes SHA-256 -> PBKDF2
        sid = new_session()
        self.out_cookies.append(f"adm={sid}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL}")
        self.redirect("/admin")

    def admin_settings(self, f):
        with conn() as c:
            for k in DEFAULT_SETTINGS:
                c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, "1" if f.get(k) == "1" else "0"))
        self.go("/admin", "settings_ok")

    def admin_generate(self, f):
        batch = clean(f.get("batch"), 40)
        if not batch:
            return self.go("/admin", "err_batch", anchor="#batches")
        try:
            count = int(f.get("count", ""))
        except ValueError:
            count = 0
        if not 1 <= count <= MAX_TOKENS_PER_REQUEST:
            return self.go("/admin", "err_count", anchor="#batches")
        made = 0
        with conn() as c:
            while made < count:
                t = make_token()
                try:
                    c.execute("INSERT INTO tokens(tok,norm,batch) VALUES(?,?,?)", (t, norm_token(t), batch))
                    made += 1
                except sqlite3.IntegrityError:
                    continue      # extrem selten: Kollision -> neues Token
        self.go("/admin", "gen_ok", made, anchor="#batches")

    def admin_delete_proposal(self, f):
        try:
            pid = int(f.get("id", ""))
        except ValueError:
            return self.go("/admin", "err_request")
        with conn() as c:
            r = c.execute("SELECT filename FROM proposals WHERE id=?", (pid,)).fetchone()
            c.execute("DELETE FROM votes WHERE proposal_id=?", (pid,))
            c.execute("DELETE FROM proposals WHERE id=?", (pid,))
        if r and UPLOAD_RE.match(r["filename"]):
            try:
                os.remove(os.path.join(UPLOAD_DIR, r["filename"]))
            except FileNotFoundError:
                pass
        self.go("/admin", "proposal_deleted", anchor="#proposals")

    def admin_delete_batch(self, f):
        with conn() as c:
            n = c.execute("DELETE FROM tokens WHERE batch=? AND used=0", (f.get("batch", ""),)).rowcount
        self.go("/admin", "batch_deleted", n, anchor="#batches")

    def admin_reset(self):
        with conn() as c:
            c.execute("DELETE FROM votes")
            c.execute("DELETE FROM tokens")
        self.go("/admin", "reset_ok")

    def admin_wipe(self, f):
        if f.get("confirm", "").strip().upper() not in ("LÖSCHEN", "LOESCHEN"):
            return self.go("/admin", "err_wipe")
        with conn() as c:
            c.execute("DELETE FROM votes")
            c.execute("DELETE FROM tokens")
            c.execute("DELETE FROM proposals")
        for name in os.listdir(UPLOAD_DIR):
            if UPLOAD_RE.match(name):
                os.remove(os.path.join(UPLOAD_DIR, name))
        self.go("/admin", "wipe_ok")

    def admin_password(self, f, sid):
        ok, _ = check_pw(f.get("current", ""))
        if not ok:
            return self.go("/admin", "pw_wrong")
        new = f.get("new", "")
        if len(new) < 10:
            return self.go("/admin", "pw_short")
        if new != f.get("new2", ""):
            return self.go("/admin", "pw_mismatch")
        set_pw(new)
        with conn() as c:
            c.execute("DELETE FROM admin_sessions WHERE sid!=?", (sid,))
        if os.path.exists(INITIAL_PW_PATH):
            os.remove(INITIAL_PW_PATH)
        self.go("/admin", "pw_ok")

    def admin_pdf(self, q):
        batch = q["batch"][0] if "batch" in q else None
        try:
            per_page = int((q.get("n") or ["12"])[0])
        except ValueError:
            per_page = 12
        if per_page not in pfadi_pdf.PER_PAGE_OPTIONS:
            per_page = 12
        sql = ("SELECT tok, batch, nr FROM (SELECT tok, batch, used, ROW_NUMBER() OVER "
               "(PARTITION BY batch ORDER BY rowid) AS nr FROM tokens) WHERE used=0")
        args = ()
        if batch is not None:
            sql += " AND batch=?"
            args = (batch,)
        with conn() as c:
            rows = c.execute(sql + " ORDER BY batch COLLATE NOCASE, nr", args).fetchall()
        if not rows:
            return self.go("/admin", "pdf_empty", anchor="#batches")
        label = (batch or "ohne Stamm") if batch is not None else "alle Stämme"
        data = pfadi_pdf.tokens_pdf([(r["nr"], r["tok"], r["batch"]) for r in rows], per_page=per_page,
                                    subtitle=f"Wahl-Tokens · {label}", url=PUBLIC_URL)
        fname = f"wahl-tokens-{slug(label)}-{per_page}.pdf"
        self.send(data, ctype="application/pdf",
                  headers={"Content-Disposition": f'inline; filename="{fname}"'})

    def admin_csv(self):
        with conn() as c:
            rows = ranking(c)
        out = io.StringIO()
        w = csv.writer(out, delimiter=";")
        w.writerow(["Platz", "Vorschlag", "Titel", "Name", "E-Mail", "Stimmen", "Eingereicht"])
        for r in rows:
            rank = 1 + sum(1 for x in rows if x["n"] > r["n"])
            w.writerow([rank, r["id"], r["title"] or "", r["uploader_name"], r["uploader_email"] or "",
                        r["n"], r["created_at"]])
        self.send(("\ufeff" + out.getvalue()).encode("utf-8"), ctype="text/csv; charset=utf-8",
                  headers={"Content-Disposition": 'attachment; filename="logo-wettbewerb-ergebnis.csv"'})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64


def make_tls_server_bind():
    import ssl
    cert = os.environ.get("TLS_CERT") or os.path.join(DATA_DIR, "tls", "cert.pem")
    key = os.environ.get("TLS_KEY") or os.path.join(DATA_DIR, "tls", "key.pem")
    if not (os.path.exists(cert) and os.path.exists(key)):
        return None, None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=cert, keyfile=key)
    return ctx, cert


class TLSServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, ctx, *a, **kw):
        self._ctx = ctx
        super().__init__(*a, **kw)

    def get_request(self):
        sock, addr = self.socket.accept()
        try:
            return self._ctx.wrap_socket(sock, server_side=True), addr
        except (ssl.SSLError, OSError):
            try:
                sock.close()
            except OSError:
                pass
            raise


def main():
    init_db()
    ensure_admin_hash()
    tls_ctx, cert = make_tls_server_bind()
    https_port = int(os.environ.get("HTTPS_PORT", "8443"))
    https_srv = None
    if tls_ctx:
        https_srv = TLSServer(tls_ctx, ("0.0.0.0", https_port), App)
        print(f"HTTPS läuft auf :{https_port} (Zertifikat: {cert})", flush=True)
    srv = Server(("0.0.0.0", PORT), App)
    print(f"Logo-Wettbewerb läuft auf :{PORT} (Daten: {DATA_DIR})", flush=True)
    try:
        if https_srv:
            import threading
            threading.Thread(target=https_srv.serve_forever, daemon=True).start()
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
