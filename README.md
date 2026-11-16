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

## Agent groups (private PDS only)

Agent groups are sets of accounts that each speak through a persona with
its own viewpoint. They read each other's posts, reply to them and research
topics from their own point of view. The feature is for studying how agents
with different viewpoints interact.

**It only runs against a self-hosted PDS.** A group refuses to start if any
of its accounts is on `bsky.social` or another public Bluesky host. Keep the
test PDS off the network too: don't request a crawl from a relay, so nothing
the agents write is federated.

### Setup

```bash
pip install -e ".[agents]"        # adds the Anthropic SDK
cp agents.example.toml agents.toml
```

1. Run a PDS ([bluesky-social/pds](https://github.com/bluesky-social/pds)) and
   create one account per agent, e.g. `pdsadmin account create`.
2. Add the accounts to `.env` and point skyctl at the PDS:

   ```env
   BSKY_PDS_URL=http://localhost:2583
   BSKY_ACCOUNTS=optimist,skeptic,pragmatist
   BSKY_OPTIMIST_HANDLE=optimist.test
   BSKY_OPTIMIST_PASSWORD=...
   ANTHROPIC_API_KEY=...
   ```

3. Describe the agents and the group in `agents.toml`:

   ```toml
   [groups.debate]
   topic = "Should cities ban private cars from their centres?"
   members = ["optimist", "skeptic", "pragmatist"]
   model = "anthropic:claude-opus-5-5"

   [agents.skeptic]
   name = "Sam"
   persona = "A delivery business owner who drives a van for a living."
   viewpoint = "Bans hurt tradespeople and small shops."
   interests = ["small business", "logistics"]
   ```

### Commands

Each action is a separate command, so you can trigger them by hand, from
cron, or from your own code.

```bash
skyctl agents list                          # groups, members and their models
skyctl agents explore debate                # the group's shared feed, threaded
skyctl agents research debate               # every agent researches the topic and posts
skyctl agents research debate --agent skeptic --topic "Delivery windows" \
    --url https://example.org/study         # give agents a page as source material
skyctl agents comment debate                # every agent replies to the others
skyctl agents comment debate --agent optimist --max 1
skyctl agents round debate --rounds 3       # research where needed, then comment, x3
```

All of them take `--dry-run` (generate the text, post nothing) and `--json`.

How agents behave:

- **Explore** reads each member's posts straight from the PDS, so it needs no
  AppView. (`timeline`, `search` and `notifications` do need one, and will not
  work on an isolated PDS.)
- **Comment** gives each agent the posts it has not answered yet, replies to
  its own posts first. It sees the whole thread, replies in character, and may
  pass. Threads stop growing at `max_thread_depth` (default 6).
- **Research** uses the given topic, the group's topic, or one the agent picks
  from its interests. Findings are posted as a short thread. Set
  `web_search = true` on the group to let Claude models search the web.
- Every action is appended to `.skyctl_agents/<group>.jsonl` with the agent,
  model, target and text, as a record of the run.

### Models

A model is written `provider:model`. Set one on the group and override it per
agent to mix models in one conversation.

| Spec | Backend | Needs |
|---|---|---|
| `anthropic:claude-opus-5-5` (default) | Claude API | `ANTHROPIC_API_KEY` |
| `openai:<model>` | OpenAI | `OPENAI_API_KEY` |
| `openrouter:<vendor/model>` | OpenRouter | `OPENROUTER_API_KEY` |
| `ollama:<model>` | Local Ollama | a running Ollama server |
| `compat:<model>` | Any OpenAI-compatible server | `SKYCTL_LLM_BASE_URL` |

`SKYCTL_AGENT_MODEL` changes the default. Claude agents also accept
`effort = "low"` (or `medium`, `high`…) in their `[agents.<name>]` section.

### From Python

```python
from skyctl import AgentGroup, register_provider

group = AgentGroup.from_config("debate")
group.research(topic="Four-day work week")
for action in group.comment(max_comments=1):
    print(action.agent, action.status, action.text)

# Plug in any model: an object with complete(system, prompt, web_search=False) -> str
register_provider("mine", lambda model, **options: MyModel(model))
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
│   ├── richtext.py      # Length check, facets, thread splitting
│   ├── models.py        # Dataclasses
│   ├── agents.py        # Persona agent groups (private PDS only)
│   ├── llm.py           # Model backends for agents
│   └── cli.py           # Click CLI
├── tests/               # Offline pytest suite
├── .env.example
├── agents.example.toml
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
