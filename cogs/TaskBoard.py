# cogs/TaskBoard.py
"""
A cog for managing a server-specific team task board.

This cog provides a complete task management system for Discord servers.
Users can be assigned tasks with due dates, view their own or others' tasks,
and mark them as complete. The system is designed to be multiserver, with
all data saved persistently to a JSON file. A background task also handles
sending reminders to users for tasks that are due soon.

Key Features:
- Per-server task lists.
- Task assignment to specific server members.
- Persistent storage of tasks in a JSON file using aiofiles.
- Background reminder system for tasks due the next day.
- Commands for adding, viewing, completing, and removing tasks.
"""

import asyncio
import json
import datetime
from typing import List, Optional, Dict

import aiofiles
import discord
from discord.ext import commands, tasks


# --- Data Structures ---

class Task:
    """
    A data class representing a single task.

    This class serves as a structured model for a task, containing all its
    relevant information. It includes methods for serialization to and from

    dictionaries, which is essential for saving the data to a JSON file.

    Attributes:
        user_id (int): The Discord ID of the user the task is assigned to.
        task (str): The description of the task.
        due_date (datetime.datetime): The date the task is due.
        finished (Optional[str]): A timestamp if the task is done, otherwise None.
    """
    def __init__(self, user_id: int, task: str, due_date: datetime.datetime, finished: Optional[str] = None):
        self.user_id: int = user_id
        self.task: str = task
        self.due_date: datetime.datetime = due_date
        # finished is None for incomplete tasks, making it easy to check the status.
        self.finished: Optional[str] = finished

    def mark_done(self):
        """Marks the task as done by setting the current timestamp."""
        self.finished = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    def mark_undone(self):
        """Marks the task as not done by resetting the finished timestamp."""
        self.finished = None

    @property
    def is_finished(self) -> bool:
        """A convenient property to check if the task is finished."""
        return self.finished is not None

    def to_dict(self) -> dict:
        """
        Converts the Task object to a dictionary for JSON serialization.

        This is crucial for the FileManager to write the task data to a file.
        Dates are converted to a standard string format.
        """
        return {
            'user_id': self.user_id,
            'task': self.task,
            'due_date': self.due_date.strftime('%Y-%m-%d'),
            'finished': self.finished
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'Task':
        """
        Creates a Task object from a dictionary.

        This is used by the FileManager to reconstruct Task objects after
        reading the data from the JSON file.
        """
        due_date = datetime.datetime.strptime(data['due_date'], '%Y-%m-%d')
        return cls(data['user_id'], data['task'], due_date, data.get('finished'))


# --- File Management ---
class FileManager:
    """
    Manages thread-safe loading and saving of tasks to a JSON file.

    This class abstracts away the file I/O operations, ensuring that reading
    and writing to the tasks file doesn't cause race conditions, which is
    important in an asynchronous environment.
    """
    def __init__(self, file_path: str = 'tasks.json'):
        self.file_path = file_path
        # An asyncio.Lock prevents multiple coroutines from writing to the file at the same time.
        self._lock = asyncio.Lock()

    async def load_tasks(self) -> Dict[int, Dict[int, List[Task]]]:
        """
        Loads tasks from the JSON file into a nested dictionary structure.

        The structure is: {guild_id: {user_id: [Task, ...]}}. This allows for
        efficient, server-specific lookups.
        """
        async with self._lock:
            try:
                async with aiofiles.open(self.file_path, 'r', encoding='utf-8') as f:
                    data = json.loads(await f.read())
                    tasks = {}
                    # Reconstruct the nested dictionary structure from the file data.
                    for guild_id_str, user_task_map in data.items():
                        guild_id = int(guild_id_str)
                        tasks[guild_id] = {}
                        for user_id_str, task_list in user_task_map.items():
                            user_id = int(user_id_str)
                            # Convert each task dictionary back into a Task object.
                            tasks[guild_id][user_id] = [Task.from_dict(task_data) for task_data in task_list]
                    return tasks
            # If the file doesn't exist or is empty, return an empty dictionary.
            except (FileNotFoundError, json.JSONDecodeError):
                return {}

    async def save_tasks(self, tasks: Dict[int, Dict[int, List[Task]]]):
        """Saves the current state of all tasks to the JSON file."""
        async with self._lock:
            # Use a dictionary comprehension to prepare the data for JSON.
            # Keys (IDs) are converted to strings, and Task objects are converted to dicts.
            serializable_tasks = {
                str(guild_id): {
                    str(user_id): [task.to_dict() for task in task_list]
                    for user_id, task_list in user_task_map.items()
                }
                for guild_id, user_task_map in tasks.items()
            }
            async with aiofiles.open(self.file_path, 'w', encoding='utf-8') as f:
                await f.write(json.dumps(serializable_tasks, indent=4))


# --- Discord Cog ---
class TaskBoard(commands.Cog):
    """
    The main cog for handling all task-related commands and events.

    This class manages the bot's task state, including the main task dictionary,
    the background reminder loop, and all user-facing commands for task management.
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.file_manager = FileManager()
        # The main in-memory data structure for storing tasks.
        # Format: {guild_id: {user_id: [Task, ...]}}
        self.tasks: Dict[int, Dict[int, List[Task]]] = {}
        self.check_reminders.start()

    async def cog_load(self):
        """
        Special event that runs when the cog is loaded.
        This is the ideal place to load our data from the file.
        """
        self.tasks = await self.file_manager.load_tasks()

    def cog_unload(self):
        """
        Special event that runs when the cog is about to be unloaded.
        We cancel the background task and save the current state to ensure no data is lost.
        """
        self.check_reminders.cancel()
        asyncio.create_task(self.file_manager.save_tasks(self.tasks))

    @tasks.loop(minutes=30)
    async def check_reminders(self):
        """
        A background task that runs periodically to send reminders.

        It checks all tasks across all servers. If a task is not finished
        and is due the next day, it sends a direct message to the assigned user.
        """
        now = datetime.datetime.now()
        one_day_later = now + datetime.timedelta(days=1)

        # Iterate through all guilds, then all users within each guild.
        for guild_id, user_task_map in self.tasks.items():
            for user_id, user_tasks in user_task_map.items():
                for task in user_tasks:
                    # Check if the task is unfinished and due within the next 24 hours.
                    if not task.is_finished and now < task.due_date < one_day_later:
                        user = self.bot.get_user(user_id)
                        guild = self.bot.get_guild(guild_id)
                        if user and guild:
                            try:
                                await user.send(
                                    f"👋 Reminder from server **{guild.name}**: Your task **'{task.task}'** is due tomorrow!")
                            # This handles cases where the user has blocked the bot or disabled DMs.
                            except discord.Forbidden:
                                print(f"Could not send reminder to user {user_id}.")

    @check_reminders.before_loop
    async def before_check_reminders(self):
        """Ensures the bot is fully logged in and ready before the loop starts."""
        await self.bot.wait_until_ready()

    # --- Commands ---

    @commands.command(name='addtask')
    @commands.guild_only()
    async def add_task(self, ctx: commands.Context, member: discord.Member, due_date_str: str, *, description: str):
        """Adds a new task for a user on this server. Date format: YYYY-MM-DD."""
        try:
            due_date = datetime.datetime.strptime(due_date_str, '%Y-%m-%d')
        except ValueError:
            return await ctx.send("❌ Incorrect date format. Please use **YYYY-MM-DD**.")

        new_task = Task(member.id, description, due_date)

        # Using setdefault is a robust way to initialize nested dictionaries if they don't exist.
        guild_tasks = self.tasks.setdefault(ctx.guild.id, {})
        user_tasks = guild_tasks.setdefault(member.id, [])
        user_tasks.append(new_task)

        await self.file_manager.save_tasks(self.tasks)
        await ctx.send(f"✅ Task added for {member.mention}: **{description}**")

    @commands.command(name='showtasks')
    @commands.guild_only()
    async def show_tasks(self, ctx: commands.Context, member: Optional[discord.Member]):
        """Shows your tasks or the tasks of a specified member on this server."""
        # If no member is specified, the target is the command author.
        target_user = member or ctx.author

        # Access the tasks safely, providing an empty dict as a default to avoid errors.
        guild_tasks = self.tasks.get(ctx.guild.id, {})
        user_tasks = guild_tasks.get(target_user.id)

        if not user_tasks:
            description = "You have no tasks on this server!" if target_user == ctx.author else f"{target_user.display_name} has no tasks on this server."
            embed = discord.Embed(title=f"{target_user.display_name}'s Tasks", description=description,
                                  color=discord.Color.orange())
            return await ctx.send(embed=embed)

        embed = discord.Embed(title=f"{target_user.display_name}'s Tasks", color=discord.Color.blue())
        for i, task in enumerate(user_tasks, 1):
            status = f"✅ Done on {task.finished}" if task.is_finished else "❌ Not Finished"
            embed.add_field(
                name=f"📌 Task #{i}: {task.task}",
                value=f"Due: `{task.due_date.strftime('%Y-%m-%d')}`\nStatus: {status}",
                inline=False
            )
        await ctx.send(embed=embed)

    @commands.command(name='showalltasks')
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def show_all_tasks(self, ctx: commands.Context):
        """Shows all tasks for everyone on this server (Admin only)."""
        # Retrieve tasks only for the guild where the command was used.
        guild_tasks = self.tasks.get(ctx.guild.id)
        if not guild_tasks:
            return await ctx.send(
                embed=discord.Embed(title="All Server Tasks", description="This server's task board is empty!",
                                    color=discord.Color.green()))

        embed = discord.Embed(title=f"All Tasks on {ctx.guild.name}", color=discord.Color.purple())
        for user_id, user_tasks in guild_tasks.items():
            member = ctx.guild.get_member(user_id)
            # Skip users who may have left the server.
            if not member: continue

            task_list_str = []
            for i, task in enumerate(user_tasks, 1):
                status = "✅" if task.is_finished else "❌"
                task_list_str.append(f"`{i}.` {status} {task.task} (Due: {task.due_date.strftime('%Y-%m-%d')})")

            if task_list_str:
                embed.add_field(name=member.display_name, value="\n".join(task_list_str), inline=False)

        await ctx.send(embed=embed)

    @commands.command(name='taskdone')
    @commands.guild_only()
    async def task_done(self, ctx: commands.Context, indices: commands.Greedy[int]):
        """Marks one or more of your tasks on this server as done."""
        if not indices:
            return await ctx.send("Please provide the task number(s). Example: `!taskdone 1 3`")

        guild_id, user_id = ctx.guild.id, ctx.author.id
        user_tasks = self.tasks.get(guild_id, {}).get(user_id)
        if not user_tasks:
            return await ctx.send("You have no tasks on this server.")

        marked_tasks = []
        # Using a set removes duplicate numbers, and sorting ensures a logical order.
        for index in sorted(list(set(indices))):
            if 1 <= index <= len(user_tasks):
                task = user_tasks[index - 1]
                if not task.is_finished:
                    task.mark_done()
                    marked_tasks.append(task.task)
            else:
                await ctx.send(f"⚠️ Invalid task number: `{index}`.")

        if marked_tasks:
            await self.file_manager.save_tasks(self.tasks)
            await ctx.send(f"✅ Marked as done: **{', '.join(marked_tasks)}**")

    @commands.command(name='removetask')
    @commands.guild_only()
    async def remove_task(self, ctx: commands.Context, indices: commands.Greedy[int]):
        """Removes one or more of your tasks on this server."""
        if not indices:
            return await ctx.send("Please provide the task number(s). Example: `!removetask 2`")

        guild_id, user_id = ctx.guild.id, ctx.author.id
        user_tasks = self.tasks.get(guild_id, {}).get(user_id)
        if not user_tasks:
            return await ctx.send("You have no tasks to remove.")

        removed_tasks = []
        # Sorting unique indices in descending order is crucial when using .pop()
        # as it prevents index shifting from affecting subsequent removals.
        for index in sorted(list(set(indices)), reverse=True):
            if 1 <= index <= len(user_tasks):
                removed_task = user_tasks.pop(index - 1)
                removed_tasks.append(removed_task.task)
            else:
                await ctx.send(f"⚠️ Invalid task number: `{index}`.")

        if removed_tasks:
            # Clean up empty dictionaries to keep the data file tidy.
            if not self.tasks.get(guild_id, {}).get(user_id):
                del self.tasks[guild_id][user_id]
            if not self.tasks.get(guild_id):
                del self.tasks[guild_id]

            await self.file_manager.save_tasks(self.tasks)
            # Reverse the list of removed tasks for a more logical display order.
            await ctx.send(f"🗑️ Task(s) removed: **{', '.join(reversed(removed_tasks))}**")


async def setup(bot: commands.Bot):
    """The entry point for loading the cog."""
    await bot.add_cog(TaskBoard(bot))