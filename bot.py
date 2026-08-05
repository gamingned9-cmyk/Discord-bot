import os
import json
import logging
from datetime import datetime, timezone
from typing import Literal, Optional, Dict, Any
import discord
from discord import app_commands
from discord.ext import commands

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("discord_bot")

DATA_FILE = os.environ.get("BOT_DATA_FILE", "bot_data.json")
BILLY_USER_ID = int(os.environ.get("BILLY_USER_ID", "123456789012345678"))

def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                data = json.load(f)
                if "guilds" not in data:
                    data["guilds"] = {}
                return data
        except Exception as e:
            logger.error(f"Error loading data: {e}")
    return {"guilds": {}}

def save_data(data: dict):
    try:
        with open(DATA_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving data: {e}")

def get_guild_data(guild_id: int) -> dict:
    data = load_data()
    g_id = str(guild_id)
    if g_id not in data["guilds"]:
        data["guilds"][g_id] = {
            "channels": {
                "repeat": None,
                "nickname": None,
                "modlog": None,
                "announce": None,
                "botlog": None
            }
        }
        save_data(data)
    else:
        channels = data["guilds"][g_id].setdefault("channels", {})
        for k in ["repeat", "nickname", "modlog", "announce", "botlog"]:
            if k not in channels:
                channels[k] = None
        save_data(data)
    return data["guilds"][g_id]

async def log_bot_event(guild: Optional[discord.Guild], event_data: Dict[str, Any]):
    """
    Centralized helper function `log_bot_event(guild: discord.Guild, event_data: dict)`
    that posts to the configured `botlog` channel for the guild.
    """
    logger.info(f"BOT LOG EVENT: {event_data}")
    if not guild:
        return
    
    g_data = get_guild_data(guild.id)
    botlog_channel_id = g_data["channels"].get("botlog")
    if botlog_channel_id:
        channel = guild.get_channel(int(botlog_channel_id))
        if channel:
            try:
                msg = (
                    f"🤖 **Bot Activity Log**\n"
                    f"• **Command/Action:** {event_data.get('command', 'N/A')}\n"
                    f"• **User:** {event_data.get('username', 'N/A')} (`{event_data.get('user_id', 'N/A')}`)\n"
                    f"• **Status:** {event_data.get('status', 'UNKNOWN')}\n"
                    f"• **Reason:** {event_data.get('reason', 'N/A')}\n"
                    f"• **Timestamp:** {event_data.get('timestamp', datetime.now(timezone.utc).isoformat())}"
                )
                await channel.send(msg)
            except Exception as e:
                logger.error(f"Failed to send botlog message: {e}")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} slash commands.")
    except Exception as e:
        logger.error(f"Failed to sync commands: {e}")

@bot.tree.command(name="dev", description="Show bot developer information")
@app_commands.describe()
async def dev_cmd(interaction: discord.Interaction):
    """
    5. `/dev` Command:
       - Make `/dev` visible in the Application Commands list to any user.
       - Display information about the bot developer (DEV/DEVELOPER = BILLY, e.g. <@{BILLY_USER_ID}>).
       - The response must be ephemeral (ephemeral=True).
    """
    user = interaction.user
    await log_bot_event(interaction.guild, {
        "command": "/dev",
        "username": str(user),
        "user_id": user.id,
        "status": "ALLOWED",
        "reason": "Developer info requested",
        "timestamp": datetime.now(timezone.utc).isoformat()
    })
    
    dev_mention = f"<@{BILLY_USER_ID}>"
    await interaction.response.send_message(
        f"🛠️ **Bot Developer Info**\nThis bot was developed and maintained by **BILLY** ({dev_mention}).",
        ephemeral=True
    )

@bot.tree.command(name="setup", description="Configure bot channels")
@app_commands.describe(
    kind="Channel type to configure",
    channel="The channel to set"
)
@app_commands.choices(kind=[
    app_commands.Choice(name="repeat", value="repeat"),
    app_commands.Choice(name="nickname", value="nickname"),
    app_commands.Choice(name="modlog", value="modlog"),
    app_commands.Choice(name="announce", value="announce"),
    app_commands.Choice(name="botlog", value="botlog"),
])
async def setup_cmd(
    interaction: discord.Interaction,
    kind: Literal["repeat", "nickname", "modlog", "announce", "botlog"],
    channel: discord.TextChannel
):
    """
    1. Add `botlog` to `/setup`.
    3. Restrict `/setup` exclusively to BILLY.
    """
    user = interaction.user
    is_billy = (user.id == BILLY_USER_ID)

    if not is_billy:
        await log_bot_event(interaction.guild, {
            "command": f"/setup {kind}",
            "username": str(user),
            "user_id": user.id,
            "status": "DENIED",
            "reason": "User is not BILLY",
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        await interaction.response.send_message("You're not BILLY.", ephemeral=True)
        return

    if not interaction.guild:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    data = load_data()
    g_id = str(interaction.guild.id)
    if g_id not in data["guilds"]:
        data["guilds"][g_id] = {"channels": {}}
    
    data["guilds"][g_id]["channels"][kind] = str(channel.id)
    save_data(data)

    await log_bot_event(interaction.guild, {
        "command": f"/setup {kind}",
        "username": str(user),
        "user_id": user.id,
        "status": "ALLOWED",
        "reason": f"Configured {kind} to #{channel.name}",
        "timestamp": datetime.now(timezone.utc).isoformat()
    })

    await interaction.response.send_message(
        f"✅ Successfully configured `{kind}` channel to {channel.mention}.",
        ephemeral=True
    )

@bot.tree.command(name="repeat", description="Repeat a message multiple times")
@app_commands.describe(
    message="The message to repeat",
    count="Number of times to repeat (max 20 for non-admins)"
)
async def repeat_cmd(
    interaction: discord.Interaction,
    message: str,
    count: int
):
    """
    6. Repeat Completion Message: Ephemeral 'Done'.
    7. Repeat Limit for Non-Admins: Deny count > 20 with specific message.
    """
    user = interaction.user
    is_admin = False
    if interaction.guild and isinstance(user, discord.Member):
        if user.guild_permissions.administrator or user.guild_permissions.manage_guild or user.id == BILLY_USER_ID:
            is_admin = True

    if not is_admin and count > 20:
        await log_bot_event(interaction.guild, {
            "command": "/repeat",
            "username": str(user),
            "user_id": user.id,
            "status": "DENIED",
            "reason": f"Non-admin attempted repeat with count {count} > 20",
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        await interaction.response.send_message(
            "KAZHAP CHILLARA ALLA NINTE BAN IDATTE FUNDE NINAK",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    target_channel = interaction.channel
    if interaction.guild:
        g_data = get_guild_data(interaction.guild.id)
        rep_ch_id = g_data["channels"].get("repeat")
        if rep_ch_id:
            ch = interaction.guild.get_channel(int(rep_ch_id))
            if ch:
                target_channel = ch

    for _ in range(min(count, 100 if is_admin else 20)):
        try:
            await target_channel.send(message)
        except Exception as e:
            logger.error(f"Failed to send repeat message: {e}")

    await log_bot_event(interaction.guild, {
        "command": "/repeat",
        "username": str(user),
        "user_id": user.id,
        "status": "ALLOWED",
        "reason": f"Repeated message {count} times successfully",
        "timestamp": datetime.now(timezone.utc).isoformat()
    })

    await interaction.followup.send("Done", ephemeral=True)

async def deny_nickname_request(guild: discord.Guild, member: discord.Member, reason: str):
    """
    4. Name Change Denial Notification to announcement channel & DM.
    """
    await log_bot_event(guild, {
        "command": "deny_nickname",
        "username": str(member),
        "user_id": member.id,
        "status": "DENIED",
        "reason": reason,
        "timestamp": datetime.now(timezone.utc).isoformat()
    })

    g_data = get_guild_data(guild.id)
    ann_channel_id = g_data["channels"].get("announce")
    if ann_channel_id:
        ann_channel = guild.get_channel(int(ann_channel_id))
        if ann_channel:
            try:
                await ann_channel.send(
                    f"❌ Nickname request for {member.mention} was denied.\n**Reason:** {reason}"
                )
            except Exception as e:
                logger.error(f"Failed to send announcement for nickname denial: {e}")

    try:
        await member.send(f"Your nickname request in **{guild.name}** was denied.\n**Reason:** {reason}")
    except Exception:
        pass

if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if token:
        bot.run(token)
    else:
        logger.warning("DISCORD_TOKEN environment variable not set. Bot can be tested via unit tests.")
```
