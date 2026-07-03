#!/usr/bin/env python3
"""
Archive a Meetup.com group to local JSON + CSV + photos before requesting closure.

Pulls everything an organizer / Pro admin can reach via Meetup's GraphQL API
(endpoint: https://api.meetup.com/gql-ext) into a lossless local archive:

  archive/<urlname>/
    group.json                 group metadata, settings, stats
    members.json / .csv        full roster with join dates + roles
    events.json                past (and any active) events
    events.csv                 flat event summary
    rsvps/<eventId>.json       attendee / RSVP list per event
    comments/<eventId>.json    event discussion threads
    photos/<eventId>/...       downloaded photo binaries
    _meta.json                 run metadata (counts, timestamp, schema endpoint)

Auth (two options):
  1. MEETUP_ACCESS_TOKEN=<token>     -- simplest; paste a token you already have.
  2. JWT server flow (no browser):  set
        MEETUP_CLIENT_KEY    (OAuth consumer / client key)
        MEETUP_SIGNING_KEY_ID
        MEETUP_MEMBER_ID     (your authorized member id)
        MEETUP_PRIVATE_KEY_FILE  (path to the RSA private key .pem)
     The script signs a JWT and exchanges it at
     https://secure.meetup.com/oauth2/access for an access token.

See README.md for how to create the OAuth client and key.

The schema field names below were verified by live introspection of
api.meetup.com/gql-ext. If Meetup changes the schema, re-run introspection
(see README) and adjust the query constants.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterator

import requests

GQL_ENDPOINT = "https://api.meetup.com/gql-ext"
OAUTH_TOKEN_URL = "https://secure.meetup.com/oauth2/access"
WEB_BASE = "https://www.meetup.com"
PAGE_SIZE = 50
REQUEST_TIMEOUT = 60


def album_page_url(urlname: str, album_id: str) -> str:
    """Clickable album page (canonical Meetup path). Requires being logged in."""
    return f"{WEB_BASE}/{urlname}/photos/{album_id}/"


def all_photos_url(urlname: str) -> str:
    """The group's all-albums landing page — lists everything once logged in."""
    return f"{WEB_BASE}/{urlname}/photos/"


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def get_access_token() -> str:
    token = os.environ.get("MEETUP_ACCESS_TOKEN")
    if token:
        return token.strip()

    client_key = os.environ.get("MEETUP_CLIENT_KEY")
    signing_key_id = os.environ.get("MEETUP_SIGNING_KEY_ID")
    member_id = os.environ.get("MEETUP_MEMBER_ID")
    key_file = os.environ.get("MEETUP_PRIVATE_KEY_FILE")

    if not all([client_key, signing_key_id, member_id, key_file]):
        sys.exit(
            "No auth configured. Set MEETUP_ACCESS_TOKEN, or the JWT-flow vars "
            "(MEETUP_CLIENT_KEY, MEETUP_SIGNING_KEY_ID, MEETUP_MEMBER_ID, "
            "MEETUP_PRIVATE_KEY_FILE). See README.md."
        )

    try:
        import jwt  # PyJWT
    except ImportError:
        sys.exit("JWT flow needs PyJWT[crypto]: pip install -r requirements.txt")

    private_key = Path(key_file).read_text()
    now = int(time.time())
    assertion = jwt.encode(
        {
            "sub": member_id,
            "iss": client_key,
            "aud": "api.meetup.com",
            "exp": now + 120,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": signing_key_id},
    )
    resp = requests.post(
        OAUTH_TOKEN_URL,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# --------------------------------------------------------------------------- #
# GraphQL client with pagination + backoff
# --------------------------------------------------------------------------- #
class MeetupClient:
    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
        )

    def query(self, query: str, variables: dict[str, Any] | None = None) -> dict:
        for attempt in range(6):
            resp = self.session.post(
                GQL_ENDPOINT,
                json={"query": query, "variables": variables or {}},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = min(2 ** attempt, 30)
                print(f"  rate/err {resp.status_code}; backing off {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("errors"):
                # Surface but don't necessarily abort; partial data may still come back.
                print(f"  GraphQL errors: {json.dumps(payload['errors'])[:500]}", file=sys.stderr)
            return payload.get("data") or {}
        sys.exit("Gave up after repeated rate-limit/5xx responses.")

    def paginate(
        self, query: str, variables: dict[str, Any], path: list[str]
    ) -> Iterator[dict]:
        """Yield every `edges[].node` across pages.

        `path` walks the response down to the *Connection object (the thing that
        has edges + pageInfo), e.g. ["groupByUrlname", "memberships"].
        """
        after = None
        while True:
            data = self.query(query, {**variables, "after": after})
            conn = data
            for key in path:
                conn = (conn or {}).get(key)
            if not conn:
                return
            for edge in conn.get("edges") or []:
                if edge and edge.get("node"):
                    yield edge
            page = conn.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                return
            after = page.get("endCursor")
            time.sleep(0.2)  # be polite to the Pro API


# --------------------------------------------------------------------------- #
# Queries (field names verified via live introspection)
# --------------------------------------------------------------------------- #
GROUP_QUERY = """
query Group($urlname: String!) {
  groupByUrlname(urlname: $urlname) {
    id name urlname link description welcomeBlurb
    status isPrivate joinMode foundedDate proJoinDate
    city state zip country timezone lat lon
    customMemberLabel emailAnnounceAddress
    keyGroupPhoto { id baseUrl highResUrl }
    organizer { id name email memberUrl }
    stats { memberCounts { all leadership pending pendingPayment banned } }
    topicCategory { id name }
    featuredEventPhotos(first: 200) {
      totalCount
      edges { node { id baseUrl highResUrl } }
    }
  }
}
"""

MEMBERS_QUERY = """
query Members($urlname: String!, $first: Int!, $after: String) {
  groupByUrlname(urlname: $urlname) {
    memberships(first: $first, after: $after) {
      totalCount
      pageInfo { hasNextPage endCursor }
      edges {
        metadata { joinTime lastAccessTime role status }
        node {
          id name email username memberUrl
          city state country zip bio
          memberPhoto { id baseUrl highResUrl }
        }
      }
    }
  }
}
"""

EVENTS_QUERY = """
query Events($urlname: String!, $first: Int!, $after: String, $status: EventStatus) {
  groupByUrlname(urlname: $urlname) {
    events(first: $first, after: $after, status: $status, sort: ASC) {
      totalCount
      pageInfo { hasNextPage endCursor }
      edges {
        node {
          id title eventUrl status eventType
          dateTime endTime duration createdTime
          description howToFindUs
          venue { name address city state country lat lon }
          eventHosts { name member { id name } }
          rsvps { yesCount noCount waitlistCount attendedCount totalCount }
          displayPhoto { id baseUrl highResUrl }
          featuredEventPhoto { id baseUrl highResUrl }
          photoAlbum { id photoCount title }
        }
      }
    }
  }
}
"""

EVENT_RSVPS_QUERY = """
query EventRsvps($eventId: ID!, $first: Int!, $after: String) {
  event(id: $eventId) {
    rsvps(first: $first, after: $after) {
      totalCount yesCount noCount waitlistCount attendedCount
      pageInfo { hasNextPage endCursor }
      edges {
        node {
          id status guestsCount isHost updated
          member { id name email memberUrl }
        }
      }
    }
  }
}
"""

EVENT_COMMENTS_QUERY = """
query EventComments($eventId: ID!, $first: Int!, $after: String) {
  event(id: $eventId) {
    comments(first: $first, after: $after) {
      pageInfo { hasNextPage endCursor }
      edges { node { id text created member { id name } } }
    }
  }
}
"""

# NOTE: api.meetup.com/gql-ext does not expose a full per-event photo-album
# enumeration (EventPhotoAlbum only returns id/photoCount/title). We capture the
# photos the API *does* surface: each event's displayPhoto + featuredEventPhoto
# (from EVENTS_QUERY) and the group's featuredEventPhotos. Full albums, if any,
# must be saved manually from the web UI while logged in.


# --------------------------------------------------------------------------- #
# Archive routines
# --------------------------------------------------------------------------- #
def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False))


def _best_url(photo: dict | None) -> str | None:
    if not photo:
        return None
    return photo.get("highResUrl") or photo.get("standardUrl") or photo.get("baseUrl")


def _md_cell(text: str) -> str:
    """Make a string safe for a single markdown table cell."""
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").strip()


def write_photo_manifests(
    outdir: Path, urlname: str, group: dict, events: list[dict]
) -> tuple[int, int]:
    """Write two clickable manifests:

      photo_urls.csv  -- direct CDN image URLs for every photo the API exposes
                         (group + per-event display/featured). These download
                         directly when clicked.
      albums.md       -- markdown table of event photo albums, with clickable
                         album-page and event links. Open each album (logged in)
                         to bulk-download the photos, since the API can't
                         enumerate album contents.
    """
    # --- direct, downloadable photo URLs -----------------------------------
    rows: list[tuple] = []
    key = group.get("keyGroupPhoto")
    key_id = key.get("id") if key else None
    if key:
        rows.append(("group", group.get("urlname"), group.get("name"),
                     key.get("id"), _best_url(key)))
    for edge in ((group.get("featuredEventPhotos") or {}).get("edges") or []):
        n = edge.get("node") or {}
        rows.append(("group-featured", group.get("urlname"), group.get("name"),
                     n.get("id"), _best_url(n)))
    for e in events:
        for kind in ("displayPhoto", "featuredEventPhoto"):
            p = e.get(kind)
            # Skip the group key photo returned as an event's fallback displayPhoto.
            if p and _best_url(p) and p.get("id") != key_id:
                rows.append((f"event-{kind}", e.get("id"), e.get("title"),
                             p.get("id"), _best_url(p)))

    with (outdir / "photo_urls.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["source", "ownerId", "ownerTitle", "photoId", "url"])
        w.writerows(rows)

    # --- album pages for manual bulk download (markdown) -------------------
    albums = []
    for e in events:
        album = e.get("photoAlbum") or {}
        count = album.get("photoCount") or 0
        if album.get("id") and count:
            albums.append((e, album, count))
    # Sort newest-first for readability.
    albums.sort(key=lambda x: x[0].get("dateTime") or "", reverse=True)

    group_name = group.get("name") or urlname
    lines = [
        f"# Photo albums — {group_name} (`{urlname}`)",
        "",
        f"{len(albums)} event albums with photos. Open each **Album** link "
        "**while logged in to Meetup** to download the photos — the API can't "
        "enumerate album contents, so this is the manual step.",
        "",
        f"**[All albums (group landing page)]({all_photos_url(urlname)})**",
        "",
        "| # | Date | Event | Photos | Album | Event page |",
        "|--:|------|-------|-------:|-------|-----------|",
    ]
    for i, (e, album, count) in enumerate(albums, 1):
        date = (e.get("dateTime") or "")[:10]
        title = _md_cell(e.get("title") or "(untitled)")
        album_url = album_page_url(urlname, album["id"])
        event_url = e.get("eventUrl") or ""
        event_link = f"[event]({event_url})" if event_url else ""
        lines.append(
            f"| {i} | {date} | {title} | {count} | [album]({album_url}) | {event_link} |"
        )
    lines.append("")
    (outdir / "albums.md").write_text("\n".join(lines))

    print(f"    {len(rows)} direct photo URLs → photo_urls.csv")
    print(f"    {len(albums)} albums for manual download → albums.md")
    return len(rows), len(albums)


def archive_group(client: MeetupClient, urlname: str, outdir: Path) -> dict:
    print("• group metadata")
    data = client.query(GROUP_QUERY, {"urlname": urlname})
    group = data.get("groupByUrlname")
    if not group:
        sys.exit(f"Group '{urlname}' not found or not visible to this token.")
    write_json(outdir / "group.json", group)

    # Download the key group photo + any featured event photos the API surfaces.
    group_photos = []
    if group.get("keyGroupPhoto"):
        group_photos.append(group["keyGroupPhoto"])
    for edge in ((group.get("featuredEventPhotos") or {}).get("edges") or []):
        if edge.get("node"):
            group_photos.append(edge["node"])
    if group_photos:
        download_photos(group_photos, outdir / "photos" / "_group")
    return group


def archive_members(client: MeetupClient, urlname: str, outdir: Path) -> int:
    print("• members")
    members = []
    for edge in client.paginate(
        MEMBERS_QUERY,
        {"urlname": urlname, "first": PAGE_SIZE},
        ["groupByUrlname", "memberships"],
    ):
        node = edge["node"]
        meta = edge.get("metadata") or {}
        members.append({**node, "_membership": meta})
        if len(members) % 200 == 0:
            print(f"    {len(members)} members…")
    write_json(outdir / "members.json", members)

    with (outdir / "members.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "name", "email", "username", "city", "state",
                    "country", "role", "status", "joinTime", "lastAccessTime"])
        for m in members:
            meta = m.get("_membership") or {}
            w.writerow([m.get("id"), m.get("name"), m.get("email"),
                        m.get("username"), m.get("city"), m.get("state"),
                        m.get("country"), meta.get("role"), meta.get("status"),
                        meta.get("joinTime"), meta.get("lastAccessTime")])
    print(f"    {len(members)} members archived")
    return len(members)


def archive_events(
    client: MeetupClient, urlname: str, outdir: Path, with_attendees: bool,
    skip_photo_id: str | None = None,
) -> list[dict]:
    print("• events")
    events: list[dict] = []
    # Pull PAST and ACTIVE separately; a stale group is mostly PAST.
    for status in ("PAST", "ACTIVE"):
        for edge in client.paginate(
            EVENTS_QUERY,
            {"urlname": urlname, "first": PAGE_SIZE, "status": status},
            ["groupByUrlname", "events"],
        ):
            events.append(edge["node"])
    write_json(outdir / "events.json", events)

    with (outdir / "events.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "title", "dateTime", "status", "eventType",
                    "venue", "rsvpYes", "attended", "eventUrl"])
        for e in events:
            v = e.get("venue") or {}
            r = e.get("rsvps") or {}
            w.writerow([e.get("id"), e.get("title"), e.get("dateTime"),
                        e.get("status"), e.get("eventType"), v.get("name"),
                        r.get("yesCount"), r.get("attendedCount"), e.get("eventUrl")])
    print(f"    {len(events)} events archived")

    if with_attendees:
        for i, e in enumerate(events, 1):
            eid = e["id"]
            print(f"    [{i}/{len(events)}] attendees+comments+photos for {eid}")
            rsvps = [edge["node"] for edge in client.paginate(
                EVENT_RSVPS_QUERY, {"eventId": eid, "first": PAGE_SIZE}, ["event", "rsvps"])]
            write_json(outdir / "rsvps" / f"{eid}.json", rsvps)

            comments = [edge["node"] for edge in client.paginate(
                EVENT_COMMENTS_QUERY, {"eventId": eid, "first": PAGE_SIZE}, ["event", "comments"])]
            if comments:
                write_json(outdir / "comments" / f"{eid}.json", comments)

            # Photos the API exposes for the event (full albums aren't enumerable
            # via gql-ext — see note above EVENT_COMMENTS_QUERY). Meetup returns
            # the group's key photo as an event's displayPhoto when the event has
            # none of its own; skip that so we don't save N copies of the banner.
            event_photos = [p for p in (e.get("displayPhoto"), e.get("featuredEventPhoto"))
                            if p and p.get("id") != skip_photo_id]
            if event_photos:
                download_photos(event_photos, outdir / "photos" / eid)
    return events


def download_photos(photos: list[dict], dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for p in photos:
        url = p.get("highResUrl") or p.get("baseUrl")
        if not url:
            continue
        try:
            r = requests.get(url, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            ext = ".jpg"
            if "." in url.split("/")[-1]:
                ext = "." + url.split("/")[-1].split(".")[-1].split("?")[0]
            (dest / f"{p['id']}{ext}").write_bytes(r.content)
        except Exception as exc:  # noqa: BLE001 -- best-effort archive
            print(f"      photo {p.get('id')} failed: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description="Archive a Meetup group before closure.")
    ap.add_argument("urlname", help="Group urlname, e.g. acm-pittsburgh")
    ap.add_argument("--out", default="archive", help="Output directory root (default: archive)")
    ap.add_argument("--no-attendees", action="store_true",
                    help="Skip per-event RSVP/comment/photo pulls (faster, fewer API calls)")
    ap.add_argument("--members-only", action="store_true",
                    help="Archive only group metadata + members")
    args = ap.parse_args()

    token = get_access_token()
    client = MeetupClient(token)
    outdir = Path(args.out) / args.urlname
    outdir.mkdir(parents=True, exist_ok=True)

    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    group = archive_group(client, args.urlname, outdir)
    member_count = archive_members(client, args.urlname, outdir)
    events: list[dict] = []
    if not args.members_only:
        key_photo_id = (group.get("keyGroupPhoto") or {}).get("id")
        events = archive_events(client, args.urlname, outdir,
                                with_attendees=not args.no_attendees,
                                skip_photo_id=key_photo_id)
        write_photo_manifests(outdir, args.urlname, group, events)

    write_json(outdir / "_meta.json", {
        "group": args.urlname,
        "groupName": group.get("name"),
        "endpoint": GQL_ENDPOINT,
        "startedAt": started,
        "finishedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "memberCount": member_count,
        "eventCount": len(events),
    })
    print(f"\nDone → {outdir.resolve()}")
    print("Next: verify the JSON/CSV, copy to org-owned storage, snapshot the public "
          "page at https://web.archive.org/save, then request closure.")


if __name__ == "__main__":
    main()
