"""Read-only: count how many Bluesky posts used each candidate tag in the last 7 days."""
import datetime, os, sys
from atproto import Client

TAGS = """art artsky myart artwork drawing illustration sketch sketchbook sketching doodle
traditionalart traditionaldrawing pencil pencildrawing pencilsketch coloredpencil colouredpencil
markers markerart alcoholmarkers copic fineliner ink inkdrawing penart ballpoint ballpointpen
oc originalcharacter ocart characterdesign design fanart nostalgia 2010s oldart throwback
artprogress artimprovement wip workinprogress fnaf fivenightsatfreddys fnaffanart mlp mylittlepony
mlpfanart brony zelda legendofzelda zeldafanart skylanders tangled rapunzel disneyfanart markiplier
yanderesimulator mirainikki yuno futurediary anime animeart animefanart genshin genshinimpact
genshinfanart refsheet referencesheet zombie monochrome ventart fairytale humanart noai humanmade
notai blinkies y2k pixelart sleepy dailysketch artjourney oldvsnew drawingprogress""".split()

since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
c = Client()
c.login(os.environ["BSKY_HANDLE"], os.environ["BSKY_APP_PASSWORD"])
CAP = 1500
rows = []
for t in TAGS:
    n, cursor = 0, None
    try:
        while n < CAP:
            r = c.app.bsky.feed.search_posts({"q": f"#{t}", "tag": [t], "since": since, "limit": 100, "cursor": cursor})
            n += len(r.posts)
            cursor = r.cursor
            if not cursor or not r.posts:
                break
    except Exception as e:
        print(f"{t}\tERR {e}", file=sys.stderr)
    rows.append((n, t))
    print(f"{t}\t{n}{'+' if n >= CAP else ''}", flush=True)
print("\nSORTED")
for n, t in sorted(rows, reverse=True):
    print(f"{n}{'+' if n >= CAP else ''}\t#{t}")
