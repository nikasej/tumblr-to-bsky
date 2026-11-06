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

POPULARITY.write_text(json.dumps({"measured": datetime.date.today().isoformat(), "counts": counts}, indent=2))
print(f"saved {len(counts)} tags to {POPULARITY}")
