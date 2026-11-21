"""
skyctl.cli
==========
Command-line interface for skyctl.

Commands:
    skyctl accounts                  List all configured accounts
    skyctl post <alias> <text>       Post from one account
    skyctl post-all <text>           Post from all accounts
    skyctl thread <alias> <text>     Post long text as a thread
    skyctl reply <alias> <uri> <text>  Reply to a post
    skyctl repost <alias> <uri>      Repost from one account
    skyctl like <alias> <uri>        Like a post
    skyctl delete <alias> <uri>      Delete one of your posts
    skyctl search <alias> <query>    Search posts
    skyctl schedule <alias> <text> [datetime]  Schedule a post (or --cron)
    skyctl jobs                      List scheduled jobs
    skyctl cancel <job-id>           Cancel a scheduled job
    skyctl run-scheduler             Start the scheduler (blocking)
    skyctl timeline <alias>          Show home timeline
    skyctl notifications <alias>     Show notifications
    skyctl profile [alias] [handle]  Show a profile
    skyctl logout [alias]            Forget cached login sessions
    skyctl agents list               List agent groups (private PDS only)
    skyctl agents research <group>   Agents search the web and post their take
    skyctl agents feed <group>       Show what each agent has posted
    skyctl task run "<goal>"         Task agent: plan and act with the skyctl tools
    skyctl task approve <run-id>     Send the writes a run queued for approval
    skyctl mcp                       MCP server on stdio (Claude Desktop, Claude Code, Cursor)
    skyctl serve                     HTTP: /mcp, /runs, /tick, /healthz (cloud service)
    skyctl tick                      One scheduler pass (cloud cron job)
    skyctl invoke '<json>'           Run one Invocation (how every platform trigger enters)
    skyctl worker                    Drain the work queue (event jobs, KEDA, ECS worker)
    skyctl daemon                    Schedules + worker in one process (Compose / VM)
    skyctl platform info|list        What platform this is and what it can do
    skyctl deploy render|check       Render one deployment spec for every target

Most commands accept --json for machine-readable output.

Deployment settings (SKYCTL_SECRETS, SKYCTL_STATE_URL, ...) are read from
the environment; see docs/DESIGN.md §4. When none are set, the CLI keeps
its local defaults (.env, .skyctl_sessions/, .skyctl_queue.json).
"""

import contextlib
import json
import os
import sys
from datetime import datetime, timezone

import click
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .agents import (
    AgentConfigError,
    AgentGroup,
    PublicNetworkError,
    load_agents_config,
)
from .llm import LLMError, default_model
from .manager import BskyManager
from .richtext import (
    MAX_GRAPHEMES,
    PostTooLongError,
    detect_facets,
    grapheme_len,
    split_thread,
    validate_length,
)
from .scheduler import BskyScheduler

console = Console()

json_option = click.option(
    "--json", "as_json", is_flag=True, help="Print machine-readable JSON."
)


def emit_json(data) -> None:
    click.echo(json.dumps(data, indent=2, default=str))


def fail(message: str, as_json: bool = False):
    if as_json:
        emit_json({"success": False, "error": message})
    else:
        console.print(f"[red]✗[/red] {escape(message)}")
    sys.exit(1)


def status(message: str, as_json: bool):
    """A spinner for humans; nothing when emitting JSON."""
    return contextlib.nullcontext() if as_json else console.status(message)


def deployment_configured() -> bool:
    """True when cloud settings are in play; else the CLI keeps its local file layout."""
    return any(os.getenv(k, "").strip() for k in ("SKYCTL_SECRETS", "SKYCTL_STATE_URL", "SKYCTL_CLOUD"))


def get_manager() -> BskyManager:
    try:
        if deployment_configured():
            return BskyManager.from_settings()
        return BskyManager.from_env()
    except ValueError as e:
        console.print(f"[red]Configuration error:[/red] {e}")
        sys.exit(1)
    except Exception as e:
        console.print(f"[red]Could not load configuration:[/red] {escape(str(e))}")
        sys.exit(1)


def get_scheduler(manager: BskyManager, **kwargs) -> BskyScheduler:
    if deployment_configured():
        return BskyScheduler.from_settings(manager, **kwargs)
    return BskyScheduler(manager, **kwargs)


def get_client(manager: BskyManager, alias: str, as_json: bool = False):
    try:
        return manager.client(alias)
    except KeyError as e:
        fail(str(e.args[0]), as_json)


def report(result, as_json: bool, verb: str):
    """Print a single PostResult and exit non-zero on failure."""
    if as_json:
        emit_json(result.to_dict())
    elif result.success:
        console.print(f"[green]✓[/green] {verb} from [cyan]{result.account}[/cyan]")
        if result.url:
            console.print(f"  {result.url}")
    else:
        console.print(f"[red]✗[/red] Failed: {escape(result.error)}")
    if not result.success:
        sys.exit(1)


def describe_post(text, images=(), alts=(), quote=None, link_card=None) -> dict:
    """Everything --dry-run shows about one post. No network access."""
    return {
        "text": text,
        "graphemes": grapheme_len(text),
        "limit": MAX_GRAPHEMES,
        "facets": [
            {"type": f.kind, "text": f.text, "value": f.value}
            for f in detect_facets(text)
        ],
        "images": [
            {"path": path, "alt": alts[i] if i < len(alts) else None}
            for i, path in enumerate(images)
        ],
        "quote": quote,
        "link_card": link_card,
    }


def print_preview(info: dict, heading: str = "") -> None:
    labels = {"link": "Link", "mention": "Mention", "tag": "Hashtag"}
    if heading:
        console.print(f"[bold]{heading}[/bold]")
    console.print(f"  Length  : {info['graphemes']}/{info['limit']} graphemes")
    console.print(f"  Text    : {escape(info['text'])}")
    for f in info["facets"]:
        console.print(f"  {labels[f['type']]:<8}: {escape(f['text'])}")
    for img in info["images"]:
        alt = f"alt: {img['alt']}" if img["alt"] else "no alt text"
        console.print(f"  Image   : {escape(img['path'])} ({escape(alt)})")
    if info["quote"]:
        console.print(f"  Quote   : {escape(info['quote'])}")
    if info["link_card"]:
        console.print(f"  Card    : {escape(info['link_card'])}")


def check_post_args(text, image, alt, link_card, as_json):
    """Validate a post locally, before any login or API call."""
    try:
        validate_length(text)
    except PostTooLongError as e:
        fail(str(e), as_json)
    if len(image) > 4:
        fail("At most 4 images per post.", as_json)
    if len(alt) > len(image):
        fail("More --alt values than --image values.", as_json)
    if image and link_card:
        fail("A post cannot have both images and a link card.", as_json)


@click.group()
def cli():
    """skyctl — Multi-account Bluesky automation tool."""
    pass


# ------------------------------------------------------------------
# Account info
# ------------------------------------------------------------------

@cli.command()
@json_option
def accounts(as_json):
    """List all configured accounts."""
    manager = get_manager()
    rows = [
        {"alias": alias, "handle": manager.client(alias).config.handle}
        for alias in manager.aliases()
    ]
    if as_json:
        emit_json(rows)
        return
    table = Table(title="Configured Accounts")
    table.add_column("Alias", style="cyan")
    table.add_column("Handle", style="green")
    for row in rows:
        table.add_row(row["alias"], row["handle"])
    console.print(table)


@cli.command()
@click.argument("alias", required=False)
def logout(alias):
    """Forget cached login sessions (all accounts, or just ALIAS)."""
    manager = get_manager()
    targets = [alias] if alias else manager.aliases()
    for a in targets:
        get_client(manager, a).logout()
        console.print(f"[green]✓[/green] Cleared session for [cyan]{a}[/cyan]")


# ------------------------------------------------------------------
# Posting
# ------------------------------------------------------------------

@cli.command()
@click.argument("alias")
@click.argument("text")
@click.option("--image", "-i", multiple=True, help="Image file path (up to 4).")
@click.option("--alt", "-a", multiple=True, help="Alt text for each image, in order.")
@click.option("--quote", "-q", default=None, help="AT URI or bsky.app URL of a post to quote.")
@click.option("--link-card", default=None, help="URL to attach as a link preview card.")
@click.option("--dry-run", is_flag=True, help="Show what would be posted without sending.")
@json_option
def post(alias, text, image, alt, quote, link_card, dry_run, as_json):
    """Post from a single account.

    Links, @mentions and #hashtags in TEXT become clickable.

    \b
    Example:
        skyctl post personal "Hello Bluesky!"
        skyctl post personal "Check this out" -i photo.jpg --alt "A red kite"
        skyctl post personal "Worth a read" --quote at://did:plc:xxx/app.bsky.feed.post/yyy
        skyctl post personal "Draft" --dry-run
    """
    manager = get_manager()
    client = get_client(manager, alias, as_json)
    check_post_args(text, image, alt, link_card, as_json)

    if dry_run:
        info = describe_post(text, image, alt, quote, link_card)
        info.update(dry_run=True, account=client.alias, handle=client.config.handle)
        if as_json:
            emit_json(info)
        else:
            console.print("[yellow]Dry run[/yellow] — nothing was sent.")
            console.print(f"  Account : {client.alias} ({client.config.handle})")
            print_preview(info)
        return

    with status(f"Posting from [cyan]{alias}[/cyan]...", as_json):
        result = client.post(
            text,
            image_paths=list(image) or None,
            image_alts=list(alt) or None,
            quote_uri=quote,
            link_card=link_card,
        )
    report(result, as_json, "Posted")


@cli.command("post-all")
@click.argument("text")
@click.option("--exclude", "-x", multiple=True, help="Account aliases to skip.")
@click.option("--image", "-i", multiple=True, help="Image file path (up to 4).")
@click.option("--alt", "-a", multiple=True, help="Alt text for each image, in order.")
@click.option("--dry-run", is_flag=True, help="Show what would be posted without sending.")
@json_option
def post_all(text, exclude, image, alt, dry_run, as_json):
    """Post from all accounts simultaneously.

    \b
    Example:
        skyctl post-all "Big announcement!"
        skyctl post-all "Hello!" --exclude work
    """
    manager = get_manager()
    check_post_args(text, image, alt, None, as_json)

    if dry_run:
        skip = [a.lower() for a in exclude]
        info = describe_post(text, image, alt)
        info.update(dry_run=True, accounts=[a for a in manager.aliases() if a not in skip])
        if as_json:
            emit_json(info)
        else:
            console.print("[yellow]Dry run[/yellow] — nothing was sent.")
            console.print(f"  Accounts: {', '.join(info['accounts'])}")
            print_preview(info)
        return

    with status("Posting from all accounts...", as_json):
        results = manager.post_all(
            text,
            image_paths=list(image) or None,
            exclude=list(exclude),
            image_alts=list(alt) or None,
        )

    if as_json:
        emit_json([r.to_dict() for r in results])
    else:
        for r in results:
            if r.success:
                console.print(f"[green]✓[/green] [cyan]{r.account}[/cyan] → {r.url}")
            else:
                console.print(f"[red]✗[/red] [cyan]{r.account}[/cyan] → {escape(r.error)}")
    if any(not r.success for r in results):
        sys.exit(1)


@cli.command()
@click.argument("alias")
@click.argument("text")
@click.option("--image", "-i", multiple=True, help="Image for the first post (up to 4).")
@click.option("--alt", "-a", multiple=True, help="Alt text for each image, in order.")
@click.option("--dry-run", is_flag=True, help="Show how the text would be split without sending.")
@json_option
def thread(alias, text, image, alt, dry_run, as_json):
    """Post long text as a thread, split into posts of 300 graphemes or fewer.

    \b
    Example:
        skyctl thread personal "A long piece of text ..."
        skyctl thread personal "A long piece of text ..." --dry-run
    """
    manager = get_manager()
    client = get_client(manager, alias, as_json)
    parts = split_thread(text)
    if not parts:
        fail("Thread text is empty.", as_json)
    if len(alt) > len(image):
        fail("More --alt values than --image values.", as_json)

    if dry_run:
        posts = [describe_post(p) for p in parts]
        if posts:
            posts[0] = describe_post(parts[0], image, alt)
        if as_json:
            emit_json({"dry_run": True, "account": client.alias, "posts": posts})
        else:
            console.print(
                f"[yellow]Dry run[/yellow] — nothing was sent. "
                f"{len(posts)} post(s) from {client.alias}."
            )
            for n, info in enumerate(posts, 1):
                print_preview(info, heading=f"Post {n}/{len(posts)}")
        return

    with status(f"Posting thread from [cyan]{alias}[/cyan]...", as_json):
        results = client.thread(
            parts, image_paths=list(image) or None, image_alts=list(alt) or None
        )

    ok = len(results) == len(parts) and all(r.success for r in results)
    if as_json:
        emit_json({
            "success": ok,
            "planned": len(parts),
            "posts": [r.to_dict() for r in results],
        })
    else:
        for n, r in enumerate(results, 1):
            if r.success:
                console.print(f"[green]✓[/green] {n}/{len(parts)} {r.url}")
            else:
                console.print(f"[red]✗[/red] {n}/{len(parts)} Failed: {escape(r.error)}")
        if not ok and results and results[0].success:
            console.print(
                f"[yellow]Thread is incomplete:[/yellow] "
                f"{sum(r.success for r in results)} of {len(parts)} posts were sent."
            )
    if not ok:
        sys.exit(1)


@cli.command()
@click.argument("alias")
@click.argument("parent_uri")
@click.argument("text")
@click.option("--image", "-i", multiple=True, help="Image file path (up to 4).")
@click.option("--alt", "-a", multiple=True, help="Alt text for each image, in order.")
@json_option
def reply(alias, parent_uri, text, image, alt, as_json):
    """Reply to a post.

    \b
    Example:
        skyctl reply personal at://did:plc:xxx/app.bsky.feed.post/yyy "Great point!"
    """
    manager = get_manager()
    client = get_client(manager, alias, as_json)
    check_post_args(text, image, alt, None, as_json)
    with status(f"Replying from [cyan]{alias}[/cyan]...", as_json):
        result = client.reply(
            text,
            parent_uri=parent_uri,
            image_paths=list(image) or None,
            image_alts=list(alt) or None,
        )
    report(result, as_json, "Reply posted")


@cli.command()
@click.argument("alias")
@click.argument("uri")
@json_option
def repost(alias, uri, as_json):
    """Repost from a specific account.

    \b
    Example:
        skyctl repost work at://did:plc:xxx/app.bsky.feed.post/yyy
    """
    manager = get_manager()
    client = get_client(manager, alias, as_json)
    with status(f"Reposting from [cyan]{alias}[/cyan]...", as_json):
        result = client.repost(uri)
    if not as_json and result.success:
        console.print(f"[green]✓[/green] Reposted from [cyan]{alias}[/cyan]")
        return
    report(result, as_json, "Reposted")


@cli.command()
@click.argument("alias")
@click.argument("uri")
@json_option
def like(alias, uri, as_json):
    """Like a post.

    \b
    Example:
        skyctl like personal at://did:plc:xxx/app.bsky.feed.post/yyy
    """
    manager = get_manager()
    client = get_client(manager, alias, as_json)
    with status(f"Liking from [cyan]{alias}[/cyan]...", as_json):
        result = client.like(uri)
    if not as_json and result.success:
        console.print(f"[green]✓[/green] Liked from [cyan]{alias}[/cyan]")
        return
    report(result, as_json, "Liked")


@cli.command()
@click.argument("alias")
@click.argument("uri")
@click.option("--yes", "-y", is_flag=True, help="Do not ask for confirmation.")
@json_option
def delete(alias, uri, yes, as_json):
    """Delete one of your own posts. This cannot be undone.

    \b
    Example:
        skyctl delete personal at://did:plc:xxx/app.bsky.feed.post/yyy
    """
    manager = get_manager()
    client = get_client(manager, alias, as_json)
    if not yes:
        click.confirm(f"Permanently delete {uri} from {alias}?", abort=True)
    with status(f"Deleting from [cyan]{alias}[/cyan]...", as_json):
        deleted = client.delete_post(uri)
    if as_json:
        emit_json({"success": deleted, "account": client.alias, "uri": uri})
    elif deleted:
        console.print(f"[green]✓[/green] Deleted from [cyan]{alias}[/cyan]")
    else:
        console.print("[red]✗[/red] Could not delete that post.")
    if not deleted:
        sys.exit(1)


# ------------------------------------------------------------------
# Scheduler
# ------------------------------------------------------------------

@cli.command()
@click.argument("alias")
@click.argument("text")
@click.argument("run_at", required=False)  # ISO datetime string e.g. "2026-03-01T09:00:00"
@click.option("--cron", default=None, help='Recurring schedule as a UTC crontab, e.g. "0 9 * * mon-fri".')
@click.option("--reply-to", default=None, help="AT URI to reply to.")
@json_option
def schedule(alias, text, run_at, cron, reply_to, as_json):
    """Schedule a post for a future time, or on a recurring schedule.

    RUN_AT should be an ISO datetime string in UTC, e.g. 2026-03-01T09:00:00.
    Use --cron instead of RUN_AT for a recurring post.

    \b
    Example:
        skyctl schedule personal "Morning!" 2026-03-01T09:00:00
        skyctl schedule work "Follow-up" 2026-03-01T10:00:00 --reply-to at://...
        skyctl schedule work "Daily update" --cron "0 9 * * mon-fri"
    """
    if bool(run_at) == bool(cron):
        fail("Give either RUN_AT or --cron (one of them, not both).", as_json)
    if cron and reply_to:
        fail("--reply-to cannot be combined with --cron.", as_json)

    manager = get_manager()
    get_client(manager, alias, as_json)
    scheduler = get_scheduler(manager)

    try:
        if cron:
            job_id = scheduler.schedule_cron(alias=alias, text=text, cron=cron)
            when = f"cron {cron} (UTC)"
        else:
            try:
                dt = datetime.fromisoformat(run_at).replace(tzinfo=timezone.utc)
            except ValueError:
                fail(f"Invalid datetime: {run_at}. Use ISO format: 2026-03-01T09:00:00", as_json)
            sp = scheduler.schedule(alias=alias, text=text, run_at=dt, reply_to_uri=reply_to)
            job_id = sp.id
            when = f"{dt.isoformat()} UTC"
    except ValueError as e:
        fail(str(e), as_json)

    if as_json:
        emit_json({"success": True, "id": job_id, "account": alias, "schedule": cron or dt.isoformat()})
        return
    console.print(f"[green]✓[/green] Scheduled job [cyan]{job_id}[/cyan]")
    console.print(f"  Account : {alias}")
    console.print(f"  Send at : {when}")
    console.print(f"  Preview : {escape(text[:80])}")
    console.print("[dim]  Jobs are sent while `skyctl run-scheduler` is running.[/dim]")


@cli.command()
@json_option
def jobs(as_json):
    """List all scheduled jobs."""
    manager = get_manager()
    scheduler = get_scheduler(manager)
    job_list = scheduler.list_jobs()

    if as_json:
        emit_json(job_list)
        return
    if not job_list:
        console.print("No scheduled jobs.")
        return

    table = Table(title="Scheduled Jobs")
    table.add_column("ID", style="cyan")
    table.add_column("Account", style="green")
    table.add_column("Schedule")
    table.add_column("Next Run", style="yellow")
    table.add_column("Status")
    table.add_column("Text Preview")

    for j in job_list:
        table.add_row(
            j["id"], j["account"], j["schedule"], j["next_run"], j["status"],
            escape(j["text_preview"]),
        )

    console.print(table)


@cli.command()
@click.argument("job_id")
def cancel(job_id):
    """Cancel a scheduled job by ID."""
    manager = get_manager()
    scheduler = get_scheduler(manager)
    removed = scheduler.cancel(job_id)
    if removed:
        console.print(f"[green]✓[/green] Cancelled job [cyan]{job_id}[/cyan]")
    else:
        console.print(f"[yellow]Job {job_id} not found.[/yellow]")


@cli.command("run-scheduler")
@click.option("--max-retries", default=3, show_default=True, help="Retries for a failed send.")
@click.option("--retry-delay", default=60.0, show_default=True, help="Seconds before the first retry (doubles each time).")
def run_scheduler(max_retries, retry_delay):
    """Start the scheduler and process all queued jobs (blocking)."""
    manager = get_manager()
    scheduler = get_scheduler(manager, max_retries=max_retries, retry_delay=retry_delay)
    jobs_list = scheduler.list_jobs()
    console.print(f"Starting scheduler with [cyan]{len(jobs_list)}[/cyan] job(s)...")
    scheduler.start(blocking=True)


# ------------------------------------------------------------------
# Feeds
# ------------------------------------------------------------------

def print_posts(posts):
    for p in posts:
        console.rule(f"[dim]{p['author']}[/dim]  {str(p['created_at'])[:10]}")
        console.print(escape(p["text"]))
        counts = f"❤ {p['likes']}  🔁 {p['reposts']}"
        if "replies" in p:
            counts += f"  💬 {p['replies']}"
        console.print(f"[dim]{counts}[/dim]")
        console.print(f"[dim]{p['uri']}[/dim]")
    console.print()


@cli.command()
@click.argument("alias")
@click.option("--limit", "-n", default=10, show_default=True, help="Number of posts.")
@json_option
def timeline(alias, limit, as_json):
    """Show the home timeline for an account."""
    manager = get_manager()
    client = get_client(manager, alias, as_json)
    with status(f"Loading timeline for [cyan]{alias}[/cyan]...", as_json):
        posts = client.get_timeline(limit=limit)
    if as_json:
        emit_json(posts)
    else:
        print_posts(posts)


@cli.command()
@click.argument("alias")
@click.argument("query")
@click.option("--limit", "-n", default=10, show_default=True, help="Number of posts.")
@json_option
def search(alias, query, limit, as_json):
    """Search posts by keyword.

    \b
    Example:
        skyctl search personal "open source" -n 25
    """
    manager = get_manager()
    client = get_client(manager, alias, as_json)
    with status(f"Searching for [cyan]{escape(query)}[/cyan]...", as_json):
        posts = client.search_posts(query, limit=limit)
    if as_json:
        emit_json(posts)
    elif not posts:
        console.print("No posts found.")
    else:
        print_posts(posts)


@cli.command()
@click.argument("alias")
@click.option("--limit", "-n", default=10, show_default=True)
@json_option
def notifications(alias, limit, as_json):
    """Show recent notifications for an account."""
    manager = get_manager()
    client = get_client(manager, alias, as_json)
    with status(f"Loading notifications for [cyan]{alias}[/cyan]...", as_json):
        notifs = client.get_notifications(limit=limit)

    if as_json:
        emit_json(notifs)
        return
    for n in notifs:
        reason = n.get("reason", "unknown")
        actor = n.get("author", {}).get("handle", "?")
        is_read = n.get("is_read", True)
        marker = "[yellow]●[/yellow]" if not is_read else " "
        console.print(f"{marker} [cyan]{actor}[/cyan] [dim]{reason}[/dim]")


@cli.command()
@click.argument("alias")
@click.option("--handle", default=None, help="Look up a different handle instead.")
@json_option
def profile(alias, handle, as_json):
    """Show profile info for an account (or any handle)."""
    manager = get_manager()
    client = get_client(manager, alias, as_json)
    with status("Loading profile...", as_json):
        data = client.get_profile(handle)

    if as_json:
        emit_json(data)
        return
    console.print(f"[bold]{data.get('display_name', '')}[/bold]  @{data.get('handle', '')}")
    console.print(data.get("description", ""))
    console.print(
        f"[dim]Followers: {data.get('followers_count', 0)}  "
        f"Following: {data.get('follows_count', 0)}  "
        f"Posts: {data.get('posts_count', 0)}[/dim]"
    )


# ------------------------------------------------------------------
# Agent groups
# ------------------------------------------------------------------

config_option = click.option(
    "--config", "config_file", default=None,
    help="Agents config file (default: agents.toml or $SKYCTL_AGENTS_FILE).",
)
agent_option = click.option(
    "--agent", default=None, help="Only this agent acts (default: every member)."
)
dry_run_option = click.option(
    "--dry-run", is_flag=True, help="Generate the text but post nothing."
)


def get_group(name, config_file, as_json) -> AgentGroup:
    try:
        return AgentGroup.from_config(name, config_file, manager=get_manager())
    except (AgentConfigError, PublicNetworkError) as e:
        fail(str(e), as_json)


def report_actions(actions, as_json):
    """Print what each agent did and exit non-zero if anything failed."""
    if as_json:
        emit_json([a.to_dict() for a in actions])
    else:
        marks = {
            "posted": "[green]✓[/green]",
            "dry-run": "[yellow]~[/yellow]",
            "failed": "[red]✗[/red]",
        }
        for a in actions:
            console.print(
                f"{marks[a.status]} [cyan]{a.agent}[/cyan] "
                f"[dim]({a.status}, {escape(a.model)})[/dim]"
            )
            if a.text:
                console.print(f"  {escape(a.text)}")
            if a.source_url:
                console.print(f"  [dim]{escape(a.source_url)}[/dim]")
            if a.error:
                console.print(f"  [red]{escape(a.error)}[/red]")
    if any(a.status == "failed" for a in actions):
        sys.exit(1)


def run_agents(as_json, message, work):
    try:
        with status(message, as_json):
            return work()
    except (AgentConfigError, LLMError) as e:
        fail(str(e), as_json)


@cli.group()
def agents():
    """Persona-driven agents on a private test PDS.

    Each agent searches the web for content matching its interests and
    posts its own take, shaped by its personality, post style and
    viewpoint. Agents are defined in agents.toml (see agents.example.toml)
    and only run against a self-hosted PDS, never the public Bluesky network.
    """


@agents.command("list")
@config_option
@json_option
def agents_list(config_file, as_json):
    """List the configured groups and their members."""
    try:
        groups = load_agents_config(config_file)
    except AgentConfigError as e:
        fail(str(e), as_json)
    if as_json:
        emit_json([
            {
                "group": g.name,
                "topic": g.topic,
                "members": [
                    {
                        "agent": p.alias,
                        "account": p.account,
                        "name": p.name,
                        "mbti": p.personality.mbti,
                        "interests": p.interests,
                        "model": p.model or g.model or default_model(),
                    }
                    for p in g.members
                ],
            }
            for g in groups.values()
        ])
        return
    for g in groups.values():
        table = Table(title=f"{g.name} — {g.topic}" if g.topic else g.name)
        table.add_column("Agent", style="cyan")
        table.add_column("Account")
        table.add_column("Name")
        table.add_column("MBTI")
        table.add_column("Interests")
        table.add_column("Model", style="dim")
        for p in g.members:
            table.add_row(
                p.alias, p.account, p.name, p.personality.mbti or "-",
                escape(", ".join(p.interests)), p.model or g.model or default_model(),
            )
        console.print(table)


@agents.command("feed")
@click.argument("group")
@agent_option
@click.option("--limit", "-n", default=10, show_default=True, help="Posts per agent.")
@config_option
@json_option
def agents_feed(group, agent, limit, config_file, as_json):
    """Show what each agent has posted, newest first."""
    g = get_group(group, config_file, as_json)
    names = [agent] if agent else list(g.personas)
    feeds = run_agents(
        as_json, "Reading feeds...",
        lambda: {name.lower(): g.feed(name, limit=limit) for name in names},
    )
    if as_json:
        emit_json(feeds)
        return
    for name, posts in feeds.items():
        console.rule(f"[cyan]{name}[/cyan]")
        if not posts:
            console.print("[dim]No posts yet.[/dim]")
        for p in posts:
            console.print(f"[dim]{str(p['created_at'])[:16]}[/dim]  {escape(p['text'])}")


@agents.command("research")
@click.argument("group")
@agent_option
@click.option("--topic", default=None,
              help="What to look for (default: the group's topic, or each agent's interests).")
@click.option("--url", "urls", multiple=True,
              help="A page to give the agents as source material. Repeatable.")
@click.option("--max-posts", default=1, show_default=True,
              help="Most posts each agent makes.")
@dry_run_option
@config_option
@json_option
def agents_research(group, agent, topic, urls, max_posts, dry_run, config_file, as_json):
    """Have agents search the web and post their response to what they find.

    Each agent works alone, on its own interests, and posts to its own feed.

    \b
    Examples:
        skyctl agents research newsroom --dry-run
        skyctl agents research newsroom --agent sam --topic "Delivery robots"
    """
    g = get_group(group, config_file, as_json)
    actions = run_agents(
        as_json, "Agents are researching...",
        lambda: g.research(
            agent=agent, topic=topic, urls=list(urls),
            max_posts=max_posts, dry_run=dry_run,
        ),
    )
    report_actions(actions, as_json)


# ------------------------------------------------------------------
# Cloud entry points: tick, serve, mcp
# ------------------------------------------------------------------

@cli.command()
@click.option("--max-retries", default=3, show_default=True, help="Retries for a failed send.")
@click.option("--retry-delay", default=60.0, show_default=True, help="Seconds before the first retry (doubles each time).")
@json_option
def tick(max_retries, retry_delay, as_json):
    """Run one scheduler pass and exit: send every due job, record retries.

    This is the entry point for cloud cron (EventBridge Scheduler, Container
    Apps Jobs, Cloud Scheduler). Run it every 1-5 minutes.
    """
    manager = get_manager()
    scheduler = get_scheduler(manager, max_retries=max_retries, retry_delay=retry_delay)
    result = scheduler.tick()
    if as_json:
        emit_json(result.to_dict())
    else:
        if result.skipped:
            console.print(f"[yellow]~[/yellow] Tick skipped: {result.skipped}")
        console.print(f"[green]✓[/green] sent {len(result.sent)}, failed {len(result.failed)}")
        for job_id in result.failed:
            console.print(f"  [red]✗[/red] {job_id}")
    if result.failed:
        sys.exit(1)


@cli.command()
@click.option("--host", default="0.0.0.0", show_default=True)
@click.option("--port", default=None, type=int, help="Defaults to $PORT or 8080.")
def serve(host, port):
    """Start the HTTP service: /mcp, /runs, /tick, /healthz, /readyz.

    Requires SKYCTL_HTTP_TOKEN (or SKYCTL_HTTP_ALLOW_ANON=true behind platform auth).
    """
    from .server import serve as _serve

    _serve(host=host, port=port)


@cli.command()
@click.option("--read-only", is_flag=True, help="Expose only non-mutating tools.")
@click.option("--dry-run", is_flag=True, help="Expose write tools but only preview them.")
@click.option("--account", "accounts", multiple=True, help="Restrict to these aliases. Repeatable.")
def mcp(read_only, dry_run, accounts):
    """Serve skyctl as an MCP server over stdio.

    \b
    Claude Desktop / Claude Code config:
        {"mcpServers": {"skyctl": {"command": "skyctl", "args": ["mcp"]}}}
    """
    from .mcp_server import run_stdio
    from .tools import ToolPolicy

    policy = ToolPolicy(
        accounts=list(accounts) or None,
        allow_writes=not read_only,
        allow_delete=not read_only,
        max_writes=10_000,
    )
    run_stdio(policy=policy, dry_run=dry_run)


# ------------------------------------------------------------------
# Task agent
# ------------------------------------------------------------------

@cli.group()
def task():
    """The task agent: give it a goal, it works through the skyctl tools.

    Writes are off unless you pass --allow-writes; the policy is enforced
    in code, not in the prompt. Every run is written to <state>/runs/.
    Not to be confused with `skyctl agents`, the persona research agents.
    """


def get_runner():
    from .agent import AgentRunner

    manager = get_manager()
    if deployment_configured():
        return AgentRunner.from_settings(manager)
    from .adapters import object_store_for

    return AgentRunner(manager, state=object_store_for("file://./.skyctl"))


def print_run(result, as_json):
    if as_json:
        emit_json(result.to_dict())
    else:
        mark = "[green]✓[/green]" if result.ok else "[red]✗[/red]"
        console.print(
            f"{mark} run [cyan]{result.run_id}[/cyan] {result.status} "
            f"[dim]({result.turns} turns, {result.tool_calls} tool calls, {result.writes} writes, "
            f"{result.pending_writes} pending approval, model {escape(result.model)})[/dim]"
        )
        if result.summary:
            console.print(escape(result.summary))
        if result.error:
            console.print(f"[red]{escape(result.error)}[/red]")
    if not result.ok:
        sys.exit(1)


@task.command("run")
@click.argument("goal", required=False)
@click.option("--task", "task_name", default=None, help="Run a named task from tasks.toml instead of GOAL.")
@click.option("--tasks-file", default=None, help="Path to tasks.toml.")
@click.option("--account", "accounts", multiple=True, help="Account alias the agent may use. Repeatable.")
@click.option("--allow-writes", is_flag=True, help="Let the agent post, reply, like, repost and schedule.")
@click.option("--allow-delete", is_flag=True, help="Also allow delete_post and cancel_job.")
@click.option("--max-writes", default=None, type=int, help="Cap on executed writes.")
@click.option("--max-turns", default=None, type=int, help="Cap on model turns.")
@click.option("--approval", type=click.Choice(["none", "writes"]), default=None,
              help="writes: queue every write for `skyctl task approve` instead of sending.")
@click.option("--model", default=None, help="provider:model, e.g. bedrock:anthropic.claude-opus-5-5")
@click.option("--context", "context_items", multiple=True, metavar="KEY=VALUE", help="Extra facts for the prompt. Repeatable.")
@click.option("--dry-run", is_flag=True, help="Plan only; writes are previewed, not sent.")
@click.option("--verbose", "-v", is_flag=True, help="Show each tool call as it happens.")
@json_option
def task_run(goal, task_name, tasks_file, accounts, allow_writes, allow_delete, max_writes, max_turns,
             approval, model, context_items, dry_run, verbose, as_json):
    """Run the task agent on GOAL (or a named task from tasks.toml).

    \b
    Examples:
        skyctl task run "Summarise today's mentions on the work account"
        skyctl task run "Reply to questions about pricing; point to example.com/pricing" \\
            --account work --allow-writes --max-writes 5
        skyctl task run --task daily-engagement --approval writes
    """
    from .agent import TaskConfigError, TaskSpec, load_tasks
    from .settings import Settings

    defaults = Settings.from_env().agent
    if bool(goal) == bool(task_name):
        fail("Give either GOAL or --task (one of them).", as_json)
    try:
        if task_name:
            spec = load_tasks(tasks_file).get(task_name.lower())
            if spec is None:
                fail(f"Task '{task_name}' not found in tasks.toml.", as_json)
        else:
            spec = TaskSpec(
                goal=goal,
                accounts=defaults.accounts,
                allow_writes=defaults.allow_writes,
                allow_delete=defaults.allow_delete,
                max_writes=defaults.max_writes,
                max_turns=defaults.max_turns,
            )
        # Flags override whatever the task or the environment said.
        if accounts:
            spec.accounts = [a.lower() for a in accounts]
        if allow_writes:
            spec.allow_writes = True
        if allow_delete:
            spec.allow_delete = True
        if max_writes is not None:
            spec.max_writes = max_writes
        if max_turns is not None:
            spec.max_turns = max_turns
        if approval:
            spec.approval = approval
        if model:
            spec.model = model
        if dry_run:
            spec.dry_run = True
        for item in context_items:
            key, sep, value = item.partition("=")
            if not sep:
                fail(f"--context expects KEY=VALUE, got {item!r}", as_json)
            spec.context[key.strip()] = value.strip()
    except TaskConfigError as e:
        fail(str(e), as_json)

    runner = get_runner()
    show = None
    if verbose and not as_json:
        def show(entry):
            if entry["type"] == "tool_call":
                mark = "[red]✗[/red]" if not entry["ok"] else ("[yellow]~[/yellow]" if entry.get("dry_run") or entry.get("pending_approval") else "[green]✓[/green]")
                console.print(f"  {mark} {entry['name']} {escape(json.dumps(entry['args'])[:100])}")
            elif entry["type"] == "turn" and entry.get("text"):
                console.print(f"  [dim]{escape(entry['text'][:200])}[/dim]")
    with status("Agent is working...", as_json or verbose):
        result = runner.run(spec, on_event=show)
    print_run(result, as_json)


@task.command("approve")
@click.argument("run_id")
@click.option("--call-id", "call_ids", multiple=True, help="Approve only these call ids (default: all pending).")
@json_option
def task_approve(run_id, call_ids, as_json):
    """Execute the writes a previous run queued for approval."""
    runner = get_runner()
    try:
        pending = runner.pending_calls(run_id)
    except FileNotFoundError as e:
        fail(str(e), as_json)
    if not pending:
        fail(f"Run {run_id} has no writes pending approval.", as_json)
    if not as_json:
        for p in pending:
            console.print(f"  [yellow]~[/yellow] {p.get('call_id')} {p['name']} {escape(json.dumps(p['args'])[:120])}")
        if not click.confirm("Send these?", default=False):
            console.print("Nothing sent.")
            return
    records = runner.approve(run_id, list(call_ids) or None)
    if as_json:
        emit_json(records)
        return
    for r in records:
        mark = "[green]✓[/green]" if r["ok"] else "[red]✗[/red]"
        console.print(f"{mark} {r['name']} {escape(str(r.get('error') or ''))}")
    if any(not r["ok"] for r in records):
        sys.exit(1)


@task.command("show")
@click.argument("run_id")
@json_option
def task_show(run_id, as_json):
    """Show a run's transcript."""
    from .agent import RunLog

    runner = get_runner()
    try:
        events = RunLog.load(runner.state, run_id)
    except FileNotFoundError as e:
        fail(str(e), as_json)
    if as_json:
        emit_json(events)
        return
    for e in events:
        kind = e["type"]
        if kind == "turn":
            calls = ", ".join(c["name"] for c in e.get("tool_calls", []))
            console.print(f"[bold]turn {e['n']}[/bold] [dim]{e['stop_reason']}[/dim] {escape(calls)}")
            if e.get("text"):
                console.print(f"  {escape(e['text'])}")
        elif kind in ("tool_call", "approved_call"):
            mark = "[green]✓[/green]" if e["ok"] else "[red]✗[/red]"
            console.print(f"  {mark} {e['name']} {escape(json.dumps(e['args'])[:120])}")
            if e.get("error"):
                console.print(f"    [red]{escape(e['error'])}[/red]")
        elif kind == "result":
            console.print(f"[bold]{e['status']}[/bold] {escape(e.get('summary') or '')}")


@task.command("list")
@click.option("--tasks-file", default=None, help="Path to tasks.toml.")
@json_option
def task_list(tasks_file, as_json):
    """List the named tasks in tasks.toml."""
    from .agent import TaskConfigError, load_tasks

    try:
        tasks = load_tasks(tasks_file)
    except TaskConfigError as e:
        fail(str(e), as_json)
    if as_json:
        emit_json({name: t.to_dict() for name, t in tasks.items()})
        return
    table = Table(title="Tasks")
    table.add_column("Name", style="cyan")
    table.add_column("Accounts")
    table.add_column("Writes")
    table.add_column("Approval")
    table.add_column("Goal")
    for name, t in tasks.items():
        table.add_row(
            name, ", ".join(t.accounts or ["*"]),
            f"max {t.max_writes}" if t.allow_writes else "off", t.approval, escape(t.goal[:70]),
        )
    console.print(table)



# ------------------------------------------------------------------
# Invocation plane: invoke, worker, daemon, platform
# ------------------------------------------------------------------

def _runtime_and_dispatcher():
    from .platform.invocation import default_dispatcher
    from .platform.logging import configure_logging
    from .platform.runtime import get_runtime

    runtime = get_runtime()
    configure_logging(runtime.platform, os.getenv("SKYCTL_LOG_FORMAT", "auto"))
    return runtime, default_dispatcher(runtime)


@cli.command()
@click.argument("payload", required=False)
@click.option("--file", "-f", "event_file", type=click.File("r"), default=None,
              help="Read the event from a file ('-' for stdin): a raw payload or any platform envelope.")
def invoke(payload, event_file):
    """Run one Invocation, e.g. skyctl invoke '{"kind": "tick"}'.

    Container jobs on every platform (ECS RunTask, Container Apps Jobs,
    Cloud Run Jobs, Kubernetes CronJobs) start the image with this command,
    so a schedule means the same thing everywhere. With --file, any
    supported envelope (CloudEvents, Pub/Sub push, SQS, EventBridge) is
    accepted, which makes it the tool for replaying a platform event locally.
    """
    from .platform.invocation import InvocationError, decode_http, decode_json

    if bool(payload) == bool(event_file):
        fail("Give a JSON PAYLOAD or --file (one of them).")
    runtime, dispatcher = _runtime_and_dispatcher()
    try:
        if payload:
            invocations = [decode_json(payload, source="cli")]
        else:
            invocations = decode_http({"content-type": "application/json"}, event_file.read().encode("utf-8"), runtime.platform)
    except InvocationError as e:
        fail(str(e), True)
    failed = False
    for inv in invocations:
        outcome = dispatcher.dispatch(inv)
        emit_json(outcome.to_dict())
        failed = failed or not outcome.ok
    if failed:
        sys.exit(1)


@cli.command()
@click.option("--once", is_flag=True, help="Exit as soon as the queue is empty (event-driven jobs).")
@click.option("--idle-exit", default=None, type=float, help="Exit after this many idle seconds (default: never, or 0 with --once).")
@click.option("--max-messages", default=1, show_default=True)
@click.option("--visibility", default=900, show_default=True, help="Seconds a message stays hidden while being handled.")
def worker(once, idle_exit, max_messages, visibility):
    """Consume SKYCTL_QUEUE_URL: queued agent runs and delayed deliveries."""
    import signal
    import threading

    from .platform.invocation import run_worker

    runtime, dispatcher = _runtime_and_dispatcher()
    queue = runtime.queue()
    if queue is None:
        fail("SKYCTL_QUEUE_URL is not set; there is no queue to consume.")
    if idle_exit is None:
        idle_exit = 0 if once else None
    # ECS scale-in, Kubernetes and Container Apps send SIGTERM: stop taking new
    # messages and let the current one finish (unfinished work is redelivered).
    stopping = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopping.set())
    stats = run_worker(queue, dispatcher, max_messages=max_messages, visibility_seconds=visibility,
                       idle_exit_seconds=idle_exit, wait_seconds=1 if once else 10, stop=stopping.is_set)
    emit_json({"event": "worker.exit", **stats.to_dict()})


@cli.command()
def daemon():
    """Run every schedule and the queue worker in one long-lived process.

    The substitute for platforms without scheduled jobs (Docker Compose, a
    VM). Schedules come from SKYCTL_SCHEDULES_JSON (rendered by `skyctl
    deploy render --target compose`); without it, a tick runs every minute.
    """
    from .platform.daemon import run_daemon

    runtime, dispatcher = _runtime_and_dispatcher()
    run_daemon(dispatcher, queue=runtime.queue())


@cli.group()
def platform():
    """Inspect the platform abstraction: detection, capabilities, limits, toolsets."""


@platform.command("info")
@json_option
def platform_info(as_json):
    """Describe the runtime this process would use."""
    from .platform.runtime import Runtime
    from .settings import Settings

    rt = Runtime(Settings.from_env())
    info = rt.describe()
    info["toolset"] = {k: v for k, v in vars(rt.platform.toolset).items() if v}
    if as_json:
        emit_json(info)
        return
    console.print(f"[bold]{rt.platform.display_name}[/bold] / compute [cyan]{rt.compute.name}[/cyan] ({rt.compute.kind})")
    for key in ("state", "queue", "delayed", "run_strategy"):
        console.print(f"  {key:<13}: {escape(str(info.get(key)))}")
    console.print(f"  capabilities : {', '.join(info['capabilities'])}")
    for k, v in info["limits"].items():
        console.print(f"  {k:<13}: {escape(str(v))}")


@platform.command("list")
@json_option
def platform_list(as_json):
    """Every registered platform, its compute profiles and their capabilities."""
    from .platform import platforms

    rows = []
    for p in platforms().values():
        for prof in p.compute_profiles.values():
            rows.append({"platform": p.name, "compute": prof.name, "kind": prof.kind,
                         "capabilities": sorted(p.capabilities(prof.name)),
                         "job_timeout_s": prof.limits.job_timeout_s, "request_timeout_s": prof.limits.request_timeout_s})
    if as_json:
        emit_json(rows)
        return
    table = Table(title="Platforms")
    for col in ("Platform", "Compute", "Kind", "Job limit", "Request limit", "Capabilities"):
        table.add_column(col)
    for r in rows:
        table.add_row(r["platform"], r["compute"], r["kind"], str(r["job_timeout_s"] or "-"),
                      str(r["request_timeout_s"] or "-"),
                      ", ".join(c.split(".", 1)[1] for c in r["capabilities"]))
    console.print(table)


# ------------------------------------------------------------------
# Provisioning plane: deploy
# ------------------------------------------------------------------

@cli.group()
def deploy():
    """Render one cloud-neutral deployment spec for AWS, Azure, GCP, Kubernetes and Compose."""


def _load_deploy_spec(path, as_json=False):
    from pydantic import ValidationError

    from .deploy import load_spec

    try:
        return load_spec(path)
    except (ValidationError, ValueError, OSError) as e:
        fail(f"Invalid deployment spec {path}: {e}", as_json)


def _print_findings(findings):
    colours = {"error": "red", "warning": "yellow", "info": "dim"}
    for f in findings:
        c = colours[f.level]
        console.print(f"[{c}]{f.level:<7}[/{c}] [cyan]{f.target:<10}[/cyan] {escape(f.message)}")
        if f.remedy:
            console.print(f"{'':19}[dim]→ {escape(f.remedy)}[/dim]")


@deploy.command("validate")
@click.argument("spec_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--target", "-t", "targets", multiple=True, help="Only these targets (default: all configured).")
@json_option
def deploy_validate(spec_file, targets, as_json):
    """Check the spec against each target's capabilities and limits."""
    from .deploy import validate

    spec = _load_deploy_spec(spec_file, as_json)
    findings = [f for t in (targets or spec.targets.names()) for f in validate(spec, t)]
    if as_json:
        emit_json([vars(f) for f in findings])
    else:
        _print_findings(findings) if findings else console.print("[green]✓[/green] no findings")
    if any(f.level == "error" for f in findings):
        sys.exit(1)


@deploy.command("render")
@click.argument("spec_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--target", "-t", "targets", multiple=True, help="Target(s) to render.")
@click.option("--all", "all_targets", is_flag=True, help="Render every configured target.")
@click.option("--out", "out_dir", required=True, type=click.Path(file_okay=False), help="Output directory (one subdirectory per target).")
@click.option("--force", is_flag=True, help="Render even if validation reports errors.")
def deploy_render(spec_file, targets, all_targets, out_dir, force):
    """Write Terraform JSON / manifests / compose files for the chosen targets."""
    from pathlib import Path

    from .deploy import render, validate

    spec = _load_deploy_spec(spec_file)
    chosen = spec.targets.names() if all_targets else list(targets)
    if not chosen:
        fail("Give --target or --all.")
    for target in chosen:
        findings = validate(spec, target)
        errors = [f for f in findings if f.level == "error"]
        if errors and not force:
            _print_findings(errors)
            fail(f"{target}: validation failed (use --force to render anyway).")
        art = render(spec, target)
        dest = Path(out_dir) / target
        dest.mkdir(parents=True, exist_ok=True)
        for name, content in art.files.items():
            (dest / name).write_text(content)
        console.print(f"[green]✓[/green] {target}: {', '.join(str(dest / n) for n in art.files)}")
        for note in art.notes:
            console.print(f"  [dim]{escape(note)}[/dim]")


@deploy.command("check")
@click.argument("spec_file", type=click.Path(exists=True, dir_okay=False))
@json_option
def deploy_check(spec_file, as_json):
    """Validate, render every target, and run the cross-target conformance checks."""
    from .deploy import check, render, validate_all

    spec = _load_deploy_spec(spec_file, as_json)
    findings = validate_all(spec)
    errors = [f for f in findings if f.level == "error"]
    results = [] if errors else check(spec, {t: render(spec, t) for t in spec.targets.names()})
    if as_json:
        emit_json({"findings": [vars(f) for f in findings], "checks": [vars(c) for c in results]})
    else:
        _print_findings(findings)
        if results:
            table = Table(title="Conformance")
            table.add_column("Check")
            for t in spec.targets.names():
                table.add_column(t)
            for name in dict.fromkeys(c.name for c in results):
                row = [name]
                for t in spec.targets.names():
                    cs = [c for c in results if c.name == name and c.target == t]
                    row.append("-" if not cs else ("[green]pass[/green]" if all(c.ok for c in cs) else "[red]FAIL[/red]"))
                table.add_row(*row)
            console.print(table)
            for c in results:
                if not c.ok:
                    console.print(f"[red]✗[/red] {c.name} {c.target}: {escape(c.detail)}")
    if errors or any(not c.ok for c in results):
        sys.exit(1)

if __name__ == "__main__":
    cli()
