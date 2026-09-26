"""Fetch every Discord-CDN attachment in your data package into a local cache.

Generic + resumable: skips files already downloaded. Writes the registry into dm.db.

Usage:
    python fetch_attachments.py                  # auto-discover package (or --package=PATH)
    python fetch_attachments.py --package C:\\path\\to\\package
    python fetch_attachments.py --sample 30      # just first N (smoke test)
    python fetch_attachments.py --workers 8      # parallel connections (default 12)

Notes:
  - Only downloads URLs on cdn.discordapp.com / media.discordapp.net.
  - Files expire on Discord's CDN: old uploads may 404. They're recorded with a
    fail:<code> status and the browser shows them as "expired" instead of hiding them.
  - Re-run any time — it resumes where it left off.
"""
import json, os, re, sqlite3, sys, time, threading, urllib.request
from queue import Queue, Empty
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

CACHE = os.environ.get("DM_ATTACH_DIR", os.path.join(HERE, "attachments"))
DB = os.environ.get("DM_DB", os.path.join(HERE, "dm.db"))
URL_RE = re.compile(r"https://[^\s]+")
CDN_HOSTS = ("cdn.discordapp.com", "media.discordapp.net")
CHANNEL_TYPES = {"DM", "GROUP_DM", "GUILD_TEXT", "PUBLIC_THREAD", "PRIVATE_THREAD", "GUILD_VOICE"}


def collect(base):
    """yield (channel_dir, discord_msg_id, url) for every Discord-CDN attachment URL"""
    out = []
    mdir = os.path.join(base, "Messages")
    for d in sorted(os.listdir(mdir)):
        cdir = os.path.join(mdir, d)
        if not os.path.isdir(cdir):
            continue
        try:
            ch = json.load(open(os.path.join(cdir, "channel.json"), encoding="utf-8"))
        except Exception:
            continue
        if ch.get("type") not in CHANNEL_TYPES:
            continue
        mp = os.path.join(cdir, "messages.json")
        if not os.path.exists(mp):
            continue
        try:
            msgs = json.load(open(mp, encoding="utf-8"))
        except Exception:
            continue
        for m in msgs:
            a = m.get("Attachments")
            seen = set()
            if a and isinstance(a, str):
                for url in URL_RE.findall(a):
                    if any(h in url for h in CDN_HOSTS):
                        out.append((d, m.get("ID"), url)); seen.add(url)
            # some images were pasted as bare URLs in the message body, not the Attachments field
            c = m.get("Contents") or ""
            for url in URL_RE.findall(c):
                if any(h in url for h in CDN_HOSTS) and url not in seen:
                    out.append((d, m.get("ID"), url)); seen.add(url)
    return out


def safe_name(url, mid):
    fn = url.split("/")[-1].split("?")[0] or "file"
    fn = re.sub(r"[^\w.\-]", "_", fn)
    # Windows forbids trailing dots/spaces and reserved names; keep it portable anyway
    fn = fn.rstrip(". ") or "file"
    if fn.upper().split(".")[0] in {"CON","PRN","AUX","NUL","COM1","COM2","COM3","COM4","LPT1","LPT2","LPT3"}:
        fn = "_" + fn
    att_id = url.split("/attachments/")[1].split("?")[0].replace("/", "_") if "/attachments/" in url else str(mid)
    return f"{att_id}__{fn}"[:180]


def main():
    args = sys.argv[1:]
    limit = None
    workers = 12
    pkg = None
    if "--sample" in args:
        limit = int(args[args.index("--sample") + 1])
    if "--workers" in args:
        workers = int(args[args.index("--workers") + 1])
    for a in args:
        if a.startswith("--package="):
            pkg = a.split("=", 1)[1]
        elif a == "--package":
            pkg = args[args.index(a) + 1]
    if not pkg:
        import build_dm_db
        pkg = build_dm_db.find_package()
    if not pkg or not os.path.isdir(os.path.join(pkg, "Messages")):
        print("Could not find the data package. Pass  --package C:\\path\\to\\package")
        sys.exit(1)
    if not os.path.isfile(DB):
        print(f"{DB} missing — run build_dm_db.py first.")
        sys.exit(1)

    os.makedirs(CACHE, exist_ok=True)
    items = collect(pkg)
    if limit:
        items = items[:limit]
    print(f"{len(items)} attachment urls to fetch -> {CACHE}", flush=True)

    con = sqlite3.connect(DB, check_same_thread=False)
    con.executescript("""
      CREATE TABLE IF NOT EXISTS attachments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        key TEXT, discord_id TEXT, url TEXT,
        file TEXT, bytes INTEGER, status TEXT,
        UNIQUE(key, discord_id, url)
      );
    """)
    con.executemany(
        "INSERT OR IGNORE INTO attachments(key,discord_id,url,status) VALUES(?,?,?,'pending')",
        [(d, str(mid), u) for d, mid, u in items])
    con.commit()

    q = Queue()
    for it in items:
        q.put(it)
    stats = Counter()
    lock = threading.Lock()

    def worker():
        while True:
            try:
                d, mid, url = q.get_nowait()
            except Empty:
                return
            try:
                with lock:
                    row = con.execute(
                        "SELECT file,status FROM attachments WHERE key=? AND discord_id=? AND url=?",
                        (d, str(mid), url)).fetchone()
                if row and row[1] == "ok" and row[0] and os.path.exists(os.path.join(HERE, row[0])):
                    stats["skip"] += 1
                else:
                    name = safe_name(url, mid)
                    path = os.path.join(CACHE, name)
                    if not (os.path.exists(path) and os.path.getsize(path) > 0):
                        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(req, timeout=30) as r, open(path + ".part", "wb") as f:
                            while True:
                                chunk = r.read(65536)
                                if not chunk:
                                    break
                                f.write(chunk)
                        os.replace(path + ".part", path)
                        stats["dl"] += 1
                    else:
                        stats["skip"] += 1
                    with lock:
                        con.execute(
                            "UPDATE attachments SET file=?, bytes=?, status='ok' WHERE key=? AND discord_id=? AND url=?",
                            (f"attachments/{name}", os.path.getsize(path), d, str(mid), url))
            except Exception as e:
                code = getattr(e, "code", None)
                with lock:
                    con.execute(
                        "UPDATE attachments SET status=? WHERE key=? AND discord_id=? AND url=?",
                        (f"fail:{code or type(e).__name__}", d, str(mid), url))
                stats["fail"] += 1
                if stats["fail"] % 100 == 1:
                    print("fail:", code or type(e).__name__, str(e)[:60], url[:80], flush=True)
            finally:
                done = sum(stats[c] for c in ("skip", "dl", "fail"))
                if done % 200 == 0:
                    with lock:
                        con.commit()
                    print(f"progress {done}/{len(items)} dl={stats['dl']} skip={stats['skip']} fail={stats['fail']}", flush=True)
                q.task_done()

    t0 = time.time()
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    for t in threads: t.start()
    for t in threads: t.join()
    con.commit()
    n = con.execute("SELECT COUNT(*), SUM(bytes) FROM attachments WHERE status='ok'").fetchone()
    print(f"done: cached {n[0]} files, {round((n[1] or 0)/1e6)} MB in {time.time()-t0:.0f}s. fails={stats['fail']}")
    con.close()


if __name__ == "__main__":
    main()
