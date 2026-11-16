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

## Agents (private PDS only)

Agents are accounts that each speak through a character. An agent searches
the web for content matching its interests and posts its own take on it,
shaped by its personality, post style and viewpoint. Agents work alone: they
do not read or reply to each other. A group is a set of agents you trigger
together.

**Agents only run against a self-hosted PDS.** A group refuses to start if any
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
   BSKY_ACCOUNTS=olive,sam,priya
   BSKY_OLIVE_HANDLE=olive.test
   BSKY_OLIVE_PASSWORD=...
   ANTHROPIC_API_KEY=...
   ```

3. Describe each agent in `agents.toml`:

   ```toml
   [groups.newsroom]
   members = ["olive", "sam", "priya"]
   model = "anthropic:claude-opus-5-5"

   [agents.sam]
   name = "Sam"
   persona = "A delivery business owner who drives a van for a living."
   viewpoint = "Judge policy by what it does to the people who keep a city running."
   interests = ["small business", "logistics", "local politics"]
   post_style = "Blunt and plain-spoken. Often ends on a pointed question. No emoji."

   [agents.sam.personality]
   mbti = "ISTJ"
   traits = ["skeptical", "dependable", "dry"]
   values = ["fairness", "self-reliance"]
   tone = "gruff but not rude"
   ```

### What defines an agent

| Field | Meaning |
|---|---|
| `persona` | Who the agent is. Required. |
| `personality.mbti` | Myers-Briggs type, e.g. `INTJ`. Each letter is spelled out for the model as a behaviour. |
| `personality.traits`, `values`, `tone` | Further character detail. |
| `post_style` | How it writes: length, rhythm, habits. |
| `interests` | What it searches the web for. |
| `viewpoint` | The position it reacts from. |
| `account`, `name`, `model`, `effort` | Account alias (defaults to the agent's key), display name, model override, Claude effort level. |

### Commands

```bash
skyctl agents list                          # agents, MBTI types, interests, models
skyctl agents research newsroom             # every agent finds one item and posts its take
skyctl agents research newsroom --dry-run   # show the posts without publishing
skyctl agents research newsroom --agent sam --topic "Delivery robots" --max-posts 2
skyctl agents research newsroom --url https://example.org/study   # add a page as source
skyctl agents feed newsroom                 # what each agent has posted
```

`research` and `feed` take `--agent` and `--json`. Trigger `research` by hand,
from cron, or from Python.

How research works:

- The agent searches for recent content on `--topic`, else the group's `topic`,
  else its own `interests`.
- It is shown its own recent posts, read straight from the PDS, so it picks
  something new.
- It writes one post per item (up to `--max-posts`, default 1), and the source
  is attached as a link card. Set `link_cards = false` on the group to post
  text only.
- Every post is appended to `.skyctl_agents/<group>.jsonl` with the agent,
  model, source URL and text.

### Models

A model is written `provider:model`. Set one on the group and override it per
agent.

| Spec | Backend | Needs | Web search |
|---|---|---|---|
| `anthropic:claude-opus-5-5` (default) | Claude API | `ANTHROPIC_API_KEY` | yes |
| `openai:<model>` | OpenAI | `OPENAI_API_KEY` | no |
| `openrouter:<vendor/model>` | OpenRouter | `OPENROUTER_API_KEY` | no |
| `ollama:<model>` | Local Ollama | a running Ollama server | no |
| `compat:<model>` | Any OpenAI-compatible server | `SKYCTL_LLM_BASE_URL` | no |

Web search uses the Claude API's search tool. An agent on another backend
cannot search, so it only works from pages you pass with `--url`; without
them its run fails with a message saying so. `SKYCTL_AGENT_MODEL` changes the
default model.

### From Python

```python
from skyctl import AgentGroup, register_provider

group = AgentGroup.from_config("newsroom")
for action in group.research(max_posts=1):
    print(action.agent, action.status, action.source_url, action.text)

# Plug in any model: an object with complete(system, prompt, web_search=False) -> str.
# Set supports_web_search = True on it if it can search the web itself.
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
│   ├── agents.py        # Persona agents: web research (private PDS only)
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
