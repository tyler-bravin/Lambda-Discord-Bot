# cogs/views.py
"""
Interactive discord.ui components for the music cog.

These views hold no music logic of their own — they read and drive state on the
Music cog they're constructed with, so all playback rules (votes, permissions)
stay in one place.
"""
import math
import urllib.parse

import discord


class PlayerControls(discord.ui.View):
    """
    A persistent view attached to the 'Now Playing' message with player controls.

    Built with `timeout=None` so the buttons keep working even after the bot
    restarts (the cog re-registers this view on load).
    """
    def __init__(self, music_cog, player, player_url: str = None):
        super().__init__(timeout=None)
        self.cog = music_cog

        # Dynamically create and add a "Lyrics" button that links to a Genius search.
        if player and player.title:
            search_query = f"{player.title} {player.uploader}"
            encoded_query = urllib.parse.quote_plus(search_query)
            lyrics_url = f"https://genius.com/search?q={encoded_query}"

            lyrics_button = discord.ui.Button(
                label="Lyrics", emoji="📜", style=discord.ButtonStyle.link, url=lyrics_url
            )
            self.add_item(lyrics_button)

        # Link to the live web player when the dashboard is running.
        if player_url:
            self.add_item(discord.ui.Button(
                label="Live Player", emoji="🎧", style=discord.ButtonStyle.link, url=player_url
            ))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """
        Validates that the interacting user is in the bot's voice channel.
        This prevents users outside the current music session from using the controls.
        """
        vc = interaction.guild.voice_client
        if not vc:
            await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
            return False
        if not interaction.user.voice or interaction.user.voice.channel != vc.channel:
            await interaction.response.send_message("❌ You must be in the voice channel to use this.", ephemeral=True)
            return False
        return True

    @discord.ui.button(emoji='⏮️', style=discord.ButtonStyle.secondary, custom_id="player_previous")
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Button to play the previously played song."""
        if not await self.cog._web_previous(interaction.guild):
            return await interaction.response.send_message("There is no previous song in the history.", ephemeral=True)
        await interaction.response.send_message("⏪ Playing previous song.", ephemeral=True)

    @discord.ui.button(emoji='⏯️', style=discord.ButtonStyle.secondary, custom_id="player_play_pause")
    async def play_pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Toggles between playing and pausing the current song."""
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            await self.cog._pause_logic(interaction)
        elif vc and vc.is_paused():
            # Resuming does not require a vote.
            vc.resume()
            self.cog.inactive_since.pop(interaction.guild.id, None)
            self.cog.pause_votes.pop(interaction.guild.id, None)
            await interaction.response.send_message("▶️ Resumed.", ephemeral=True)

    @discord.ui.button(emoji='⏹️', style=discord.ButtonStyle.danger, custom_id="player_stop")
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Stops playback and clears the queue, subject to a vote."""
        await self.cog._stop_logic(interaction)

    @discord.ui.button(emoji='⏭️', style=discord.ButtonStyle.secondary, custom_id="player_skip")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Skips the current song, subject to a vote."""
        await self.cog._skip_logic(interaction)


class LoopControlsView(discord.ui.View):
    """A temporary, user-restricted view for the `!loop` command to select a loop mode."""
    def __init__(self, music_cog, ctx):
        super().__init__(timeout=120.0)
        self.cog = music_cog
        self.ctx = ctx
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Ensures only the command author can use this interactive menu."""
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("❌ You cannot use this menu.", ephemeral=True)
            return False
        # Further checks ensure the user is still in the correct voice channel.
        vc = interaction.guild.voice_client
        if not vc or not interaction.user.voice or interaction.user.voice.channel != vc.channel:
            await interaction.response.send_message("❌ You must be in the voice channel to use this.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        """Disables all buttons and updates the message when the view expires."""
        if self.message:
            for item in self.children:
                item.disabled = True
            await self.message.edit(content="*This loop menu has expired.*", view=self)

    async def _disable_and_stop(self):
        """Greys out the menu once a mode has been chosen."""
        for item in self.children:
            item.disabled = True
        await self.message.edit(view=self)
        self.stop()

    async def _handle_vote(self, interaction: discord.Interaction, mode: str):
        """Handles the logic for voting on a loop mode."""
        # Admins can bypass the vote entirely.
        if interaction.user.guild_permissions.administrator:
            self.cog.loop_states[interaction.guild.id] = mode
            await self._disable_and_stop()
            await interaction.response.send_message(f"✅ Loop mode force-set to **{mode}** by an admin.", ephemeral=True)
            return

        vc = interaction.guild.voice_client
        guild_votes = self.cog.loop_votes.setdefault(interaction.guild.id, {})
        voters = guild_votes.setdefault(mode, set())
        status, votes, required_votes = self.cog._tally_vote(vc, voters, interaction.user.id)

        if status == self.cog.VOTE_ALREADY:
            await interaction.response.send_message(f"ℹ️ You have already voted to set loop to **{mode}**.", ephemeral=True)
            return

        # Check if the vote threshold has been met.
        if status == self.cog.VOTE_PASSED:
            self.cog.loop_states[interaction.guild.id] = mode
            self.cog.loop_votes.pop(interaction.guild.id, None)
            await self._disable_and_stop()
            await interaction.response.send_message(f"🗳️ Vote passed! Loop mode has been set to **{mode}**.")
        else:
            await interaction.response.send_message(
                f"🗳️ Your vote to set loop to **{mode}** was added. Now at **{votes}/{required_votes}** votes.",
                ephemeral=True)

    @discord.ui.button(label="Loop Song", emoji="🔂", style=discord.ButtonStyle.secondary, custom_id="loop_song")
    async def loop_song_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_vote(interaction, 'song')

    @discord.ui.button(label="Loop Queue", emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="loop_queue")
    async def loop_queue_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_vote(interaction, 'queue')

    @discord.ui.button(label="Turn Off", emoji="❌", style=discord.ButtonStyle.danger, custom_id="loop_off")
    async def loop_off_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_vote(interaction, 'off')


class QueuePaginator(discord.ui.View):
    """A view for paginating through the song queue with interactive buttons."""
    QUEUE_SONGS_PER_PAGE = 5

    def __init__(self, queue, now_playing):
        super().__init__(timeout=120)
        self.queue = queue
        self.now_playing = now_playing
        self.current_page = 0
        self.songs_per_page = self.QUEUE_SONGS_PER_PAGE
        self.total_pages = math.ceil(len(self.queue) / self.songs_per_page)

        # Disable navigation buttons if there's only one page or no pages.
        if self.total_pages <= 1:
            self.previous_button.disabled = True
            self.next_button.disabled = True

    async def get_page_embed(self):
        """Constructs the embed for the current page of the queue."""
        embed = discord.Embed(title="🎵 Music Queue", color=discord.Color.purple())
        if self.now_playing:
            embed.add_field(name="Now Playing", value=f"[{self.now_playing.title}]({self.now_playing.url})", inline=False)
            if self.now_playing.thumbnail:
                embed.set_thumbnail(url=self.now_playing.thumbnail)
        else:
            embed.add_field(name="Now Playing", value="Nothing is currently playing.", inline=False)

        # Calculate the slice of the queue for the current page.
        start_index = self.current_page * self.songs_per_page
        end_index = start_index + self.songs_per_page
        if self.queue:
            upcoming_list = ""
            for i, song in enumerate(self.queue[start_index:end_index], start=start_index):
                if song.url:
                    upcoming_list += f"**{i + 1}.** [{discord.utils.escape_markdown(song.title)}]({song.url})\n"
                else:
                    upcoming_list += f"**{i + 1}.** {discord.utils.escape_markdown(song.title)}\n"
            if upcoming_list:
                embed.add_field(name="Up Next", value=upcoming_list, inline=False)

        footer_text = f"{len(self.queue)} songs in queue"
        if self.total_pages > 0:
            footer_text = f"Page {self.current_page + 1}/{self.total_pages} | {footer_text}"
        embed.set_footer(text=footer_text)
        return embed

    @discord.ui.button(label='⬅️', style=discord.ButtonStyle.primary)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Navigates to the previous page of the queue."""
        self.current_page = (self.current_page - 1 + self.total_pages) % self.total_pages
        embed = await self.get_page_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label='➡️', style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Navigates to the next page of the queue."""
        self.current_page = (self.current_page + 1) % self.total_pages
        embed = await self.get_page_embed()
        await interaction.response.edit_message(embed=embed, view=self)
