import pytest
import datetime
import discord
from unittest.mock import MagicMock, AsyncMock

from cogs.TaskBoard import FileManager, Task
from freezegun import freeze_time
from discord.ext.commands.errors import CheckFailure

pytestmark = pytest.mark.asyncio


async def test_add_task(cog, mock_context):
    """Tests if the !addtask command successfully adds a task."""
    # Arrange
    member_to_add = MagicMock(spec=discord.Member)
    member_to_add.id = 3000
    member_to_add.mention = "@TestMember"
    due_date_str = "2025-12-25"
    description = "Finish pytest setup"

    # Act: Call the command's underlying callback function directly
    # CHANGED: Added .callback
    await cog.add_task.callback(cog, mock_context, member=member_to_add, due_date_str=due_date_str,
                                description=description)

    # Assert
    guild_id = mock_context.guild.id
    assert guild_id in cog.tasks
    assert member_to_add.id in cog.tasks[guild_id]
    assert len(cog.tasks[guild_id][member_to_add.id]) == 1
    new_task = cog.tasks[guild_id][member_to_add.id][0]
    assert new_task.task == description

    mock_context.send.assert_called_once_with(f"✅ Task added for {member_to_add.mention}: **{description}**")
    cog.file_manager.save_tasks.assert_awaited_once()


async def test_remove_task_and_cleanup(cog, mock_context):
    """Tests if !removetask correctly removes a task and cleans up empty entries."""
    # Arrange
    guild_id = mock_context.guild.id
    user_id = mock_context.author.id
    task = Task(user_id=user_id, task="A task to be removed", due_date=datetime.datetime.now())
    cog.tasks = {guild_id: {user_id: [task]}}

    # Act
    # CHANGED: Added .callback
    await cog.remove_task.callback(cog, mock_context, indices=[1])

    # Assert
    assert guild_id not in cog.tasks
    mock_context.send.assert_called_once_with("🗑️ Task(s) removed: **A task to be removed**")
    cog.file_manager.save_tasks.assert_awaited_once()


async def test_remove_task_invalid_index(cog, mock_context):
    """Tests if !removetask handles an out-of-bounds index gracefully."""
    # Arrange
    guild_id = mock_context.guild.id
    user_id = mock_context.author.id
    task = Task(user_id=user_id, task="An existing task", due_date=datetime.datetime.now())
    cog.tasks = {guild_id: {user_id: [task]}}

    # Act
    # CHANGED: Added .callback
    await cog.remove_task.callback(cog, mock_context, indices=[99])

    # Assert
    assert len(cog.tasks[guild_id][user_id]) == 1
    mock_context.send.assert_called_once_with("⚠️ Invalid task number: `99`.")
    cog.file_manager.save_tasks.assert_not_awaited()


async def test_show_tasks_for_user_with_no_tasks(cog, mock_context):
    """Tests the !showtasks command for a user who has no tasks."""
    # Act
    # CHANGED: Added .callback
    await cog.show_tasks.callback(cog, mock_context, member=None)

    # Assert
    call_args, call_kwargs = mock_context.send.call_args
    sent_embed = call_kwargs['embed']
    assert sent_embed.description == "You have no tasks on this server!"

async def test_remove_task_from_multiple(cog, mock_context):
    """Tests removing one task when the user has multiple."""
    # Arrange
    guild_id = mock_context.guild.id
    user_id = mock_context.author.id
    task1 = Task(user_id=user_id, task="Task 1", due_date=datetime.datetime.now())
    task2 = Task(user_id=user_id, task="Task 2", due_date=datetime.datetime.now())
    cog.tasks = {guild_id: {user_id: [task1, task2]}}

    # Act: Remove the first task
    await cog.remove_task.callback(cog, mock_context, indices=[1])

    # Assert
    # Check that the user and guild entries still exist
    assert guild_id in cog.tasks
    assert user_id in cog.tasks[guild_id]

    # Check that only one task remains, and it's the correct one
    remaining_tasks = cog.tasks[guild_id][user_id]
    assert len(remaining_tasks) == 1
    assert remaining_tasks[0].task == "Task 2"

    cog.file_manager.save_tasks.assert_awaited_once()


@freeze_time("2025-08-05 16:00:00")  # Freeze time to a known point
async def test_check_reminders_sends_dm(cog, mock_context):
    """Tests that the reminder loop sends a DM for a task due in <24 hours."""
    # Arrange
    guild_id = mock_context.guild.id
    user_id = mock_context.author.id

    # This task is due in 22 hours, so it should trigger a reminder
    due_date = datetime.datetime.now() + datetime.timedelta(hours=22)
    task_to_remind = Task(user_id=user_id, task="Urgent Task", due_date=due_date)

    # This task is due in 48 hours, so it should NOT trigger a reminder
    task_not_to_remind = Task(user_id=user_id, task="Future Task",
                              due_date=datetime.datetime.now() + datetime.timedelta(hours=48))

    cog.tasks = {guild_id: {user_id: [task_to_remind, task_not_to_remind]}}

    # We need a mock user object that can be found by bot.get_user()
    # and has a .send() method we can check.
    mock_user = MagicMock(spec=discord.User)
    mock_user.send = AsyncMock()
    cog.bot.get_user.return_value = mock_user

    # Mock get_guild to return a mock guild with a name
    mock_guild = MagicMock(spec=discord.Guild)
    mock_guild.name = "Test Server"
    cog.bot.get_guild.return_value = mock_guild

    # Act: Manually run one iteration of the background loop's coroutine
    await cog.check_reminders.coro(cog)

    # Assert
    # Check that the user's DMs were sent exactly once.
    mock_user.send.assert_awaited_once()

    # Check the content of the reminder DM
    call_args, _ = mock_user.send.call_args
    sent_message = call_args[0]
    assert "Urgent Task" in sent_message
    assert "Test Server" in sent_message


async def test_file_manager_save_and_load(tmp_path):
    """
    Tests that the FileManager can correctly save and then load
    the nested task structure to a real (temporary) file.
    """
    # Arrange
    # Create a temporary file path
    file_path = tmp_path / "test_tasks.json"
    manager = FileManager(file_path=str(file_path))

    # Create sample data
    guild_id = 1000
    user_id = 2000
    task = Task(user_id, "Test persistence", datetime.datetime(2025, 1, 1))
    original_tasks = {guild_id: {user_id: [task]}}

    # Act: Save the data, then load it back into a new variable
    await manager.save_tasks(original_tasks)
    loaded_tasks = await manager.load_tasks()

    # Assert
    assert guild_id in loaded_tasks
    assert user_id in loaded_tasks[guild_id]
    assert len(loaded_tasks[guild_id][user_id]) == 1

    loaded_task = loaded_tasks[guild_id][user_id][0]
    assert loaded_task.task == "Test persistence"
    assert loaded_task.user_id == user_id


async def test_full_task_lifecycle(cog, mock_context):
    """
    Simulates a full user workflow: add, mark done, then remove a task.
    """
    # Arrange
    member = mock_context.author
    guild_id = mock_context.guild.id
    user_id = member.id

    # --- 1. ADD a task ---
    await cog.add_task.callback(
        cog, mock_context, member=member, due_date_str="2026-01-01", description="Lifecycle task"
    )

    # Assert initial state
    assert len(cog.tasks[guild_id][user_id]) == 1
    assert not cog.tasks[guild_id][user_id][0].is_finished

    # --- 2. MARK a task as DONE ---
    await cog.task_done.callback(cog, mock_context, indices=[1])

    # Assert done state
    assert cog.tasks[guild_id][user_id][0].is_finished

    # --- 3. REMOVE the task ---
    await cog.remove_task.callback(cog, mock_context, indices=[1])

    # Assert final state is empty
    assert guild_id not in cog.tasks


async def test_show_all_tasks_permission_failure(cog, mock_context):
    """
    Tests that !showalltasks fails if the user lacks 'manage_guild' permissions.
    """
    # Arrange: Mock the author's permissions to NOT have manage_guild
    mock_context.author.guild_permissions = discord.Permissions()
    mock_context.author.guild_permissions.manage_guild = False

    # Act & Assert: Use pytest.raises to confirm a CheckFailure is thrown
    with pytest.raises(CheckFailure):
        # CHANGED: Use .invoke() to run the command with its permission checks.
        # .invoke() correctly handles passing the context and cog instance.
        await cog.show_all_tasks.invoke(mock_context)