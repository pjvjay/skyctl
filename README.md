# skyctl

Multi-account Bluesky automation library and CLI.

## Install

```bash
pip install -e .
```

## Setup

Copy `.env.example` to `.env` and fill in your credentials.
Always use [app passwords](https://bsky.app/settings/app-passwords), not your main account password.

```env
BSKY_ACCOUNTS=personal,work
BSKY_PERSONAL_HANDLE=omnidion.bsky.social
BSKY_PERSONAL_PASSWORD=xxxx-xxxx-xxxx-xxxx
BSKY_WORK_HANDLE=mywork.bsky.social
BSKY_WORK_PASSWORD=xxxx-xxxx-xxxx-xxxx
```

---

## CLI Usage

### Account management

```bash
skyctl accounts                        # List all configured accounts
skyctl profile personal                # Show your profile
skyctl profile personal --handle nytimes.com  # Look up any handle
```

### Posting

```bash
skyctl post personal "Hello Bluesky!"
skyctl post personal "Photo post" -i photo.jpg -i photo2.jpg
skyctl post-all "Announcement from all accounts!"
skyctl post-all "Announcement" --exclude work
```

Links, `@mentions` and `#hashtags` in the text are made clickable automatically.
Posts over 300 graphemes are rejected before anything is sent.

```bash
# Alt text, one --alt per image, in order
skyctl post personal "Photo post" -i photo.jpg --alt "A red kite over the bay"

# Quote post (AT URI or bsky.app URL) and link preview card
skyctl post personal "Worth a read" --quote https://bsky.app/profile/someone.bsky.social/post/abc123
skyctl post personal "Our new release" --link-card https://example.com/release

# Preview without logging in or sending
skyctl post personal "hi @bsky.app https://example.com #test" --dry-run
```

### Threads

```bash
skyctl thread personal "Long text is split into posts of 300 graphemes or fewer..."
skyctl thread personal "Long text ..." --dry-run    # show the split only
```

### Replying & reposting

```bash
skyctl reply personal at://did:plc:.../post/... "Great point!"
skyctl repost work at://did:plc:.../post/...
skyctl like personal at://did:plc:.../post/...
skyctl delete personal at://did:plc:.../post/...   # asks first; --yes to skip
skyctl search personal "open source" -n 25
```

Replies to a post deep in a thread are attached to the original thread root.

### Scheduling

```bash
# Schedule a one-time post (UTC datetime)
skyctl schedule personal "Good morning!" 2026-03-01T09:00:00

# Schedule a reply
skyctl schedule personal "Follow up" 2026-03-01T10:00:00 --reply-to at://...

# Schedule a recurring post (UTC crontab; use day names for weekdays)
skyctl schedule work "Daily update" --cron "0 9 * * mon-fri"

# List and manage jobs
skyctl jobs
skyctl cancel <job-id>

# Start the scheduler process (runs until Ctrl+C)
skyctl run-scheduler
skyctl run-scheduler --max-retries 5 --retry-delay 30
```

All jobs, including recurring ones, are saved to `.skyctl_queue.json` and are
sent only while `skyctl run-scheduler` is running. A running scheduler picks up
jobs added or cancelled from another terminal within 30 seconds. A failed send
is retried up to 3 times, waiting 60s, 120s, then 240s.

### Feeds & notifications

```bash
skyctl timeline personal               # Home timeline
skyctl timeline personal -n 25         # More posts
skyctl notifications personal          # Recent notifications
```

### JSON output and sessions

Most commands accept `--json` for scripting:

```bash
skyctl post personal "Hello" --json
skyctl jobs --json
```

The CLI caches each account's login session in `.skyctl_sessions/` (owner-only
files, git-ignored) and reuses it, so it does not log in with the app password
on every run. `skyctl logout [alias]` clears the cache.

---

## SDK Usage

```python
from skyctl import BskyManager, BskyScheduler
from datetime import datetime, timezone

# --- Manager (multi-account) ---
manager = BskyManager.from_env()       # reads .env automatically
manager.post("personal", "Hello!")
manager.post_all("Hello from all accounts!")
manager.post_all("Hello!", exclude=["work"])

# Reply and repost
manager.reply("personal", "Nice post!", parent_uri="at://...")
manager.repost("work", "at://...")

# --- Single client ---
client = manager["personal"]           # logs in automatically
result = client.post("Hello!")
print(result.url)                      # https://bsky.app/...

client.reply("Great!", parent_uri="at://...")
client.like("at://...")
client.delete_post("at://...")

# Alt text, quote posts, link cards, threads
client.post("Photo", image_paths=["kite.jpg"], image_alts=["A red kite"])
client.post("Worth a read", quote_uri="at://...")
client.post("Our release", link_card="https://example.com/release")
results = client.thread("Long text that is split automatically ...")

# Timeline and search
posts = client.get_timeline(limit=20)
results = client.search_posts("canada tariff")

# --- Scheduler ---
scheduler = BskyScheduler(manager)

# One-time post
scheduler.schedule(
    alias="personal",
    text="Good morning Bluesky!",
    run_at=datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc),
)

# Daily recurring post
scheduler.schedule_cron(
    alias="work",
    text="Daily update!",
    hour=9, minute=0,
    day_of_week="mon-fri",
)

# Or a crontab expression; retries are configurable
scheduler = BskyScheduler(manager, max_retries=3, retry_delay=60)
scheduler.schedule_cron(alias="work", text="Weekly", cron="0 9 * * mon")

# Run in background thread (non-blocking)
scheduler.start(blocking=False)

# Or block until Ctrl+C
scheduler.start(blocking=True)
```

### Running the scheduler from cron (tick mode)

`start()` keeps a process alive. `tick()` does one pass and returns, which
suits a cron job or a scheduled cloud job: it sends everything that is due,
records retries, and exits.

```python
from skyctl import BskyManager, BskyScheduler
from skyctl.adapters import job_store_for, object_store_for

store = object_store_for("s3://my-bucket/skyctl")      # or file://, az://, gs://
scheduler = BskyScheduler(BskyManager.from_env(), job_store=job_store_for(store))
result = scheduler.tick()
print(result.to_dict())
```

Several workers may tick the same queue. Each job is claimed before it is
sent, so two workers never send the same post. If a worker dies mid-send, a
later tick picks the job up once the claim expires and checks the account's
recent posts first, so the post is not sent twice.

### Building from a dict (no .env needed)

```python
manager = BskyManager.from_dict({
    "personal": ("omnidion.bsky.social", "app-password"),
    "news":     ("news.bsky.social",     "app-password"),
})
```

---

## Storage and queue adapters

`skyctl.adapters` keeps scheduler state, cached sessions and images in a
shared store instead of local files, so more than one machine can work on
the same accounts.

| URL | Store | Install |
|---|---|---|
| `file://./.skyctl`, `memory://x` | local files, in-memory | built in |
| `s3://bucket/prefix` | Amazon S3 | `pip install "skyctl[aws]"` |
| `az://container/prefix` | Azure Blob Storage (set `AZURE_STORAGE_ACCOUNT_NAME`) | `pip install "skyctl[azure]"` |
| `gs://bucket/prefix` | Google Cloud Storage | `pip install "skyctl[gcp]"` |
| any other fsspec URL | fsspec fallback, without atomic compare-and-swap | `pip install "skyctl[fsspec]"` |

```python
from skyctl import BskyManager
from skyctl.adapters import object_store_for, session_store_for

store = object_store_for("s3://my-bucket/skyctl")
manager = BskyManager.from_env(session_store=session_store_for(store), media_store=store)
manager.post("personal", "From the bucket", image_paths=["media/kite.jpg"])
```

`skyctl.adapters.queues` has work queues with one contract (at-least-once
delivery, a visibility window, ack and nack) over SQS, Azure Storage Queues,
Service Bus and Pub/Sub; `queue_for(url)` builds one from a URL.

Every adapter passes the same checks in `skyctl.testing.contracts`. To add
a store or queue of your own, implement the protocol in `skyctl.ports` and
run those checks against it.

## PostResult

Every post/reply/repost action returns a `PostResult`:

```python
result = client.post("Hello!")
result.success      # True / False
result.url          # https://bsky.app/profile/.../post/...
result.uri          # AT URI (at://...)
result.cid          # Content identifier
result.error        # Error message string if failed, else None
```

---

## Project Structure

```
skyctl/
├── skyctl/
│   ├── __init__.py      # Public API
│   ├── config.py        # Env var loader
│   ├── client.py        # Single-account wrapper
│   ├── manager.py       # Multi-account orchestrator
│   ├── scheduler.py     # APScheduler integration and tick mode
│   ├── ports.py         # Storage and queue interfaces
│   ├── adapters/        # File, memory, S3, Azure Blob, GCS and fsspec stores; queues
│   ├── testing/         # Contract checks and SDK fakes for adapters
│   ├── richtext.py      # Length check, facets, thread splitting
│   ├── models.py        # Dataclasses
│   └── cli.py           # Click CLI
├── tests/               # Offline pytest suite
├── .env.example
├── pyproject.toml
└── README.md
```

---

## Tests

```bash
pip install -e ".[test]"
pytest
```

The suite uses a fake Bluesky client, so it needs no network and no credentials.
