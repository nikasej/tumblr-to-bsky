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
SKIP = Path("skip.json")
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
            # keep only your own originals: skip reblogs and answered asks
            is_reblog = p.get("trail") or p.get("reblogged_from_id") or p.get("parent_post_url")
            is_ask = any(l.get("type") == "ask" for l in p.get("layout", []))
            if is_reblog or is_ask:
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
WATERMARK = Path("watermark.png")
WM_WIDTH = 0.18    # watermark width as a share of the image's shorter side
WM_MARGIN = 0.025  # gap from the bottom-right edges, same unit
WM_FOUND = 0.6     # match score above which the image counts as already signed


def has_watermark(img, wm) -> bool:
    """Look for the signature anywhere in the image, at any size, by
    matching its silhouette against the pixels that share its pink colour."""
    import cv2
    import numpy as np

    alpha = np.array(wm.getchannel("A"))
    wm_hsv = cv2.cvtColor(np.array(wm.convert("RGB")), cv2.COLOR_RGB2HSV)[alpha > 200]
    hue = int(np.median(wm_hsv[:, 0]))
    hsv = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2HSV)
    h = hsv[..., 0].astype(int)
    dist = np.minimum(abs(h - hue), 180 - abs(h - hue))
    mask = ((dist <= 12) & (hsv[..., 1] > 70) & (hsv[..., 2] > 70)).astype(np.uint8) * 255
    if not mask.any():
        return False
    mask = cv2.GaussianBlur(mask, (3, 3), 0)
    H, W = mask.shape
    for frac in np.geomspace(0.04, 0.6, 30):
        w = int(min(H, W) * frac)
        h = int(w * alpha.shape[0] / alpha.shape[1])
        if w < 16 or h >= H or w >= W:
            continue
        tpl = (cv2.resize(alpha, (w, h), interpolation=cv2.INTER_AREA) > 127).astype(np.uint8) * 255
        score = np.nan_to_num(cv2.matchTemplate(mask, tpl, cv2.TM_CCOEFF_NORMED)).max()
        if score >= WM_FOUND:
            return True
    return False


def stamp(img):
    """Add the signature bottom-right unless it's already in the image."""
    from PIL import Image

    if not WATERMARK.exists():
        return img
    # leave tiny or banner-shaped images (blinkies, buttons) alone:
    # a signature would cover most of them
    if min(img.size) < 200 or max(img.size) / min(img.size) > 3:
        return img
    wm = Image.open(WATERMARK).convert("RGBA")
    wm = wm.crop(wm.getchannel("A").getbbox())
    if has_watermark(img, wm):
        return img
    short = min(img.size)
    w = max(40, int(short * WM_WIDTH))
    wm = wm.resize((w, int(w * wm.height / wm.width)), Image.LANCZOS)
    pad = int(short * WM_MARGIN)
    out = img.convert("RGBA")
    out.alpha_composite(wm, (out.width - wm.width - pad, out.height - wm.height - pad))
    return out.convert("RGB")


def shrink(path: Path) -> bytes:
    from PIL import Image

    img = stamp(Image.open(path).convert("RGB"))
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


# ---------- TAGS ----------
# Tumblr tags are never posted as-is. Each Bluesky tag below is only used when
# one of the post's Tumblr tags matches its pattern, so a traditional piece can
# never pick up e.g. #digitalart just because that tag is popular.
# Groups are in priority order; within a group the more popular tag wins.
# Popularity = Bluesky posts using the tag in the 7 days before 2026-11-06
# (tools/tag_stats.py). Tags used by fewer than 50 posts/week are left out.
TAG_RULES = [
    # (group, bluesky tag, posts/week, regex matched against each Tumblr tag)
    ("medium", "traditionalart", 1600, r"traditional"),
    ("subject", "oc", 1600, r"^oc$|original character"),
    ("subject", "fanart", 1600, r"fan ?art"),
    ("fandom", "zelda", 1577, r"zelda|\bloz\b|^link fanart$"),
    ("fandom", "anime", 1586, r"mirai nikki|yuno gasai|anime"),
    ("fandom", "mlp", 1268, r"^mlp|my little pony"),
    ("fandom", "genshinimpact", 936, r"genshin"),
    ("fandom", "fnaf", 563, r"fnaf|five ?nights"),
    ("technique", "sketch", 1597, r"sketch(es)?$|^sketch"),
    ("technique", "characterdesign", 1432, r"design|ref(erence)? sheet"),
    ("technique", "monochrome", 1266, r"monochrome"),
    ("technique", "ink", 777, r"fineliner|ballpoint|\bpen\b|bic pen"),
    ("technique", "sketchbook", 726, r"sketchbook"),
    ("technique", "pencil", 243, r"^(?!.*colou?red).*pencil"),
    ("technique", "coloredpencil", 104, r"colou?red pencil"),
    ("theme", "wip", 1595, r"^wip$|work in progress"),
    ("theme", "noai", 1484, r"fuck ai|no ?ai|human art"),
    ("theme", "nostalgia", 454, r"nostalgia"),
    ("theme", "zombie", 374, r"zombie"),
    ("theme", "throwback", 220, r"^old art$"),
    ("theme", "y2k", 158, r"y2k"),
    ("theme", "refsheet", 107, r"ref(erence)? sheet"),
    ("theme", "fairytale", 75, r"fairy ?tale|märchen|hänsel"),
    ("theme", "ventart", 51, r"vent art"),
]
GROUP_ORDER = ["medium", "subject", "fandom", "technique", "theme"]
MAX_TAGS = 5  # #art plus up to 4 specific tags


def bsky_tags(tumblr_tags):
    """Translate a post's Tumblr tags into a short list of popular Bluesky tags."""
    src = [t.lower().strip() for t in tumblr_tags]
    hits = {}
    for group, tag, pop, rx in TAG_RULES:
        if any(re.search(rx, t) for t in src):
            hits[tag] = (GROUP_ORDER.index(group), -pop)
    picked = sorted(hits, key=hits.get)
    # one per group first so a post gets variety, then fill by priority
    out, seen = [], set()
    for tag in picked:
        if hits[tag][0] not in seen:
            out.append(tag)
            seen.add(hits[tag][0])
    out += [t for t in picked if t not in out]
    return ["art"] + out[: MAX_TAGS - 1]


def post_next():
    from atproto import Client, client_utils, models

    posted = set(json.loads(STATE.read_text())) if STATE.exists() else set()
    # skip.json: list of Tumblr post ids that should never be posted
    skip = set(json.loads(SKIP.read_text())) if SKIP.exists() else set()
    queue = []
    for f in ARCHIVE.glob("*/post.json"):
        m = json.loads(f.read_text())
        # only posts with images; text-only posts are never reposted
        if m["id"] not in posted and m["id"] not in skip and m["images"]:
            queue.append((m["timestamp"], f.parent, m))
    if not queue:
        print("Nothing left to post.")
        return
    _, folder, m = sorted(queue, key=lambda x: x[0])[0]  # oldest first

    # "Day X (old art) - Month Year (age N)" header: always the opening line
    # of the first post, never split or dropped
    text = m["text"].strip()
    header, rest = "", text
    first, _, after = text.partition("\n")
    if re.match(r"^\s*day\s*\d+\b", first, re.I):
        header, rest = first.strip(), after.strip()
    chunks = split_text(rest) if rest else [""]
    if header:
        joined = f"{header}\n\n{chunks[0]}".strip()
        if len(joined) <= MAX_CHARS - 10:
            chunks[0] = joined
        else:  # header gets the first post to itself
            chunks.insert(0, header)
    # Bluesky allows 4 images per post: spread groups of 4 over the thread,
    # adding image-only replies if there are more groups than text chunks
    groups = [m["images"][k : k + 4] for k in range(0, len(m["images"]), 4)]
    chunks += [""] * max(0, len(groups) - len(chunks))
    n = len(chunks)
    client = Client()
    client.login(os.environ["BSKY_HANDLE"], os.environ["BSKY_APP_PASSWORD"])

    root = parent = None
    for i, chunk in enumerate(chunks):
        label = f" {i + 1}/{n}" if n > 1 else ""
        tb = client_utils.TextBuilder()
        tb.text((chunk + label).strip())
        if i == n - 1:  # hashtags go on the last post if they fit
            used = len(chunk + label)
            for tag in bsky_tags(m["tags"]):
                if tag and used + len(tag) + 2 <= MAX_CHARS:
                    tb.text("\n" if used == len(chunk + label) else " ")
                    tb.tag(f"#{tag}", tag)
                    used += len(tag) + 2
        reply_to = (
            models.AppBskyFeedPost.ReplyRef(parent=parent, root=root) if parent else None
        )
        if i < len(groups):
            resp = client.send_images(
                tb,
                images=[shrink(folder / im["file"]) for im in groups[i]],
                image_alts=[im["alt"] for im in groups[i]],
                reply_to=reply_to,
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
