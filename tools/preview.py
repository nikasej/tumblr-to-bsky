"""Render the remaining queue exactly as it would post (no network, nothing is posted).
Usage: python tools/preview.py [output.html]"""
import base64, datetime, glob, html, io, json, os, shutil, sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "queue-preview.html").resolve()
work = Path(tempfile.mkdtemp())
for name in ("archive", "watermark.png", "tumblr_to_bsky.py", "posted.json", "skip.json"):
    src = REPO / name
    if src.is_dir():
        shutil.copytree(src, work / name)
    elif src.exists():
        shutil.copy(src, work / name)
os.chdir(work)
sys.path.insert(0, str(work))
os.environ.setdefault("BSKY_HANDLE", "preview")
os.environ.setdefault("BSKY_APP_PASSWORD", "preview")

import atproto
from atproto import models
from PIL import Image
import tumblr_to_bsky as t

import types
from atproto_client.models.blob_ref import BlobRef, IpldLink
CID = "bafyreib2rxk3rh6kzwq4yqx7vuxkqcv7xwb4mgyrqhqo2yqk5x5tq2hspe"
calls, uploads, quotes = [], [], {}
class _R:
    def __init__(s, i): s.uri, s.cid = f"at://preview/{i}", CID
class _Fake:
    def login(s, *a): pass
    def upload_blob(s, b):
        uploads.append(b)
        return types.SimpleNamespace(blob=BlobRef(mime_type="image/jpeg", size=len(b), ref=IpldLink(link=CID)))
    def send_post(s, tb, reply_to=None, embed=None):
        imgs = list(uploads); uploads.clear()
        if embed is not None:
            rec = getattr(embed, "record", None)
            rec = getattr(rec, "record", rec)
            quotes[len(calls)] = getattr(rec, "uri", "")
        calls.append((tb, imgs)); return _R(len(calls))
    def send_images(s, tb, images, image_alts=None, reply_to=None): calls.append((tb, images)); return _R(len(calls))
    def get_author_feed(s, actor, cursor=None, filter=None, limit=None):
        return types.SimpleNamespace(feed=[], cursor=None)
atproto.Client = _Fake
models.create_strong_ref = lambda r: models.ComAtprotoRepoStrongRef.Main(uri=r.uri, cid=CID)

meta = {json.load(open(f))["id"]: json.load(open(f)) for f in glob.glob("archive/*/post.json")}

def thumb(b):
    im = Image.open(io.BytesIO(b)); im.thumbnail((560, 560)); o = io.BytesIO(); im.save(o, "JPEG", quality=75)
    return base64.b64encode(o.getvalue()).decode()

def rich(tb):
    bt = tb.build_text().encode(); out, pos = [], 0
    for f in sorted(tb.build_facets(), key=lambda f: f.index.byte_start):
        out.append(html.escape(bt[pos:f.index.byte_start].decode()))
        out.append(f'<span class="tag">{html.escape(bt[f.index.byte_start:f.index.byte_end].decode())}</span>')
        pos = f.index.byte_end
    out.append(html.escape(bt[pos:].decode()))
    return "".join(out).replace("\n", "<br>")

# step a fake clock hour by hour through the real schedule
import contextlib
clock = [datetime.datetime.now(datetime.timezone.utc).replace(minute=17, second=0, microsecond=0)]
t.berlin_now = lambda: clock[0].astimezone(t.TZ)
cards, n = [], 0
for _ in range(24 * 120):
    action, _why = t.plan()
    if not action:
        if not t.load_queue():
            break
        clock[0] += datetime.timedelta(hours=1)
        continue
    calls.clear(); quotes.clear()
    before = set(json.load(open("posted.json"))) if os.path.exists("posted.json") else set()
    with contextlib.redirect_stdout(io.StringIO()):
        t.post_next(action)
    kind = "add-on" if action == "extra" else "daily"
    when = t.berlin_now().strftime("%a %d %b, %H:%M")
    clock[0] += datetime.timedelta(hours=1)
    if not calls:
        break
    n += 1
    pid = (set(json.load(open("posted.json"))) - before).pop()
    d = datetime.date.fromtimestamp(meta[pid]["timestamp"]).strftime("%d %b %Y")
    parts = []
    orig = meta.get(meta[pid].get("reblog_of") or "", {})
    for k, (tb, imgs) in enumerate(calls):
        g = "".join(f'<img src="data:image/jpeg;base64,{thumb(b)}" alt="">' for b in imgs)
        cls = "imgs one" if len(imgs) == 1 else "imgs"
        block = f'<div class="{cls}">{g}</div>' if imgs else ""
        if k == 0 and meta[pid].get("reblog_of"):
            title = html.escape((orig.get("text") or "your earlier post").strip().split("\n")[0][:80])
            block += f'<div class="quote">↪ quotes your Bluesky post: <b>{title}</b></div>' 
        parts.append(f'<div class="post{" reply" if k else ""}"><div class="av"></div><div class="body"><div class="txt">{rich(tb)}</div>{block}</div></div>')
    s = "s" if len(calls) > 1 else ""
    cards.append(f'<section><header><b>{n}</b><span class="kind {kind}">{kind}</span><span>{when}</span><span class="cnt">{len(calls)} post{s}</span></header>{"".join(parts)}</section>')

CSS = """:root{--bg:#f3f5f8;--card:#fff;--ink:#0b0f14;--mute:#6f7b88;--line:#e3e8ee;--acc:#1083fe;--av:#d9a3bd}
@media (prefers-color-scheme:dark){:root{--bg:#0b0f14;--card:#161e27;--ink:#f1f3f5;--mute:#8b98a5;--line:#2a3541;--acc:#3d9bff;--av:#8a4e6c}}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,sans-serif}
main{max-width:600px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}.sub{color:var(--mute);margin:0 0 20px}
section{background:var(--card);border:1px solid var(--line);border-radius:14px;margin:0 0 16px;overflow:hidden}
header{display:flex;gap:10px;align-items:center;padding:10px 14px;border-bottom:1px solid var(--line);color:var(--mute);font-size:13px}
header b{color:#fff;background:var(--acc);border-radius:999px;min-width:28px;text-align:center;padding:1px 8px;font-size:14px}
.cnt{margin-left:auto}.kind{font-weight:600;text-transform:uppercase;font-size:11px;letter-spacing:.04em;padding:1px 7px;border-radius:6px;background:var(--line);color:var(--ink)}.kind.add-on{background:#f5d0e3;color:#7a1d4d}.post{display:flex;gap:10px;padding:12px 14px;position:relative}
.post.reply::before{content:"";position:absolute;left:33px;top:-12px;height:16px;border-left:2px solid var(--line)}
.av{flex:0 0 40px;height:40px;border-radius:50%;background:var(--av)}
.body{min-width:0;flex:1}.txt{overflow-wrap:anywhere}.tag{color:var(--acc)}
.imgs{display:grid;grid-template-columns:1fr 1fr;gap:4px;margin-top:8px}.imgs.one{grid-template-columns:1fr}
.quote{margin-top:8px;border:1px solid var(--line);border-radius:10px;padding:8px 10px;font-size:13px;color:var(--mute)}.quote b{color:var(--ink);font-weight:600}
.imgs img{width:100%;border-radius:10px;display:block;border:1px solid var(--line)}"""
OUT.write_text(f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bluesky Queue Preview</title><style>{CSS}</style></head><body><main><h1>Bluesky queue preview</h1>
<p class="sub">{n} posts with their planned Berlin times ({t.posts_per_day()} Day post(s) a day). Add-ons always go out before the next Day post, like on Tumblr. Images are shown exactly as they'll be uploaded, watermark included. Replies in a thread are stacked under their first post.</p>
{"".join(cards)}</main></body></html>""")
print(f"{n} posts -> {OUT}")
