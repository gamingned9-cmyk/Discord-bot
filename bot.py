"""
Discord multi-purpose bot.

============================================================================
COMMANDS (work both as "!prefix" text commands AND as "/slash" commands)
============================================================================

  repeat <count> <interval_seconds> <message>
      Sends <message> to the current channel <count> times, waiting
      <interval_seconds> seconds between each send. Access + limits are
      controlled by role tier -- see ACCESS TIERS below. If an admin has run
      `/setup kind:repeat channel:#x`, this only works in that one channel.

  stop
      Stops any repeating job currently running in the current channel.

  status
      Shows whether a repeating job is running here, plus the caller's tier.

  setup kind:<repeat|nickname|modlog|announce> channel:<#channel>   [ADMIN]
      kind=repeat   -> locks the `repeat` command to only work in that
                       channel (server-wide).
      kind=nickname -> posts a message with a "Change Nickname" button in
                       that channel. Anyone can click it to request a
                       nickname change.
      kind=modlog   -> nickname requests get posted here for admins to
                       Approve / Deny. Make this channel admin-only using
                       Discord's normal channel permissions -- the bot does
                       not restrict channel visibility itself, though it
                       double-checks the clicker has Administrator before
                       acting on Approve/Deny either way.
      kind=announce -> when a request is approved, a confirmation message
                       is posted here. If not set, it falls back to the
                       channel the user originally requested from.

  backup <message>
      Sends a message in the current channel that pings a predefined
      "backup" role, followed by your message. Requires an access tier.
      Configure the role with BACKUP_ROLE_ID.

  dev
      Hidden from the "/" command list for non-admins (Discord only
      supports hiding commands by permission, not per-command secrecy).
      Posts "THIS BOT IS DEVELOPED BY <mention>". Configure DEV_USER_ID.

============================================================================
NICKNAME REQUEST FLOW
============================================================================
  1. User clicks the button posted via `/setup kind:nickname ...` and enters
     a desired nickname in the popup.
  2. If a `modlog` channel is configured, the request is posted there with
     Approve/Deny buttons instead of being applied immediately.
  3. An admin clicks Approve -> nickname is changed, and a confirmation is
     posted in the `announce` channel (or the original request channel if
     `announce` isn't set). Deny -> the requester gets a DM explaining the
     request was denied.
  4. If no `modlog` channel is configured at all, nicknames are changed
     immediately with no approval step (useful for smaller/simpler servers).
  Pending requests survive bot restarts (saved to bot_data.json).

============================================================================
VISIBILITY
============================================================================
  Confirmation / error / status replies are private (ephemeral) ONLY when a
  command is run as a slash command ("/repeat ..."), because Discord only
  supports private replies for slash commands, not "!" text commands.

============================================================================
ACCESS TIERS  (controls who can use `repeat` / `backup`, and their limits)
============================================================================
  - TIER_UNLIMITED : no message-count cap, no cooldown
  - TIER_10        : max 10 messages per job, cooldown between jobs
  - TIER_5         : max 5 messages per job,  cooldown between jobs

  Anyone not listed in any tier cannot use `repeat`, `backup`, or `stop`.

  Configure in Railway variables (or a local .env) with comma-separated role
  NAMES or role IDs, and/or specific user IDs:

    TIER_UNLIMITED_ROLES=Admin,Moderator
    TIER_UNLIMITED_USER_IDS=123456789012345678

    TIER_10_ROLES=Trusted
    TIER_10_USER_IDS=

    TIER_5_ROLES=Member
    TIER_5_USER_IDS=

    TIER_COOLDOWN_SECONDS=300

  If a user qualifies for more than one tier, the highest tier wins.

============================================================================
OTHER CONFIG
============================================================================
    BACKUP_ROLE_ID=role_id_to_ping_on_/backup
    DEV_USER_ID=your_discord_user_id
    BILLY_USER_ID=the_only_discord_user_id_allowed_to_run_/setup

============================================================================
BOT ACTIVITY LOGGING
============================================================================
  Configure a log channel with `/setup kind:botlog channel:#x`. Once set,
  every command attempt (allowed or denied) is posted there, including:
  command used, the caller's username + Discord ID, a timestamp, whether
  the action was allowed or denied, and the reason for any denial.

============================================================================
/setup ACCESS
============================================================================
  `/setup` is visible to everyone in the Application Commands menu, but
  only the user configured via BILLY_USER_ID can actually run it. Anyone
  else who tries gets an ephemeral "You're not BILLY." reply.

============================================================================
/dev
============================================================================
  Visible to everyone, usable by everyone. Always replies ephemerally.
"""

import os
import json
import time
import asyncio
from pathlib import Path
from typing import Literal

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()  # reads config from a local .env file (no-op if none exists)

TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("DISCORD_PREFIX", "/")

# Optional: set to sync slash commands instantly to one server while testing.
GUILD_ID = os.getenv("DISCORD_GUILD_ID")

BACKUP_ROLE_ID = os.getenv("BACKUP_ROLE_ID", "")
DEV_USER_ID = os.getenv("DEV_USER_ID", "")  # leave blank until you set it
BILLY_USER_ID = os.getenv("BILLY_USER_ID", "")  # only this user can run /setup

# ---------------------------------------------------------------------------
# Small local JSON store so /setup choices and pending nickname requests
# survive a bot restart. NOTE: on Railway this file lives on the container
# disk -- it persists across restarts but is wiped on a fresh redeploy
# unless you attach a Railway volume.
# ---------------------------------------------------------------------------
DATA_FILE = Path("bot_data.json")

_DEFAULT_DATA = {
    "setup_channels": {"repeat": {}, "nickname": {}, "modlog": {}, "announce": {}, "botlog": {}},
    "nickname_requests": {},
    "next_request_id": 1,
}


def load_data() -> dict:
    if DATA_FILE.exists():
        try:
            data = json.loads(DATA_FILE.read_text())
            # backfill any keys missing from older data files
            for key, default in _DEFAULT_DATA.items():
                data.setdefault(key, default if not isinstance(default, dict) else dict(default))
            for kind in ("repeat", "nickname", "modlog", "announce", "botlog"):
                data["setup_channels"].setdefault(kind, {})
            return data
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "setup_channels": {"repeat": {}, "nickname": {}, "modlog": {}, "announce": {}, "botlog": {}},
        "nickname_requests": {},
        "next_request_id": 1,
    }


def save_data(data: dict) -> None:
    DATA_FILE.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Access tier configuration
# ---------------------------------------------------------------------------
def _parse_list(env_value: str) -> set[str]:
    if not env_value:
        return set()
    return {token.strip().lower() for token in env_value.split(",") if token.strip()}


def _split_ids_and_names(tokens: set[str]) -> tuple[set[int], set[str]]:
    ids, names = set(), set()
    for token in tokens:
        if token.isdigit():
            ids.add(int(token))
        else:
            names.add(token)
    return ids, names


_TIER_COOLDOWN = float(os.getenv("TIER_COOLDOWN_SECONDS", "300"))

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

_TIER_ORDER = ["unlimited", "10", "5"]  # most permissive first

_last_used: dict[int, float] = {}


def get_tier(member: discord.abc.User):
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


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)

running_jobs: dict[int, asyncio.Task] = {}


async def _reply(ctx: commands.Context, content: str):
    if ctx.interaction is not None:
        await ctx.send(content, ephemeral=True)
    else:
        await ctx.send(content)


# ---------------------------------------------------------------------------
# Bot activity logging (posts to the configured `botlog` channel, if any)
# ---------------------------------------------------------------------------
async def log_action(
    guild: discord.Guild | None,
    command: str,
    user: discord.abc.User,
    allowed: bool,
    reason: str | None = None,
):
    if guild is None:
        return

    data = load_data()
    botlog_channel_id = data["setup_channels"].get("botlog", {}).get(str(guild.id))
    if not botlog_channel_id:
        return

    channel = guild.get_channel(botlog_channel_id)
    if channel is None:
        return

    embed = discord.Embed(
        title=f"/{command}",
        color=discord.Color.green() if allowed else discord.Color.red(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="User", value=f"{user} ({user.id})", inline=False)
    embed.add_field(name="Result", value="✅ Allowed" if allowed else "❌ Denied", inline=True)
    if reason:
        embed.add_field(name="Reason", value=reason, inline=False)

    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        pass  # bot lacks permission to post in the configured botlog channel


# ---------------------------------------------------------------------------
# /setup access control -- only BILLY_USER_ID may run it, regardless of
# server Administrator status.
# ---------------------------------------------------------------------------
class NotBilly(commands.CheckFailure):
    pass


def is_billy():
    async def predicate(ctx: commands.Context) -> bool:
        if not BILLY_USER_ID or str(ctx.author.id) != str(BILLY_USER_ID):
            raise NotBilly()
        return True
    return commands.check(predicate)


# ---------------------------------------------------------------------------
# Nickname request / approval flow
# ---------------------------------------------------------------------------
async def _apply_nickname_change(guild: discord.Guild, user_id: int, nickname: str):
    """Try to change a member's nickname. Returns (success, error_message)."""
    member = guild.get_member(user_id)
    if member is None:
        return False, "That member is no longer in the server."
    try:
        await member.edit(nick=nickname)
        return True, None
    except discord.Forbidden:
        return False, (
            "I don't have permission to change that member's nickname. "
            "Make sure my role is positioned above theirs in Server Settings > Roles."
        )
    except discord.HTTPException as e:
        return False, f"Discord rejected the nickname change: {e}"


class ApproveButton(discord.ui.Button):
    def __init__(self, request_id: int):
        super().__init__(label="Approve", style=discord.ButtonStyle.success,
                          custom_id=f"nickname_approve:{request_id}")
        self.request_id = request_id

    async def callback(self, interaction: discord.Interaction):
        await _handle_nickname_decision(interaction, self.request_id, approve=True)


class DenyButton(discord.ui.Button):
    def __init__(self, request_id: int):
        super().__init__(label="Deny", style=discord.ButtonStyle.danger,
                          custom_id=f"nickname_deny:{request_id}")
        self.request_id = request_id

    async def callback(self, interaction: discord.Interaction):
        await _handle_nickname_decision(interaction, self.request_id, approve=False)


class ApprovalView(discord.ui.View):
    def __init__(self, request_id: int):
        super().__init__(timeout=None)
        self.add_item(ApproveButton(request_id))
        self.add_item(DenyButton(request_id))


async def _handle_nickname_decision(interaction: discord.Interaction, request_id: int, approve: bool):
    if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Only administrators can approve or deny requests.", ephemeral=True)
        await log_action(
            interaction.guild, "nickname_decision", interaction.user, False,
            reason="Not a server administrator",
        )
        return

    data = load_data()
    req = data.get("nickname_requests", {}).get(str(request_id))
    if req is None or req.get("status") != "pending":
        await interaction.response.send_message("This request is no longer valid (already handled).", ephemeral=True)
        await log_action(
            interaction.guild, "nickname_decision", interaction.user, False,
            reason="Request already handled or invalid",
        )
        return

    guild = interaction.guild
    nickname = req["nickname"]
    requester_id = req["user_id"]

    if approve:
        success, error = await _apply_nickname_change(guild, requester_id, nickname)
        if not success:
            await interaction.response.send_message(error, ephemeral=True)
            return

        req["status"] = "approved"
        save_data(data)
        await interaction.response.edit_message(
            content=f"✅ Nickname request approved by {interaction.user.mention}.", view=None
        )

        announce_channel_id = data["setup_channels"]["announce"].get(str(guild.id))
        target_channel = guild.get_channel(announce_channel_id) if announce_channel_id else None
        if target_channel is None:
            target_channel = guild.get_channel(req.get("channel_id"))
        if target_channel:
            member = guild.get_member(requester_id)
            mention = member.mention if member else f"<@{requester_id}>"
            await target_channel.send(f"✅ Name change approved for {mention}: now **{nickname}**.")
        await log_action(
            guild, "nickname_approve", interaction.user, True,
            reason=f"Approved nickname '{nickname}' for <@{requester_id}>",
        )
    else:
        req["status"] = "denied"
        save_data(data)
        await interaction.response.edit_message(
            content=f"❌ Nickname request denied by {interaction.user.mention}.", view=None
        )
        member = guild.get_member(requester_id)
        if member:
            try:
                await member.send(f"Your nickname change request to **{nickname}** was denied.")
            except discord.Forbidden:
                pass  # user has DMs closed -- nothing more we can do

        announce_channel_id = data["setup_channels"]["announce"].get(str(guild.id))
        target_channel = guild.get_channel(announce_channel_id) if announce_channel_id else None
        if target_channel is None:
            target_channel = guild.get_channel(req.get("channel_id"))
        if target_channel:
            mention = member.mention if member else f"<@{requester_id}>"
            await target_channel.send(f"❌ Name change denied for {mention}: requested **{nickname}**.")

        await log_action(
            guild, "nickname_deny", interaction.user, True,
            reason=f"Denied nickname '{nickname}' for <@{requester_id}>",
        )


class NicknameModal(discord.ui.Modal, title="Change Your Nickname"):
    nickname = discord.ui.TextInput(
        label="New nickname",
        placeholder="Enter the nickname you want",
        max_length=32,
        min_length=1,
    )

    async def on_submit(self, interaction: discord.Interaction):
        new_nick = self.nickname.value.strip()
        guild = interaction.guild
        data = load_data()
        modlog_channel_id = data["setup_channels"]["modlog"].get(str(guild.id))

        if not modlog_channel_id:
            # No approval workflow configured -- change immediately.
            success, error = await _apply_nickname_change(guild, interaction.user.id, new_nick)
            if success:
                await interaction.response.send_message(f"Your nickname has been changed to **{new_nick}**.", ephemeral=True)
                await log_action(guild, "nickname_request", interaction.user, True, reason=f"Changed immediately to '{new_nick}'")
            else:
                await interaction.response.send_message(error, ephemeral=True)
                await log_action(guild, "nickname_request", interaction.user, False, reason=error)
            return

        modlog_channel = guild.get_channel(modlog_channel_id)
        if modlog_channel is None:
            await interaction.response.send_message(
                "Nickname requests are misconfigured (mod channel not found). Contact an admin.", ephemeral=True
            )
            await log_action(guild, "nickname_request", interaction.user, False, reason="modlog channel not found")
            return

        request_id = data["next_request_id"]
        data["next_request_id"] = request_id + 1
        data["nickname_requests"][str(request_id)] = {
            "guild_id": guild.id,
            "user_id": interaction.user.id,
            "nickname": new_nick,
            "channel_id": interaction.channel_id,
            "status": "pending",
        }
        save_data(data)

        await modlog_channel.send(
            f"🔔 Nickname change request from {interaction.user.mention}: **{new_nick}**",
            view=ApprovalView(request_id),
        )
        await interaction.response.send_message("Your request has been sent for approval.", ephemeral=True)
        await log_action(guild, "nickname_request", interaction.user, True, reason=f"Requested '{new_nick}', pending approval")


class NicknameButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Change Nickname",
        style=discord.ButtonStyle.primary,
        custom_id="nickname_change_button",
    )
    async def change_nickname(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(NicknameModal())


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")

    # Re-register persistent views so buttons keep working after restarts.
    bot.add_view(NicknameButtonView())
    data = load_data()
    for req_id_str, req in data.get("nickname_requests", {}).items():
        if req.get("status") == "pending":
            bot.add_view(ApprovalView(int(req_id_str)))

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


# ---------------------------------------------------------------------------
# /repeat
# ---------------------------------------------------------------------------
@bot.hybrid_command(name="repeat", description="Send a message a number of times, with a delay between each.")
@discord.app_commands.describe(
    count="How many times to send the message",
    interval_seconds="Seconds to wait between each send",
    message="The message to repeat",
)
async def repeat(ctx: commands.Context, count: int, interval_seconds: float, *, message: str):
    if ctx.guild is None:
        await _reply(ctx, "This command can only be used in a server.")
        return

    data = load_data()
    locked_channel_id = data["setup_channels"]["repeat"].get(str(ctx.guild.id))
    if locked_channel_id and ctx.channel.id != locked_channel_id:
        channel_obj = ctx.guild.get_channel(locked_channel_id)
        location = channel_obj.mention if channel_obj else "the designated channel"
        await _reply(ctx, f"`repeat` can only be used in {location}.")
        await log_action(ctx.guild, "repeat", ctx.author, False, reason="Used outside the locked repeat channel")
        return

    tier_result = get_tier(ctx.author)
    if tier_result is None:
        await _reply(ctx, "You don't have permission to use this command.")
        await log_action(ctx.guild, "repeat", ctx.author, False, reason="No access tier assigned")
        return
    tier_name, tier_cfg = tier_result

    if count < 1:
        await _reply(ctx, "Count must be at least 1.")
        await log_action(ctx.guild, "repeat", ctx.author, False, reason="Count < 1")
        return
    if interval_seconds < 1:
        await _reply(ctx, "Interval must be at least 1 second.")
        await log_action(ctx.guild, "repeat", ctx.author, False, reason="Interval < 1 second")
        return

    is_admin = isinstance(ctx.author, discord.Member) and ctx.author.guild_permissions.administrator
    if count > 20 and not is_admin:
        await _reply(ctx, "KAZHAP CHILLARA ALLA NINTE BAN IDATTE FUNDE NINAK")
        await log_action(
            ctx.guild, "repeat", ctx.author, False,
            reason=f"Non-admin requested count={count} (limit is 20)",
        )
        return

    max_count = tier_cfg["max_count"]
    if max_count is not None and count > max_count:
        await _reply(ctx, f"Your access level allows a maximum of {max_count} messages per job (you asked for {count}).")
        await log_action(
            ctx.guild, "repeat", ctx.author, False,
            reason=f"Requested count={count} exceeds tier max of {max_count}",
        )
        return

    cooldown = tier_cfg["cooldown"]
    if cooldown > 0:
        last = _last_used.get(ctx.author.id)
        if last is not None:
            elapsed = time.time() - last
            if elapsed < cooldown:
                remaining = cooldown - elapsed
                await _reply(ctx, f"You're on cooldown. Try again in {format_seconds(remaining)}.")
                await log_action(
                    ctx.guild, "repeat", ctx.author, False,
                    reason=f"On cooldown, {format_seconds(remaining)} remaining",
                )
                return

    channel_id = ctx.channel.id
    if channel_id in running_jobs:
        await _reply(ctx, "There's already a repeating job running in this channel. Use `stop` first.")
        await log_action(ctx.guild, "repeat", ctx.author, False, reason="A job is already running in this channel")
        return

    channel = ctx.channel
    interaction = ctx.interaction  # captured now so the job can reply ephemerally when done

    async def job():
        try:
            for i in range(1, count + 1):
                await channel.send(message)
                if i < count:
                    await asyncio.sleep(interval_seconds)
            done_text = f"Done — sent {count} messages."
            if interaction is not None:
                try:
                    await interaction.followup.send(done_text, ephemeral=True)
                except discord.HTTPException:
                    await channel.send(done_text)
            else:
                # Text ("!") commands can't be made ephemeral -- Discord only
                # supports private replies for slash commands.
                await channel.send(done_text)
        except asyncio.CancelledError:
            await channel.send("Repeating job stopped.")
            raise
        finally:
            running_jobs.pop(channel_id, None)

    task = asyncio.create_task(job())
    running_jobs[channel_id] = task
    _last_used[ctx.author.id] = time.time()

    await _reply(ctx, f"Starting: will send that message {count} time(s), every {interval_seconds}s.")
    await log_action(
        ctx.guild, "repeat", ctx.author, True,
        reason=f"count={count}, interval={interval_seconds}s",
    )


@bot.hybrid_command(name="stop", description="Stop the repeating job running in this channel, if any.")
async def stop(ctx: commands.Context):
    if ctx.guild is None or get_tier(ctx.author) is None:
        await _reply(ctx, "You don't have permission to use this command.")
        await log_action(ctx.guild, "stop", ctx.author, False, reason="No access tier assigned")
        return

    task = running_jobs.get(ctx.channel.id)
    if not task:
        await _reply(ctx, "No repeating job is running in this channel.")
        await log_action(ctx.guild, "stop", ctx.author, False, reason="No job running in this channel")
        return
    task.cancel()
    await _reply(ctx, "Stopping the repeating job...")
    await log_action(ctx.guild, "stop", ctx.author, True)


@bot.hybrid_command(name="status", description="Check whether a repeating job is running in this channel.")
async def status(ctx: commands.Context):
    if ctx.guild is None or get_tier(ctx.author) is None:
        await _reply(ctx, "You don't have permission to use this command.")
        await log_action(ctx.guild, "status", ctx.author, False, reason="No access tier assigned")
        return

    tier_name, tier_cfg = get_tier(ctx.author)
    if tier_cfg["max_count"] is None:
        limit_text = "no message limit, no cooldown"
    else:
        limit_text = f"max {tier_cfg['max_count']} messages per job, {format_seconds(tier_cfg['cooldown'])} cooldown"

    job_text = (
        "A repeating job is currently running in this channel."
        if ctx.channel.id in running_jobs
        else "No repeating job is running in this channel."
    )

    await _reply(ctx, f"{job_text}\nYour access level: {limit_text}.")
    await log_action(ctx.guild, "status", ctx.author, True)


# ---------------------------------------------------------------------------
# /setup  (admin only)
# ---------------------------------------------------------------------------
@bot.hybrid_command(name="setup", description="Configure a single-purpose channel.")
@discord.app_commands.describe(
    kind="What this channel should be dedicated to",
    channel="The channel to configure",
)
@is_billy()
async def setup_cmd(
    ctx: commands.Context,
    kind: Literal["repeat", "nickname", "modlog", "announce", "botlog"],
    channel: discord.TextChannel,
):
    if ctx.guild is None:
        await _reply(ctx, "This command can only be used in a server.")
        return

    data = load_data()
    data["setup_channels"][kind][str(ctx.guild.id)] = channel.id
    save_data(data)

    if kind == "repeat":
        await _reply(ctx, f"Done. `repeat` can now only be used in {channel.mention}.")
    elif kind == "nickname":
        try:
            await channel.send(
                "Click the button below to request a server nickname change.",
                view=NicknameButtonView(),
            )
            await _reply(ctx, f"Done. Posted the nickname-request button in {channel.mention}.")
        except discord.Forbidden:
            await _reply(ctx, f"I don't have permission to post messages in {channel.mention}.")
    elif kind == "modlog":
        await _reply(
            ctx,
            f"Done. Nickname requests will now be sent to {channel.mention} for approval. "
            f"Make sure that channel's permissions restrict it to admins/mods.",
        )
    elif kind == "botlog":
        await _reply(ctx, f"Done. Bot activity logs will now be posted in {channel.mention}.")
    else:  # announce
        await _reply(ctx, f"Done. Approved nickname changes will be announced in {channel.mention}.")

    await log_action(ctx.guild, "setup", ctx.author, True, reason=f"kind={kind}, channel=#{channel.name}")


@setup_cmd.error
async def setup_cmd_error(ctx: commands.Context, error):
    if isinstance(error, NotBilly):
        await _reply(ctx, "You're not BILLY.")
        await log_action(ctx.guild, "setup", ctx.author, False, reason="Not the authorized BILLY user")
    elif isinstance(error, commands.MissingRequiredArgument):
        await _reply(ctx, "Usage: `/setup kind:<repeat|nickname|modlog|announce|botlog> channel:#channel`")
        await log_action(ctx.guild, "setup", ctx.author, False, reason="Missing required argument")
    else:
        raise error


# ---------------------------------------------------------------------------
# /backup
# ---------------------------------------------------------------------------
@bot.hybrid_command(name="backup", description="Ping the backup role with a message.")
@discord.app_commands.describe(message="The message to send along with the backup ping")
async def backup(ctx: commands.Context, *, message: str):
    if ctx.guild is None:
        await _reply(ctx, "This command can only be used in a server.")
        return

    if get_tier(ctx.author) is None:
        await _reply(ctx, "You don't have permission to use this command.")
        await log_action(ctx.guild, "backup", ctx.author, False, reason="No access tier assigned")
        return

    if not BACKUP_ROLE_ID:
        await _reply(ctx, "No backup role is configured. Set BACKUP_ROLE_ID.")
        await log_action(ctx.guild, "backup", ctx.author, False, reason="BACKUP_ROLE_ID not configured")
        return

    role = ctx.guild.get_role(int(BACKUP_ROLE_ID))
    if role is None:
        await _reply(ctx, "The configured backup role couldn't be found in this server.")
        await log_action(ctx.guild, "backup", ctx.author, False, reason="Configured backup role not found")
        return

    await ctx.channel.send(f"{role.mention} {message}")
    await _reply(ctx, "Backup call sent.")
    await log_action(ctx.guild, "backup", ctx.author, True)


# ---------------------------------------------------------------------------
# /dev  (hidden from the picker for non-admins)
# ---------------------------------------------------------------------------
@bot.hybrid_command(name="dev", description="Show info about the bot developer.")
async def dev(ctx: commands.Context):
    if not DEV_USER_ID:
        await _reply(ctx, "DEV_USER_ID isn't configured yet.")
        await log_action(ctx.guild, "dev", ctx.author, False, reason="DEV_USER_ID not configured")
        return
    await _reply(ctx, f"THIS BOT IS DEVELOPED BY <@{DEV_USER_ID}>")
    await log_action(ctx.guild, "dev", ctx.author, True)


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
