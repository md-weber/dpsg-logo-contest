#!/usr/bin/env python3
"""End-to-End-Tests für den Logo-Wettbewerb (v3) — gegen eine echte, frische Instanz.

Start in einen temporären Datenordner (kein Kontakt zur Produktion). Nutzt nur die
stdlib + pypdf, falls im Scratch-Validierungspfad vorhanden.
"""
import io
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
import http.cookiejar
import random
import struct
import zlib
import re
import urllib.parse

# Ins Scratch laufen lassen mit frischen Pfaden VOR app-Import
DATA = tempfile.mkdtemp(prefix="logo-test-")
os.environ["LOGO_DATA_DIR"] = DATA
os.environ["PUBLIC_URL"] = "http://127.0.0.1"

sys.path.insert(0, "/home/hermes/logo-contest")
import app as A
import sqlite3

_PORTS = iter([random.randint(25000, 32000) for _ in range(8)])
CURRENT_PORT = [0]
A.init_db()
A.set_pw("Test-Passwort-123")
os.chmod(A.DATA_DIR, 0o700)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def db_exec(q, p=()):
    c = sqlite3.connect(A.DB_PATH, timeout=15)
    try:
        c.execute(q, p)
        c.commit()
        return c
    finally:
        c.close()


def db_row(q, p=()):
    c = sqlite3.connect(A.DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    try:
        return c.execute(q, p).fetchone()
    finally:
        c.close()


class Client:
    """Einfacher HTTP-Client mit Cookie-Jar, der Browser-Requests (urlencoded)
    nachbildet. Redirects werden NICHT gefolgt, damit man Status + Location prüfen kann."""

    def __init__(self):
        self.port = CURRENT_PORT[0]
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            NoRedirect, urllib.request.HTTPCookieProcessor(self.jar))

    def req(self, method, path, data=None, headers=None, ctype=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        body = data.encode("utf-8") if isinstance(data, str) else data
        req = urllib.request.Request(url, data=body, method=method)
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)
        if ctype:
            req.add_header("Content-Type", ctype)
        try:
            r = self.opener.open(req, timeout=20)
            return r.status, r.read(), dict(r.headers), r.headers.get("Location") or r.geturl()
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers), e.headers.get("Location") or e.geturl()

    def get(self, path):
        return self.req("GET", path)

    def post_form(self, path, fields):
        body = urllib.parse.urlencode(fields).encode()
        return self.req("POST", path, body, ctype="application/x-www-form-urlencoded")


# vereinfachtes multipart für Uploads (Content genau ein Bild + Name, keine Boundary im Bild)
def _mp(fields):
    boundary = "----WebKit" + "%016x" % random.getrandbits(64)
    buf = io.BytesIO()
    for k, v in fields.items():
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{k}"'.encode())
        if isinstance(v, bytes):
            buf.write(b"; filename=\"bild.png\"\r\nContent-Type: image/png\r\n\r\n")
            buf.write(v)
        else:
            buf.write(b"\r\n\r\n")
            buf.write(str(v).encode())
        buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())
    return boundary, buf.getvalue()


def tiny_png(w=4, h=4):
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # truecolor
    raw = b"".join(b"\x00" + bytes([10, 200, 90]) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def multipart_upload(client, fields):
    b, body = _mp(fields)
    return client.req("POST", "/upload", body, ctype=f"multipart/form-data; boundary={b}")


class LiveServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        CURRENT_PORT[0] = next(_PORTS)
        cls.srv = A.Server(("127.0.0.1", CURRENT_PORT[0]), A.App)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.anon = Client()
        cls.admin = Client()
        # Admin-Login
        st, _, _, _ = cls.admin.req("POST", "/admin/login",
                                    urllib.parse.urlencode({"password": "Test-Passwort-123"}).encode(),
                                    ctype="application/x-www-form-urlencoded")
        assert st == 303, st

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def csrf(self):
        st, body, _, _ = self.admin.get("/admin")
        self.assertEqual(st, 200)
        m = re.search(rb'name=csrf value="([0-9a-f]+)"', body)
        self.assertTrue(m, "csrf not found in admin page")
        return m.group(1).decode()

    # ---- Sicherheit
    def test_no_cookie_leak_to_anon(self):
        st, body, h, _ = self.anon.get("/")
        self.assertEqual(st, 200)
        self.assertNotIn("Set-Cookie", h, "Anon visitor must not receive any cookie")

    def test_anon_cannot_reach_admin_tokens(self):
        st, body, _, _ = self.anon.get("/admin")
        self.assertEqual(st, 200)
        self.assertNotIn(b"Tokens erzeugen", body)   # nur Login-Form
        self.assertNotIn(b"Stamm", body)

    def test_path_traversal_blocked(self):
        for path in ("/static/../contest.db", "/static/%2e%2e/contest.db",
                     "/static/../admin.hash", "/static/../../etc/passwd",
                     "/static/uploads/../contest.db"):
            st, _, _, _ = self.anon.get(path)
            self.assertIn(st, (400, 404), f"{path} -> {st}")

    def test_bad_admin_cookie_rejected(self):
        c = Client()
        # einen gefälschten adm-Cookie senden (raw), der in keiner Server-Sitzung existiert
        st, body, _, _ = c.req("GET", "/admin", headers={"Cookie": "adm=forgedvalue123"})
        self.assertEqual(st, 200)
        self.assertNotIn(b"Tokens erzeugen", body)

    # ---- Phasen + admin
    def test_admin_login_wrong_and_rate(self):
        c = Client()
        for _ in range(5):
            st, body, _, _ = c.req("POST", "/admin/login",
                                   urllib.parse.urlencode({"password": "NopeX"}).encode(),
                                   ctype="application/x-www-form-urlencoded")
        st, body, _, loc = c.req("POST", "/admin/login",
                                 urllib.parse.urlencode({"password": "Test-Passwort-123"}).encode(),
                                 ctype="application/x-www-form-urlencoded")
        self.assertEqual(st, 303)
        self.assertIn("login_rate", loc or body.decode())

    def test_csrf_blocks_mutation(self):
        st, body, _, loc = self.admin.post_form("/admin/settings", {"uploads_open": "0"})
        self.assertEqual(st, 303)
        self.assertIn("csrf", loc or body.decode())

    def test_generate_and_closed_phases(self):
        csrf = self.csrf()
        st, body, _, loc = self.admin.post_form("/admin/generate",
                                                {"csrf": csrf, "batch": "Stamm Daxlanden", "count": "20"})
        self.assertIn("gen_ok", loc)
        # Vote geschlossen
        st, body, _, _ = self.admin.post_form("/admin/settings",
                                              {"csrf": csrf, "uploads_open": "1", "voting_open": "0", "show_results": "0"})
        bid, _ = self.upload_one()
        db_exec("INSERT INTO tokens(tok,norm,used,batch) VALUES('Test-Vote-C0DE','testvotec0de',0,'Stamm Daxlanden')")
        st, body, _, loc = self.anon.post_form("/vote", {"proposal_id": str(bid), "token": "Test-Vote-C0DE"})
        self.assertIn("err_voting_closed", loc)

    def upload_one(self):
        st, body, _, loc = multipart_upload(self.anon,
                                            {"name": "Lena", "title": "Mein Logo", "image": tiny_png()})
        self.assertEqual(st, 303)
        self.assertIn("upload_ok", loc)
        m = re.search(r'#p(\d+)', loc)
        self.assertTrue(m, loc)
        return int(m.group(1)), b"" 

    def test_bad_email_rejected(self):
        st, body, _, loc = multipart_upload(self.anon, {"name": "X", "email": "keine-mail",
                                                        "image": tiny_png()})
        self.assertIn("err_email", loc)

    def test_bad_image_rejected(self):
        # Echter Nicht-Bild-Inhalt wird abgelehnt (Magic-Bytes-Check)
        st, body, _, loc = multipart_upload(self.anon, {"name": "X", "image": b"not-an-image"})
        self.assertEqual(st, 303)
        self.assertIn("err_type", loc)

    def test_svg_script_rejected(self):
        evil = (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script>'
                b'<rect width="1" height="1"/></svg>')
        st, body, _, loc = multipart_upload(self.anon, {"name": "T", "image": evil})
        self.assertEqual(st, 303)
        self.assertIn("err_svg", loc)

    def test_svg_safe_accepted(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10" fill="#0a7a3d"/></svg>'
        st, body, _, loc = multipart_upload(self.anon, {"name": "SVG-Fan", "image": svg})
        self.assertEqual(st, 303)
        # fetch it and ensure sandboxed
        st, body, _, _ = self.anon.get("/")
        m = re.search(rb'href="(/static/uploads/[0-9a-f]{32}\.svg)"', body)
        self.assertTrue(m)
        st2, body2, h2, _ = self.anon.get(m.group(1).decode())
        self.assertEqual(st2, 200)
        self.assertIn("sandbox", h2.get("Content-Security-Policy", ""))


class Voting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        CURRENT_PORT[0] = next(_PORTS)
        db_exec("INSERT OR REPLACE INTO settings(key,value) VALUES('uploads_open','1'),"
                 "('voting_open','1'),('show_results','0')")
        cls.srv = A.Server(("127.0.0.1", CURRENT_PORT[0]), A.App)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.anon = Client()
        st, body, _, loc = multipart_upload(cls.anon, {"name": "Lena", "image": tiny_png()})
        m = re.search(r'#p(\d+)', loc)
        cls.pid = int(m.group(1))
        db_exec("INSERT INTO tokens(tok,norm,used,batch) VALUES"
                 "('Wolf-Zelt-7B3F','wolfzelt7b3f',0,'Stamm A'),"
                 "('Adler-Eule-9C22','adlereule9c22',0,'Stamm A')")

    def vote(self, pid, tok, name=None):
        f = {"proposal_id": str(pid), "token": tok}
        if name:
            f["voter_name"] = name
        st, body, _, loc = self.anon.post_form("/vote", f)
        return st, loc or body.decode()

    def test_vote_and_used(self):
        st, body = self.vote(self.pid, "Wolf-Zelt-7B3F")
        self.assertIn("vote_ok", body)
        st, body = self.vote(self.pid, "Wolf-Zelt-7B3F")
        self.assertIn("err_token_used", body)

    def test_vote_other_as_lowercase(self):
        st, body = self.vote(self.pid, "adlereule-9c22")
        self.assertIn("vote_ok", body)

    def test_self_vote_blocked_token_kept(self):
        # Lena darf nicht für eigenes Bild stimmen (Name übereinstimmung)
        st, body = self.vote(self.pid, "Adler-Eule-9C22", name="lena")
        self.assertIn("err_self_vote", body)
        row = db_row("SELECT used FROM tokens WHERE tok='Adler-Eule-9C22'")
        self.assertEqual(row["used"], 0)  # Token bleibt gültig


class Concurrent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        CURRENT_PORT[0] = next(_PORTS)
        db_exec("INSERT OR REPLACE INTO settings(key,value) VALUES('uploads_open','1'),"
                 "('voting_open','1'),('show_results','0')")
        cls.srv = A.Server(("127.0.0.1", CURRENT_PORT[0]), A.App)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pid = 1
        db_exec("INSERT INTO tokens(tok,norm,used,batch) VALUES('Rabe-Pfad-7F00','rabepfad7f00',0,'Stamm X')")
        db_exec("INSERT OR IGNORE INTO proposals(id,uploader_name,title,filename) VALUES"
                 "(1,'P','','f' || ('0' || '0' || '0' || '0'))")
        db_exec("UPDATE proposals SET filename='00000000000000000000000000000000.png' WHERE id=1")

    def test_single_use_race(self):
        errors = []

        def v():
            c = Client()
            st, body, _, loc = c.post_form("/vote", {"proposal_id": str(self.pid), "token": "Rabe-Pfad-7F00"})
            errors.append(loc or body.decode())

        threads = [threading.Thread(target=v) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        used = db_row("SELECT used FROM tokens WHERE tok='Rabe-Pfad-7F00'")["used"]
        self.assertEqual(used, 1)
        ok = sum(1 for e in errors if "vote_ok" in e)
        self.assertEqual(ok, 1)


class Pdf(unittest.TestCase):
    def test_pdf_layout(self):
        import pfadi_pdf
        toks = [(i, f"Wolf-Zelt-{i:04X}", "Stamm Daxlanden") for i in range(1, 50)]
        for pp in pfadi_pdf.PER_PAGE_OPTIONS:
            boxes = []
            data = pfadi_pdf.tokens_pdf(toks, pp, url="http://127.0.0.1:8391", debug=boxes)
            self.assertTrue(data.startswith(b"%PDF-1.4"))
            self.assertTrue(data.rstrip().endswith(b"%%EOF"))
            # geometry: keine Textbreite überläuft Container
            for b in boxes:
                self.assertGreaterEqual(b["x0"], b["c0"] - 0.01, f"{pp}: left overflow {b}")
                self.assertLessEqual(b["x1"], b["c1"] + 0.01, f"{pp}: right overflow {b}")
            # pypdf validation, falls verfügbar
            try:
                sys.path.insert(0, "/home/hermes/.hermes/cache/scratch/pyval")
                import pypdf
                r = pypdf.PdfReader(io.BytesIO(data), strict=True)
                self.assertEqual(len(r.pages), (len(toks) + pp - 1) // pp)
                self.assertTrue(all(p.get("/Parent") is not None for p in r.pages))
            except ImportError:
                pass

    def test_pdf_full_roundtrip_binary(self):
        import pfadi_pdf, zlib
        data = pfadi_pdf.tokens_pdf([(1, "Fährte-Lied-C543", "Stamm A")], 12, url="http://x", debug=[])
        # Token liegt im komprimierten Content-Stream; dekomprimieren und prüfen
        m = re.search(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S)
        self.assertTrue(m)
        content = zlib.decompress(m.group(1))
        self.assertIn(b"F\xe4hrte-Lied-C543", content)


class Tls(unittest.TestCase):
    """HTTPS-Listener: bindet nur, wenn ein Zertifikat vorhanden ist."""

    @classmethod
    def setUpClass(cls):
        import ssl, subprocess, tempfile, os
        cls.certdir = tempfile.mkdtemp(prefix="logo-tls-")
        cls.cert = os.path.join(cls.certdir, "cert.pem")
        cls.key = os.path.join(cls.certdir, "key.pem")
        # selbstsigniertes Zert für localhost erzeugen (openssl, nur für den Test)
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", cls.key, "-out", cls.cert, "-days", "1",
            "-subj", "/CN=localhost",
        ], check=True, capture_output=True)
        # in einer Neben-Instanz mit TLS_TEST_PORT laufen lassen
        import threading
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cls.cert, cls.key)
        cls.port = next(_PORTS)
        cls.srv = A.TLSServer(ctx, ("127.0.0.1", cls.port), A.App)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_tls_serves(self):
        import ssl, urllib.request
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE   # Testzert ist selbstsigniert
        try:
            r = urllib.request.urlopen(f"https://127.0.0.1:{self.port}/", context=ctx, timeout=10)
            self.assertEqual(r.status, 200)
            body = r.read()
            self.assertIn("Logo-Wettbewerb".encode(), body)
        except urllib.error.URLError as e:
            self.fail(f"TLS request failed: {e}")

    def test_bad_handshake_does_not_kill_listener(self):
        """Regression: ein fehlgeschlagener TLS-Handshake (Scanner/Bots) darf den
        Annahme-Thread nicht beenden - sonst nimmt der Server keine Verbindungen mehr an."""
        import socket, ssl, urllib.request
        # Müll statt TLS-Handshake senden
        for payload in (b"GARBAGE\r\n\r\n", b"\x00\x01\x02\x03", b""):
            try:
                s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
                if payload:
                    s.sendall(payload)
                s.close()
            except OSError:
                pass
        # Server muss danach weiterhin antworten
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            r = urllib.request.urlopen(f"https://127.0.0.1:{self.port}/", context=ctx, timeout=10)
            self.assertEqual(r.status, 200, "Listener hat nach Bad-Handshake nicht mehr geantwortet")
        except Exception as e:
            self.fail(f"Listener wurde durch fehlerhaften Handshake getötet: {e}")


if __name__ == "__main__":
    unittest.main(verbosity=2)