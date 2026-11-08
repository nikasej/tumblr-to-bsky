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
import datetime
import random
from pathlib import Path

ARCHIVE = Path("archive")
STATE = Path("posted.json")
SKIP = Path("skip.json")
LAST_POST = Path("last_post.json")
BSKY_POSTS = Path("bsky_posts.json")  # Tumblr id -> Bluesky post it became
MAX_CHARS = 300          # Bluesky limit
MAX_IMG_BYTES = 950_000  # Bluesky image limit is ~1MB


# ---------- EXPORT ----------
def export():
    import requests

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
        resp = r.json()["response"]
        posts = resp["posts"]
        me = (resp.get("blog") or {}).get("name", "").lower()
        if not posts:
            break
        for p in posts:
            offset += 1
            # keep your own originals, plus reblogs of your own posts (those
            # become quote posts of the original on Bluesky); skip other
            # people's posts you reblogged and answered asks
            is_reblog = p.get("trail") or p.get("reblogged_from_id") or p.get("parent_post_url")
            is_ask = any(l.get("type") == "ask" for l in p.get("layout", []))
            reblog_of = None
            if is_reblog:
                trail = p.get("trail") or [{}]
                root_blog = ((trail[0].get("blog") or {}).get("name")
                             or p.get("reblogged_root_name") or "").lower()
                root_id = str((trail[0].get("post") or {}).get("id")
                              or p.get("reblogged_root_id") or "")
                if me and root_blog == me and root_id:
                    reblog_of = root_id
                else:
                    continue
            if is_ask:
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
                "reblog_of": reblog_of,  # Tumblr id of your own post this reblogs
            }
            (folder / "post.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
            saved += 1
        print(f"...{offset} scanned, {saved} saved")
    print(f"Done. {saved} original posts archived in {ARCHIVE}/")


# ---------- SCHEDULE ----------
# The workflow wakes up hourly. Times are planned in Berlin time, one Berlin
# day at a time:
#   - one daily post per day, at an hour picked at random (but fixed for the
#     day) from the busiest Bluesky art hours that fall between 12:00 and 23:59
#   - extras (non-"Day X" posts) go out the day after the Day post they
#     followed on Tumblr, earlier in the day, so the order matches Tumblr:
#     Day 4 in the evening, "born to be eepy" the next afternoon, then Day 5
# Activity per UTC hour is measured weekly by tools/tag_stats.py.
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")
PEAK_HOURS = 6           # choose the daily hour among this many busiest hours
DAILY_WINDOW = (12, 23)  # Berlin hours allowed for the daily post
EXTRA_START = (11, 13)   # first extra of the day goes out in this hour range
EXTRA_EARLIEST = 7       # extras never go out before this hour
DAILY_GAP = 3            # min hours between Day posts when posting several a day
EXTRA_SPACING = (2, 3)   # hours between extras
EXTRA_BEFORE_DAILY = 2   # extras stop this many hours before the daily post
DEFAULT_ACTIVITY = {h: (3 if 14 <= h <= 21 else 1) for h in range(24)}  # UTC


def berlin_now():
    return datetime.datetime.now(datetime.timezone.utc).astimezone(TZ)


def posts_per_day():
    """Day posts per day: repo variable POSTS_PER_DAY (GitHub > Settings >
    Secrets and variables > Actions > Variables), default 1."""
    try:
        return max(1, min(6, int(os.environ.get("POSTS_PER_DAY") or 1)))
    except ValueError:
        return 1


def todays_slots(day):
    """Sorted Berlin hours of the Day posts on `day`."""
    n = posts_per_day()
    if n == 1:
        return [todays_slot(day)]
    activity = dict(DEFAULT_ACTIVITY)
    if POPULARITY.exists():
        try:
            hours = json.loads(POPULARITY.read_text()).get("hours") or {}
            if hours:
                activity = {int(h): v for h, v in hours.items()}
        except ValueError:
            pass
    noon = datetime.datetime(day.year, day.month, day.day, 12, tzinfo=TZ)
    offset = int(noon.utcoffset().total_seconds() // 3600)
    local = {(h + offset) % 24: v for h, v in activity.items()}
    allowed = sorted(h for h in local if DAILY_WINDOW[0] <= h <= DAILY_WINDOW[1])
    rng = random.Random(f"daily-{day.isoformat()}-{n}")
    for gap in (DAILY_GAP, 2, 1):
        best = None
        for _ in range(300):
            # weighted sample without replacement, busier hours more likely
            pick = sorted(sorted(allowed, key=lambda h: -rng.random() ** (1 / max(local[h], 1)))[:n])
            if len(pick) == n and all(b - a >= gap for a, b in zip(pick, pick[1:])):
                score = sum(local[h] for h in pick)
                if best is None or score > best[0]:
                    best = (score, pick)
        if best:
            return best[1]
    return allowed[-n:]


def todays_slot(day):
    """Berlin hour of the daily post on `day` (a date)."""
    activity = dict(DEFAULT_ACTIVITY)
    if POPULARITY.exists():
        try:
            hours = json.loads(POPULARITY.read_text()).get("hours") or {}
            if hours:
                activity = {int(h): n for h, n in hours.items()}
        except ValueError:
            pass
    noon = datetime.datetime(day.year, day.month, day.day, 12, tzinfo=TZ)
    offset = int(noon.utcoffset().total_seconds() // 3600)
    local = {(h + offset) % 24: n for h, n in activity.items()}
    allowed = [h for h in local if DAILY_WINDOW[0] <= h <= DAILY_WINDOW[1]]
    top = sorted(allowed, key=local.get, reverse=True)[:PEAK_HOURS]
    rng = random.Random(f"daily-{day.isoformat()}")  # same pick all day
    return rng.choices(top, weights=[local[h] for h in top])[0]


DAY_RX = re.compile(r"^\s*day\s*\d+", re.I)
EXTRA_GAP = (2, 4)  # extras go out this many hours after the previous post


def is_day(m):
    """True for 'Day X (old art) - ...' series posts; everything else is an extra."""
    return bool(DAY_RX.match(m["text"]))


def load_queue():
    """Unposted, non-skipped image posts as (folder, meta), oldest first."""
    posted = set(json.loads(STATE.read_text())) if STATE.exists() else set()
    skip = set(json.loads(SKIP.read_text())) if SKIP.exists() else set()
    q = []
    for f in ARCHIVE.glob("*/post.json"):
        m = json.loads(f.read_text())
        # only posts with images; text-only posts are never reposted
        if m["id"] not in posted and m["id"] not in skip and m["images"]:
            q.append((f.parent, m))
    return sorted(q, key=lambda x: x[1]["timestamp"])


def load_state():
    st = json.loads(LAST_POST.read_text()) if LAST_POST.exists() else {}
    st.setdefault("pending", [])
    return st


def plan(now=None):
    """What to post right now: 'extra', 'daily' or None, plus a reason.

    Add-ons always go out before the next Day post, so the order matches
    Tumblr: a Day post waits until the add-ons before it are out."""
    now = (now or berlin_now()).astimezone(TZ)
    today = now.date().isoformat()
    queue = load_queue()
    if not queue:
        return None, "Queue is empty."
    st = load_state()
    waiting = {m["id"] for _, m in queue}
    pending = [i for i in st["pending"] if i in waiting]
    slots = todays_slots(now.date())
    done = st.get("daily_count", 1) if st.get("date") == today else 0
    if done >= len(slots):
        return None, f"All {len(slots)} Day post(s) done today."
    slot = slots[done]

    if pending:
        k = len(pending)
        last = datetime.datetime.fromisoformat(st["last_time"]).astimezone(TZ) if st.get("last_time") else None
        if last and last.date() == now.date():
            # spread the rest evenly between the last post and the next Day slot
            step = max(1, min(EXTRA_SPACING[1], (slot - last.hour) // (k + 1)))
            next_hour = last.hour + step
        else:
            rng = random.Random(f"extra-{today}-{k}")
            start = rng.randint(*EXTRA_START)
            # leave room for all of them, an hour or more apart, before the slot
            next_hour = max(EXTRA_EARLIEST, min(start, slot - EXTRA_BEFORE_DAILY - (k - 1) * 2, slot - k))
        if now.hour >= next_hour:
            return "extra", f"Add-on due ({next_hour:02d}:00 Berlin), {k} before the next Day post."
        return None, f"Next add-on at {next_hour:02d}:00 Berlin, Day post at {slot:02d}:00 or right after the add-ons."
    if now.hour < slot:
        return None, f"Next Day post today at {slot:02d}:00 Berlin ({done + 1} of {len(slots)})."
    if done and st.get("last_day_time"):
        # catching up on missed slots: still keep Day posts 2h+ apart
        last_day = datetime.datetime.fromisoformat(st["last_day_time"]).astimezone(TZ)
        if now - last_day < datetime.timedelta(hours=2):
            return None, "Waiting so Day posts stay 2+ hours apart."
    return "daily", f"Day post due ({slot:02d}:00 Berlin slot, {done + 1} of {len(slots)})."


def due():
    """Print the action to take now ('daily'/'extra') on stdout, exit 0;
    exit 1 if nothing is due. Uses only the standard library."""
    action, why = plan()
    print(why, file=sys.stderr)
    if action:
        print(action)
        return 0
    return 1


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


WM_QUIET = 0.06    # max share of "busy" pixels allowed under the signature


def busy_map(img):
    """0..1 map of where something is drawn/written (edges, coloured marks,
    dark pencil lines), computed at ~600px for speed. Returns (map, scale)."""
    import cv2
    import numpy as np

    a = np.array(img.convert("RGB"))
    h, w = a.shape[:2]
    s = min(1.0, 600 / max(h, w))
    a = cv2.resize(a, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA)
    g = cv2.cvtColor(a, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(cv2.GaussianBlur(g, (3, 3), 0), 30, 90) / 255.0
    hsv = cv2.cvtColor(a, cv2.COLOR_RGB2HSV)
    colour = ((hsv[..., 1] > 60) & (hsv[..., 2] > 40)).astype(float)
    # faint pencil: anything noticeably darker than the paper around it
    paper = cv2.medianBlur(g, 31).astype(int)
    faint = ((paper - g.astype(int)) > 14).astype(float)
    b = np.maximum(np.maximum(edges, faint), colour)
    # a line "occupies" the space right around it too
    k = max(5, int(min(h, w) * s / 60)) | 1
    return cv2.dilate(b, np.ones((int(k), int(k)))), s


def quiet_spot(img, wm_ratio):
    """Pick where the signature goes: bottom-right if that's empty enough,
    otherwise the emptiest spot, preferring places near bottom-right, and
    trying smaller sizes before settling. Returns (x, y, width) in pixels."""
    import cv2
    import numpy as np

    b, s = busy_map(img)
    H, W = b.shape
    short = min(H, W)
    ii = cv2.integral(b)  # summed-area table: fast box sums

    def score(x, y, w, h):
        return (ii[y + h, x + w] - ii[y, x + w] - ii[y + h, x] + ii[y, x]) / (w * h)

    best = None
    for frac in (WM_WIDTH, WM_WIDTH * 0.8, WM_WIDTH * 0.65):
        w = max(8, int(short * frac))
        h = max(8, int(w * wm_ratio))
        pad = max(1, int(short * WM_MARGIN))
        if w + 2 * pad >= W or h + 2 * pad >= H:
            continue
        bx, by = W - w - pad, H - h - pad
        if score(bx, by, w, h) <= WM_QUIET:
            return int(bx / s), int(by / s), int(w / s)
        step = max(2, short // 40)
        for y in range(pad, H - h - pad + 1, step):
            for x in range(pad, W - w - pad + 1, step):
                # small penalty for drifting away from the bottom-right corner
                drift = ((bx - x) / W + (by - y) / H) * 0.02
                sc = score(x, y, w, h) + drift
                if best is None or sc < best[0]:
                    best = (sc, x, y, w)
        if best and best[0] <= WM_QUIET:
            break
    if best is None:
        return None
    _, x, y, w = best
    return int(x / s), int(y / s), int(w / s)


def stamp(img):
    """Add the signature unless it's already in the image: bottom-right when
    that corner is empty, otherwise the nearest quiet spot."""
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
    spot = quiet_spot(img, wm.height / wm.width)
    if spot is None:
        return img
    x, y, w = spot
    wm = wm.resize((w, max(1, int(w * wm.height / wm.width))), Image.LANCZOS)
    out = img.convert("RGBA")
    out.alpha_composite(wm, (min(x, out.width - wm.width), min(y, out.height - wm.height)))
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
# the post's own Tumblr tags (or, for materials, its caption) match its
# pattern, so a traditional piece can never pick up e.g. #digitalart just
# because that tag is popular. Popularity only decides between accurate tags.
#
# Live popularity comes from tag_popularity.json (Bluesky posts per tag over
# the last 7 days), refreshed weekly by tools/tag_stats.py. The numbers below
# are the fallback snapshot from 2026-11-06. Tags under MIN_WEEKLY are skipped.
TAG_RULES = [
    # (group, bluesky tag, fallback posts/week, regex on Tumblr tags, regex on caption or None)
    ("medium", "traditionalart", 1600, r"traditional", None),
    ("medium", "digitalart", 1000, r"^digital", r"\bdigital (art|version|drawing|painting|piece)\b"),
    ("subject", "oc", 1600, r"^oc$|original character", None),
    ("subject", "fanart", 1600, r"fan ?art", None),
    ("fandom", "zelda", 1577, r"zelda|\bloz\b|^link fanart$", None),
    ("fandom", "anime", 1586, r"mirai nikki|yuno gasai|anime", None),
    ("fandom", "mlp", 1268, r"^mlp|my little pony", None),
    ("fandom", "genshinimpact", 936, r"genshin", None),
    ("fandom", "fnaf", 563, r"fnaf|five ?nights", None),
    ("fandom", "skylanders", 20, r"skylanders", None),
    ("fandom", "tangled", 19, r"tangled|rapunzel", None),
    ("fandom", "markiplier", 20, r"markiplier", None),
    ("material", "coloredpencil", 104, r"colou?red pencil", r"\bcolou?red pencils?\b"),
    ("material", "pencil", 243, r"^(?!.*colou?red).*pencil", r"(?<!colored )(?<!coloured )\bpencils?\b"),
    ("material", "markers", 36, r"marker", r"\bmarkers?\b"),
    ("technique", "sketch", 1597, r"sketch(es)?$|^sketch", None),
    ("technique", "characterdesign", 1432, r"design|ref(erence)? sheet", None),
    ("technique", "monochrome", 1266, r"monochrome", None),
    ("technique", "sketchbook", 726, r"sketchbook", r"\bsketchbook\b"),
    ("theme", "wip", 1595, r"^wip$|work in progress", None),
    ("theme", "noai", 1484, r"fuck ai|no ?ai|human art", None),
    ("theme", "nostalgia", 454, r"nostalgia", None),
    ("theme", "zombie", 374, r"zombie", None),
    ("theme", "throwback", 220, r"^old art$", None),
    ("theme", "y2k", 158, r"y2k", None),
    ("theme", "refsheet", 107, r"ref(erence)? sheet", None),
    ("theme", "fairytale", 75, r"fairy ?tale|märchen|hänsel", None),
    ("theme", "ventart", 51, r"vent art", None),
    ("theme", "artprogress", 22, r"art (progress|improvement|development)", None),
]
GROUP_ORDER = ["medium", "subject", "fandom", "material", "technique", "theme"]
MAX_TAGS = 5      # #art plus up to 4 specific tags
MIN_WEEKLY = 50   # skip tags almost nobody on Bluesky uses
POPULARITY = Path("tag_popularity.json")


def popularity():
    live = {}
    if POPULARITY.exists():
        try:
            live = json.loads(POPULARITY.read_text()).get("counts", {})
        except ValueError:
            pass
    return {tag: live.get(tag, pop) for _, tag, pop, _, _ in TAG_RULES}


def bsky_tags(tumblr_tags, caption=""):
    """Translate a post's Tumblr tags into a short list of popular Bluesky tags."""
    src = [t.lower().strip() for t in tumblr_tags]
    cap = caption.lower()
    pop = popularity()
    hits = {}
    for group, tag, _, rx, cap_rx in TAG_RULES:
        if pop[tag] < MIN_WEEKLY:
            continue
        if any(re.search(rx, t) for t in src) or (cap_rx and re.search(cap_rx, cap)):
            hits[tag] = (GROUP_ORDER.index(group), -pop[tag])
    picked = sorted(hits, key=hits.get)
    # one per group first so a post gets variety, then fill by priority
    out, seen = [], set()
    for tag in picked:
        if hits[tag][0] not in seen:
            out.append(tag)
            seen.add(hits[tag][0])
    out += [t for t in picked if t not in out]
    return ["art"] + out[: MAX_TAGS - 1]


# ---------- SELF-REBLOGS ----------
# A Tumblr reblog of your own post (usually with a new drawing added) becomes
# a Bluesky quote post of the post the original turned into, so it reads as
# "here's an update to this". If the original never made it to Bluesky
# (text-only, skipped, or can't be found) it's posted as a normal post.


def remember(tumblr_id, ref):
    links = json.loads(BSKY_POSTS.read_text()) if BSKY_POSTS.exists() else {}
    links[tumblr_id] = {"uri": ref.uri, "cid": ref.cid}
    BSKY_POSTS.write_text(json.dumps(links, indent=2))


def original_ref(client, tumblr_id):
    """Strong ref to the Bluesky post made from Tumblr post `tumblr_id`, or None."""
    from atproto import models

    links = json.loads(BSKY_POSTS.read_text()) if BSKY_POSTS.exists() else {}
    if tumblr_id in links:
        return models.ComAtprotoRepoStrongRef.Main(**links[tumblr_id])
    posted = set(json.loads(STATE.read_text())) if STATE.exists() else set()
    f = ARCHIVE / tumblr_id / "post.json"
    if tumblr_id not in posted or not f.exists():
        return None
    # posted before links were saved: find it on the profile by its opening
    # text (ignoring the Day number, which Bluesky counts on its own)
    def norm(t):
        t = re.sub(r"^\s*day\s*\d+(\s+part\s+\d+)?", "", t or "", flags=re.I)
        return " ".join(t.split())
    opening = norm(json.loads(f.read_text())["text"])[:100]
    if not opening:
        return None
    cursor = None
    for _ in range(10):  # up to ~1000 recent posts
        feed = client.get_author_feed(actor=os.environ["BSKY_HANDLE"], cursor=cursor,
                                      filter="posts_no_replies", limit=100)
        for item in feed.feed:
            rec = item.post.record
            if getattr(rec, "reply", None) is None and norm(rec.text).startswith(opening):
                ref = models.ComAtprotoRepoStrongRef.Main(uri=item.post.uri, cid=item.post.cid)
                remember(tumblr_id, ref)
                return ref
        cursor = feed.cursor
        if not cursor:
            break
    return None


def send(client, tb, images=(), alts=(), reply_to=None, quote=None):
    """Post text with up to 4 images, optionally as a reply and/or quoting a post."""
    from atproto import models

    if quote is None:
        if images:
            return client.send_images(tb, images=list(images), image_alts=list(alts), reply_to=reply_to)
        return client.send_post(tb, reply_to=reply_to)
    record = models.AppBskyEmbedRecord.Main(record=quote)
    if images:
        imgs = [models.AppBskyEmbedImages.Image(alt=a or "", image=client.upload_blob(b).blob)
                for b, a in zip(images, alts)]
        embed = models.AppBskyEmbedRecordWithMedia.Main(
            record=record, media=models.AppBskyEmbedImages.Main(images=imgs))
    else:
        embed = record
    return client.send_post(tb, reply_to=reply_to, embed=embed)


def post_next(kind="auto"):
    """Post one item. kind: 'daily', 'extra', or 'auto' (extra if one is
    pending, else daily; used by the preview)."""
    from atproto import Client, client_utils, models

    queue = load_queue()
    if not queue:
        print("Nothing left to post.")
        return
    st = load_state()
    byid = {m["id"]: (f, m) for f, m in queue}
    pending = [i for i in st["pending"] if i in byid]
    if kind == "auto":
        kind = "extra" if pending else "daily"
    if kind == "extra" and pending:
        folder, m = byid[pending.pop(0)]
    else:
        kind = "daily"
        days = [x for x in queue if is_day(x[1])]
        folder, m = days[0] if days else queue[0]
        if is_day(m):
            # extras up to the next Day post belong to this one
            later = [d[1]["timestamp"] for d in days[1:]]
            cutoff = later[0] if later else float("inf")
            pending += [x[1]["id"] for x in queue
                        if not is_day(x[1]) and x[1]["timestamp"] < cutoff and x[1]["id"] not in pending]

    # "Day X (old art) - Month Year (age N)" header: always the opening line
    # of the first post, never split or dropped
    text = m["text"].strip()
    header, rest = "", text
    first, _, after = text.partition("\n")
    if re.match(r"^\s*day\s*\d+\b", first, re.I):
        header, rest = first.strip(), after.strip()
    today = berlin_now().date().isoformat()
    day_no = day_part = None
    if kind == "daily" and header:
        # Bluesky's own Day count: +1 only when the calendar day changes; a
        # second Day post on the same day becomes "Day N part 2", and so on
        if "day_no" in st:
            prev_no, prev_date, prev_part = st["day_no"], st.get("day_date"), st.get("day_part", 1)
        else:  # first run with numbering: one Day post per day so far
            posted_ids = set(json.loads(STATE.read_text())) if STATE.exists() else set()
            prev_no = sum(1 for f in ARCHIVE.glob("*/post.json")
                          if f.parent.name in posted_ids and is_day(json.loads(f.read_text())))
            prev_date, prev_part = st.get("date"), 1
        if prev_date == today and prev_no:
            day_no, day_part = prev_no, prev_part + 1
        else:
            day_no, day_part = prev_no + 1, 1
        label = f"Day {day_no}" + (f" part {day_part}" if day_part > 1 else "")
        header = re.sub(r"^\s*day\s*\d+", label, header, count=1, flags=re.I)
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

    quote = original_ref(client, m["reblog_of"]) if m.get("reblog_of") else None
    root = parent = None
    for i, chunk in enumerate(chunks):
        label = f" {i + 1}/{n}" if n > 1 else ""
        tb = client_utils.TextBuilder()
        tb.text((chunk + label).strip())
        if i == n - 1:  # hashtags go on the last post if they fit
            used = len(chunk + label)
            for tag in bsky_tags(m["tags"], m["text"]):
                if tag and used + len(tag) + 2 <= MAX_CHARS:
                    tb.text("\n" if used == len(chunk + label) else " ")
                    tb.tag(f"#{tag}", tag)
                    used += len(tag) + 2
        reply_to = (
            models.AppBskyFeedPost.ReplyRef(parent=parent, root=root) if parent else None
        )
        group = groups[i] if i < len(groups) else []
        resp = send(client, tb,
                    images=[shrink(folder / im["file"]) for im in group],
                    alts=[im["alt"] for im in group],
                    reply_to=reply_to,
                    quote=quote if i == 0 else None)
        ref = models.create_strong_ref(resp)
        root = root or ref
        parent = ref

    remember(m["id"], root)
    posted = set(json.loads(STATE.read_text())) if STATE.exists() else set()
    posted.add(m["id"])
    STATE.write_text(json.dumps(sorted(posted)))
    now = berlin_now()
    if kind == "daily":
        st["daily_count"] = st.get("daily_count", 1) + 1 if st.get("date") == now.date().isoformat() else 1
        st["date"] = now.date().isoformat()
        st["last_day_time"] = now.isoformat(timespec="seconds")
    if day_no is not None:
        st.update(day_no=day_no, day_part=day_part, day_date=today)
    st.update(id=m["id"], last_time=now.isoformat(timespec="seconds"), pending=pending)
    LAST_POST.write_text(json.dumps(st, indent=2))
    print(f"Posted {kind} {m['id']} as {n} post(s)" + (" quoting the original" if quote else ""))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "export":
        export()
    elif cmd == "post":
        post_next(sys.argv[2] if len(sys.argv) > 2 else "auto")
    elif cmd == "due":
        sys.exit(due())
    elif cmd == "slots":  # show the next week's daily posting hours
        for d in range(7):
            day = berlin_now().date() + datetime.timedelta(days=d)
            print(day, f"daily post at {todays_slot(day):02d}:xx Berlin")
    else:
        print("Usage: python tumblr_to_bsky.py [export|post|due|slots]")
