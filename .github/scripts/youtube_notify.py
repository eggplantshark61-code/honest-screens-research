#!/usr/bin/env python3
"""
youtube_notify.py -- announce new Honest Screens YouTube uploads in Discord.

Polls the channel's public RSS feed, finds videos that have not been announced
yet, and posts each one to the #youtube webhook with an @everyone ping, the
video's own description, the video link, and a link back to the channel.

State lives in .github/state/youtube_posted.json (the list of video IDs already
announced). On the very first run -- or any run where that file is missing --
this SEEDS state from the current feed and posts nothing, so switching the
automation on can never spam the server with the existing back catalogue.

Driven by .github/workflows/youtube-notify.yml. No Discord bot token and no
always-on server required: a Discord webhook is just an HTTPS POST.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

REPO_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
STATE_PATH = os.path.join(REPO_ROOT, ".github", "state", "youtube_posted.json")

CHANNEL_ID = os.environ.get("CHANNEL_ID", "").strip()
CHANNEL_URL = os.environ.get("CHANNEL_URL", "").strip()
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_YOUTUBE", "").strip()
TEST_MODE = os.environ.get("TEST_MODE", "").strip().lower() == "true"
RESEED = os.environ.get("RESEED", "").strip().lower() == "true"

# Announce at most this many videos in one run. Anything over the cap is left
# unrecorded and picked up next run, so nothing is silently dropped -- a burst
# of uploads is just spread over a few cycles instead of mass-pinging at once.
MAX_POSTS_PER_RUN = 3
DESCRIPTION_LIMIT = 700


def fetch_feed():
    url = "https://www.youtube.com/feeds/videos.xml?channel_id=" + CHANNEL_ID
    request = urllib.request.Request(url, headers={"User-Agent": "HonestScreensBot/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def parse_entries(xml_bytes):
    """Return the feed's entries as dicts, oldest first."""
    root = ET.fromstring(xml_bytes)
    entries = []
    for node in root.findall("atom:entry", NS):
        video_id = (node.findtext("yt:videoId", "", NS) or "").strip()
        if not video_id:
            continue
        group = node.find("media:group", NS)
        description = ""
        if group is not None:
            description = (group.findtext("media:description", "", NS) or "").strip()
        entries.append(
            {
                "id": video_id,
                "title": (node.findtext("atom:title", "", NS) or "").strip(),
                "published": (node.findtext("atom:published", "", NS) or "").strip(),
                "description": description,
            }
        )
    entries.sort(key=lambda item: item["published"])
    return entries


BOILERPLATE_PREFIXES = (
    "website:",
    "join the discord",
    "discord:",
    "subscribe",
    "follow ",
    "disclaimer",
    "this video is for informational",
    "chapters:",
    "timestamps:",
)


def is_boilerplate(paragraph):
    """True for the trailing link / disclaimer / hashtag blocks the upload
    pipeline appends to every description."""
    stripped = paragraph.strip()
    if not stripped:
        return True
    if stripped.lower().startswith(BOILERPLATE_PREFIXES):
        return True
    words = stripped.split()
    if words and all(word.startswith("#") for word in words):
        return True
    if len(words) == 1 and stripped.startswith("http"):
        return True
    return False


def summarize(text, limit=DESCRIPTION_LIMIT):
    """Take the lead of a description -- the actual write-up -- and drop the
    boilerplate tail, then trim to a sentence or word boundary."""
    text = (text or "").strip()
    if not text:
        return ""
    kept = []
    for paragraph in re.split(r"\n\s*\n", text):
        if is_boilerplate(paragraph):
            break
        kept.append(paragraph.strip())
    # Fall back to the raw text if a description does not follow that shape.
    body = "\n\n".join(kept).strip() or text
    body = re.sub(r"\n{3,}", "\n\n", body)
    if len(body) <= limit:
        return body
    clipped = body[:limit]
    for marker in (". ", "! ", "? ", "\n"):
        cut = clipped.rfind(marker)
        if cut > limit * 0.5:
            return clipped[: cut + 1].strip() + " ..."
    cut = clipped.rfind(" ")
    return (clipped[:cut] if cut > 0 else clipped).strip() + " ..."


def load_state():
    if not os.path.exists(STATE_PATH):
        return None
    try:
        with open(STATE_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except (ValueError, OSError) as exc:
        print("::warning::Could not read the state file (" + str(exc) + "); treating this as a first run and reseeding.")
        return None
    ids = data.get("posted_video_ids")
    return list(ids) if isinstance(ids, list) else None


def save_state(video_ids):
    unique = []
    seen = set()
    for video_id in video_ids:
        if video_id not in seen:
            seen.add(video_id)
            unique.append(video_id)
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    payload = {
        "_comment": "Video IDs already announced in Discord. Managed automatically by .github/workflows/youtube-notify.yml -- hand-editing this can cause duplicate or missing announcements.",
        "posted_video_ids": unique[-200:],
    }
    with open(STATE_PATH, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")


def build_message(video):
    prefix = "[TEST] " if TEST_MODE else ""
    ping = "(test run - no ping)" if TEST_MODE else "@everyone"
    lines = [prefix + ping + " 📺 New video posted — " + video["title"]]
    description = summarize(video["description"])
    if description:
        lines.append("")
        lines.append(description)
    lines.append("")
    # Bare link so Discord unfurls the video player card.
    lines.append("https://youtu.be/" + video["id"])
    if CHANNEL_URL:
        # Angle brackets keep this one from unfurling a second card.
        lines.append("More research videos: <" + CHANNEL_URL + ">")
    return "\n".join(lines)


def post_to_discord(video):
    payload = {
        "content": build_message(video),
        # Required for @everyone to actually notify; without it Discord renders
        # the mention as plain text.
        "allowed_mentions": {"parse": [] if TEST_MODE else ["everyone"]},
    }
    request = urllib.request.Request(
        WEBHOOK,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "HonestScreensBot/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        print("[discord] HTTP " + str(response.status) + " -- " + video["id"] + " -- " + video["title"])


def main():
    if not CHANNEL_ID:
        sys.exit("CHANNEL_ID is not set in the workflow environment.")
    if not WEBHOOK:
        sys.exit(
            "DISCORD_WEBHOOK_YOUTUBE is not set. Create a webhook on the #youtube channel "
            "(Edit Channel -> Integrations -> Webhooks), save the URL as that repository "
            "secret under Settings -> Secrets and variables -> Actions, then re-run."
        )

    entries = parse_entries(fetch_feed())
    if not entries:
        print("Feed returned no entries; nothing to do.")
        return
    print("Feed has " + str(len(entries)) + " entries; newest is: " + entries[-1]["title"])

    known = load_state()

    if known is None or RESEED:
        reason = "a reseed was requested" if RESEED else "no state file exists yet (first run)"
        save_state([item["id"] for item in entries])
        print(
            "::notice::Seeded state with " + str(len(entries)) + " existing video(s) because "
            + reason + ". Nothing was announced. Uploads from here on will be."
        )
        return

    fresh = [item for item in entries if item["id"] not in known]
    if not fresh:
        print("No new videos since the last check.")
        return

    deferred = fresh[MAX_POSTS_PER_RUN:]
    fresh = fresh[:MAX_POSTS_PER_RUN]
    if deferred:
        print(
            "::warning::" + str(len(deferred)) + " additional new video(s) were NOT posted this run "
            "(safety cap of " + str(MAX_POSTS_PER_RUN) + "): "
            + ", ".join(item["id"] for item in deferred)
            + ". They stay unrecorded and will be announced on the next run."
        )

    posted = []
    for video in fresh:
        try:
            post_to_discord(video)
            posted.append(video["id"])
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            print("::error::Discord rejected " + video["id"] + ": HTTP " + str(exc.code) + " " + detail)
            break
        except urllib.error.URLError as exc:
            print("::error::Could not reach Discord for " + video["id"] + ": " + str(exc))
            break

    if not posted:
        print("Nothing was posted; leaving state unchanged.")
        return

    if TEST_MODE:
        print("::notice::Test mode -- state left unchanged, so these will be announced for real on the next scheduled run.")
        return

    save_state(known + posted)
    print("State updated: announced " + str(len(posted)) + " video(s).")


if __name__ == "__main__":
    main()
