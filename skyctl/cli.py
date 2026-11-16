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
    skyctl agents explore <group>    Show the group's shared feed
    skyctl agents comment <group>    Agents reply to each other's posts
    skyctl agents research <group>   Agents research a topic and post findings
    skyctl agents round <group>      Run full rounds of interaction

Most commands accept --json for machine-readable output.
"""

import contextlib
import json
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


def get_manager() -> BskyManager:
    try:
        return BskyManager.from_env()
    except ValueError as e:
        console.print(f"[red]Configuration error:[/red] {e}")
        sys.exit(1)


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
    scheduler = BskyScheduler(manager)

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
    scheduler = BskyScheduler(manager)
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
    scheduler = BskyScheduler(manager)
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
    scheduler = BskyScheduler(manager, max_retries=max_retries, retry_delay=retry_delay)
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
            "skipped": "[dim]-[/dim]",
            "failed": "[red]✗[/red]",
        }
        if not actions:
            console.print("Nothing to do.")
        for a in actions:
            what = a.action
            if a.target_author:
                what += f" → {a.target_author}"
            console.print(
                f"{marks[a.status]} [cyan]{a.agent}[/cyan] {what} "
                f"[dim]({a.status}, {escape(a.model)})[/dim]"
            )
            if a.text:
                console.print(f"  {escape(a.text)}".replace("\n", "\n  "))
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
    """Persona-driven agent groups on a private test PDS.

    Groups are defined in agents.toml (see agents.example.toml). They only
    run against a self-hosted PDS, never the public Bluesky network.
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
        table.add_column("Model", style="dim")
        for p in g.members:
            table.add_row(
                p.alias, p.account, p.name, p.model or g.model or default_model()
            )
        console.print(table)


@agents.command("explore")
@click.argument("group")
@click.option("--limit", "-n", default=30, show_default=True, help="Posts per member.")
@config_option
@json_option
def agents_explore(group, limit, config_file, as_json):
    """Show the posts the group's members have written, oldest first."""
    g = get_group(group, config_file, as_json)
    feed = run_agents(as_json, "Reading feeds...", lambda: g.explore(limit=limit))
    if as_json:
        emit_json(feed)
        return
    if not feed:
        console.print("No posts yet. Try: skyctl agents research " + group)
    for p in feed:
        indent = "  " * p["depth"]
        console.print(
            f"{indent}[cyan]{p['agent']}[/cyan] [dim]{str(p['created_at'])[:16]}[/dim]"
        )
        console.print(f"{indent}{escape(p['text'])}")


@agents.command("comment")
@click.argument("group")
@agent_option
@click.option("--max", "max_comments", default=2, show_default=True,
              help="Most replies each agent writes.")
@dry_run_option
@config_option
@json_option
def agents_comment(group, agent, max_comments, dry_run, config_file, as_json):
    """Have agents reply to each other's posts, in character.

    \b
    Example:
        skyctl agents comment debate --agent skeptic --dry-run
    """
    g = get_group(group, config_file, as_json)
    actions = run_agents(
        as_json, "Agents are reading and replying...",
        lambda: g.comment(agent=agent, max_comments=max_comments, dry_run=dry_run),
    )
    report_actions(actions, as_json)


@agents.command("research")
@click.argument("group")
@agent_option
@click.option("--topic", default=None,
              help="What to research (default: the group's topic, or the agent's choice).")
@click.option("--url", "urls", multiple=True,
              help="A page to give the agents as source material. Repeatable.")
@click.option("--max-posts", default=3, show_default=True,
              help="Longest thread an agent may post.")
@dry_run_option
@config_option
@json_option
def agents_research(group, agent, topic, urls, max_posts, dry_run, config_file, as_json):
    """Have agents research a topic and post their findings.

    \b
    Example:
        skyctl agents research debate --topic "Four-day work week" \\
            --url https://example.org/study
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


@agents.command("round")
@click.argument("group")
@click.option("--rounds", "-r", default=1, show_default=True, help="Rounds to run.")
@click.option("--research", "research", is_flag=True,
              help="Every agent researches and posts before the first round.")
@click.option("--topic", default=None, help="Topic for any research this run.")
@click.option("--max", "max_comments", default=2, show_default=True,
              help="Most replies each agent writes per round.")
@dry_run_option
@config_option
@json_option
def agents_round(group, rounds, research, topic, max_comments, dry_run, config_file, as_json):
    """Run rounds: agents with no posts research first, then all comment."""
    g = get_group(group, config_file, as_json)
    actions = run_agents(
        as_json, "Running rounds...",
        lambda: g.run_round(
            rounds=rounds, research=research, topic=topic,
            max_comments=max_comments, dry_run=dry_run,
        ),
    )
    report_actions(actions, as_json)


if __name__ == "__main__":
    cli()
