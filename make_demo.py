"""Generate demo/package.zip — a tiny synthetic Discord data package.

Fully fake: fake users, fake snowflake ids, PIL-generated images.
Used by the app's built-in demo mode so anyone can see what the viewer
does before pointing it at their own export.

Run:  python make_demo.py
"""
import io, json, os, random, zipfile
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "demo", "package.zip")

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None

OWNER = {"id": "900100000000000001", "username": "demo_user", "global_name": "Demo User"}
FRIENDS = [
    ("900200000000000002", "ari.jpg",   "Ari"),
    ("900300000000000003", "bram_",     "Bram"),
    ("900400000000000004", "cleo.exe",  "Cleo"),
    ("900500000000000005", "devi",      "Devi"),
    ("900600000000000006", "enzo_tv",   "Enzo"),
]
PALETTE = ["#5865F2", "#3ba55c", "#faa61a", "#eb459e", "#1abc9c"]

CONVOS = [  # (channel dir, type, recipients, label, friend_idx)
    ("c910000000000000101", "DM",         [0],        "Direct Message with Ari",  0),
    ("c910000000000000102", "DM",         [1],        "Direct Message with Bram", 1),
    ("c910000000000000103", "DM",         [2],        "Direct Message with Cleo", 2),
    ("c910000000000000104", "DM",         [3],        "Direct Message with Devi", 3),
    ("c910000000000000105", "GROUP_DM",   [0, 1, 4],  None,                         4),
    ("c910000000000000106", "GUILD_TEXT", [0],        "general, Demo Server",       2),
]

LINES = [
    "did you see the update today??", "yeah the new build looks clean",
    "we should try it tonight", "im in", "on my way", "look at this",
    "no way lol", "that's actually wild", "brb grabbing food",
    "check the pinned message", "okok", "same time tomorrow?",
    "works for me", "just pushed it", "testing testing 123",
    "the weather here is unreal", "send pics", "here u go",
    "one sec", "did it work?", "yep works perfectly", "legendary",
    "i'll send the files later", "tonight then 🎉", "good night",
    "morning", "the cafe was packed", "we can go to the other one",
    "sounds good", "lets do it",
]
IMG_MSGS = {"here u go": 1, "send pics": 1, "look at this": 1, "testing testing 123": 1}


def make_image(n, label="DEMO"):
    """Deterministic gradient card with a number — no real-world content."""
    if Image is None:
        return b"\x89PNG\r\n\x1a\n" + b"\x00" * 60  # 1x1-ish stub
    w, h = 640, 400
    c1 = PALETTE[n % len(PALETTE)].lstrip("#")
    c2 = PALETTE[(n + 2) % len(PALETTE)].lstrip("#")
    col1 = tuple(int(c1[i:i + 2], 16) for i in (0, 2, 4))
    col2 = tuple(int(c2[i:i + 2], 16) for i in (0, 2, 4))
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        t = y / h
        row = tuple(int(col1[k] + (col2[k] - col1[k]) * t) for k in range(3))
        for x in range(w):
            px[x, y] = row
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 86)
        font2 = ImageFont.truetype("arial.ttf", 26)
    except Exception:
        font = font2 = ImageFont.load_default()
    d.text((w / 2, h / 2 - 40), f"{label} {n}", fill="white", anchor="mm", font=font)
    d.text((w / 2, h - 28), "synthetic sample — not from a real account",
           fill=(235, 235, 235), anchor="mm", font=font2)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def main():
    rnd = random.Random(1234)  # deterministic
    z = zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED)
    media_count = 0

    # Account
    z.writestr("package/Account/user.json", json.dumps({
        "id": OWNER["id"], "username": OWNER["username"], "global_name": OWNER["global_name"],
        "email": "demo@example.invalid", "phone": None,
        "relationships": [
            {"id": f[0], "type": 1, "nick": None, "user": {"id": f[0], "username": f[1], "global_name": f[2]}}
            for f in FRIENDS],
    }, indent=2))
    z.writestr("package/Account/billing.json", json.dumps({"payment_sources": [], "profile": {}}))

    # Messages/index.json  (Discord keys DM labels by CHANNEL id, guild-name entries by guild id)
    idx = {}
    for cd, ctype, recips, label, fi in CONVOS:
        ch_id = cd.lstrip("c")
        if ctype == "DM":
            idx[ch_id] = label
        elif ctype == "GUILD_TEXT":
            idx["920000000000000001"] = "Demo Server"
            idx[ch_id] = label
        elif ctype == "GROUP_DM":
            idx[ch_id] = "Group DM"
    z.writestr("package/Messages/index.json", json.dumps(idx, indent=1))

    id2name = {f[0]: f[2] for f in FRIENDS}
    # spread the fake history over the last ~60 days so the demo looks alive
    end = datetime.now().replace(microsecond=0)
    start = end - timedelta(days=60)

    for cd, ctype, recips, label, fi in CONVOS:
        ch_id = cd.lstrip("c")
        recips_full = [OWNER["id"]] + [FRIENDS[i][0] for i in recips]
        z.writestr(f"package/Messages/{cd}/channel.json", json.dumps({
            "id": ch_id, "name": (label or "demo-group").split(", ")[-1], "type": ctype,
            "recipients": recips_full,
            **({"guild_id": "920000000000000001"} if ctype == "GUILD_TEXT" else {}),
        }, indent=1))

        ms = []
        t = start + timedelta(hours=rnd.randint(0, 60*24-6*24))
        n_img = 0
        step = (end - t) / 26.0
        for li in range(rnd.randint(14, 26)):
            line = LINES[li % len(LINES)]
            attach = ""
            if line in IMG_MSGS and n_img < 4:
                n_img += 1
                media_count += 1
                spoiler = "SPOILER_" if (n_img == 3) else ""
                fn = f"{spoiler}demo_{media_count:03d}.png"
                z.writestr(f"package/media/{fn}", make_image(media_count))
                attach = f"local://media/{fn}"
            ms.append({"ID": str(930000000000000000 + len(ms) + media_count * 100 +
                                CONVOS.index((cd, ctype, recips, label, fi)) * 7000),
                       "Timestamp": t.strftime("%Y-%m-%d %H:%M:%S"),
                       "Contents": line, "Attachments": attach})
            t += timedelta(minutes=int(step.total_seconds()/60*rnd.uniform(0.2, 1.8)))
        z.writestr(f"package/Messages/{cd}/messages.json", json.dumps(ms, indent=1))

    z.close()
    print(f"wrote {OUT}: {os.path.getsize(OUT)/1024:.0f} KB, {media_count} images, "
          f"{len(CONVOS)} conversations")


if __name__ == "__main__":
    main()
