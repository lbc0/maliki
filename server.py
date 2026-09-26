#!/usr/bin/env python3
"""
Local server for the Maliki fiqh library editor.  Standard library only (Python 3.8+), nothing to install.

    python3 server.py                          ->  http://127.0.0.1:8765   (data in ./data next to this file)
    python3 server.py --port 9000 --no-browser
    python3 server.py --data "D:\\my-library-data"

Your library is stored as plain files you can back up, copy or put under git:

    data/library.json          the whole library (readable JSON)
    data/pdfs/<sha256>.pdf     PDF files, named by their content hash (never overwritten)
    data/backups/              automatic timestamped copies of library.json

It only listens on this computer (127.0.0.1). Writes need a custom header and a matching Origin, so another
website you happen to open cannot change your data. There is NO password: do not expose it with --host on a network you do not trust.
"""
import argparse, hashlib, json, os, re, shutil, sys, tempfile, threading, time, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote, parse_qs

VERSION = "1.0.0"
APP_DIR = Path(__file__).resolve().parent
MAX_LIB_BYTES = 256 * 1024 * 1024
BACKUP_EVERY_S = 600          # at most one automatic backup per 10 minutes of editing
BACKUP_KEEP = 200
STATIC_OK = re.compile(r"^(index\.html|vendor/[A-Za-z0-9_./-]+|fonts/[A-Za-z0-9_.-]+)$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
BACKUP_RE = re.compile(r"^library-\d{8}-\d{6}(-\d+)?\.json$")
MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".json": "application/json; charset=utf-8", ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf",
        ".txt": "text/plain; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png", ".pdf": "application/pdf"}


class Store:
    """library.json + pdfs/ + backups/.  All library writes go through one lock and are atomic."""
    def __init__(self, data_dir):
        self.dir = Path(data_dir).resolve()
        self.lib, self.pdfs, self.backups = self.dir / "library.json", self.dir / "pdfs", self.dir / "backups"
        for d in (self.dir, self.pdfs, self.pdfs / ".tmp", self.backups):
            d.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.last_backup = 0.0

    @staticmethod
    def rev_of(raw):
        return "empty" if raw is None else hashlib.sha256(raw).hexdigest()[:16]

    def _raw(self):
        try:
            return self.lib.read_bytes()
        except FileNotFoundError:
            return None

    def read(self):
        with self.lock:
            raw = self._raw()
            return self.rev_of(raw), (json.loads(raw.decode("utf-8")) if raw is not None else None)

    def write(self, expected_rev, doc, force_backup=False):
        """Returns (ok, rev). ok is False when the file changed since the client last read it."""
        with self.lock:
            raw = self._raw()
            cur = self.rev_of(raw)
            if expected_rev != cur:
                return False, cur
            now = time.time()
            if raw is not None and (force_backup or now - self.last_backup > BACKUP_EVERY_S):
                self._backup(raw)
                self.last_backup = now
            data = json.dumps(doc, ensure_ascii=False, indent=1).encode("utf-8")
            fd, tmp = tempfile.mkstemp(dir=str(self.dir), suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data); f.flush(); os.fsync(f.fileno())
                os.replace(tmp, self.lib)
            except Exception:
                try: os.remove(tmp)
                except OSError: pass
                raise
            return True, self.rev_of(data)

    def _backup(self, raw):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name, n = "library-%s.json" % stamp, 0
        while (self.backups / name).exists():
            n += 1; name = "library-%s-%d.json" % (stamp, n)
        (self.backups / name).write_bytes(raw)
        old = sorted(self.backups.glob("library-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in old[BACKUP_KEEP:]:
            try: p.unlink()
            except OSError: pass

    def list_backups(self):
        out = []
        for p in sorted(self.backups.glob("library-*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            st = p.stat(); out.append({"name": p.name, "bytes": st.st_size, "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))})
        return out


class Handler(BaseHTTPRequestHandler):
    server_version = "MFL/" + VERSION
    protocol_version = "HTTP/1.1"
    _head = False

    def log_message(self, fmt, *args):
        if self.server.verbose:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ---------- small helpers ----------
    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None, close=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        if close:
            self.send_header("Connection", "close"); self.close_connection = True
        self.end_headers()
        if not self._head:
            self.wfile.write(body)

    def _json(self, code, obj, close=False):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), extra={"Cache-Control": "no-store"}, close=close)

    def _err(self, code, msg):
        self._json(code, {"error": msg}, close=True)

    def _host_ok(self):
        if not self.server.check_host:
            return True
        return self.headers.get("Host", "").lower() in self.server.allowed_hosts

    def _write_ok(self):
        """State-changing requests need our custom header (which forces a CORS preflight that we never grant)
           and, if the browser sent an Origin, it must be this very server."""
        if self.headers.get("X-MFL") != "1":
            return False
        origin = self.headers.get("Origin")
        if origin is None or self.server.allowed_origins is None:      # None: bound to a network on request; the header still blocks cross-site forms
            return True
        return origin.lower() in self.server.allowed_origins

    # ---------- GET / HEAD ----------
    def do_HEAD(self):
        self._head = True
        try:
            self._get()
        finally:
            self._head = False              # a keep-alive connection reuses this handler object

    def do_GET(self):
        self._head = False
        self._get()

    def _get(self):
        if not self._host_ok():
            return self._err(403, "host not allowed")
        u = urlparse(self.path); path = unquote(u.path)
        if path in ("/", "/index.html"):
            return self._static("index.html", cache="no-store")
        if path == "/api/ping":
            return self._json(200, {"ok": True, "app": "mfl", "version": VERSION, "dataDir": str(self.server.store.dir)})
        if path == "/api/library":
            rev, doc = self.server.store.read()
            return self._json(200, {"rev": rev, "doc": doc})
        if path == "/api/backups":
            return self._json(200, self.server.store.list_backups())
        if path.startswith("/api/backups/"):
            name = path[len("/api/backups/"):]
            if not BACKUP_RE.match(name):
                return self._err(404, "not found")
            return self._file(self.server.store.backups / name, "application/json; charset=utf-8", "no-store")
        if path.startswith("/pdfs/"):
            name = path[len("/pdfs/"):]
            if not re.match(r"^[0-9a-f]{64}\.pdf$", name):
                return self._err(404, "not found")
            return self._file(self.server.store.pdfs / name, "application/pdf", "public, max-age=31536000, immutable", inline=True)
        rel = path.lstrip("/")
        if STATIC_OK.match(rel) and ".." not in rel:
            return self._static(rel, cache="public, max-age=3600")
        return self._err(404, "not found")

    def _static(self, rel, cache):
        p = (APP_DIR / rel).resolve()
        try:
            p.relative_to(APP_DIR)
        except ValueError:
            return self._err(404, "not found")
        return self._file(p, MIME.get(p.suffix.lower(), "application/octet-stream"), cache)

    def _file(self, p, ctype, cache, inline=False):
        try:
            size = p.stat().st_size
            if not p.is_file():
                raise FileNotFoundError
        except (FileNotFoundError, OSError):
            return self._err(404, "not found")
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng and size > 0:
            m = re.match(r"^bytes=(\d*)-(\d*)$", rng.strip())
            if m and (m.group(1) or m.group(2)):
                a, b = m.groups()
                if a == "":
                    start, end = max(0, size - int(b)), size - 1
                else:
                    start, end = int(a), (min(int(b), size - 1) if b else size - 1)
                if start > end or start >= size:
                    return self._send(416, b"", extra={"Content-Range": "bytes */%d" % size})
                code = 206
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(end - start + 1 if size else 0))
        self.send_header("Accept-Ranges", "bytes")
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        if inline:
            self.send_header("Content-Disposition", "inline")
        self.end_headers()
        if self._head or size == 0:
            return
        try:
            with open(p, "rb") as f:
                f.seek(start); left = end - start + 1
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk); left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True          # pdf.js cancels range requests all the time; that is fine

    # ---------- PUT: the library ----------
    def do_PUT(self):
        self._head = False
        if not self._host_ok():
            return self._err(403, "host not allowed")
        u = urlparse(self.path)
        if u.path != "/api/library":
            return self._err(404, "not found")
        if not self._write_ok():
            return self._err(403, "write not allowed")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_LIB_BYTES:
            return self._err(400, "bad length")
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return self._err(400, "not json")
        doc = body.get("doc") if isinstance(body, dict) else None
        if not (isinstance(doc, dict) and isinstance(doc.get("masail"), list) and isinstance(doc.get("nodes"), list)
                and isinstance(doc.get("editions"), list) and isinstance(doc.get("vocab"), dict)):
            return self._err(400, "not a library document")
        force = parse_qs(u.query).get("force_backup") == ["1"]
        ok, rev = self.server.store.write(body.get("rev"), doc, force)
        if not ok:
            return self._json(409, {"error": "conflict", "rev": rev})
        return self._json(200, {"rev": rev})

    # ---------- POST: upload a PDF ----------
    def do_POST(self):
        self._head = False
        if not self._host_ok():
            return self._err(403, "host not allowed")
        if urlparse(self.path).path != "/api/upload":
            return self._err(404, "not found")
        if not self._write_ok():
            return self._err(403, "write not allowed")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return self._err(400, "الملف فارغ")
        if length > self.server.max_upload:
            return self._err(413, "الملف أكبر من الحد المسموح (%d ميغابايت)" % (self.server.max_upload // (1024 * 1024)))
        name = unquote(self.headers.get("X-Filename", "file.pdf"))[:200]
        store = self.server.store
        fd, tmp = tempfile.mkstemp(dir=str(store.pdfs / ".tmp"), suffix=".part")
        h, head, left = hashlib.sha256(), b"", length
        try:
            with os.fdopen(fd, "wb") as f:
                while left > 0:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        break
                    if not head:
                        head = chunk[:8]
                    h.update(chunk); f.write(chunk); left -= len(chunk)
            if left > 0:
                os.remove(tmp); return self._err(400, "انقطع الرفع قبل اكتماله")
            if not head.startswith(b"%PDF-"):
                os.remove(tmp); return self._err(415, "الملف ليس بصيغة PDF")
            sha = h.hexdigest(); final = store.pdfs / (sha + ".pdf")
            if final.exists():
                os.remove(tmp)
            else:
                os.replace(tmp, final)
            return self._json(200, {"sha": sha, "bytes": length, "url": "pdfs/%s.pdf" % sha, "name": name})
        except Exception as e:
            try: os.remove(tmp)
            except OSError: pass
            return self._err(500, "تعذّر حفظ الملف: %s" % e)


def main():
    try:                                   # never crash printing a non-ASCII path on a Windows console
        sys.stdout.reconfigure(errors="replace"); sys.stderr.reconfigure(errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Local server for the Maliki fiqh library editor")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1", help="default: this computer only")
    ap.add_argument("--data", default=str(APP_DIR / "data"), help="folder for library.json, pdfs/ and backups/")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--max-upload-mb", type=int, default=4096)
    a = ap.parse_args()
    if not (APP_DIR / "index.html").exists():
        sys.exit("index.html was not found next to server.py. Keep the files of the package together.")
    store = Store(a.data)
    srv = None
    for port in range(a.port, a.port + 25):
        try:
            srv = ThreadingHTTPServer((a.host, port), Handler); break
        except OSError:
            continue
    if srv is None:
        sys.exit("No free port between %d and %d. Close the other program, or choose another with --port." % (a.port, a.port + 24))
    port = srv.server_address[1]
    loop = a.host in ("127.0.0.1", "localhost", "::1")
    srv.store, srv.verbose, srv.max_upload = store, a.verbose, a.max_upload_mb * 1024 * 1024
    srv.check_host = loop                                   # on a network bind we cannot know the names people will use
    srv.allowed_hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port, "[::1]:%d" % port}
    srv.allowed_origins = {"http://" + h for h in srv.allowed_hosts}
    if not loop:
        srv.allowed_origins = None
    url = "http://127.0.0.1:%d/" % port
    print("")
    print("  Maliki fiqh library editor %s" % VERSION)
    print("  Open:   %s" % url)
    print("  Data:   %s" % store.dir)
    if not loop:
        print("  WARNING: listening on %s with NO password. Anyone who can reach this machine can edit the library." % a.host)
    print("  Stop:   press Ctrl+C in this window")
    print("")
    if not a.no_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped. Your data is in: %s" % store.dir)
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
