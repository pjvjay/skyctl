"""
skyctl.cli
==========
Command-line interface for skyctl.

Commands:
    skyctl accounts                  List all configured accounts
    skyctl post <alias> <text>       Post from one account
    skyctl post-all <text>           Post from all accounts
    skyctl reply <alias> <uri> <text>  Reply to a post
    skyctl repost <alias> <uri>      Repost from one account
    skyctl schedule <alias> <text> <datetime>  Schedule a post
    skyctl jobs                      List scheduled jobs
    skyctl cancel <job-id>           Cancel a scheduled job
    skyctl run-scheduler             Start the scheduler (blocking)
    skyctl timeline <alias>          Show home timeline
    skyctl notifications <alias>     Show notifications
    skyctl profile [alias] [handle]  Show a profile
"""

import sys
from datetime import datetime, timezone

import click
from rich.console import Console
from rich.table import Table
from rich import print as rprint

from .manager import BskyManager
from .scheduler import BskyScheduler

console = Console()


def get_manager() -> BskyManager:
    try:
        return BskyManager.from_env()
    except ValueError as e:
        console.print(f"[red]Configuration error:[/red] {e}")
        sys.exit(1)


@click.group()
def cli():
    """skyctl — Multi-account Bluesky automation tool."""
    pass


# ------------------------------------------------------------------
# Account info
# ------------------------------------------------------------------

@cli.command()
def accounts():
    """List all configured accounts."""
    manager = get_manager()
    table = Table(title="Configured Accounts")
    table.add_column("Alias", style="cyan")
    table.add_column("Handle", style="green")
    for alias in manager.aliases():
        client = manager._clients[alias]
        table.add_row(alias, client.config.handle)
    console.print(table)


# ------------------------------------------------------------------
# Posting
# ------------------------------------------------------------------

@cli.command()
@click.argument("alias")
@click.argument("text")
@click.option("--image", "-i", multiple=True, help="Image file path (up to 4).")
def post(alias, text, image):
    """Post from a single account.

    \b
    Example:
        skyctl post personal "Hello Bluesky!"
        skyctl post personal "Check this out" -i photo.jpg
    """
    manager = get_manager()
    with console.status(f"Posting from [cyan]{alias}[/cyan]..."):
        result = manager.post(alias, text, image_paths=list(image) or None)

    if result.success:
        console.print(f"[green]✓[/green] Posted from [cyan]{alias}[/cyan]")
        console.print(f"  {result.url}")
    else:
        console.print(f"[red]✗[/red] Failed: {result.error}")
        sys.exit(1)


@cli.command("post-all")
@click.argument("text")
@click.option("--exclude", "-x", multiple=True, help="Account aliases to skip.")
@click.option("--image", "-i", multiple=True, help="Image file path (up to 4).")
def post_all(text, exclude, image):
    """Post from all accounts simultaneously.

    \b
    Example:
        skyctl post-all "Big announcement!"
        skyctl post-all "Hello!" --exclude work
    """
    manager = get_manager()
    with console.status("Posting from all accounts..."):
        results = manager.post_all(text, image_paths=list(image) or None, exclude=list(exclude))

    for r in results:
        if r.success:
            console.print(f"[green]✓[/green] [cyan]{r.account}[/cyan] → {r.url}")
        else:
            console.print(f"[red]✗[/red] [cyan]{r.account}[/cyan] → {r.error}")


@cli.command()
@click.argument("alias")
@click.argument("parent_uri")
@click.argument("text")
def reply(alias, parent_uri, text):
    """Reply to a post.

    \b
    Example:
        skyctl reply personal at://did:plc:xxx/app.bsky.feed.post/yyy "Great point!"
    """
    manager = get_manager()
    with console.status(f"Replying from [cyan]{alias}[/cyan]..."):
        result = manager.reply(alias, text, parent_uri=parent_uri)

    if result.success:
        console.print(f"[green]✓[/green] Reply posted from [cyan]{alias}[/cyan]")
        console.print(f"  {result.url}")
    else:
        console.print(f"[red]✗[/red] Failed: {result.error}")
        sys.exit(1)


@cli.command()
@click.argument("alias")
@click.argument("uri")
def repost(alias, uri):
    """Repost from a specific account.

    \b
    Example:
        skyctl repost work at://did:plc:xxx/app.bsky.feed.post/yyy
    """
    manager = get_manager()
    with console.status(f"Reposting from [cyan]{alias}[/cyan]..."):
        result = manager.repost(alias, uri)

    if result.success:
        console.print(f"[green]✓[/green] Reposted from [cyan]{alias}[/cyan]")
    else:
        console.print(f"[red]✗[/red] Failed: {result.error}")
        sys.exit(1)


# ------------------------------------------------------------------
# Scheduler
# ------------------------------------------------------------------

@cli.command()
@click.argument("alias")
@click.argument("text")
@click.argument("run_at")  # ISO datetime string e.g. "2026-03-01T09:00:00"
@click.option("--reply-to", default=None, help="AT URI to reply to.")
def schedule(alias, text, run_at, reply_to):
    """Schedule a post for a future time.

    RUN_AT should be an ISO datetime string in UTC, e.g. 2026-03-01T09:00:00

    \b
    Example:
        skyctl schedule personal "Morning!" 2026-03-01T09:00:00
        skyctl schedule work "Follow-up" 2026-03-01T10:00:00 --reply-to at://...
    """
    try:
        dt = datetime.fromisoformat(run_at).replace(tzinfo=timezone.utc)
    except ValueError:
        console.print(f"[red]Invalid datetime:[/red] {run_at}. Use ISO format: 2026-03-01T09:00:00")
        sys.exit(1)

    manager = get_manager()
    scheduler = BskyScheduler(manager)
    sp = scheduler.schedule(alias=alias, text=text, run_at=dt, reply_to_uri=reply_to)
    console.print(f"[green]✓[/green] Scheduled job [cyan]{sp.id}[/cyan]")
    console.print(f"  Account : {alias}")
    console.print(f"  Send at : {dt.isoformat()} UTC")
    console.print(f"  Preview : {text[:80]}")


@cli.command()
def jobs():
    """List all scheduled jobs."""
    manager = get_manager()
    scheduler = BskyScheduler(manager)
    job_list = scheduler.list_jobs()

    if not job_list:
        console.print("No scheduled jobs.")
        return

    table = Table(title="Scheduled Jobs")
    table.add_column("ID", style="cyan")
    table.add_column("Account", style="green")
    table.add_column("Next Run", style="yellow")
    table.add_column("Status")
    table.add_column("Text Preview")

    for j in job_list:
        table.add_row(j["id"], j["account"], j["next_run"], j["status"], j["text_preview"])

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
def run_scheduler():
    """Start the scheduler and process all queued jobs (blocking)."""
    manager = get_manager()
    scheduler = BskyScheduler(manager)
    jobs_list = scheduler.list_jobs()
    console.print(f"Starting scheduler with [cyan]{len(jobs_list)}[/cyan] job(s)...")
    scheduler.start(blocking=True)


# ------------------------------------------------------------------
# Feeds
# ------------------------------------------------------------------

@cli.command()
@click.argument("alias")
@click.option("--limit", "-n", default=10, show_default=True, help="Number of posts.")
def timeline(alias, limit):
    """Show the home timeline for an account."""
    manager = get_manager()
    with console.status(f"Loading timeline for [cyan]{alias}[/cyan]..."):
        posts = manager[alias].get_timeline(limit=limit)

    for p in posts:
        console.rule(f"[dim]{p['author']}[/dim]  {p['created_at'][:10]}")
        console.print(p["text"])
        console.print(f"[dim]❤ {p['likes']}  🔁 {p['reposts']}  💬 {p['replies']}[/dim]")
    console.print()


@cli.command()
@click.argument("alias")
@click.option("--limit", "-n", default=10, show_default=True)
def notifications(alias, limit):
    """Show recent notifications for an account."""
    manager = get_manager()
    with console.status(f"Loading notifications for [cyan]{alias}[/cyan]..."):
        notifs = manager[alias].get_notifications(limit=limit)

    for n in notifs:
        reason = n.get("reason", "unknown")
        actor = n.get("author", {}).get("handle", "?")
        is_read = n.get("is_read", True)
        marker = "[yellow]●[/yellow]" if not is_read else " "
        console.print(f"{marker} [cyan]{actor}[/cyan] [dim]{reason}[/dim]")


@cli.command()
@click.argument("alias")
@click.option("--handle", default=None, help="Look up a different handle instead.")
def profile(alias, handle):
    """Show profile info for an account (or any handle)."""
    manager = get_manager()
    with console.status("Loading profile..."):
        data = manager[alias].get_profile(handle)

    console.print(f"[bold]{data.get('display_name', '')}[/bold]  @{data.get('handle', '')}")
    console.print(data.get("description", ""))
    console.print(
        f"[dim]Followers: {data.get('followers_count', 0)}  "
        f"Following: {data.get('follows_count', 0)}  "
        f"Posts: {data.get('posts_count', 0)}[/dim]"
    )


if __name__ == "__main__":
    cli()
