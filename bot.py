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
# Logging (unbuffered for VPS)
# ---------------------------
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s')
logging.getLogger("discord").setLevel(logging.WARNING)

# ---------------------------
# Env / Config
# ---------------------------
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
TORN_API_KEY = os.getenv("TORN_API_KEY", "").strip()
DIRECTOR_ID = int(os.getenv("DISCORD_USER_ID", "0"))
GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0"))
TIMEZONE = os.getenv("TIMEZONE", "Europe/London")

# welcome channel name (change if needed)
WELCOME_CHANNEL_NAME = os.getenv("WELCOME_CHANNEL", "general")

# data file (rotation + snapshot)
DATA_FILE = os.getenv("DATA_FILE", "data.json")

# scheduler time (18:30 local)
SYNC_HOUR = 18
SYNC_MINUTE = 30

tz = pytz.timezone(TIMEZONE)

# ---------------------------
# Discord Intents / Bot
# ---------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True  # required for on_member_join

bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------------------
# Utils: data store
# ---------------------------
def load_data() -> dict:
    if not os.path.exists(DATA_FILE):
        return {"employees": [], "trained": {}, "rotation_cycle": 0, "company_snapshot": {}, "last_sync": None}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.exception("Failed to load data.json")
        return {"employees": [], "trained": {}, "rotation_cycle": 0, "company_snapshot": {}, "last_sync": None}

def save_data(d: dict):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except Exception:
        logging.exception("Failed to save data.json")

# ---------------------------
# Utils: names / rotation
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

def is_director(interaction: discord.Interaction) -> bool:
    return interaction.user.id == DIRECTOR_ID or any(r.name.lower() == "director" for r in getattr(interaction.user, "roles", []))

def has_company_role(interaction: discord.Interaction) -> bool:
    if interaction.user.id == DIRECTOR_ID:
        return True
    roles = [r.name.lower() for r in getattr(interaction.user, "roles", [])]
    return ("employee" in roles) or ("director" in roles)

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
        # Basic sanity check
        if "company_detailed" in data and "company_employees" in data:
            return data
        logging.error("Torn API returned unexpected structure.")
        return None
    except Exception as e:
        logging.exception("Error fetching Torn API")
        return None

def sync_torn_data() -> bool:
    """Fetch Torn company, merge rotation safely, auto-reset if everyone is trained."""
    base = load_data()
    company = get_company_data()
    if not company or "company_employees" not in company:
        logging.error("Error: invalid Torn data.")
        return False

    # Build ordered employee list (oldest first, then name)
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

    # If everyone is trained (even after reordering), reset
    if all_trained(base):
        reset_rotation(base)

    trains = company["company_detailed"].get("trains_available", 0)
    logging.info(f"[sync] Employees: {len(api_emps)}, trains={trains}")
    return True

# ---------------------------
# Scheduler job
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
    """Runs at 18:30 local every day: sync + DM if trains >= 10."""
    ok = sync_torn_data()
    if not ok:
        return
    data = load_data()
    company = data.get("company_snapshot", {})
    trains = 0
    try:
        trains = int(company.get("company_detailed", {}).get("trains_available", 0))
    except Exception:
        trains = 0
    if trains >= 10:
        # dispatch coroutine from scheduler thread
        bot.loop.create_task(dm_director(f"🔔 **Trains available: {trains}** — time to train two employees (5× each)."))

# ---------------------------
# Discord Events
# ---------------------------
@bot.event
async def on_ready():
    try:
        await bot.tree.sync()
        logging.info("🔁 Synced slash commands.")
    except Exception:
        logging.exception("Failed to sync commands")
    logging.info(f"✅ Logged in as {bot.user} ({bot.user.id})")

    # start scheduler exactly once
    try:
        scheduler.add_job(scheduled_sync, "cron", hour=SYNC_HOUR, minute=SYNC_MINUTE)
        scheduler.start()
        logging.info("📅 Scheduler started.")
    except Exception:
        logging.exception("Failed to start scheduler (already running?)")

@bot.event
async def on_member_join(member: discord.Member):
    # Welcome message to a specific channel
    guild = member.guild
    channel = discord.utils.get(guild.text_channels, name=WELCOME_CHANNEL_NAME)
    if channel:
        try:
            await channel.send(
                f"👋 Welcome to **{guild.name}**, {member.mention}!\n"
                "Please use the `/verify` command to get your employee role."
            )
        except Exception:
            logging.exception("Failed to send welcome message")

# ---------------------------
# Slash Commands
# ---------------------------
@bot.tree.command(name="forceupdate", description="Director only: force a Torn company sync now")
async def forceupdate(interaction: discord.Interaction):
    if not is_director(interaction):
        await interaction.response.send_message("🚫 Directors only.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    ok = sync_torn_data()
    if ok:
        await interaction.followup.send("✅ Torn company data synced successfully.")
    else:
        await interaction.followup.send("❌ Failed to sync (check Torn API / logs).", ephemeral=True)

@bot.tree.command(name="status", description="Show company sync and training status summary")
async def status(interaction: discord.Interaction):
    if not has_company_role(interaction):
        await interaction.response.send_message("🚫 You don’t have permission to use this command.", ephemeral=True)
        return

    await interaction.response.defer()

    try:
        data = load_data()
        employees = data.get("employees", [])
        trained = data.get("trained", {})
        trained_count = sum(1 for v in trained.values() if v == "Y")
        total = len(employees)

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
            description=f"Summary for **Violent RE:Solutions**",
            color=discord.Color.blurple(),
            timestamp=datetime.now()
        )
        embed.add_field(name="🏢 Company", value="Violent RE:Solutions", inline=True)
        embed.add_field(name="💪 Trains Available", value=str(trains), inline=True)
        embed.add_field(name="📅 Last Sync", value=f"{last_sync} (UK)", inline=False)
        embed.add_field(name="🔄 Next Sync", value=f"In {hours}h {minutes}m ({SYNC_HOUR:02d}:{SYNC_MINUTE:02d} UK)", inline=False)
        embed.add_field(name="🎯 Rotation Progress", value=f"{trained_count}/{total} trained", inline=True)
        embed.set_footer(text="Updated via Torn API")

        await interaction.followup.send(embed=embed)
    except Exception:
        logging.exception("Error in /status")
        await interaction.followup.send("⚠️ Failed to retrieve status.", ephemeral=True)

@bot.tree.command(name="rotation", description="Show current rotation and trained status")
async def rotation(interaction: discord.Interaction):
    if not has_company_role(interaction):
        await interaction.response.send_message("🚫 You don’t have permission.", ephemeral=True)
        return

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

@bot.tree.command(name="remaining", description="Show employees who still need training this rotation")
async def remaining(interaction: discord.Interaction):
    if not has_company_role(interaction):
        await interaction.response.send_message("🚫 You don’t have permission to use this command.", ephemeral=True)
        return

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

@bot.tree.command(name="train", description="Mark an employee as trained for this rotation")
async def train_cmd(interaction: discord.Interaction, name: str):
    if not is_director(interaction):
        await interaction.response.send_message("🚫 Directors only.", ephemeral=True)
        return

    await interaction.response.defer()  # public update

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

    # Otherwise show next up
    remaining_list = [e for e in employees if data["trained"].get(e) != "Y"]
    nxt = remaining_list[0] if remaining_list else "—"
    await interaction.followup.send(f"✅ Marked **{target}** as trained.\n🔜 Next up: **{nxt}**")

@bot.tree.command(name="resetrotation", description="Director only: manually reset the entire training rotation (failsafe)")
async def resetrotation(interaction: discord.Interaction):
    if not is_director(interaction):
        await interaction.response.send_message("🚫 Directors only.", ephemeral=True)
        return

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

# ---------------------------
# Run
# ---------------------------
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN in .env")
    if not TORN_API_KEY:
        logging.warning("No TORN_API_KEY set — some features will not work.")

    bot.run(DISCORD_TOKEN)
