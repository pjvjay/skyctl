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

### Replying & reposting

```bash
skyctl reply personal at://did:plc:.../post/... "Great point!"
skyctl repost work at://did:plc:.../post/...
```

### Scheduling

```bash
# Schedule a one-time post (UTC datetime)
skyctl schedule personal "Good morning!" 2026-03-01T09:00:00

# Schedule a reply
skyctl schedule personal "Follow up" 2026-03-01T10:00:00 --reply-to at://...

# List and manage jobs
skyctl jobs
skyctl cancel <job-id>

# Start the scheduler process (runs until Ctrl+C)
skyctl run-scheduler
```

### Feeds & notifications

```bash
skyctl timeline personal               # Home timeline
skyctl timeline personal -n 25         # More posts
skyctl notifications personal          # Recent notifications
```

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

# Run in background thread (non-blocking)
scheduler.start(blocking=False)

# Or block until Ctrl+C
scheduler.start(blocking=True)
```

### Building from a dict (no .env needed)

```python
manager = BskyManager.from_dict({
    "personal": ("omnidion.bsky.social", "app-password"),
    "news":     ("news.bsky.social",     "app-password"),
})
```

---

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
│   ├── scheduler.py     # APScheduler integration
│   ├── models.py        # Dataclasses
│   └── cli.py           # Click CLI
├── .env.example
├── pyproject.toml
└── README.md
```
