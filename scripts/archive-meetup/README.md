# Meetup group archiver

Pulls a complete local archive of a Meetup.com group (metadata, members,
events, RSVPs, comments, the photos the API exposes) **before** you request the
group be closed. Once Meetup deletes a group the data is unrecoverable, so run
this, verify the output, and copy it to org-owned storage first.

It talks to Meetup's GraphQL API at `https://api.meetup.com/gql-ext`. All field
names were verified by live schema introspection (see
[Re-checking the schema](#re-checking-the-schema) if Meetup changes things).

## What you get

```
archive/<urlname>/
  group.json              metadata, settings, stats, organizer
  members.json            full roster (raw)
  members.csv             roster flattened: name, email, role, joinTime, …
  events.json             past + active events (raw)
  events.csv              event summary: title, date, venue, RSVP/attended counts
  rsvps/<eventId>.json    attendee / RSVP list per event
  comments/<eventId>.json event discussion threads (when present)
  photo_urls.csv          direct CDN URLs for every photo the API exposes
  albums.md               markdown table of albums w/ clickable links (manual DL)
  photos/_group/…         key group photo + featured event photos (downloaded)
  photos/<eventId>/…      each event's display/featured photo (downloaded)
  _meta.json              run metadata: counts, timestamps, endpoint
```

### Photos — what you get and the one manual step

The GraphQL API does **not** expose the photos inside an event album — it only
returns the album's `id`, `title`, and `photoCount`, plus a single
display/featured photo per event. So the script produces two manifests:

- **`photo_urls.csv`** — direct image URLs for every photo the API *does*
  surface (group key/featured photos + each event's display/featured photo).
  These download directly when opened.
- **`albums.md`** — a markdown table of every event album with **clickable
  Album** links (`…/photos/<albumId>/`) and event links, newest first. Open it
  in any markdown viewer, click an album while logged in, and bulk-download the
  full album by hand. A group all-albums landing link sits at the top as a
  catch-all.

That album step is the only part that can't be fully automated, because Meetup
doesn't return per-photo IDs for album contents through any API surface.

## Prerequisites

- Python 3.9+
- Organizer access to the group (and Pro/Network admin for anything network-wide).
- An access token — see [Authentication](#authentication).

## Install

```bash
cd scripts/archive-meetup
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Authentication

> **Clearing up the confusion:** Meetup has no separate "API key." Every call
> uses an OAuth2 **access token**. The OAuth client registration page looks like
> it's for a third-party app acting on behalf of *other* users — but you're
> registering a client for *yourself*. Authorizing your own client gives you a
> token that acts as **you** (the organizer). That's exactly what we want;
> there's no "end user" but you.

There are two ways to get that token. For a one-time archive, **use Option A** —
it's far less setup. Option B (true server-to-server, no browser) is only worth
it if you want this to run repeatedly and unattended.

### Option A — get a token once in the browser (recommended for a one-off)

Meetup rejects `localhost` as a redirect URI — it wants a real HTTPS URL. Use a
domain you control. This org already owns **`https://local.acm.org`** (the live
site in this repo), which works perfectly: it's HTTPS, it loads, and the
redirect simply lands on the homepage with `?code=…` in the address bar. The
page doesn't need to *do* anything with the code — you read it from the URL.

1. Create an OAuth client at **https://www.meetup.com/api/oauth/list/**.
   - Set the **Redirect URI** to `https://local.acm.org/`.
   - Copy the **Client ID (Key)** and **Client Secret**.
2. In a browser, visit (substitute your client id):
   ```
   https://secure.meetup.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&response_type=code&redirect_uri=https://local.acm.org/
   ```
   Approve it. Your browser redirects to
   `https://local.acm.org/?code=XXXXXXXX` — the homepage loads; copy the `code`
   value out of the address bar.
3. Exchange that code for a token (the `redirect_uri` must match **exactly**
   what you registered):
   ```bash
   curl -s https://secure.meetup.com/oauth2/access \
     -d client_id=YOUR_CLIENT_ID \
     -d client_secret=YOUR_CLIENT_SECRET \
     -d grant_type=authorization_code \
     -d redirect_uri=https://local.acm.org/ \
     -d code=XXXXXXXX
   ```
   Copy the `access_token` from the JSON response.
4. Export it and run:
   ```bash
   export MEETUP_ACCESS_TOKEN="eyJ..."
   python archive_meetup.py acm-pittsburgh
   ```

Tokens last ~1 hour. The response also includes a `refresh_token`; if a run
outlives the token, repeat step 3 with `grant_type=refresh_token` and
`-d refresh_token=...` to mint a fresh one, or use Option B.

### Option B — self-signed JWT, server-to-server (no browser, self-renewing)

This is the genuine "server-to-server" path: no redirects, no end-user consent
screen. It reuses the **same OAuth client** from Option A — you just attach a
signing key and use a JWT grant instead of the redirect flow.

1. Generate an RSA keypair:
   ```bash
   openssl genrsa -out meetup_private.pem 2048
   openssl rsa -in meetup_private.pem -pubout -out meetup_public.pem
   ```
2. On your OAuth client at **https://www.meetup.com/api/oauth/list/**, add the
   **public key** (`meetup_public.pem`) as a signing key. Meetup returns a
   **Signing Key ID**. (Exact button labels shift over time; look for "signing
   key" / "self-signed" on the client's detail page.)
3. Note your **member ID** — the account with organizer/admin rights. (Find it
   in the URL of your own Meetup profile, or via a `self { id }` query.)
4. Export and run — the script signs a short-lived JWT and exchanges it at
   `https://secure.meetup.com/oauth2/access` automatically:
   ```bash
   export MEETUP_CLIENT_KEY="your-client-key"
   export MEETUP_SIGNING_KEY_ID="your-signing-key-id"
   export MEETUP_MEMBER_ID="your-member-id"
   export MEETUP_PRIVATE_KEY_FILE="$PWD/meetup_private.pem"
   python archive_meetup.py acm-pittsburgh
   ```

> Keep the private key and any token out of git. The local `.gitignore` already
> excludes `*.pem`, `*.key`, `.env`, and the `archive/` output.

## Run

Full archive of the ACM Pittsburgh group:

```bash
python archive_meetup.py acm-pittsburgh
```

Useful flags:

| Flag | Effect |
|------|--------|
| `--out DIR`       | Output root (default `archive/`). Final path is `DIR/<urlname>/`. |
| `--members-only`  | Only group metadata + roster. Fast; good first pass. |
| `--no-attendees`  | Archive events but skip per-event RSVP/comment/photo pulls (far fewer API calls). |

Examples:

```bash
# Quick roster grab first
python archive_meetup.py acm-pittsburgh --members-only

# Everything, into a dated folder
python archive_meetup.py acm-pittsburgh --out "archive-$(date +%F)"
```

The script paginates with cursors, backs off on HTTP 429/5xx, and sleeps
briefly between pages to stay within Pro API rate limits. A large group may take
a while — that's expected.

## After it finishes

1. **Verify** the output — open `members.csv` and `events.csv`, confirm counts
   in `_meta.json` look right.
2. **Copy** the `archive/` folder to durable, org-owned storage (shared Drive,
   org cloud, or a private repo) — not a personal account that could lapse.
3. **Snapshot the public page** independently at
   <https://web.archive.org/save> for `https://www.meetup.com/acm-pittsburgh/`
   and its `/events/past/` page.
4. **Then** request the group closure.

## Re-checking the schema

If a query starts failing with "Cannot query field …", Meetup changed the
schema. Introspect the live type and adjust the query constants in
`archive_meetup.py`:

```bash
curl -s -X POST https://api.meetup.com/gql-ext \
  -H "Content-Type: application/json" \
  -d '{"query":"query{__type(name:\"Group\"){fields{name}}}"}' | python3 -m json.tool
```

Swap `"Group"` for `Member`, `Event`, `Rsvp`, `Membership`, etc.
