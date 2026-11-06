# tumblr-to-bsky

Reposts my original Tumblr image posts to Bluesky, one per day, oldest first.
Text-only posts, reblogs and asks are never posted. Add a post id to `skip.json` to exclude it.
Long posts become numbered threads; images (max 4) go on the first post, tags on the last.

## Setup
Add these under **Settings → Secrets and variables → Actions**:

| Secret | Value |
|---|---|
| `TUMBLR_BLOG` | e.g. `yourname.tumblr.com` |
| `TUMBLR_API_KEY` | OAuth consumer key from https://www.tumblr.com/oauth/apps |
| `BSKY_HANDLE` | e.g. `yourname.bsky.social` |
| `BSKY_APP_PASSWORD` | bsky.app → Settings → Privacy and security → App passwords |

Then in the **Actions** tab:
1. Run **export-tumblr** once (fills `archive/`; it also re-runs every Monday for new posts).
2. Run **daily-bluesky-post** once to test. After that it posts every day at 16:00 UTC.

`posted.json` tracks what's been posted, so nothing repeats. Delete an entry from it to repost.
