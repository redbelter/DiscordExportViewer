"""Build dm.db — SQLite FTS5 index over any Discord data package.

Generic version: figures out everything it needs from the package itself.

Usage:
    python build_dm_db.py                       # auto-discover the package
    python build_dm_db.py C:\\path\\to\\package    # or point it explicitly

Auto-discovery looks for:
  1. config.json  {"package": "C:\\...\\package"} next to this script
  2. $DISCORD_PACKAGE env var
  3. a folder literally named "package" containing Messages/ on the Desktop or home
"""
import json, os, re, sqlite3, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))

CHANNEL_TYPES = {"DM", "GROUP_DM", "GUILD_TEXT", "PUBLIC_THREAD", "PRIVATE_THREAD", "GUILD_VOICE"}


def find_package():
    """Locate the Discord data package (the folder containing Messages/ + Account/)."""
    def ok(p):
        return p and os.path.isdir(os.path.join(p, "Messages"))
    # 1. config.json
    cfg = os.path.join(HERE, "config.json")
    if os.path.isfile(cfg):
        try:
            p = json.load(open(cfg, encoding="utf-8")).get("package")
            if p and os.path.isdir(p):
                if ok(p):
                    return p
                m = os.path.join(p, "package")
                if ok(m):
                    return m
        except Exception:
            pass
    # 2. env var
    p = os.environ.get("DISCORD_PACKAGE")
    if p and ok(p):
        return p
    # 3. scan common spots for a folder that looks like a package
    home = os.path.expanduser("~")
    cands = []
    for root in (os.path.join(home, "Desktop"), home, os.path.join(home, "Downloads")):
        if not os.path.isdir(root):
            continue
        try:
            for name in sorted(os.listdir(root)):
                fp = os.path.join(root, name)
                if os.path.isdir(fp):
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
        if ok(c) and os.path.isdir(os.path.join(c, "Account")):
            return c
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
    t0 = time.time()
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

    u = {}
    upath = os.path.join(base, "Account", "user.json")
    if os.path.isfile(upath):
        u = json.load(open(upath, encoding="utf-8"))
    ME = u.get("id") or "?"
    uname = u.get("global_name") or u.get("username") or "You"

    mi = {}
    mipath = os.path.join(base, "Messages", "index.json")
    if os.path.isfile(mipath):
        mi = json.load(open(mipath, encoding="utf-8"))

    id2name = {}
    for r in u.get("relationships", []):
        uu = r.get("user") or {}
        if uu.get("id"):
            id2name[uu["id"]] = uu.get("global_name") or uu.get("username") or "?"

    mdir = os.path.join(base, "Messages")
    rows_msg, rows_ppl = [], []
    mid = 0
    link_re = re.compile(r"https?://")

    for d in sorted(os.listdir(mdir)):
        cdir = os.path.join(mdir, d)
        if not os.path.isdir(cdir):
            continue
        try:
            ch = json.load(open(os.path.join(cdir, "channel.json"), encoding="utf-8"))
        except Exception:
            continue
        ctype = ch.get("type")
        if ctype not in CHANNEL_TYPES:
            continue
        recips = [str(x) for x in ch.get("recipients", []) or []]
        ch_id = str(ch.get("id") or d.lstrip("c"))
        ch_label = mi.get(ch_id)
        if not ch_label:
            # guild channel labels live under the guild id sometimes
            gid = str(ch.get("guild_id") or "")
            ch_label = mi.get(gid)
        kind = {"DM": "dm", "GROUP_DM": "group"}.get(ctype, "guild")
        name2 = resolve_name(ch_label, recips, id2name, ME, kind)
        name = name2
        mp = os.path.join(cdir, "messages.json")
        if not os.path.exists(mp):
            continue
        try:
            ms = json.load(open(mp, encoding="utf-8"))
        except Exception:
            continue
        if not ms:
            continue
        days = set(); att = 0; links = 0; first = last = None
        for m in ms:
            ts = m.get("Timestamp") or ""
            c = m.get("Contents") or ""
            a = bool(m.get("Attachments"))
            mid += 1
            # the export contains ONLY messages you sent (Discord policy)
            rows_msg.append((mid, d, ts, ts[:10], c, 1 if a else 0, str(m.get("ID", "")), "me"))
            if ts:
                days.add(ts[:10])
                if first is None or ts < first: first = ts
                if last  is None or ts > last:  last = ts
            if a: att += 1
            if link_re.search(c): links += 1
        rows_ppl.append((d, name, name2, kind, json.dumps(recips), len(ms), first, last, len(days), att, links))

    con.executemany("INSERT INTO people VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows_ppl)
    con.executemany("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?)", rows_msg)
    con.execute("INSERT INTO fts(rowid, contents) SELECT id, contents FROM messages")
    con.commit()
    con.execute("INSERT INTO fts(fts) VALUES('optimize')")
    con.executemany("INSERT INTO meta VALUES(?,?)", [
        ("username", uname), ("owner_id", ME), ("package", base),
        ("created", time.strftime("%Y-%m-%d %H:%M:%S"))])
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    p = con.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    con.close()
    print(f"built {out}: {p} conversations, {n} messages in {time.time()-t0:.1f}s  (owner: {uname})")
    return base


def main():
    base = None
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        base = args[0]
        if not os.path.isdir(os.path.join(base, "Messages")) and os.path.isdir(os.path.join(base, "package")):
            base = os.path.join(base, "package")
    else:
        base = find_package()
    if not base:
        print('Could not find a Discord data package.')
        print('Usage:  python build_dm_db.py C:\\path\\to\\package')
        print('   or:  create config.json next to this script:  {"package": "C:\\\\path\\\\to\\\\package"}')
        print('        (point it at the folder extracted from your request_data.zip — the one containing Messages/)')
        sys.exit(1)
    out = os.environ.get("DM_DB", os.path.join(HERE, "dm.db"))
    build(base, out)


if __name__ == "__main__":
    main()
