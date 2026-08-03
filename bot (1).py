"""
Discord "repeat message" bot.

Commands (work both as prefix commands, e.g. "!repeat ...", AND as
slash commands, e.g. "/repeat ..."):

  repeat <count> <interval_seconds> <message>
      Sends <message> to the current channel <count> times,
      waiting <interval_seconds> seconds between each send.
      Access is restricted by role / user tier -- see ACCESS TIERS below.

  stop
      Stops any repeating job currently running in the current channel.
      Usable by anyone who belongs to any access tier.

  status
      Shows whether a repeating job is currently running in this channel,
      and reminds the caller of their own tier limits / cooldown.

Example:
  !repeat 10 60 Don't forget to vote!
      -> sends "Don't forget to vote!" 10 times, once a minute.


VISIBILITY
  - Confirmation / error / status replies are private (ephemeral) ONLY when
    the command is run as a slash command ("/repeat ..."), because Discord
    only supports private replies for slash commands, not "!" text commands.
  - The actual repeated broadcast messages are always sent normally to the
    channel, visible to everyone -- only the bot's own status replies can
    be made private.


ACCESS TIERS
  Three tiers control who can use `repeat`, how many messages they can send
  per job, and how long they must wait between launching jobs:

    - TIER_UNLIMITED : no message-count cap, no cooldown
    - TIER_10        : max 10 messages per job, cooldown between jobs
    - TIER_5         : max 5 messages per job,  cooldown between jobs

  Anyone not listed in any tier cannot use `repeat` (or `stop` / `status`).

  Configure tiers in your .env file with comma-separated role NAMES or role
  IDs (IDs are safer -- names break if someone renames the role), and/or
  specific user IDs to grant access to individual people regardless of role:

    TIER_UNLIMITED_ROLES=Admin,Moderator
    TIER_UNLIMITED_USER_IDS=123456789012345678

    TIER_10_ROLES=Trusted
    TIER_10_USER_IDS=

    TIER_5_ROLES=Member
    TIER_5_USER_IDS=

    TIER_COOLDOWN_SECONDS=300

  If a user qualifies for more than one tier (e.g. they have both the
  "Trusted" and "Member" roles), the highest tier (most permissive) wins.
"""

import os
import time
import asyncio

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()  # reads config from a local .env file

TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("DISCORD_PREFIX", "!")

# Optional: set DISCORD_GUILD_ID to sync slash commands instantly to one
# server while testing. Guild-scoped syncs show up immediately; a global
# sync (no guild id) can take up to ~an hour to propagate the first time.
GUILD_ID = os.getenv("DISCORD_GUILD_ID")


def _parse_list(env_value: str) -> set[str]:
    """Split a comma-separated .env value into a set of lowercase, trimmed
    tokens. Empty / unset values become an empty set."""
    if not env_value:
        return set()
    return {token.strip().lower() for token in env_value.split(",") if token.strip()}


def _split_ids_and_names(tokens: set[str]) -> tuple[set[int], set[str]]:
    """Separate a set of tokens into numeric Discord IDs and plain names."""
    ids, names = set(), set()
    for token in tokens:
        if token.isdigit():
            ids.add(int(token))
        else:
            names.add(token)
    return ids, names


_TIER_COOLDOWN = float(os.getenv("TIER_COOLDOWN_SECONDS", "300"))

# Each tier: (max_count_or_None, cooldown_seconds, roles_env, users_env)
# Defaults below are pre-filled with the IDs provided at setup time. You can
# still override any of these from Railway's Variables tab (or a local .env)
# without touching this file -- just set the matching env var name.
_raw_tiers = {
    "unlimited": (None, 0.0,
                  os.getenv("TIER_UNLIMITED_ROLES", "1533819175015809044"),
                  os.getenv("TIER_UNLIMITED_USER_IDS", "1391015337801154580")),
    "10": (10, _TIER_COOLDOWN,
           os.getenv("TIER_10_ROLES", "1514689326099988693"),
           os.getenv("TIER_10_USER_IDS", "")),
    "5": (5, _TIER_COOLDOWN,
          os.getenv("TIER_5_ROLES", "1470667260816392328"),
          os.getenv("TIER_5_USER_IDS", "")),
}

TIERS: dict[str, dict] = {}
for tier_name, (max_count, cooldown, roles_env, users_env) in _raw_tiers.items():
    role_ids, role_names = _split_ids_and_names(_parse_list(roles_env))
    user_ids, _ = _split_ids_and_names(_parse_list(users_env))
    TIERS[tier_name] = {
        "max_count": max_count,
        "cooldown": cooldown,
        "role_ids": role_ids,
        "role_names": role_names,
        "user_ids": user_ids,
    }

# Tier precedence, most permissive first.
_TIER_ORDER = ["unlimited", "10", "5"]

# Per-user cooldown tracking: {user_id: last_job_start_unix_time}
_last_used: dict[int, float] = {}


def get_tier(member: discord.abc.User):
    """Return (tier_name, tier_config) for a member, or None if they don't
    belong to any configured tier."""
    member_role_ids = set()
    member_role_names = set()
    if isinstance(member, discord.Member):
        member_role_ids = {r.id for r in member.roles}
        member_role_names = {r.name.lower() for r in member.roles}

    for tier_name in _TIER_ORDER:
        cfg = TIERS[tier_name]
        if member.id in cfg["user_ids"]:
            return tier_name, cfg
        if member_role_ids & cfg["role_ids"]:
            return tier_name, cfg
        if member_role_names & cfg["role_names"]:
            return tier_name, cfg
    return None


def format_seconds(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}m{secs:02d}s" if secs else f"{minutes}m"


intents = discord.Intents.default()
intents.message_content = True  # required to read "!" command text
intents.members = True  # required to reliably read roles for access control

bot = commands.Bot(command_prefix=PREFIX, intents=intents)

# Tracks running jobs so we can stop them: {channel_id: asyncio.Task}
running_jobs: dict[int, asyncio.Task] = {}


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            synced = await bot.tree.sync(guild=guild)
            print(f"Synced {len(synced)} slash command(s) to guild {GUILD_ID}.")
        else:
            synced = await bot.tree.sync()
            print(f"Synced {len(synced)} slash command(s) globally (can take up to ~1hr to appear).")
    except Exception as e:
        print(f"Failed to sync slash commands: {e}")


async def _reply(ctx: commands.Context, content: str):
    """Send a reply that's private (ephemeral) when invoked as a slash
    command, and a normal message when invoked with the prefix (plain text
    messages can't be made private)."""
    if ctx.interaction is not None:
        await ctx.send(content, ephemeral=True)
    else:
        await ctx.send(content)


@bot.hybrid_command(name="repeat", description="Send a message a number of times, with a delay between each.")
@discord.app_commands.describe(
    count="How many times to send the message",
    interval_seconds="Seconds to wait between each send",
    message="The message to repeat",
)
async def repeat(ctx: commands.Context, count: int, interval_seconds: float, *, message: str):
    """Send `message` `count` times, `interval_seconds` apart, in this channel."""

    if ctx.guild is None:
        await _reply(ctx, "This command can only be used in a server.")
        return

    tier_result = get_tier(ctx.author)
    if tier_result is None:
        await _reply(ctx, "You don't have permission to use this command.")
        return
    tier_name, tier_cfg = tier_result

    if count < 1:
        await _reply(ctx, "Count must be at least 1.")
        return
    if interval_seconds < 1:
        await _reply(ctx, "Interval must be at least 1 second.")
        return

    max_count = tier_cfg["max_count"]
    if max_count is not None and count > max_count:
        await _reply(ctx, f"Your access level allows a maximum of {max_count} messages per job (you asked for {count}).")
        return

    cooldown = tier_cfg["cooldown"]
    if cooldown > 0:
        last = _last_used.get(ctx.author.id)
        if last is not None:
            elapsed = time.time() - last
            if elapsed < cooldown:
                remaining = cooldown - elapsed
                await _reply(ctx, f"You're on cooldown. Try again in {format_seconds(remaining)}.")
                return

    channel_id = ctx.channel.id
    if channel_id in running_jobs:
        await _reply(ctx, "There's already a repeating job running in this channel. Use `stop` first.")
        return

    channel = ctx.channel

    async def job():
        try:
            for i in range(1, count + 1):
                await channel.send(message)
                if i < count:
                    await asyncio.sleep(interval_seconds)
            await channel.send(f"Done — sent {count} messages.")
        except asyncio.CancelledError:
            await channel.send("Repeating job stopped.")
            raise
        finally:
            running_jobs.pop(channel_id, None)

    task = asyncio.create_task(job())
    running_jobs[channel_id] = task
    _last_used[ctx.author.id] = time.time()

    await _reply(
        ctx,
        f"Starting: will send that message {count} time(s), every {interval_seconds}s.",
    )


@bot.hybrid_command(name="stop", description="Stop the repeating job running in this channel, if any.")
async def stop(ctx: commands.Context):
    """Stop the repeating job running in this channel, if any."""
    if ctx.guild is None or get_tier(ctx.author) is None:
        await _reply(ctx, "You don't have permission to use this command.")
        return

    task = running_jobs.get(ctx.channel.id)
    if not task:
        await _reply(ctx, "No repeating job is running in this channel.")
        return
    task.cancel()
    await _reply(ctx, "Stopping the repeating job...")


@bot.hybrid_command(name="status", description="Check whether a repeating job is running in this channel.")
async def status(ctx: commands.Context):
    """Report whether a repeating job is active in this channel, plus the caller's own tier info."""
    if ctx.guild is None or get_tier(ctx.author) is None:
        await _reply(ctx, "You don't have permission to use this command.")
        return

    tier_name, tier_cfg = get_tier(ctx.author)
    if tier_cfg["max_count"] is None:
        limit_text = "no message limit, no cooldown"
    else:
        limit_text = f"max {tier_cfg['max_count']} messages per job, {format_seconds(tier_cfg['cooldown'])} cooldown"

    if ctx.channel.id in running_jobs:
        job_text = "A repeating job is currently running in this channel."
    else:
        job_text = "No repeating job is running in this channel."

    await _reply(ctx, f"{job_text}\nYour access level: {limit_text}.")


@repeat.error
async def repeat_error(ctx: commands.Context, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await _reply(
            ctx,
            "Usage: `!repeat <count> <interval_seconds> <message>` (or `/repeat`)\n"
            "Example: `!repeat 5 30 Hello there`",
        )
    elif isinstance(error, commands.BadArgument):
        await _reply(ctx, "Count must be a whole number and interval must be a number of seconds.")
    else:
        raise error


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(
            "No DISCORD_TOKEN found. Create a .env file with DISCORD_TOKEN=your_token_here"
        )
    bot.run(TOKEN)
