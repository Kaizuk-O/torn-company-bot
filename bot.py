import os
import json
import re
import logging
from datetime import datetime, timedelta

import requests
import discord
from discord.ext import commands
from discord import app_commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz
from dotenv import load_dotenv

# ---------------------------
# Logging
# ---------------------------
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s')
logging.getLogger("discord").setLevel(logging.WARNING)

# ---------------------------
# Env / Config
# ---------------------------
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
TORN_API_KEY = os.getenv("TORN_API_KEY", "").strip()
DIRECTOR_ID = int(os.getenv("DISCORD_USER_ID", "0"))         # your user ID
GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0"))           # your server ID
TIMEZONE = os.getenv("TIMEZONE", "Europe/London")
WELCOME_CHANNEL_NAME = os.getenv("WELCOME_CHANNEL", "general")
DATA_FILE = os.getenv("DATA_FILE", "data.json")

# Scheduler time: 19:30 UK
SYNC_HOUR = 19
SYNC_MINUTE = 30
tz = pytz.timezone(TIMEZONE)

# Guild object for scoping slash commands (prevents duplicates)
GUILD_OBJ = discord.Object(id=GUILD_ID) if GUILD_ID else None
_COMMANDS_SYNCED = False

# ---------------------------
# Helper decorator to avoid conditional @ lines
# ---------------------------
def guild_only():
    """Decorator that applies app_commands.guilds(GUILD_OBJ) if set, else identity."""
    if GUILD_OBJ:
        return app_commands.guilds(GUILD_OBJ)
    def identity(fn):
        return fn
    return identity

# ---------------------------
# Discord Intents / Bot
# ---------------------------
intents = discord.Intents.default()
intents.guilds = True
intents.members = True           # for on_member_join
intents.message_content = True   # optional
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------------------
# Storage helpers
# ---------------------------
def load_data() -> dict:
    if not os.path.exists(DATA_FILE):
        return {"employees": [], "trained": {}, "rotation_cycle": 0, "company_snapshot": {}, "last_sync": None}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logging.exception("Failed to load data.json")
        return {"employees": [], "trained": {}, "rotation_cycle": 0, "company_snapshot": {}, "last_sync": None}

def save_data(d: dict):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except Exception:
        logging.exception("Failed to save data.json")

# ---------------------------
# Rotation helpers
# ---------------------------
def norm(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "")).strip().casefold()

def all_trained(data: dict) -> bool:
    emps = data.get("employees", [])
    trained = data.get("trained", {})
    return bool(emps) and all(trained.get(e, "N") == "Y" for e in emps)

def reset_rotation(data: dict):
    trained = data.setdefault("trained", {})
    for e in data.get("employees", []):
        trained[e] = "N"
    data["rotation_cycle"] = data.get("rotation_cycle", 0) + 1
    save_data(data)
    logging.info(f"Rotation reset (cycle #{data['rotation_cycle']}).")

def director_check(interaction: discord.Interaction) -> bool:
    if interaction.user.id == DIRECTOR_ID:
        return True
    roles = [r.name.lower() for r in getattr(interaction.user, "roles", [])]
    return "director" in roles

def company_role_check(interaction: discord.Interaction) -> bool:
    if interaction.user.id == DIRECTOR_ID:
        return True
    roles = [r.name.lower() for r in getattr(interaction.user, "roles", [])]
    return ("employee" in roles) or ("director" in roles)

async def verify_employee(member: discord.Member) -> str:
    """Check member nickname against Torn employees and assign Employee role if eligible."""
    guild = member.guild

    # Find Employee role (case-insensitive)
    employee_role = discord.utils.find(lambda r: r.name.lower() == "employee", guild.roles)
    if not employee_role:
        return "⚠️ I can't find an **Employee** role in this server. Ask the director to create one."

    data = load_data()
    employees = data.get("employees", [])
    if not employees:
        return "⚠️ I don't have any company employees loaded yet. Ask the director to run `/forceupdate` first."

    # Use nickname if set, otherwise username
    nickname = member.nick or member.name

    # Expect format like: Harry [1925807] → base name "Harry"
    base_name = re.split(r"\[|\(", nickname)[0].strip()
    if not base_name:
        return (
            "⚠️ I couldn't parse your Torn name from your Discord nickname.\n"
            "Set your nickname to something like `Name [1234567]` and try `/verify` again."
        )

    # Try to match case-insensitive against employee list
    match = None
    for e in employees:
        if norm(e) == norm(base_name):
            match = e
            break

    if not match:
        return (
            f"❌ I couldn't find a company employee matching `{base_name}`.\n"
            "Make sure your Discord nickname matches your Torn name (before the ID)."
        )

    # Already has the role
    if employee_role in member.roles:
        return f"✅ You're already verified as **{match}** and have the Employee role."

    # Try to add the role
    try:
        await member.add_roles(employee_role, reason="Verified via /verify command")
        return f"✅ Verified as **{match}** and given the **Employee** role."
    except discord.Forbidden:
        return (
            "⚠️ I don't have permission to assign roles.\n"
            "Ask the director to move my bot role **above** `Employee` and give me `Manage Roles`."
        )
    except Exception:
        logging.exception("Error assigning Employee role")
        return "⚠️ Something went wrong assigning your role. Ask the director to check the bot logs."


# ---------------------------
# Torn API
# ---------------------------
def get_company_data() -> dict | None:
    if not TORN_API_KEY:
        logging.error("Missing TORN_API_KEY")
        return None
    url = f"https://api.torn.com/company/?selections=detailed,employees&key={TORN_API_KEY}"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        if "company_detailed" in data and "company_employees" in data:
            return data
        logging.error("Unexpected Torn API structure")
        return None
    except Exception:
        logging.exception("Error fetching Torn API")
        return None

def sync_torn_data() -> bool:
    """
    Fetch Torn company, merge rotation safely (preserve 'trained'),
    drop leavers, init new hires, auto-reset if everyone is trained.
    """
    base = load_data()
    company = get_company_data()
    if not company or "company_employees" not in company:
        logging.error("Error: invalid Torn data.")
        return False

    # Oldest first, then name
    api_emps = [emp["name"] for _, emp in sorted(
        company["company_employees"].items(),
        key=lambda kv: (-int(kv[1].get("days_in_company", 0)), kv[1].get("name", "").lower())
    )]

    trained = base.setdefault("trained", {})

    # Drop leavers
    for k in list(trained.keys()):
        if k not in api_emps:
            trained.pop(k, None)

    # Init new hires
    for e in api_emps:
        trained.setdefault(e, "N")

    base["employees"] = api_emps
    base["company_snapshot"] = company
    base["last_sync"] = datetime.now(tz).strftime("%Y-%m-%d %H:%M")
    save_data(base)

    if all_trained(base):
        reset_rotation(base)

    trains = company["company_detailed"].get("trains_available", 0)
    logging.info(f"[sync] Employees: {len(api_emps)}, trains={trains}")
    return True

# ---------------------------
# Scheduler
# ---------------------------
scheduler = AsyncIOScheduler(timezone=tz)

async def dm_director(message: str):
    try:
        user = await bot.fetch_user(DIRECTOR_ID)
        if user:
            await user.send(message)
    except Exception:
        logging.exception("Failed to DM director")

def scheduled_sync():
    ok = sync_torn_data()
    if not ok:
        return
    data = load_data()
    trains = int(data.get("company_snapshot", {}).get("company_detailed", {}).get("trains_available", 0) or 0)
    if trains >= 10:
        bot.loop.create_task(dm_director(f"🔔 Trains available: **{trains}** — time to train two employees (5× each)."))

# ---------------------------
# Events
# ---------------------------
@bot.event
async def on_ready():
    global _COMMANDS_SYNCED
    try:
        # Sync ONLY to the guild once; do NOT copy globals (prevents duplicates)
        if not _COMMANDS_SYNCED:
            if GUILD_OBJ:
                await bot.tree.sync(guild=GUILD_OBJ)
                logging.info(f"🔁 Synced slash commands to guild {GUILD_ID}.")
            else:
                await bot.tree.sync()
                logging.info("🔁 Synced slash commands globally (no GUILD_ID set).")
            _COMMANDS_SYNCED = True
        else:
            logging.info("🔁 Commands already synced; skipping re-sync.")
    except Exception:
        logging.exception("Failed to sync commands")

    logging.info(f"✅ Logged in as {bot.user} ({bot.user.id})")

    # Start scheduler once
    try:
        if not scheduler.running:
            scheduler.add_job(scheduled_sync, "cron", hour=SYNC_HOUR, minute=SYNC_MINUTE)
            scheduler.start()
            logging.info("📅 Scheduler started (daily 19:30 UK).")
    except Exception:
        logging.exception("Failed to start scheduler")

@bot.event
async def on_member_join(member: discord.Member):
    channel = discord.utils.get(member.guild.text_channels, name=WELCOME_CHANNEL_NAME)
    if channel:
        try:
            await channel.send(
                f"👋 Welcome to **{member.guild.name}**, {member.mention}!\n"
                "Please use the `/verify` command to get your employee role."
            )
        except Exception:
            logging.exception("Failed to send welcome message")

# ---------------------------
# Slash Commands (guild-scoped to avoid duplicates)
# ---------------------------

# /forceupdate (director only)
@guild_only()
@bot.tree.command(name="forceupdate", description="Director only: force a Torn company sync now")
@app_commands.check(director_check)
async def forceupdate(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    ok = sync_torn_data()
    if ok:
        await interaction.followup.send("✅ Torn company data synced successfully.")
    else:
        await interaction.followup.send("❌ Failed to sync (check Torn API / logs).", ephemeral=True)

@forceupdate.error
async def forceupdate_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("🚫 Directors only.", ephemeral=True)

# /status (employee or director)
@guild_only()
@bot.tree.command(name="status", description="Show company sync and training status summary")
@app_commands.check(company_role_check)
async def status(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        data = load_data()
        emps = data.get("employees", [])
        trained = data.get("trained", {})
        trained_count = sum(1 for v in trained.values() if v == "Y")
        total = len(emps)

        snap = data.get("company_snapshot", {})
        trains = snap.get("company_detailed", {}).get("trains_available", "N/A")
        last_sync = data.get("last_sync", "N/A")

        now = datetime.now(tz)
        next_sync_time = now.replace(hour=SYNC_HOUR, minute=SYNC_MINUTE, second=0, microsecond=0)
        if next_sync_time < now:
            next_sync_time += timedelta(days=1)
        delta = next_sync_time - now
        hours, rem = divmod(int(delta.total_seconds()), 3600)
        minutes = rem // 60

        embed = discord.Embed(
            title="📊 Company Status Overview",
            description="Summary for **Violent RE:Solutions**",
            color=discord.Color.blurple(),
            timestamp=datetime.now()
        )
        embed.add_field(name="🏢 Company", value="Violent RE:Solutions", inline=True)
        embed.add_field(name="💪 Trains Available", value=str(trains), inline=True)
        embed.add_field(name="📅 Last Sync", value=f"{last_sync} (UK)", inline=False)
        embed.add_field(name="🔄 Next Sync", value=f"In {hours}h {minutes}m (19:30 UK)", inline=False)
        embed.add_field(name="🎯 Rotation Progress", value=f"{trained_count}/{total} trained", inline=True)
        embed.set_footer(text="Updated via Torn API")
        await interaction.followup.send(embed=embed)
    except Exception:
        logging.exception("Error in /status")
        await interaction.followup.send("⚠️ Failed to retrieve status.", ephemeral=True)

@guild_only()
@bot.tree.command(
    name="verify",
    description="Verify your Torn account and receive the Employee role if eligible"
)
async def verify(interaction: discord.Interaction):
    member = interaction.user

    # ephemeral so only they see the result
    await interaction.response.defer(thinking=True, ephemeral=True)
    result_msg = await verify_employee(member)
    await interaction.followup.send(result_msg, ephemeral=True)


@status.error
async def status_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("🚫 You don’t have permission.", ephemeral=True)

# /rotation (employee or director)
@guild_only()
@bot.tree.command(name="rotation", description="Show current rotation and trained status")
@app_commands.check(company_role_check)
async def rotation(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        data = load_data()
        emps = data.get("employees", [])
        trained = data.get("trained", {})
        if not emps:
            await interaction.followup.send("⚠️ No employees loaded. Try `/forceupdate` first.", ephemeral=True)
            return
        lines = [f"{e} — {'✅' if trained.get(e) == 'Y' else '❌'}" for e in emps]
        if all_trained(data):
            lines.append("\n🔁 All trained — rotation will reset automatically on the next mark.")
        await interaction.followup.send("\n".join(lines))
    except Exception:
        logging.exception("Error in /rotation")
        await interaction.followup.send("⚠️ Error processing /rotation.", ephemeral=True)

@rotation.error
async def rotation_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("🚫 You don’t have permission.", ephemeral=True)

# /remaining (employee or director)
@guild_only()
@bot.tree.command(name="remaining", description="Show employees who still need training this rotation")
@app_commands.check(company_role_check)
async def remaining(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        data = load_data()
        emps = data.get("employees", [])
        trained = data.get("trained", {})
        if not emps:
            await interaction.followup.send("⚠️ No employee data available yet. Try `/forceupdate`.", ephemeral=True)
            return
        remaining_list = [e for e in emps if trained.get(e, "N") != "Y"]
        if remaining_list:
            msg = "**Employees left to train:**\n" + "\n".join([f"❌ {name}" for name in remaining_list])
        else:
            msg = "✅ All employees are trained this rotation!"
        await interaction.followup.send(msg)
    except Exception:
        logging.exception("Error in /remaining")
        await interaction.followup.send("⚠️ Error processing /remaining.", ephemeral=True)

@remaining.error
async def remaining_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("🚫 You don’t have permission.", ephemeral=True)

# /train (director only)
@guild_only()
@bot.tree.command(name="train", description="Mark an employee as trained for this rotation")
@app_commands.check(director_check)
async def train_cmd(interaction: discord.Interaction, name: str):
    await interaction.response.defer()  # public so the channel sees it

    data = load_data()
    employees = data.get("employees", [])
    trained = data.setdefault("trained", {})

    target = None
    nkey = norm(name)
    for e in employees:
        if norm(e) == nkey:
            target = e
            break

    if not target:
        await interaction.followup.send(f"❌ Employee '{name}' not found in current rotation.", ephemeral=True)
        return

    trained[target] = "Y"
    save_data(data)

    # Auto-reset when everyone is trained
    if all_trained(data):
        reset_rotation(data)
        await interaction.followup.send(
            f"✅ Marked **{target}** as trained.\n🔁 All employees trained — rotation **reset** (cycle #{data['rotation_cycle']})."
        )
        return

    remaining_list = [e for e in employees if data["trained"].get(e) != "Y"]
    nxt = remaining_list[0] if remaining_list else "—"
    await interaction.followup.send(f"✅ Marked **{target}** as trained.\n🔜 Next up: **{nxt}**")

@train_cmd.error
async def train_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("🚫 Directors only.", ephemeral=True)

# /resetrotation (director only)
@guild_only()
@bot.tree.command(name="resetrotation", description="Director only: manually reset the entire training rotation (failsafe)")
@app_commands.check(director_check)
async def resetrotation(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        data = load_data()
        if not data.get("employees"):
            await interaction.followup.send("⚠️ No employees loaded. Try `/forceupdate` first.", ephemeral=True)
            return
        reset_rotation(data)
        await interaction.followup.send(
            f"🔁 Rotation has been **manually reset** (cycle #{data['rotation_cycle']}).",
            ephemeral=False
        )
    except Exception:
        logging.exception("Error in /resetrotation")
        await interaction.followup.send("❌ Failed to reset rotation. Check logs.", ephemeral=True)

@resetrotation.error
async def resetrotation_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("🚫 Directors only.", ephemeral=True)

# Optional: prune old global commands once, then remove this command
@guild_only()
@bot.tree.command(name="prune_globals", description="Director only: remove any globally-registered commands")
@app_commands.check(director_check)
async def prune_globals(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        bot.tree.clear_commands(guild=None)  # clear global defs locally
        await bot.tree.sync()                # push empty global set
        await interaction.followup.send("🧹 Pruned global commands. All commands are now guild-scoped.")
    except Exception:
        logging.exception("Failed to prune global commands")
        await interaction.followup.send("⚠️ Failed to prune global commands.", ephemeral=True)

@prune_globals.error
async def prune_globals_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("🚫 Directors only.", ephemeral=True)

# ---------------------------
# Run
# ---------------------------
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN in .env")
    bot.run(DISCORD_TOKEN)

