import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
from typing import Optional, Dict, List, Set
from datetime import datetime, timedelta, timezone
import json
import re
import os
from dotenv import load_dotenv

import sys
import pathlib

load_dotenv()

token = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.dm_messages = True

bot = commands.Bot(command_prefix="!", intents=intents)

async def global_interaction_check(interaction: discord.Interaction):
    # Block DMs
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server channel.", ephemeral=True)
        return False

    if interaction.command and interaction.command.name == "toggle_bot":
        return True

    manager = multi_manager.get_manager(interaction.guild_id)
    if not manager.settings.bot_enabled:
        await interaction.response.send_message("The bot is currently **OFF**. Please wait until an admin turns it back on.", ephemeral=True)
        return False

    return True

bot.tree.interaction_check = global_interaction_check

# Base directory for all bot data
DATA_DIR = pathlib.Path("bot_data")

def get_guild_data_dir(guild_id: int) -> pathlib.Path:
    """Get the data directory for a specific guild"""
    guild_dir = DATA_DIR / str(guild_id)
    guild_dir.mkdir(parents=True, exist_ok=True)
    return guild_dir

class TeamSettings:
    def __init__(self):
        self.max_team_size = 0
        self.duration_minutes = 0
        self.ip_range = ""          # e.g. "10.100.[41-45].0/24"
        self.start_vmid = 0
        self.number_of_machines = 0
        self.bot_enabled = True

    def parse_ip_range(self):
        """Parse ip_range and return (template, range_start, range_end), or None if invalid."""
        match = re.search(r'\[(\d+)-(\d+)\]', self.ip_range)
        if not match:
            return None
        return self.ip_range, int(match.group(1)), int(match.group(2))

    @property
    def max_teams(self) -> int:
        """Derive max_teams automatically from the bracket range in ip_range."""
        result = self.parse_ip_range()
        if result is None:
            return 0
        _, start, end = result
        return max(0, end - start + 1)

    def get_ip(self, team_num: int) -> str:
        """Return the resolved subnet string for a 1-indexed team number."""
        result = self.parse_ip_range()
        if result is None:
            return "Not configured"
        template, start, _ = result
        value = start + team_num - 1
        return re.sub(r'\[\d+-\d+\]', str(value), template)

    def to_dict(self) -> dict:
        return {
            "max_team_size": self.max_team_size,
            "duration_minutes": self.duration_minutes,
            "ip_range": self.ip_range,
            "start_vmid": self.start_vmid,
            "number_of_machines": self.number_of_machines,
            "bot_enabled": self.bot_enabled
        }

    @classmethod
    def from_dict(cls, data: dict):
        settings = cls()
        settings.max_team_size = data.get("max_team_size", 0)
        settings.duration_minutes = data.get("duration_minutes", 0)
        # Support legacy "ip_base" field from older save files
        settings.ip_range = data.get("ip_range", data.get("ip_base", ""))
        settings.start_vmid = data.get("start_vmid", 0)
        settings.number_of_machines = data.get("number_of_machines", 0)
        settings.bot_enabled = data.get("bot_enabled", True)
        return settings

class Team:
    def __init__(self, team_num: int, captain_id: int, settings: TeamSettings):
        self.team_num = team_num
        self.captain_id = captain_id
        self.members: Dict[int, str] = {captain_id: ""}
        self.settings = settings
        self.created_at = datetime.now(timezone.utc)
        self.end_time = self.created_at + timedelta(minutes=settings.duration_minutes)
        self.is_active = True
        self.timer_message_ids: Dict[int, tuple] = {}
        self.halfway_notified = False
    
    def to_dict(self) -> dict:
        return {
            "team_num": self.team_num,
            "captain_id": self.captain_id,
            "members": self.members,
            "created_at": self.created_at.isoformat(),
            "end_time": self.end_time.isoformat(),
            "is_active": self.is_active,
            "timer_message_ids": {str(k): v for k, v in self.timer_message_ids.items()},
            "halfway_notified": self.halfway_notified
        }
    
    @classmethod
    def from_dict(cls, data: dict, settings: TeamSettings):
        team = cls.__new__(cls)
        team.team_num = data["team_num"]
        team.captain_id = data["captain_id"]
        team.members = {int(k): v for k, v in data["members"].items()}
        team.settings = settings
        team.created_at = datetime.fromisoformat(data["created_at"]).replace(tzinfo=timezone.utc) if datetime.fromisoformat(data["created_at"]).tzinfo is None else datetime.fromisoformat(data["created_at"])
        team.end_time = datetime.fromisoformat(data["end_time"]).replace(tzinfo=timezone.utc) if datetime.fromisoformat(data["end_time"]).tzinfo is None else datetime.fromisoformat(data["end_time"])
        team.is_active = data["is_active"]
        team.timer_message_ids = {int(k): tuple(v) for k, v in data.get("timer_message_ids", {}).items()}
        team.halfway_notified = data.get("halfway_notified", False)
        return team

class GuildTeamManager:
    """Manages teams for a single guild"""
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self.settings = TeamSettings()
        self.teams: Dict[int, Team] = {}
        self.user_teams: Dict[int, int] = {}
        self.available_team_nums: Set[int] = set(range(1, self.settings.max_teams + 1))
        self.admins: Set[int] = set()
        self.closed_teams: Set[int] = set()
    
    def recalculate_available_teams(self):
        """Recalculate available team slots from ip_range, preserving active and closed teams."""
        new_max = self.settings.max_teams
        occupied = set(self.teams.keys()) | self.closed_teams
        self.available_team_nums = {n for n in range(1, new_max + 1) if n not in occupied}
    
    def get_settings_file(self) -> pathlib.Path:
        return get_guild_data_dir(self.guild_id) / "settings.json"
    
    def get_teams_file(self) -> pathlib.Path:
        return get_guild_data_dir(self.guild_id) / "teams.json"
    
    def get_admins_file(self) -> pathlib.Path:
        return get_guild_data_dir(self.guild_id) / "admins.json"
    
    def save_settings(self):
        """Save settings to JSON file"""
        try:
            with open(self.get_settings_file(), 'w') as f:
                json.dump(self.settings.to_dict(), f, indent=2)
            print(f"[Guild {self.guild_id}] Settings saved")
        except Exception as e:
            print(f"[Guild {self.guild_id}] Error saving settings: {e}")
    
    def load_settings(self):
        """Load settings from JSON file"""
        try:
            settings_file = self.get_settings_file()
            if settings_file.exists():
                with open(settings_file, 'r') as f:
                    data = json.load(f)
                    self.settings = TeamSettings.from_dict(data)
                    self.available_team_nums = set(range(1, self.settings.max_teams + 1))
                    print(f"[Guild {self.guild_id}] Settings loaded")
                    return True
        except Exception as e:
            print(f"[Guild {self.guild_id}] Error loading settings: {e}")
        return False
    
    def save_teams(self):
        """Save teams data to JSON file"""
        try:
            data = {
                "teams": {str(num): team.to_dict() for num, team in self.teams.items()},
                "user_teams": {str(k): v for k, v in self.user_teams.items()},
                "available_team_nums": list(self.available_team_nums),
                "closed_teams": list(self.closed_teams)
            }
            with open(self.get_teams_file(), 'w') as f:
                json.dump(data, f, indent=2)
            print(f"[Guild {self.guild_id}] Teams saved")
        except Exception as e:
            print(f"[Guild {self.guild_id}] Error saving teams: {e}")
    
    def load_teams(self):
        """Load teams data from JSON file"""
        try:
            teams_file = self.get_teams_file()
            if teams_file.exists():
                with open(teams_file, 'r') as f:
                    data = json.load(f)
                    
                    # Restore teams
                    self.teams = {}
                    for team_num_str, team_data in data.get("teams", {}).items():
                        team = Team.from_dict(team_data, self.settings)
                        self.teams[int(team_num_str)] = team
                    
                    # Restore user_teams mapping
                    self.user_teams = {int(k): v for k, v in data.get("user_teams", {}).items()}
                    
                    # Restore available team numbers
                    self.available_team_nums = set(data.get("available_team_nums", []))
                    
                    # Restore closed teams
                    self.closed_teams = set(data.get("closed_teams", []))
                    
                    print(f"[Guild {self.guild_id}] Teams loaded - {len(self.teams)} active teams")
                    return True
        except Exception as e:
            print(f"[Guild {self.guild_id}] Error loading teams: {e}")
        return False
    
    def save_admins(self):
        """Save admins to JSON file"""
        try:
            data = {
                "admins": list(self.admins)
            }
            with open(self.get_admins_file(), 'w') as f:
                json.dump(data, f, indent=2)
            print(f"[Guild {self.guild_id}] Admins saved")
        except Exception as e:
            print(f"[Guild {self.guild_id}] Error saving admins: {e}")
    
    def load_admins(self):
        """Load admins from JSON file"""
        try:
            admins_file = self.get_admins_file()
            if admins_file.exists():
                with open(admins_file, 'r') as f:
                    data = json.load(f)
                    self.admins = set(data.get("admins", []))
                    print(f"[Guild {self.guild_id}] Admins loaded - {len(self.admins)} admins")
                    return True
        except Exception as e:
            print(f"[Guild {self.guild_id}] Error loading admins: {e}")
        return False
    
    def load_all(self):
        """Load all saved data"""
        self.load_settings()
        self.load_admins()
        self.load_teams()

class MultiGuildManager:
    """Manages team managers for multiple guilds"""
    def __init__(self):
        self.guild_managers: Dict[int, GuildTeamManager] = {}
    
    def get_manager(self, guild_id: int) -> GuildTeamManager:
        """Get or create a manager for a specific guild"""
        if guild_id not in self.guild_managers:
            manager = GuildTeamManager(guild_id)
            manager.load_all()
            self.guild_managers[guild_id] = manager
        return self.guild_managers[guild_id]
    
    def load_all_guilds(self):
        """Load data for all guilds that have saved data"""
        if not DATA_DIR.exists():
            return
        
        for guild_dir in DATA_DIR.iterdir():
            if guild_dir.is_dir() and guild_dir.name.isdigit():
                guild_id = int(guild_dir.name)
                manager = GuildTeamManager(guild_id)
                manager.load_all()
                self.guild_managers[guild_id] = manager
                print(f"Loaded data for guild {guild_id}")

multi_manager = MultiGuildManager()

async def send_dm(user_id: int, content: str = None, embed: discord.Embed = None, view: discord.ui.View = None) -> tuple:
    try:
        user = await bot.fetch_user(user_id)
        msg = await user.send(content=content, embed=embed, view=view)
        return True, msg.channel.id, msg.id
    except discord.Forbidden:
        # User has DMs disabled or has blocked the bot
        return False, None, None
    except discord.HTTPException as e:
        # Covers rate limits, server errors, and other API failures
        print(f"[send_dm] HTTPException sending DM to {user_id}: {e.status} {e.text}")
        return False, None, None

async def create_timer_embed(team: Team):
    embed = discord.Embed(
        title=f"Team {team.team_num} - Timer",
        description=f"**Ends at:** <t:{int(team.end_time.timestamp())}:R>",
        color=discord.Color.blue()
    )
    embed.add_field(name="Team Members", value=", ".join(team.members.values()), inline=False)
    embed.add_field(name="IP Range", value=team.settings.get_ip(team.team_num), inline=False)
    
    return embed

async def run_network_setup(guild_id: int, team_num: int):
    """Run the revert and start subprocesses for a team's VMID range."""
    manager = multi_manager.get_manager(guild_id)
    try:
        current_dir = pathlib.Path(__file__).parent.resolve()
    except NameError:
        current_dir = pathlib.Path.cwd()

    script_path = current_dir / "SPAM" / "status.py"
    start_vmid = manager.settings.start_vmid + (team_num - 1) * manager.settings.number_of_machines
    end_vmid = start_vmid + (manager.settings.number_of_machines - 1)

    # Step 1: --revert
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(script_path),
            "--revert", "-r",
            str(start_vmid), str(end_vmid),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            print(f"[Guild {guild_id}] Error (revert) status.py for team {team_num}: {stderr.decode()}")
    except Exception as e:
        print(f"[Guild {guild_id}] Failed to start subprocess (revert) for team {team_num}: {e}")

    # Step 2: -s
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(script_path),
            "-s", "-r",
            str(start_vmid), str(end_vmid),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            print(f"[Guild {guild_id}] Error (-s) status.py for team {team_num}: {stderr.decode()}")
    except Exception as e:
        print(f"[Guild {guild_id}] Failed to start subprocess (-s) for team {team_num}: {e}")

async def end_team(guild_id: int, team_num: int, auto_end: bool = False):
    manager = multi_manager.get_manager(guild_id)
    
    if team_num not in manager.teams:
        return
    
    team = manager.teams[team_num]
    
    # Guard against double-fire: if already being torn down, bail out early
    if not team.is_active:
        return
    
    team.is_active = False
    manager.closed_teams.add(team_num)
    
    reason = "Time limit reached" if auto_end else "Captain ended the team"
    embed = discord.Embed(
        title=f"Team {team.team_num} - Ended",
        description=f"**Reason:** {reason}",
        color=discord.Color.red()
    )
    embed.add_field(name="Team Members", value=", ".join(team.members.values()), inline=False)
    
    for member_id in list(team.members.keys()):
        await send_dm(member_id, embed=embed)
        if member_id in manager.user_teams:
            del manager.user_teams[member_id]
    
    for admin_id in manager.admins:
        await send_dm(admin_id, embed=embed)

    manager.closed_teams.remove(team_num)
    manager.available_team_nums.add(team_num)
    del manager.teams[team_num]

    # Save state after team ends
    manager.save_teams()


@bot.event
async def on_ready():
    print(f"Bot logged in as {bot.user}")
    
    # Load all saved guild data
    multi_manager.load_all_guilds()
    
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(e)
    
    start_timer_updates.start()
    auto_save_task.start()

@tasks.loop(seconds=10)
async def start_timer_updates():
    for guild_id, manager in list(multi_manager.guild_managers.items()):
        for team_num, team in list(manager.teams.items()):
            if not team.is_active:
                continue
            
            time_left = team.end_time - datetime.now(timezone.utc)
            
            if time_left.total_seconds() <= 0:
                await end_team(guild_id, team_num, auto_end=True)
                continue
            
            total_seconds = team.settings.duration_minutes * 60
            if not team.halfway_notified and time_left.total_seconds() <= total_seconds / 2:
                team.halfway_notified = True
                halfway_embed = discord.Embed(
                    title=f"Team {team.team_num} - Halfway Point Reached!",
                    description="Your team has reached the halfway mark.",
                    color=discord.Color.orange()
                )
                for member_id in team.members.keys():
                    await send_dm(member_id, embed=halfway_embed)
                
                for admin_id in manager.admins:
                    await send_dm(admin_id, embed=halfway_embed)
                
                # Save state after halfway notification
                manager.save_teams()

@tasks.loop(minutes=5)
async def auto_save_task():
    """Automatically save data every 5 minutes"""
    for guild_id, manager in multi_manager.guild_managers.items():
        manager.save_teams()
    print("Auto-save completed for all guilds")

@bot.tree.command(name="admin_add", description="Administrator command to add new admins (Admin only)")
@app_commands.describe(user="The user to make an admin")
@app_commands.default_permissions(administrator=True)
async def admin_add(interaction: discord.Interaction, user: discord.User):
    manager = multi_manager.get_manager(interaction.guild_id)
    
    if interaction.user.id not in manager.admins and interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    
    if user.id in manager.admins:
        await interaction.response.send_message(f"{user.mention} is already an admin.", ephemeral=True)
        return
    
    manager.admins.add(user.id)
    manager.save_admins()
    await interaction.response.send_message(f"Added {user.mention} as an admin.", ephemeral=True)

@bot.tree.command(name="admin_remove", description="Administrator command to remove admins (Admin only)")
@app_commands.describe(user="The user to remove as an admin")
@app_commands.default_permissions(administrator=True)
async def admin_remove(interaction: discord.Interaction, user: discord.User):
    manager = multi_manager.get_manager(interaction.guild_id)
    
    if interaction.user.id not in manager.admins and interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    
    if user.id not in manager.admins:
        await interaction.response.send_message(f"{user.mention} is not an admin.", ephemeral=True)
        return
    
    manager.admins.remove(user.id)
    manager.save_admins()
    await interaction.response.send_message(f"Removed {user.mention} as an admin.", ephemeral=True)

@bot.tree.command(name="admin_settings", description="Configure team settings (Admin only)")
@app_commands.describe(
    max_size="Max team size",
    duration="Duration in minutes",
    ip_range="IP range with bracket notation, e.g. 10.100.[41-45].0/24",
    start_vmid="VMID of first machine cloned",
    number_of_machines="Number of machines per network"
)
@app_commands.default_permissions(administrator=True)
async def admin_settings(interaction: discord.Interaction, max_size: int = None, duration: int = None, ip_range: str = None, start_vmid: int = None, number_of_machines: int = None):
    manager = multi_manager.get_manager(interaction.guild_id)

    if interaction.user.id not in manager.admins and interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    if max_size:
        manager.settings.max_team_size = max_size
    if duration:
        manager.settings.duration_minutes = duration
    if ip_range is not None:
        if not re.search(r'\[\d+-\d+\]', ip_range):
            await interaction.response.send_message(
                "Invalid IP range format. Use bracket notation, e.g. `10.100.[41-45].0/24`.",
                ephemeral=True
            )
            return
        manager.settings.ip_range = ip_range
        manager.recalculate_available_teams()
    if start_vmid:
        manager.settings.start_vmid = start_vmid
    if number_of_machines:
        manager.settings.number_of_machines = number_of_machines

    manager.save_settings()
    manager.save_teams()

    await interaction.response.send_message(
        f"Settings updated:"
        f"\n- Max Team Size: {manager.settings.max_team_size}"
        f"\n- Max Teams (derived): {manager.settings.max_teams}"
        f"\n- Duration: {manager.settings.duration_minutes} minutes"
        f"\n- IP Range: {manager.settings.ip_range or 'Not configured'}"
        f"\n- VMID of First Machine: {manager.settings.start_vmid}"
        f"\n- Number of Machines per Network: {manager.settings.number_of_machines}",
        ephemeral=True
    )

@bot.tree.command(name="view_settings", description="View current team settings (Admin only)")
@app_commands.default_permissions(administrator=True)
async def view_settings(interaction: discord.Interaction):
    manager = multi_manager.get_manager(interaction.guild_id)
    
    if interaction.user.id not in manager.admins and interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    
    embed = discord.Embed(title="Current Team Settings", color=discord.Color.green())
    embed.add_field(name="Max Team Size", value=str(manager.settings.max_team_size), inline=False)
    embed.add_field(name="IP Range", value=manager.settings.ip_range or "Not configured", inline=False)
    embed.add_field(name="Max Teams (derived)", value=str(manager.settings.max_teams), inline=False)
    embed.add_field(name="Duration", value=f"{manager.settings.duration_minutes} minutes", inline=False)
    embed.add_field(name="Active Teams", value=str(len(manager.teams)), inline=False)
    embed.add_field(name="Available Team Numbers", value=str(len(manager.available_team_nums)), inline=False)
    embed.add_field(name="Closed Teams", value=str(sorted(manager.closed_teams)), inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)

class RequestMoreTeamsView(discord.ui.View):
    def __init__(self, user_id: int, username: str, guild_id: int):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.username = username
        self.guild_id = guild_id
    
    @discord.ui.button(label="Request More Teams", style=discord.ButtonStyle.danger)
    async def request_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        manager = multi_manager.get_manager(self.guild_id)
        
        embed = discord.Embed(
            title="Team Creation Request",
            description=f"{self.username} has requested more teams to be created.",
            color=discord.Color.orange()
        )
        embed.add_field(name="User ID", value=str(self.user_id), inline=False)
        embed.add_field(name="Reason", value="All teams are currently full", inline=False)
        
        for admin_id in manager.admins:
            await send_dm(admin_id, embed=embed)
        
        await interaction.response.send_message("Request sent to admins!", ephemeral=True)

@bot.tree.command(name="create_team", description="Create a new team")
async def create_team(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    manager = multi_manager.get_manager(interaction.guild_id)
    user_id = interaction.user.id
    
    if user_id in manager.user_teams:
        await interaction.followup.send("You're already in a team! Leave your current team first.", ephemeral=True)
        return
    
    if not manager.available_team_nums:
        view = RequestMoreTeamsView(user_id, interaction.user.name, interaction.guild_id)
        await interaction.followup.send("All teams are currently full! Would you like to request more teams?", view=view, ephemeral=True)
        return
    
    if len(manager.teams) >= manager.settings.max_teams:
        view = RequestMoreTeamsView(user_id, interaction.user.name, interaction.guild_id)
        await interaction.followup.send("All teams are currently full! Would you like to request more teams?", view=view, ephemeral=True)
        return
    
    team_num = min(manager.available_team_nums)
    manager.available_team_nums.remove(team_num)
    
    team = Team(team_num, user_id, manager.settings)
    manager.teams[team_num] = team
    manager.user_teams[user_id] = team_num
    
    team.members[user_id] = interaction.user.name
    
    # Replace the loading indicator with a visible status message
    await interaction.edit_original_response(content=f"Starting machines for Team {team_num}, please wait...")

    # Run revert + start subprocesses before notifying the user
    await run_network_setup(interaction.guild_id, team_num)

    # Machines are ready — now send the DM
    timer_embed = await create_timer_embed(team)
    dm_sent, channel_id, msg_id = await send_dm(user_id, embed=timer_embed)
    
    if not dm_sent:
        del manager.teams[team_num]
        del manager.user_teams[user_id]
        manager.available_team_nums.add(team_num)
        await interaction.edit_original_response(content="I couldn't DM you! Please enable DMs from server members and try again.")
        return
    
    if channel_id and msg_id:
        team.timer_message_ids[user_id] = (channel_id, msg_id)

    # Save state after team creation
    manager.save_teams()

    await interaction.edit_original_response(content=f"Team {team_num} is ready! Check your DMs for details.")

class JoinRequestView(discord.ui.View):
    def __init__(self, user_id: int, username: str, team_num: int, guild_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.username = username
        self.team_num = team_num
        self.guild_id = guild_id
    
    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        manager = multi_manager.get_manager(self.guild_id)
        
        if interaction.user.id != manager.teams[self.team_num].captain_id:
            await interaction.response.send_message("Only the team captain can approve join requests.", ephemeral=True)
            return
        
        team = manager.teams[self.team_num]
        if len(team.members) >= manager.settings.max_team_size:
            await interaction.response.send_message("That team is now full!", ephemeral=True)
            return
        
        team.members[self.user_id] = self.username
        manager.user_teams[self.user_id] = self.team_num
        
        timer_embed = await create_timer_embed(team)
        dm_sent, channel_id, msg_id = await send_dm(self.user_id, embed=timer_embed)
        
        if not dm_sent:
            await interaction.response.send_message("Couldn't send DM to user. They may have DMs disabled.", ephemeral=True)
            del team.members[self.user_id]
            del manager.user_teams[self.user_id]
            return
        
        if channel_id and msg_id:
            team.timer_message_ids[self.user_id] = (channel_id, msg_id)
        
        approved_embed = discord.Embed(
            title=f"Approved to Join Team {self.team_num}!",
            description="The team captain has approved your join request.",
            color=discord.Color.green()
        )
        await send_dm(self.user_id, embed=approved_embed)
        
        # Save state after member joins
        manager.save_teams()
        
        await interaction.response.send_message(f"Approved {self.username} to join Team {self.team_num}!", ephemeral=True)
    
    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger)
    async def deny_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        manager = multi_manager.get_manager(self.guild_id)
        
        if interaction.user.id != manager.teams[self.team_num].captain_id:
            await interaction.response.send_message("Only the team captain can deny join requests.", ephemeral=True)
            return
        
        denied_embed = discord.Embed(
            title=f"Join Request Denied for Team {self.team_num}",
            description="The team captain has denied your join request.",
            color=discord.Color.red()
        )
        await send_dm(self.user_id, embed=denied_embed)
        
        await interaction.response.send_message(f"Denied {self.username}'s join request.", ephemeral=True)

class JoinTeamButtonView(discord.ui.View):
    def __init__(self, user_id: int, username: str, guild_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.username = username
        self.guild_id = guild_id
    
    async def create_team_buttons(self):
        manager = multi_manager.get_manager(self.guild_id)
        
        for team_num, team in sorted(manager.teams.items()):
            if len(team.members) < manager.settings.max_team_size and team.is_active:
                button = discord.ui.Button(
                    label=f"Team {team_num} ({len(team.members)}/{manager.settings.max_team_size})",
                    style=discord.ButtonStyle.primary
                )
                button.callback = lambda i, tn=team_num: self.join_callback(i, tn)
                self.add_item(button)
    
    async def join_callback(self, interaction: discord.Interaction, team_num: int):
        manager = multi_manager.get_manager(self.guild_id)
        
        if team_num not in manager.teams:
            await interaction.response.send_message("That team no longer exists.", ephemeral=True)
            return
        
        team = manager.teams[team_num]
        if len(team.members) >= manager.settings.max_team_size:
            await interaction.response.send_message("That team is now full!", ephemeral=True)
            return
        
        request_view = JoinRequestView(self.user_id, self.username, team_num, self.guild_id)
        
        request_embed = discord.Embed(
            title=f"Join Request for Team {team_num}",
            description=f"{self.username} has requested to join your team.",
            color=discord.Color.yellow()
        )
        request_embed.add_field(name="Requested User ID", value=str(self.user_id), inline=False)
        
        captain_dm_sent, _, _ = await send_dm(team.captain_id, embed=request_embed, view=request_view)
        
        if not captain_dm_sent:
            await interaction.response.send_message("Couldn't send join request to captain. Captain may have DMs disabled.", ephemeral=True)
            return
        
        await interaction.response.send_message(f"Join request sent to Team {team_num} captain! They will review your request.", ephemeral=True)

@bot.tree.command(name="reset", description="Reset all teams (Admin only)")
@app_commands.default_permissions(administrator=True)
async def reset_teams(interaction: discord.Interaction):
    manager = multi_manager.get_manager(interaction.guild_id)
    
    if interaction.user.id not in manager.admins and interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    
    # Notify all team members that their teams are being reset
    for team_num, team in list(manager.teams.items()):
        reset_embed = discord.Embed(
            title=f"Team {team_num} - Reset",
            description="All teams have been reset by an administrator.",
            color=discord.Color.red()
        )
        for member_id in team.members.keys():
            await send_dm(member_id, embed=reset_embed)
            if member_id in manager.user_teams:
                del manager.user_teams[member_id]
    
    # Clear all teams and reopen them
    manager.teams.clear()
    manager.closed_teams.clear()
    manager.available_team_nums = set(range(1, manager.settings.max_teams + 1))
    
    # Save state after reset
    manager.save_teams()
    
    await interaction.followup.send("All teams have been reset and reopened!", ephemeral=True)

@bot.tree.command(name="join_team", description="Join an existing team")
async def join_team(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    manager = multi_manager.get_manager(interaction.guild_id)
    user_id = interaction.user.id
    
    if user_id in manager.user_teams:
        await interaction.followup.send("You're already in a team! Leave first using `/leave_team`.", ephemeral=True)
        return
    
    if not manager.teams:
        await interaction.followup.send("No teams available. Try creating one with `/create_team`!", ephemeral=True)
        return
    
    view = JoinTeamButtonView(user_id, interaction.user.name, interaction.guild_id)
    await view.create_team_buttons()
    
    if not view.children:
        await interaction.followup.send("No teams available to join at this moment. Try creating one with `/create_team`!", ephemeral=True)
        return
    
    info_embed = discord.Embed(title="Available Teams", color=discord.Color.blue())
    for team_num, team in sorted(manager.teams.items()):
        if len(team.members) < manager.settings.max_team_size and team.is_active:
            time_left = team.end_time - datetime.now()
            mins, secs = divmod(int(time_left.total_seconds()), 60)
            info_embed.add_field(
                name=f"Team {team_num}",
                value=f"**Members:** {len(team.members)}/{manager.settings.max_team_size}\n**Time Left:** {mins}m {secs}s\n**Members:** {', '.join(list(team.members.values())[:3])}{'...' if len(team.members) > 3 else ''}",
                inline=False
            )
    
    await interaction.followup.send(embed=info_embed, view=view, ephemeral=True)

@bot.tree.command(name="leave_team", description="Leave your current team")
async def leave_team(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    manager = multi_manager.get_manager(interaction.guild_id)
    user_id = interaction.user.id
    
    if user_id not in manager.user_teams:
        await interaction.followup.send("You're not in a team!", ephemeral=True)
        return
    
    team_num = manager.user_teams[user_id]
    team = manager.teams[team_num]
    username = interaction.user.name
    
    del team.members[user_id]
    del manager.user_teams[user_id]
    
    leave_embed = discord.Embed(
        title=f"Left Team {team_num}",
        description="You have left the team.",
        color=discord.Color.gold()
    )
    await send_dm(user_id, embed=leave_embed)
    
    if not team.members:
        await end_team(interaction.guild_id, team_num)
    else:
        if user_id == team.captain_id:
            team.captain_id = list(team.members.keys())[0]
            new_captain_embed = discord.Embed(
                title=f"You are now captain of Team {team_num}",
                description="The previous captain has left the team.",
                color=discord.Color.blue()
            )
            await send_dm(team.captain_id, embed=new_captain_embed)
        
        for member_id in team.members.keys():
            member_left_embed = discord.Embed(
                title=f"Member Left Team {team_num}",
                description=f"{username} has left the team.",
                color=discord.Color.gold()
            )
            await send_dm(member_id, embed=member_left_embed)
        
        # Save state after member leaves
        manager.save_teams()
    
    await interaction.followup.send(f"Left Team {team_num}.", ephemeral=True)

@bot.tree.command(name="end_team", description="End your team as captain")
async def end_team_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    manager = multi_manager.get_manager(interaction.guild_id)
    user_id = interaction.user.id
    
    if user_id not in manager.user_teams:
        await interaction.followup.send("You're not in a team!", ephemeral=True)
        return
    
    team_num = manager.user_teams[user_id]
    team = manager.teams[team_num]
    
    if user_id != team.captain_id:
        await interaction.followup.send("Only the team captain can end the team!", ephemeral=True)
        return
    
    await end_team(interaction.guild_id, team_num)
    await interaction.followup.send(f"Team {team_num} has been ended.", ephemeral=True)

@bot.tree.command(name="reopen_team", description="Reopen a closed team (Admin only)")
@app_commands.describe(team_num="The team number to reopen")
@app_commands.default_permissions(administrator=True)
async def reopen_team(interaction: discord.Interaction, team_num: int):
    manager = multi_manager.get_manager(interaction.guild_id)
    
    if interaction.user.id not in manager.admins and interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    
    if team_num not in manager.closed_teams:
        await interaction.response.send_message("That team is not closed or doesn't exist.", ephemeral=True)
        return
    
    manager.closed_teams.remove(team_num)
    manager.available_team_nums.add(team_num)
    
    # Save state after reopening team
    manager.save_teams()
    
    await interaction.response.send_message(f"Team {team_num} has been reopened.", ephemeral=True)

@bot.tree.command(name="save_data", description="Manually save all data (Admin only)")
@app_commands.default_permissions(administrator=True)
async def save_data(interaction: discord.Interaction):
    manager = multi_manager.get_manager(interaction.guild_id)
    
    if interaction.user.id not in manager.admins and interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    
    manager.save_settings()
    manager.save_admins()
    manager.save_teams()
    
    await interaction.followup.send("All data has been saved successfully!", ephemeral=True)

@bot.tree.command(name="toggle_bot", description="Toggle the bot on or off for this server (Admin only)")
@app_commands.default_permissions(administrator=True)
async def toggle_bot(interaction: discord.Interaction):
    manager = multi_manager.get_manager(interaction.guild_id)

    if interaction.user.id not in manager.admins and interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    manager.settings.bot_enabled = not manager.settings.bot_enabled
    manager.save_settings()

    status = "**ON**" if manager.settings.bot_enabled else "**OFF**"
    await interaction.response.send_message(f"The bot is now {status} for this server.", ephemeral=True)

# Run the bot
bot.run(token)
