"""Read-only: count Bluesky posts per candidate tag over the last 7 days and
save them to tag_popularity.json, which the poster uses to rank tags."""
import datetime, json, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from atproto import Client
from tumblr_to_bsky import TAG_RULES, POPULARITY

CAP = 2000  # stop counting past this; anything that high is "very popular"
since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
c = Client()
c.login(os.environ["BSKY_HANDLE"], os.environ["BSKY_APP_PASSWORD"])

old = json.loads(POPULARITY.read_text()).get("counts", {}) if POPULARITY.exists() else {}
counts = {}
for tag in sorted({r[1] for r in TAG_RULES}):
    n, cursor = 0, None
    try:
        while n < CAP:
            r = c.app.bsky.feed.search_posts({"q": f"#{tag}", "tag": [tag], "since": since, "limit": 100, "cursor": cursor})
            n += len(r.posts)
            cursor = r.cursor
            if not cursor or not r.posts:
                break
    except Exception as e:  # keep last week's number rather than a broken one
        print(f"#{tag}: lookup failed ({e}), keeping previous value")
        if tag in old:
            counts[tag] = old[tag]
        continue
    counts[tag] = n
    print(f"#{tag}: {n}{'+' if n >= CAP else ''}")

# --- activity by hour: art posts per UTC hour over the last 3 days ---
ACTIVITY_TAGS = ["art", "artsky", "fanart", "oc", "traditionalart"]
now = datetime.datetime.now(datetime.timezone.utc).replace(minute=0, second=0, microsecond=0)
hours = {h: 0 for h in range(24)}
fmt = "%Y-%m-%dT%H:%M:%SZ"
for back in range(1, 73):
    start = now - datetime.timedelta(hours=back)
    end = start + datetime.timedelta(hours=1)
    for tag in ACTIVITY_TAGS:
        n, cursor = 0, None
        try:
            while n < 500:
                r = c.app.bsky.feed.search_posts({"q": f"#{tag}", "tag": [tag], "since": start.strftime(fmt),
                                                   "until": end.strftime(fmt), "limit": 100, "cursor": cursor})
                n += len(r.posts)
                cursor = r.cursor
                if not cursor or not r.posts:
                    break
        except Exception as e:
            print(f"hour {start:%m-%d %H}h #{tag}: lookup failed ({e})")
        hours[start.hour] += n
print("art posts per UTC hour (3 days):", hours)
old_hours = json.loads(POPULARITY.read_text()).get("hours") if POPULARITY.exists() else None
if sum(hours.values()) == 0 and old_hours:
    hours = old_hours  # measurement failed, keep last week's

POPULARITY.write_text(json.dumps({"measured": datetime.date.today().isoformat(), "counts": counts, "hours": hours}, indent=2))
print(f"saved {len(counts)} tags to {POPULARITY}")
