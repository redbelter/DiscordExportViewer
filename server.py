"""DM Browser — local chat interface over any Discord data package.

Run:  python server.py   →  http://127.0.0.1:5055
Serves read-only data from dm.db (built by build_dm_db.py).
Port: config.json {"port": N} or DM_PORT env, default 5055.
"""
import json, os, re, sqlite3, sys
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
DB   = os.path.join(HERE, "dm.db")

CFG = {}
_cfgp = os.path.join(HERE, "config.json")
if os.path.isfile(_cfgp):
    try:
        CFG = json.load(open(_cfgp, encoding="utf-8"))
    except Exception:
        CFG = {}
PORT = int(os.environ.get("DM_PORT", CFG.get("port", 5055)))

if not os.path.isfile(DB):
    print("dm.db not found — building the index from your data package first…")
    import build_dm_db
    base = build_dm_db.main()
    if not os.path.isfile(DB):
        sys.exit(1)

con = sqlite3.connect(DB, check_same_thread=False)
con.row_factory = sqlite3.Row
con.execute("CREATE INDEX IF NOT EXISTS idx_msg_did ON messages(key, discord_id)")
try:
    con.execute("CREATE INDEX IF NOT EXISTS idx_att_status ON attachments(status)")
except sqlite3.OperationalError:
    pass  # attachments table appears once fetch_attachments.py has run
con.commit()


def has_attachments():
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='attachments'").fetchone() is not None


def meta(k):
    try:
        r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else None
    except sqlite3.OperationalError:
        return None


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
                r = dict(con.execute("SELECT COUNT(*) n, SUM(n) m FROM people").fetchone())
                r["msgs"] = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                r["attach"] = con.execute("SELECT SUM(attach) FROM messages").fetchone()[0]
                r["username"] = meta("username") or "You"
                r["attachments_cached"] = has_attachments()
                return self._send(200, json.dumps(r).encode())

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
