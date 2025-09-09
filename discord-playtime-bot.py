import os
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone, date

import discord
from discord import app_commands

# -------------------------
# CONFIG
# -------------------------
TOKEN = os.getenv("DISCORD_TOKEN")  # Set this env var before running
DB_PATH = os.getenv("PLAYTIME_DB", "playtime.sqlite3")
TIMEZONE = os.getenv("PLAYTIME_TZ", "UTC")  # IANA name recommended (e.g., "America/New_York"). Used for daily/weekly rollups.
TRACK_ACTIVITY_TYPES = {discord.ActivityType.playing}  # Edit to include others like listening/competing/streaming
HEARTBEAT_SECONDS = 60  # How often to credit time for ongoing sessions

# -------------------------
# Helpers
# -------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_local(d: datetime) -> datetime:
    try:
        import zoneinfo
        return d.astimezone(zoneinfo.ZoneInfo(TIMEZONE))
    except Exception:
        return d  # Fallback to UTC if tz not available


def iso_date_local(d: datetime | date | None = None) -> str:
    if d is None:
        d = now_utc()
    if isinstance(d, datetime):
        d = to_local(d).date()
    return d.isoformat()


# -------------------------
# Storage (SQLite)
# -------------------------
class Store:
    def __init__(self, path: str):
        self.path = path
        self._init_db()

    def _connect(self):
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    def _init_db(self):
        con = self._connect()
        cur = con.cursor()
        cur.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS totals (
              user_id INTEGER NOT NULL,
              activity TEXT NOT NULL,
              seconds INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY (user_id, activity)
            );

            CREATE TABLE IF NOT EXISTS daily_totals (
              user_id INTEGER NOT NULL,
              activity TEXT NOT NULL,
              day TEXT NOT NULL,           -- YYYY-MM-DD in LOCAL tz
              seconds INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY (user_id, activity, day)
            );

            CREATE TABLE IF NOT EXISTS guild_usernames (
              user_id INTEGER PRIMARY KEY,
              username TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            """
        )
        con.commit()
        con.close()

    def add_time(self, user_id: int, username: str, activity: str, seconds: int, when: datetime | None = None):
        if seconds <= 0:
            return
        when = when or now_utc()
        day = iso_date_local(when)
        con = self._connect()
        cur = con.cursor()
        # Update display name cache
        cur.execute(
            """
            INSERT INTO guild_usernames(user_id, username, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, updated_at=excluded.updated_at
            """,
            (user_id, username, now_utc().isoformat()),
        )

        # Totals
        cur.execute(
            """
            INSERT INTO totals(user_id, activity, seconds) VALUES(?, ?, ?)
            ON CONFLICT(user_id, activity) DO UPDATE SET seconds = seconds + excluded.seconds
            """,
            (user_id, activity, seconds),
        )
        # Daily totals
        cur.execute(
            """
            INSERT INTO daily_totals(user_id, activity, day, seconds) VALUES(?, ?, ?, ?)
            ON CONFLICT(user_id, activity, day) DO UPDATE SET seconds = seconds + excluded.seconds
            """,
            (user_id, activity, day, seconds),
        )
        con.commit()
        con.close()

    def top_activities(self, user_id: int, period: str = "week", limit: int = 10):
        con = self._connect()
        cur = con.cursor()
        if period == "all":
            cur.execute(
                "SELECT activity, seconds FROM totals WHERE user_id=? ORDER BY seconds DESC LIMIT ?",
                (user_id, limit),
            )
            rows = cur.fetchall()
        else:
            # Date window in local tz
            end = iso_date_local()
            if period == "today":
                start = end
            elif period == "week":
                # last 7 days including today
                start_dt = to_local(now_utc()).date() - timedelta(days=6)
                start = start_dt.isoformat()
            elif period == "month":
                start_dt = to_local(now_utc()).date() - timedelta(days=29)
                start = start_dt.isoformat()
            else:
                start = "0000-01-01"  # fallback
            cur.execute(
                """
                SELECT activity, SUM(seconds) AS seconds
                FROM daily_totals
                WHERE user_id=? AND day BETWEEN ? AND ?
                GROUP BY activity
                ORDER BY seconds DESC
                LIMIT ?
                """,
                (user_id, start, end, limit),
            )
            rows = cur.fetchall()
        con.close()
        return rows

    def leaderboard(self, guild_user_ids: list[int], activity: str | None, period: str = "week", limit: int = 10):
        if not guild_user_ids:
            return []
        con = self._connect()
        cur = con.cursor()

        placeholders = ",".join(["?"] * len(guild_user_ids))
        params: list = list(guild_user_ids)

        if period == "all":
            if activity:
                q = f"""
                    SELECT t.user_id, gu.username, t.seconds AS seconds
                    FROM totals t
                    JOIN guild_usernames gu ON gu.user_id = t.user_id
                    WHERE t.user_id IN ({placeholders}) AND t.activity = ?
                    ORDER BY seconds DESC
                    LIMIT ?
                """
                params += [activity, limit]
            else:
                q = f"""
                    SELECT t.user_id, gu.username, SUM(t.seconds) AS seconds
                    FROM totals t
                    JOIN guild_usernames gu ON gu.user_id = t.user_id
                    WHERE t.user_id IN ({placeholders})
                    GROUP BY t.user_id
                    ORDER BY seconds DESC
                    LIMIT ?
                """
                params += [limit]
            cur.execute(q, params)
        else:
            end = iso_date_local()
            if period == "today":
                start = end
            elif period == "week":
                start_dt = to_local(now_utc()).date() - timedelta(days=6)
                start = start_dt.isoformat()
            elif period == "month":
                start_dt = to_local(now_utc()).date() - timedelta(days=29)
                start = start_dt.isoformat()
            else:
                start = "0000-01-01"

            if activity:
                q = f"""
                    SELECT d.user_id, gu.username, SUM(d.seconds) AS seconds
                    FROM daily_totals d
                    JOIN guild_usernames gu ON gu.user_id = d.user_id
                    WHERE d.user_id IN ({placeholders}) AND d.activity = ? AND d.day BETWEEN ? AND ?
                    GROUP BY d.user_id
                    ORDER BY seconds DESC
                    LIMIT ?
                """
                params += [activity, start, end, limit]
            else:
                q = f"""
                    SELECT d.user_id, gu.username, SUM(d.seconds) AS seconds
                    FROM daily_totals d
                    JOIN guild_usernames gu ON gu.user_id = d.user_id
                    WHERE d.user_id IN ({placeholders}) AND d.day BETWEEN ? AND ?
                    GROUP BY d.user_id
                    ORDER BY seconds DESC
                    LIMIT ?
                """
                params += [start, end, limit]
            cur.execute(q, params)

        rows = cur.fetchall()
        con.close()
        return rows


# -------------------------
# Presence Tracker
# -------------------------
class PresenceTracker:
    """Tracks active game sessions and credits time every HEARTBEAT_SECONDS.

    We purposely credit time on a fixed heartbeat so if the bot restarts,
    you only lose up to HEARTBEAT_SECONDS of credit.
    """

    def __init__(self, store: Store):
        self.store = store
        # (user_id -> (activity_name -> started_at_utc))
        self.active: dict[int, dict[str, datetime]] = {}
        self.task: asyncio.Task | None = None

    def get_playing_name(self, member: discord.Member) -> str | None:
        if not member or not member.activities:
            return None
        for act in member.activities:
            # Only track configured activity types (default: playing)
            if act and isinstance(act, discord.Activity) and act.type in TRACK_ACTIVITY_TYPES and getattr(act, "name", None):
                return act.name
        return None

    def start(self, user_id: int, activity: str):
        self.active.setdefault(user_id, {})
        if activity not in self.active[user_id]:
            self.active[user_id][activity] = now_utc()

    def stop(self, user_id: int, activity: str) -> int:
        start = self.active.get(user_id, {}).pop(activity, None)
        if start is None:
            return 0
        elapsed = int((now_utc() - start).total_seconds())
        return elapsed

    async def heartbeat(self, bot: discord.Client):
        await bot.wait_until_ready()
        while not bot.is_closed():
            try:
                # credit time to everyone currently playing
                for guild in bot.guilds:
                    for member in guild.members:
                        sessions = self.active.get(member.id)
                        if not sessions:
                            continue
                        username = str(member)
                        for activity, _started in list(sessions.items()):
                            self.store.add_time(member.id, username, activity, HEARTBEAT_SECONDS)
                await asyncio.sleep(HEARTBEAT_SECONDS)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Heartbeat error: {e}")
                await asyncio.sleep(HEARTBEAT_SECONDS)


# -------------------------
# Bot
# -------------------------
intents = discord.Intents.default()
intents.members = True
intents.presences = True  # IMPORTANT: enable in Discord Developer Portal as well

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)
store = Store(DB_PATH)
tracker = PresenceTracker(store)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        await tree.sync()
        print("Slash commands synced.")
    except Exception as e:
        print(f"Slash sync failed: {e}")
    # start heartbeat task
    if tracker.task is None:
        tracker.task = asyncio.create_task(tracker.heartbeat(bot))


@bot.event
async def on_presence_update(before: discord.Member, after: discord.Member):
    # Determine previous and current tracked activity name
    before_name = tracker.get_playing_name(before)
    after_name = tracker.get_playing_name(after)
    user_id = after.id
    username = str(after)

    # If same state, nothing to do
    if before_name == after_name:
        return

    # Stopped previous game
    if before_name and before_name != after_name:
        # credit the time since start (will be additionally topped up by heartbeat but that's okay)
        elapsed = tracker.stop(user_id, before_name)
        if elapsed > 0:
            store.add_time(user_id, username, before_name, elapsed)

    # Started new game
    if after_name and before_name != after_name:
        tracker.start(user_id, after_name)


# -------------------------
# Slash Commands
# -------------------------
PERIOD_CHOICES = [
    app_commands.Choice(name="today", value="today"),
    app_commands.Choice(name="week (7d)", value="week"),
    app_commands.Choice(name="month (30d)", value="month"),
    app_commands.Choice(name="all time", value="all"),
]


def fmt_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s and not parts:
        parts.append(f"{s}s")
    return " ".join(parts) or "0m"


@tree.command(name="playtime", description="Show your playtime. Optionally choose period and activity.")
@app_commands.describe(period="Time window", activity="Filter by exact activity name (e.g., a specific game)")
@app_commands.choices(period=PERIOD_CHOICES)
async def playtime(interaction: discord.Interaction, period: app_commands.Choice[str] | None = None, activity: str | None = None):
    user = interaction.user
    p = (period.value if period else "week")
    rows = store.top_activities(user.id, p, limit=25)

    if activity:
        # sum just this activity
        total = 0
        for r in rows:
            if r["activity"].lower() == activity.lower():
                total = r["seconds"]
                break
        desc = f"**{user.mention}** — {fmt_duration(total)} in **{activity}** ({p})."
    else:
        lines = []
        for i, r in enumerate(rows[:10], start=1):
            lines.append(f"`{i:>2}.` **{r['activity']}** — {fmt_duration(r['seconds'])}")
        if not lines:
            lines = ["No playtime recorded yet. Start a game while I'm online!"]
        desc = "\n".join(lines)

    await interaction.response.send_message(desc)


@tree.command(name="leaderboard", description="Server leaderboard by total playtime.")
@app_commands.describe(period="Time window", activity="Optional specific activity (exact name)")
@app_commands.choices(period=PERIOD_CHOICES)
async def leaderboard(interaction: discord.Interaction, period: app_commands.Choice[str] | None = None, activity: str | None = None):
    guild = interaction.guild
    if not guild:
        return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)

    p = (period.value if period else "week")

    member_ids = [m.id for m in guild.members if not m.bot]
    rows = store.leaderboard(member_ids, activity, p, limit=15)

    header = f"Leaderboard ({p}{' • ' + activity if activity else ''})"
    lines = [f"**{header}**"]
    if not rows:
        lines.append("No data yet.")
    else:
        for i, r in enumerate(rows, start=1):
            name = r["username"]
            lines.append(f"`{i:>2}.` **{name}** — {fmt_duration(int(r['seconds']))}")

    await interaction.response.send_message("\n".join(lines))


@tree.command(name="nowplaying", description="Show what I'm currently tracking for you.")
async def nowplaying(interaction: discord.Interaction):
    user_id = interaction.user.id
    sessions = tracker.active.get(user_id, {}) or {}
    if not sessions:
        return await interaction.response.send_message("Not tracking anything for you right now.")
    lines = ["Currently tracking:"]
    for act, start in sessions.items():
        elapsed = int((now_utc() - start).total_seconds())
        lines.append(f"• **{act}** — {fmt_duration(elapsed)} so far")
    await interaction.response.send_message("\n".join(lines))


# -------------------------
# Main
# -------------------------
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Please set DISCORD_TOKEN env var.")
    try:
        import zoneinfo  # noqa: F401
    except Exception:
        print("zoneinfo not available; falling back to UTC for day/weekly rollups.")
    bot.run(TOKEN)
