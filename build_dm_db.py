"""Build dm.db — SQLite FTS5 index over any Discord data package.

A package can be a folder OR a .zip (raw request_data.zip works too).

Usage:
    python build_dm_db.py                         # auto-discover the package
    python build_dm_db.py C:\\path\\to\\package       # folder
    python build_dm_db.py C:\\path\\to\\request_data.zip

Auto-discovery looks for:
  1. config.json  {"package": "C:\\...\\package"} next to this script
  2. $DISCORD_PACKAGE env var
  3. a folder named "package" containing Messages/ on the Desktop / home / Downloads
"""
import json, os, re, sqlite3, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pkgopen import open_pkg, open_pkg_at

HERE = os.path.dirname(os.path.abspath(__file__))

CHANNEL_TYPES = {"DM", "GROUP_DM", "GUILD_TEXT", "PUBLIC_THREAD", "PRIVATE_THREAD", "GUILD_VOICE"}


def find_package():
    """Locate the Discord data package (folder containing Messages/ + Account/, or a .zip)."""
    def okp(p):
        try:
            pkg = open_pkg(p)
            return pkg is not None and pkg.isdir("Messages")
        except Exception:
            return False
    # 1. config.json
    cfg = os.path.join(HERE, "config.json")
    if os.path.isfile(cfg):
        try:
            p = json.load(open(cfg, encoding="utf-8")).get("package")
            if p and okp(p):
                return p
        except Exception:
            pass
    # 2. env var
    p = os.environ.get("DISCORD_PACKAGE")
    if p and okp(p):
        return p
    # 3. scan common spots for a folder/zip that looks like a package
    home = os.path.expanduser("~")
    cands = []
    for root in (os.path.join(home, "Desktop"), home, os.path.join(home, "Downloads")):
        if not os.path.isdir(root):
            continue
        try:
            for name in sorted(os.listdir(root), key=str.lower):
                fp = os.path.join(root, name)
                if os.path.isfile(fp) and name.lower().endswith(".zip") and (
                        "request_data" in name.lower() or "package" in name.lower()):
                    cands.append(fp)
                elif os.path.isdir(fp):
                    cands.append(fp)
                    try:
                        for sub in sorted(os.listdir(fp)):
                            sfp = os.path.join(fp, sub)
                            if os.path.isdir(sfp):
                                cands.append(sfp)
                    except Exception:
                        pass
        except Exception:
            pass
    for c in cands:
        if okp(c):
            try:
                if open_pkg(c).exists("Account"):
                    return c
            except Exception:
                pass
    return None


def resolve_name(ch_label, recips, id2name, ME, kind):
    """Resolve a conversation's display name from package metadata only."""
    if kind == "dm":
        other = [x for x in recips if x != ME]
        name = None
        if ch_label and ch_label.startswith("Direct Message with "):
            name = ch_label.replace("Direct Message with ", "")
        if not name and other:
            name = id2name.get(other[0])
        return name or "Unknown Participant"
    if kind == "group":
        parts = ["you" if x == ME else id2name.get(x) or "someone" for x in recips]
        return " · ".join(parts)
    # guild channel: labels look like "Unknown channel, Wah" or "general, ServerName"
    if ch_label:
        return ch_label.split(", ", 1)[-1] if ch_label.startswith("Unknown channel, ") else ch_label
    return "unknown channel"


def build(base, out):
    """base: folder path, .zip path, or an open pkg object."""
    t0 = time.time()
    pkg = base if hasattr(base, "read_json") else open_pkg_at(base)
    base_path = base if isinstance(base, str) else pkg.path

    if os.path.exists(out):
        os.remove(out)
    con = sqlite3.connect(out)
    con.executescript("""
      CREATE TABLE people(
        key TEXT PRIMARY KEY,       -- channel dir name (c<id>)
        name TEXT, name2 TEXT,      -- display name / resolved name
        kind TEXT,                  -- dm | group | guild
        members TEXT,               -- JSON list of recipient ids
        n INTEGER, first TEXT, last TEXT, active_days INTEGER,
        att INTEGER, links INTEGER
      );
      CREATE TABLE messages(
        id INTEGER PRIMARY KEY,
        key TEXT, ts TEXT, day TEXT, contents TEXT, attach INTEGER,
        discord_id TEXT, author TEXT
      );
      CREATE INDEX idx_msg_key ON messages(key, ts);
      CREATE VIRTUAL TABLE fts USING fts5(
        contents, content='messages', content_rowid='id',
        tokenize='unicode61 remove_diacritics 2'
      );
      CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
    """)

    u = pkg.read_json("Account/user.json", {}) or {}
    ME = u.get("id") or "?"
    uname = u.get("global_name") or u.get("username") or "You"

    mi = pkg.read_json("Messages/index.json", {}) or {}

    id2name = {}
    for r in u.get("relationships", []):
        uu = r.get("user") or {}
        if uu.get("id"):
            id2name[uu["id"]] = uu.get("global_name") or uu.get("username") or "?"

    rows_msg, rows_ppl = [], []
    mid = 0
    n_local_total = 0
    link_re = re.compile(r"https?://")

    for d in pkg.subdirs("Messages"):
        try:
            ch = pkg.read_json("Messages/%s/channel.json" % d)
        except Exception:
            ch = None
        if not ch:
            continue
        ctype = ch.get("type")
        if ctype not in CHANNEL_TYPES:
            continue
        recips = [str(x) for x in ch.get("recipients", []) or []]
        ch_id = str(ch.get("id") or d.lstrip("c"))
        ch_label = mi.get(ch_id)
        if not ch_label:
            gid = str(ch.get("guild_id") or "")
            ch_label = mi.get(gid)
        n_local = 0
        kind = {"DM": "dm", "GROUP_DM": "group"}.get(ctype, "guild")
        name2 = resolve_name(ch_label, recips, id2name, ME, kind)
        name = name2
        try:
            ms = pkg.read_json("Messages/%s/messages.json" % d)
        except Exception:
            ms = None
        if not ms:
            continue
        days = set(); att = 0; links = 0; first = last = None
        for m in ms:
            ts = m.get("Timestamp") or ""
            c = m.get("Contents") or ""
            av = m.get("Attachments")
            a = bool(av)
            if isinstance(av, str) and "local://" in av: n_local += 1
            mid += 1
            # the export contains ONLY messages you sent (Discord policy)
            rows_msg.append((mid, d, ts, ts[:10], c, 1 if a else 0, str(m.get("ID", "")), "me"))
            if ts:
                days.add(ts[:10])
                if first is None or ts < first: first = ts
                if last  is None or ts > last:  last = ts
            if a: att += 1
            if link_re.search(c): links += 1
        n_local_total += n_local
        rows_ppl.append((d, name, name2, kind, json.dumps(recips), len(ms), first, last, len(days), att, links))

    con.executemany("INSERT INTO people VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows_ppl)
    con.executemany("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?)", rows_msg)
    con.execute("INSERT INTO fts(rowid, contents) SELECT id, contents FROM messages")
    con.commit()
    con.execute("INSERT INTO fts(fts) VALUES('optimize')")
    con.executemany("INSERT INTO meta VALUES(?,?)", [
        ("username", uname), ("owner_id", ME), ("package", base_path),
        ("pkg_kind", pkg.kind), ("n_local", str(n_local_total)),
        ("created", time.strftime("%Y-%m-%d %H:%M:%S"))])
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    p = con.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    con.close()
    print(f"built {out}: {p} conversations, {n} messages in {time.time()-t0:.1f}s  (owner: {uname}, {pkg.kind})")
    return base_path


def main():
    base = None
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        base = args[0]
    else:
        base = find_package()
    if not base or not open_pkg(base).isdir("Messages"):
        print('Could not find a Discord data package.')
        print('Usage:  python build_dm_db.py C:\\path\\to\\package   (folder or request_data.zip)')
        print('   or:  create config.json next to this script:  {"package": "C:\\path\\to\\package"}')
        print('        (point it at the folder extracted from request_data.zip — the one containing Messages/)')
        sys.exit(1)
    out = os.environ.get("DM_DB", os.path.join(HERE, "dm.db"))
    build(base, out)


if __name__ == "__main__":
    main()
