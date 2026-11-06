"""
tumblr_to_bsky.py

Step 1:  python tumblr_to_bsky.py export   -> saves your original Tumblr posts to ./archive/
Step 2:  python tumblr_to_bsky.py post     -> posts the oldest not-yet-posted one to Bluesky
Run step 2 once a day (cron / Task Scheduler / GitHub Actions).

Setup:
  pip install requests atproto pillow
  Set env vars:
    TUMBLR_BLOG          e.g. yourname.tumblr.com
    TUMBLR_API_KEY       OAuth consumer key from https://www.tumblr.com/oauth/apps
    BSKY_HANDLE          e.g. yourname.bsky.social
    BSKY_APP_PASSWORD    create at bsky.app > Settings > Privacy and security > App passwords
"""
import io
import json
import os
import re
import sys
from pathlib import Path

import requests

ARCHIVE = Path("archive")
STATE = Path("posted.json")
MAX_CHARS = 300          # Bluesky limit
MAX_IMG_BYTES = 950_000  # Bluesky image limit is ~1MB


# ---------- EXPORT ----------
def export():
    # accept "name", "name.tumblr.com", "https://name.tumblr.com/",
    # "tumblr.com/name", "@name" etc.
    blog = os.environ["TUMBLR_BLOG"].strip().lower()
    blog = re.sub(r"^https?://", "", blog).replace("www.", "").strip("/@ ")
    if blog.startswith("tumblr.com/"):
        blog = blog.split("/", 1)[1]
    blog = blog.split("/")[0]
    if "." not in blog:
        blog += ".tumblr.com"
    key = os.environ["TUMBLR_API_KEY"].strip()
    ARCHIVE.mkdir(exist_ok=True)
    offset, saved = 0, 0
    while True:
        r = requests.get(
            f"https://api.tumblr.com/v2/blog/{blog}/posts",
            params={"api_key": key, "npf": "true", "limit": 20, "offset": offset},
            timeout=30,
        )
        if r.status_code != 200:
            try:
                msg = r.json().get("meta", {}).get("msg") or r.text[:200]
            except ValueError:
                msg = r.text[:200]
            hint = {
                401: "The API key was rejected. Check TUMBLR_API_KEY.",
                404: "Blog not found. Check TUMBLR_BLOG (just the blog name is fine). "
                "If the blog is hidden from logged-out visitors, the API can't see it.",
            }.get(r.status_code, "")
            sys.exit(f"Tumblr returned {r.status_code} ({msg}) for blog '{blog}'. {hint}")
        posts = r.json()["response"]["posts"]
        if not posts:
            break
        for p in posts:
            offset += 1
            if p.get("trail"):  # skip reblogs, keep only originals
                continue
            pid = str(p["id"])
            folder = ARCHIVE / pid
            if folder.exists():
                continue
            folder.mkdir()
            text_parts, images = [], []
            for block in p.get("content", []):
                if block["type"] == "text":
                    text_parts.append(block["text"])
                elif block["type"] == "image":
                    url = block["media"][0]["url"]  # largest version first
                    ext = url.split("?")[0].rsplit(".", 1)[-1][:4]
                    name = f"img{len(images) + 1}.{ext}"
                    (folder / name).write_bytes(requests.get(url, timeout=60).content)
                    images.append({"file": name, "alt": block.get("alt_text") or ""})
            meta = {
                "id": pid,
                "timestamp": p["timestamp"],
                "text": "\n\n".join(text_parts).strip(),
                "tags": p.get("tags", []),
                "images": images,
                "source": p.get("post_url"),
            }
            (folder / "post.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
            saved += 1
        print(f"...{offset} scanned, {saved} saved")
    print(f"Done. {saved} original posts archived in {ARCHIVE}/")


# ---------- POST ----------
def shrink(path: Path) -> bytes:
    from PIL import Image

    img = Image.open(path).convert("RGB")
    quality, scale = 90, 1.0
    while True:
        w, h = int(img.width * scale), int(img.height * scale)
        buf = io.BytesIO()
        img.resize((w, h)).save(buf, "JPEG", quality=quality)
        if buf.tell() <= MAX_IMG_BYTES or scale < 0.2:
            return buf.getvalue()
        quality = max(60, quality - 10)
        scale *= 0.85


def hard_split(s, limit):
    """Split on words; slice a single giant word if needed."""
    out, cur = [], ""
    for w in s.split():
        while len(w) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.append(w[:limit])
            w = w[limit:]
        if cur and len(cur) + 1 + len(w) > limit:
            out.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        out.append(cur)
    return out


def split_text(text, limit=MAX_CHARS - 10):
    """Break text into thread-sized chunks: paragraphs > sentences > words."""
    units = []  # (separator, piece)
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        if len(para) <= limit:
            parts = [para]
        else:
            parts = []
            for s in re.split(r"(?<=[.!?])\s+", para):
                parts += [s] if len(s) <= limit else hard_split(s, limit)
        for i, part in enumerate(parts):
            sep = "" if not units else ("\n\n" if i == 0 else " ")
            units.append((sep, part))
    chunks, cur = [], ""
    for sep, part in units:
        if cur and len(cur) + len(sep) + len(part) <= limit:
            cur += sep + part
        else:
            if cur:
                chunks.append(cur)
            cur = part
    if cur:
        chunks.append(cur)
    return chunks or [""]


def post_next():
    from atproto import Client, client_utils, models

    posted = set(json.loads(STATE.read_text())) if STATE.exists() else set()
    queue = []
    for f in ARCHIVE.glob("*/post.json"):
        m = json.loads(f.read_text())
        if m["id"] not in posted and (m["text"] or m["images"]):
            queue.append((m["timestamp"], f.parent, m))
    if not queue:
        print("Nothing left to post.")
        return
    _, folder, m = sorted(queue, key=lambda x: x[0])[0]  # oldest first

    chunks = split_text(m["text"])
    n = len(chunks)
    client = Client()
    client.login(os.environ["BSKY_HANDLE"], os.environ["BSKY_APP_PASSWORD"])
    imgs = m["images"][:4]  # Bluesky max 4, attached to the first post

    root = parent = None
    for i, chunk in enumerate(chunks):
        label = f" {i + 1}/{n}" if n > 1 else ""
        tb = client_utils.TextBuilder()
        tb.text(chunk + label)
        if i == n - 1:  # hashtags go on the last post if they fit
            used = len(chunk + label)
            for t in m["tags"]:
                tag = t.replace(" ", "").replace("#", "")
                if tag and used + len(tag) + 2 <= MAX_CHARS:
                    tb.text("\n" if used == len(chunk + label) else " ")
                    tb.tag(f"#{tag}", tag)
                    used += len(tag) + 2
        reply_to = (
            models.AppBskyFeedPost.ReplyRef(parent=parent, root=root) if parent else None
        )
        if i == 0 and imgs:
            resp = client.send_images(
                tb,
                images=[shrink(folder / im["file"]) for im in imgs],
                image_alts=[im["alt"] for im in imgs],
            )
        else:
            resp = client.send_post(tb, reply_to=reply_to)
        ref = models.create_strong_ref(resp)
        root = root or ref
        parent = ref

    posted.add(m["id"])
    STATE.write_text(json.dumps(sorted(posted)))
    print(f"Posted {m['id']} as {n} post(s)")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "export":
        export()
    elif cmd == "post":
        post_next()
    else:
        print("Usage: python tumblr_to_bsky.py [export|post]")
