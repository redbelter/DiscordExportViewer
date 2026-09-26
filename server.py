"""DM Browser — local chat interface over any Discord data package.

Run:  python server.py   →  http://127.0.0.1:5055
Serves read-only data from dm.db (built by build_dm_db.py).
Port: config.json {"port": N} or DM_PORT env, default 5055.
"""
import json, os, re, sqlite3, subprocess, sys, threading
import zipfile
from pkgopen import open_pkg_at
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
DB   = os.path.join(HERE, "dm.db")
db_lock = threading.RLock()   # reentrant: POST handler + set_package both take it
FETCH = {"proc": None}

CFG = {}
_cfgp = os.path.join(HERE, "config.json")
if os.path.isfile(_cfgp):
    try:
        CFG = json.load(open(_cfgp, encoding="utf-8"))
    except Exception:
        CFG = {}
PORT = int(os.environ.get("DM_PORT", CFG.get("port", 5055)))

if not os.path.isfile(DB):
    print("dm.db not found — trying auto-discovery…")
    try:
        import build_dm_db
        base = build_dm_db.find_package()
        if base:
            build_dm_db.build(base, DB)
            print(f"built from {base}")
    except Exception as e:
        print("auto-build failed:", e)
    if not os.path.isfile(DB):
        print("No index yet — the app will show a setup screen in the browser.")

def open_db():
    global con
    con = sqlite3.connect(DB, check_same_thread=False)
    con.row_factory = sqlite3.Row
    try:
        con.execute("CREATE INDEX IF NOT EXISTS idx_msg_did ON messages(key, discord_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_att_status ON attachments(status)")
    except sqlite3.OperationalError:
        pass  # empty db (setup screen) / attachments table comes later
    con.commit()

open_db()


def adopt_existing_cache():
    """Index exists but has no attachment registry, yet media files are on
    disk (interrupted switch/restart) — re-adopt them in the background."""
    try:
        if has_attachments():
            return
        if con.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 0:
            return
        att_dir = os.path.join(HERE, "attachments")
        if not os.path.isdir(att_dir) or not any(os.scandir(att_dir)):
            return
        log = open(os.path.join(HERE, "fetch.log"), "ab")
        FETCH["proc"] = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "fetch_attachments.py")],
            cwd=HERE, stdout=log, stderr=log)
    except Exception:
        pass


def has_attachments():
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='attachments'").fetchone() is not None

adopt_existing_cache()


def meta(k):
    try:
        r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else None
    except sqlite3.OperationalError:
        return None


DRIVE_OK = re.compile(r"^[A-Za-z]:[/\\]$")

def list_dir(path):
    """List directories under `path` (safe, read-only). Returns dict."""
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        raise ValueError("not a directory")
    out = {"path": path, "parent": None, "dirs": [], "is_package": None}
    parent = os.path.dirname(path)
    if parent and parent != path:
        out["parent"] = parent
    if path == os.path.dirname(path.rstrip("/\\")) + os.sep or DRIVE_OK.match(path + os.sep):
        out["parent"] = None
    try:
        entries = sorted(os.listdir(path), key=str.lower)
    except PermissionError:
        raise ValueError("permission denied")
    for name in entries:
        if name.startswith(".") or name in ("node_modules", "__pycache__"):
            continue
        try:
            if os.path.isdir(os.path.join(path, name)) and not os.path.islink(os.path.join(path, name)):
                out["dirs"].append(name)
        except (PermissionError, OSError):
            pass
    out["is_package"] = os.path.isdir(os.path.join(path, "Messages"))
    # .zip files that ARE a Discord data package (raw request_data.zip works directly)
    out["zips"] = []
    for name in entries:
        if not name.lower().endswith(".zip") or name.startswith("."):
            continue
        fp = os.path.join(path, name)
        try:
            if not os.path.isfile(fp) or not zipfile.is_zipfile(fp):
                continue
            with zipfile.ZipFile(fp) as zf:
                names = zf.namelist()[:40000]
            leaf = [n.rsplit("/", 1)[-1] for n in names]
            if "messages.json" in leaf and "user.json" in leaf:
                out["zips"].append({"name": name, "bytes": os.path.getsize(fp)})
        except Exception:
            pass
    return out


def drive_roots():
    import string
    if os.name == "nt":
        return [f"{c}:\\" for c in string.ascii_uppercase if os.path.isdir(f"{c}:\\")] or ["C:\\"]
    return ["/"]


def set_package(pkg):
    """Validate a data package, persist it to config.json, rebuild the index."""
    import build_dm_db
    pkg = os.path.abspath(pkg)
    try:
        probe = open_pkg_at(pkg)
    except Exception:
        raise ValueError("not a folder or .zip file")
    if probe is None or not probe.isdir("Messages"):
        raise ValueError("no Messages/ inside — pick the folder (or .zip) extracted from request_data.zip")
    if not probe.exists("Account"):
        raise ValueError("no Account/ folder — point at the main export, not a sub-folder")
    prev = CFG.get("package") or CFG.get("last_package")
    # re-picking the folder that's already indexed with a healthy index → no-op
    # (avoids a needless rebuild, which would blank the attachment gallery)
    if prev and os.path.abspath(prev) == os.path.abspath(pkg):
        try:
            healthy = con.execute("SELECT COUNT(*) FROM people").fetchone()[0] > 0
        except Exception:
            healthy = False
        if healthy:
            if CFG.get("package") != os.path.abspath(pkg):   # keep config honest
                cfg = dict(CFG); cfg["package"] = os.path.abspath(pkg); cfg.pop("last_package", None)
                with open(os.path.join(HERE, "config.json"), "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2)
                CFG.clear(); CFG.update(cfg)
            return pkg
    cfg = dict(CFG); cfg["package"] = pkg
    cfg.pop("last_package", None)
    with open(os.path.join(HERE, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    CFG.clear(); CFG.update(cfg)
    # NOTE: we deliberately do NOT delete the attachments cache when switching
    # packages. The gallery reads its registry from the (rebuilt) index db, so a
    # stale cache can never mix two packages' media — and keeping it means
    # switching back (e.g. demo -> your real package) re-adopts the files on
    # disk instantly with zero re-downloads. (fetch_attachments.py adopts files
    # already present and marks them ok.)
    # release our handle on the old dm.db before build_dm_db replaces it
    try:
        con.close()
    except Exception:
        pass
    build_dm_db.build(pkg, DB)
    with db_lock:
        open_db()
    # the rebuilt index has no attachment registry — if a media cache already
    # exists on disk (switched back, or demo), re-adopt it in the background
    # (files on disk are marked ok with zero downloads).
    att_dir = os.path.join(HERE, "attachments")
    try:
        if os.path.isdir(att_dir) and any(os.scandir(att_dir)):
            log = open(os.path.join(HERE, "fetch.log"), "ab")
            FETCH["proc"] = subprocess.Popen(
                [sys.executable, os.path.join(HERE, "fetch_attachments.py")],
                cwd=HERE, stdout=log, stderr=log)
    except OSError:
        pass
    return pkg


def fts_query(q):
    """Turn free text into a safe FTS5 query: quoted AND-joined prefix terms."""
    toks = re.findall(r"[\w']+", q)
    if not toks:
        return None
    return " AND ".join('"{}"'.format(t.replace('"', '""')) + "*" for t in toks)

HTML = open(os.path.join(HERE, "index.html"), encoding="utf-8").read().encode("utf-8")


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        try:
            if p == "/api/package":
                body = self._body()
                pkg = (body.get("path") or "").strip().strip('"')
                if not pkg:
                    return self._send(400, json.dumps({"error": "no path"}).encode())
                with db_lock:
                    try:
                        resolved = set_package(pkg)
                    except Exception:
                        open_db()   # set_package may have closed the handle mid-flight
                        raise
                return self._send(200, json.dumps({"ok": True, "package": resolved}).encode())

            if p == "/api/reset":
                # forget the current package → app returns to the setup screen.
                # attachments are kept (keyed by last_package) so re-picking the same
                # folder doesn't force a re-download.
                with db_lock:
                    try:
                        con.close()
                    except Exception:
                        pass
                    import time as _t
                    if os.path.isfile(DB):
                        # Windows: open handles block delete — rename always works,
                        # then open_db() creates a fresh EMPTY db at DB
                        try:
                            os.replace(DB, DB + ".stale-" + str(int(_t.time())))
                        except OSError:
                            try:
                                os.remove(DB)
                            except OSError:
                                pass
                    cfg = dict(CFG)
                    if cfg.get("package"):
                        cfg["last_package"] = cfg["package"]
                    cfg.pop("package", None)
                    with open(os.path.join(HERE, "config.json"), "w", encoding="utf-8") as f:
                        json.dump(cfg, f, indent=2)
                    CFG.clear(); CFG.update(cfg)
                    open_db()
                return self._send(200, json.dumps({"ok": True}).encode())

            if p == "/api/fetch":
                proc = FETCH["proc"]
                if proc and proc.poll() is None:
                    return self._send(200, json.dumps({"ok": True, "already": True}).encode())
                log = open(os.path.join(HERE, "fetch.log"), "ab")
                FETCH["proc"] = subprocess.Popen(
                    [sys.executable, os.path.join(HERE, "fetch_attachments.py")],
                    cwd=HERE, stdout=log, stderr=log)
                return self._send(200, json.dumps({"ok": True, "started": True}).encode())

            return self._send(404, b'{"error":"not found"}')
        except ValueError as e:
            return self._send(400, json.dumps({"error": str(e)}).encode())
        except Exception as e:
            return self._send(500, json.dumps({"error": str(e)}).encode())

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if p == "/" or p == "/index.html":
                return self._send(200, HTML, "text/html; charset=utf-8")

            if p == "/file":
                rel = qs.get("f", "")
                if not rel.startswith("attachments/") or ".." in rel:
                    return self._send(400, b"bad path", "text/plain")
                fp = os.path.normpath(os.path.join(HERE, rel.replace("/", os.sep)))
                if not fp.startswith(os.path.join(os.path.normpath(HERE), "attachments")) or not os.path.isfile(fp):
                    return self._send(404, b"not found", "text/plain")
                ext = fp.rsplit(".", 1)[-1].lower() if "." in fp else ""
                ctype = {"png":"image/png","jpg":"image/jpeg","jpeg":"image/jpeg","gif":"image/gif",
                         "webp":"image/webp","avif":"image/avif","mp4":"video/mp4","webm":"video/webm",
                         "mov":"video/quicktime","m4a":"audio/mp4","aac":"audio/aac","mp3":"audio/mpeg",
                         "txt":"text/plain","json":"text/plain"}.get(ext, "application/octet-stream")
                with open(fp, "rb") as f:
                    return self._send(200, f.read(), ctype)

            if p == "/api/stats":
                if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='people'").fetchone():
                    return self._send(200, json.dumps({"empty": True, "n": 0, "m": 0, "msgs": 0, "attach": 0,
                                                       "username": CFG.get("owner") or "You",
                                                       "attachments_cached": False,
                                                       "package": CFG.get("package")}).encode())
                r = dict(con.execute("SELECT COUNT(*) n, SUM(n) m FROM people").fetchone())
                r["msgs"] = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                r["attach"] = con.execute("SELECT SUM(attach) FROM messages").fetchone()[0]
                r["username"] = meta("username") or "You"
                r["attachments_cached"] = has_attachments()
                r["package"] = CFG.get("package") or meta("package")
                r["local_media"] = (meta("n_local") and int(meta("n_local")) > 0)
                return self._send(200, json.dumps(r).encode())

            if p == "/api/demo":
                d = os.path.join(HERE, "demo", "package.zip")
                return self._send(200, json.dumps({"available": os.path.isfile(d),
                                                   "path": d if os.path.isfile(d) else None}).encode())

            if p == "/api/browse":
                path = qs.get("path") or ""
                try:
                    if not path:
                        roots = drive_roots()
                        return self._send(200, json.dumps(
                            {"path": "", "parent": None, "dirs": [], "roots": roots,
                             "is_package": False, "home": os.path.expanduser("~"),
                             "current": CFG.get("package")}).encode())
                    d = list_dir(path)
                    d["current"] = CFG.get("package")
                    return self._send(200, json.dumps(d).encode())
                except ValueError as e:
                    return self._send(400, json.dumps({"error": str(e)}).encode())

            if p == "/api/people":
                rows = con.execute(
                    "SELECT key,name,name2,kind,n,first,last,active_days,att,links "
                    "FROM people ORDER BY n DESC").fetchall()
                return self._send(200, json.dumps([dict(r) for r in rows]).encode())

            if p == "/api/messages":
                key = qs.get("key", "")
                limit = min(int(qs.get("limit", 100)), 500)
                around = qs.get("around")
                have_att = has_attachments()
                def shape(rows):
                    out = []
                    for r in rows:
                        d = dict(r)
                        if have_att:
                            atts = con.execute(
                                "SELECT file,bytes,status,url FROM attachments WHERE key=? AND discord_id=?",
                                (key, d["discord_id"])).fetchall()
                            d["atts"] = [dict(a) for a in atts]
                        else:
                            d["atts"] = []
                        d.pop("discord_id", None)
                        out.append(d)
                    return out
                if around:
                    ts = con.execute("SELECT ts FROM messages WHERE id=?", (int(around),)).fetchone()
                    if ts:
                        rows = con.execute(
                            "SELECT id,ts,contents,attach,author,discord_id FROM messages "
                            "WHERE key=? AND ts<=? ORDER BY ts DESC LIMIT ?",
                            (key, ts["ts"], limit)).fetchall()
                        rows.reverse()
                        total = con.execute("SELECT COUNT(*) FROM messages WHERE key=?", (key,)).fetchone()[0]
                        return self._send(200, json.dumps(
                            {"msgs": shape(rows), "total": total,
                             "has_more": len(rows) < total}).encode())
                before = qs.get("before", "")
                if before:
                    rows = con.execute(
                        "SELECT id,ts,contents,attach,author,discord_id FROM messages "
                        "WHERE key=? AND ts<? ORDER BY ts DESC LIMIT ?",
                        (key, before, limit)).fetchall()
                else:
                    rows = con.execute(
                        "SELECT id,ts,contents,attach,author,discord_id FROM messages "
                        "WHERE key=? ORDER BY ts DESC LIMIT ?",
                        (key, limit)).fetchall()
                rows.reverse()
                total = con.execute("SELECT COUNT(*) FROM messages WHERE key=?", (key,)).fetchone()[0]
                return self._send(200, json.dumps(
                    {"msgs": shape(rows), "total": total,
                     "has_more": len(rows) < total}).encode())

            if p == "/api/gallery":
                if not has_attachments():
                    return self._send(200, b"[]")
                rows = con.execute("""
                    SELECT a.file, a.bytes, m.id, m.key, m.ts, m.contents, p.name2
                    FROM attachments a
                    JOIN messages m ON m.key=a.key AND m.discord_id=a.discord_id
                    JOIN people p ON p.key=a.key
                    WHERE a.status='ok'
                    ORDER BY m.ts DESC""").fetchall()
                return self._send(200, json.dumps([dict(r) for r in rows]).encode())

            if p == "/api/search":
                q = fts_query(qs.get("q", ""))
                key = qs.get("key", "")
                limit = min(int(qs.get("limit", 80)), 300)
                if not q:
                    return self._send(200, json.dumps([]).encode())
                sql = ("SELECT m.id, m.key, m.ts, snippet(fts,0,'<b>','</b>','…',22) snip, "
                       "p.name2 FROM fts JOIN messages m ON m.id=fts.rowid "
                       "JOIN people p ON p.key=m.key WHERE fts MATCH ? ")
                args = [q]
                if key:
                    sql += " AND m.key=? "
                    args.append(key)
                sql += " ORDER BY rank LIMIT ?"
                args.append(limit)
                rows = con.execute(sql, args).fetchall()
                return self._send(200, json.dumps([dict(r) for r in rows]).encode())

            return self._send(404, b'{"error":"not found"}')
        except Exception as e:
            return self._send(500, json.dumps({"error": str(e)}).encode())


if __name__ == "__main__":
    print(f"DM Browser → http://127.0.0.1:{PORT}  ({os.path.basename(DB)})")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
