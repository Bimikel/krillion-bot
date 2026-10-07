"""
Krillion tracker bot for Discord.

Watches for pasted results like:
    Krillion #83 🦐
    410
and keeps a per-server record, with daily summaries and leaderboards.

Run:  DISCORD_TOKEN=your_token_here python krillion_bot.py
"""
import os
import re
import sqlite3
import datetime
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks

TOKEN = os.environ["DISCORD_TOKEN"]
DB_PATH = "krillion.db"
UTC = datetime.timezone.utc
NO_PINGS = discord.AllowedMentions.none()  # show names without pinging people

# Krillion's daily reset happens at midnight in this timezone. "Today" runs
# 12:00 AM to 11:59 PM here, and the daily recap posts at 12:05 AM.
# If the game ever turns out to reset at midnight UTC instead, change this
# one line to ZoneInfo("UTC").
TIMEZONE = ZoneInfo("America/New_York")

# Temporary: logs which channels the bot sees messages in, plus the exact
# text of any message mentioning Krillion. Set to False once things work.
DEBUG = False

# "Krillion #84", then up to 30 non-digit characters (emoji, spaces,
# line breaks of any kind), then the score.
PATTERN = re.compile(r"Krillion\s*#(\d+)\D{0,30}?(\d+)", re.IGNORECASE)

# ---------- Database ----------
db = sqlite3.connect(DB_PATH)
db.executescript("""
CREATE TABLE IF NOT EXISTS results (
    guild_id  INTEGER,
    user_id   INTEGER,
    puzzle    INTEGER,
    score     INTEGER,
    posted_at TEXT,
    PRIMARY KEY (guild_id, user_id, puzzle)
);
CREATE TABLE IF NOT EXISTS settings (
    guild_id   INTEGER PRIMARY KEY,
    channel_id INTEGER
);
""")
db.commit()


async def name_of(guild: discord.Guild, user_id: int) -> str:
    """A player's server display name as plain text (no @mention, no ping)."""
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.NotFound:
            return "(left server)"
    return discord.utils.escape_markdown(member.display_name)


def last_reset() -> datetime.datetime:
    """The most recent midnight in TIMEZONE, when the current puzzle began."""
    return datetime.datetime.now(TIMEZONE).replace(hour=0, minute=0, second=0, microsecond=0)


def latest_puzzle(guild_id: int, since: datetime.datetime,
                  until: datetime.datetime | None = None) -> int | None:
    """Highest puzzle number posted in a server within a time range."""
    until = until or datetime.datetime.now(UTC) + datetime.timedelta(minutes=1)
    row = db.execute(
        "SELECT MAX(puzzle) FROM results "
        "WHERE guild_id = ? AND posted_at >= ? AND posted_at < ?",
        (guild_id, since.astimezone(UTC).isoformat(), until.astimezone(UTC).isoformat()),
    ).fetchone()
    return row[0]


async def board_for_puzzle(guild: discord.Guild, puzzle: int) -> str:
    """Every result for one puzzle in one server, best score first."""
    rows = db.execute(
        "SELECT user_id, score FROM results "
        "WHERE guild_id = ? AND puzzle = ? ORDER BY score DESC",
        (guild.id, puzzle),
    ).fetchall()
    lines = [f"**Krillion #{puzzle}**"]
    for rank, (user_id, score) in enumerate(rows, start=1):
        lines.append(f"{rank}. {await name_of(guild, user_id)} — {score}")
    return "\n".join(lines)


# ---------- Bot setup ----------
intents = discord.Intents.default()
intents.message_content = True  # needed to read pasted results


class KrillionBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        daily_summary.start()


bot = KrillionBot()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}", flush=True)


# ---------- Record results ----------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return

    if DEBUG:
        print(f"[debug] message in {message.guild} #{message.channel}", flush=True)
        if "krillion" in message.content.lower():
            print(f"[debug] content: {message.content!r}", flush=True)

    match = PATTERN.search(message.content)
    if not match:
        return

    puzzle, score = int(match[1]), int(match[2])

    # INSERT OR IGNORE: if this person already has a result for this puzzle
    # in this server, nothing changes. The first score posted is the one kept.
    cursor = db.execute(
        "INSERT OR IGNORE INTO results VALUES (?, ?, ?, ?, ?)",
        (message.guild.id, message.author.id, puzzle, score,
         datetime.datetime.now(UTC).isoformat()),
    )
    db.commit()

    # ✅ = saved, 🔁 = already had a result for this puzzle (score unchanged)
    await message.add_reaction("✅" if cursor.rowcount else "🔁")


# ---------- Slash commands ----------
@bot.tree.command(description="Show results for the current Krillion puzzle")
async def today(interaction: discord.Interaction):
    # The newest puzzle anyone here posted since midnight
    puzzle = latest_puzzle(interaction.guild_id, since=last_reset())
    if puzzle is None:
        await interaction.response.send_message(
            "No results yet today. Paste your Krillion score to get started!")
        return
    await interaction.response.send_message(
        await board_for_puzzle(interaction.guild, puzzle), allowed_mentions=NO_PINGS)


@bot.tree.command(description="All-time leaderboard by average score")
async def leaderboard(interaction: discord.Interaction):
    rows = db.execute(
        "SELECT user_id, COUNT(*), AVG(score), MAX(score) FROM results "
        "WHERE guild_id = ? GROUP BY user_id ORDER BY AVG(score) DESC LIMIT 10",
        (interaction.guild_id,),
    ).fetchall()
    if not rows:
        await interaction.response.send_message("No results yet.")
        return
    lines = ["**All-time Krillion leaderboard** (by average)"]
    for rank, (user_id, games, avg, best) in enumerate(rows, start=1):
        name = await name_of(interaction.guild, user_id)
        lines.append(f"{rank}. {name} — avg {avg:.0f}, best {best}, {games} games")
    await interaction.response.send_message("\n".join(lines), allowed_mentions=NO_PINGS)


@bot.tree.command(description="Stats for one player")
@app_commands.describe(member="Whose stats to show (defaults to you)")
async def stats(interaction: discord.Interaction, member: discord.Member | None = None):
    member = member or interaction.user
    summary = db.execute(
        "SELECT COUNT(*), AVG(score), MAX(score) FROM results "
        "WHERE guild_id = ? AND user_id = ?",
        (interaction.guild_id, member.id),
    ).fetchone()
    if summary[0] == 0:
        await interaction.response.send_message(f"No results for {member.display_name} yet.")
        return
    recent = db.execute(
        "SELECT puzzle, score FROM results WHERE guild_id = ? AND user_id = ? "
        "ORDER BY puzzle DESC LIMIT 5",
        (interaction.guild_id, member.id),
    ).fetchall()
    games, avg, best = summary
    recent_text = ", ".join(f"#{p}: {s}" for p, s in recent)
    await interaction.response.send_message(
        f"**{member.display_name}** — {games} games, avg {avg:.0f}, best {best}\n"
        f"Recent: {recent_text}"
    )


@bot.tree.command(description="Post the daily summary in this channel")
@app_commands.default_permissions(manage_guild=True)
async def setchannel(interaction: discord.Interaction):
    db.execute(
        "INSERT OR REPLACE INTO settings VALUES (?, ?)",
        (interaction.guild_id, interaction.channel_id),
    )
    db.commit()
    await interaction.response.send_message("Daily summaries will be posted here. 🌊")


# ---------- Daily recap (12:05 AM in TIMEZONE) ----------
@tasks.loop(time=datetime.time(hour=0, minute=5, tzinfo=TIMEZONE))
async def daily_summary():
    # The puzzle that ended at midnight: whatever was posted during yesterday
    reset = last_reset()
    for guild_id, channel_id in db.execute("SELECT guild_id, channel_id FROM settings").fetchall():
        channel = bot.get_channel(channel_id)
        if channel is None:
            continue
        puzzle = latest_puzzle(guild_id, since=reset - datetime.timedelta(days=1), until=reset)
        if puzzle is None:
            continue  # nobody played that puzzle here
        text = await board_for_puzzle(channel.guild, puzzle)
        await channel.send("🌊 **Final results**\n" + text, allowed_mentions=NO_PINGS)


@daily_summary.before_loop
async def wait_for_login():
    await bot.wait_until_ready()


bot.run(TOKEN)