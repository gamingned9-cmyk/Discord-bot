"""
Discord "repeat message" bot.

Commands (default prefix "!"):
  !repeat <count> <interval_seconds> <message>
      Sends <message> to the current channel <count> times,
      waiting <interval_seconds> seconds between each send.

  !stop
      Stops any repeating job currently running in the current channel.

Example:
  !repeat 10 60 Don't forget to vote!
      -> sends "Don't forget to vote!" 10 times, once a minute.
"""

import os
import asyncio

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()  # reads TOKEN from a local .env file

TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("DISCORD_PREFIX", "!")

intents = discord.Intents.default()
intents.message_content = True  # required to read command text

bot = commands.Bot(command_prefix=PREFIX, intents=intents)

# Tracks running jobs so we can stop them: {channel_id: asyncio.Task}
running_jobs: dict[int, asyncio.Task] = {}


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")


@bot.command(name="repeat")
async def repeat(ctx: commands.Context, count: int, interval_seconds: float, *, message: str):
    """Send `message` `count` times, `interval_seconds` apart, in this channel."""

    if count < 1:
        await ctx.send("Count must be at least 1.")
        return
    if interval_seconds < 1:
        await ctx.send("Interval must be at least 1 second.")
        return

    channel_id = ctx.channel.id

    if channel_id in running_jobs:
        await ctx.send("There's already a repeating job running in this channel. Use `!stop` first.")
        return

    async def job():
        try:
            for i in range(1, count + 1):
                await ctx.send(message)
                if i < count:
                    await asyncio.sleep(interval_seconds)
            await ctx.send(f"Done — sent {count} messages.")
        except asyncio.CancelledError:
            await ctx.send("Repeating job stopped.")
            raise
        finally:
            running_jobs.pop(channel_id, None)

    task = asyncio.create_task(job())
    running_jobs[channel_id] = task

    await ctx.send(
        f"Starting: will send that message {count} time(s), every {interval_seconds}s."
    )


@bot.command(name="stop")
async def stop(ctx: commands.Context):
    """Stop the repeating job running in this channel, if any."""
    task = running_jobs.get(ctx.channel.id)
    if not task:
        await ctx.send("No repeating job is running in this channel.")
        return
    task.cancel()


@repeat.error
async def repeat_error(ctx: commands.Context, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(
            "Usage: `!repeat <count> <interval_seconds> <message>`\n"
            "Example: `!repeat 5 30 Hello there`"
        )
    elif isinstance(error, commands.BadArgument):
        await ctx.send("Count must be a whole number and interval must be a number of seconds.")
    else:
        raise error


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(
            "No DISCORD_TOKEN found. Create a .env file with DISCORD_TOKEN=your_token_here"
        )
    bot.run(TOKEN)
