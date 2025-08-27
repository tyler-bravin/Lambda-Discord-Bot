# Lambda - A Multi-purpose Discord Bot

![Discord.py](https://img.shields.io/badge/discord.py-v2.3.2-7289DA?style=for-the-badge&logo=discord&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white)
![MIT License](https://img.shields.io/badge/License-MIT-green.svg?style=for-the-badge)

A comprehensive, multi-purpose Discord bot built with `discord.py`. It features a feature-rich, persistent music player and a server-specific task management system.

## ✨ Key Features

### 🎵 Music System
- **Wide Source Support**: Play music from YouTube and Spotify (tracks, playlists, and albums).
- **Persistent Queue**: The music queue is saved to a database, so it survives bot restarts.
- **Interactive Controls**: A modern UI with buttons for play/pause, skip, stop, previous, and lyrics.
- **Democratic Controls**: A vote-based system for skipping, stopping, and other actions ensures fair use.
- **Advanced Features**: Supports song/queue looping, volume control, and automatic disconnection when idle.

### ✅ Task Management
- **Server-Specific Tasks**: Each Discord server has its own independent task board.
- **Assign & Track**: Assign tasks with due dates to specific members.
- **Persistent Storage**: Tasks are saved to a `tasks.json` file.
- **Automatic Reminders**: The bot automatically sends a DM to users one day before their task is due.
- **Admin Commands**: Server managers can view all tasks on the board.



## 🛠️ Setup & Installation

Follow these steps to get your own instance of the bot running.

### 1. Prerequisites
- [Python 3.10 or higher](https://www.python.org/)
- [Git](https://git-scm.com/)
- An empty Discord application and bot token. You can create one on the [Discord Developer Portal](https://discord.com/developers/applications).

### 2. Clone the Repository
Clone this repository to your local machine.
```bash
git clone https://github.com/tyler-bravin/lambda-bot.git
cd lambda-bot
```

### 3. Set up a Virtual Environment
It's highly recommended to use a virtual environment to manage dependencies.
```bash
# Windows
python -m venv .venv
.\.venv\Scripts\activate

# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate
```

### 4. Install Dependencies
Create a file named `requirements.txt` in your project directory with the following content:
```
# requirements.txt
discord.py[voice]
python-dotenv
yt-dlp
spotipy
aiosqlite
aiofiles
PyNaCl
```
Then, install the dependencies using pip:
```bash
pip install -r requirements.txt
```

### 5. Configuration (`.env` file)
Create a file named `.env` in the root of your project directory. This file will store your secret keys. **Do not share this file.**

You will need:
- Your **Discord Bot Token**.
- A **Spotify API Client ID and Secret**. You can get these by creating an app on the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard/).

Your `.env` file should look like this:
```ini
# .env
DISCORD_TOKEN="YOUR_DISCORD_BOT_TOKEN_HERE"
SPOTIPY_CLIENT_ID="YOUR_SPOTIFY_CLIENT_ID_HERE"
SPOTIPY_CLIENT_SECRET="YOUR_SPOTIFY_CLIENT_SECRET_HERE"
```

### 6. YouTube Cookies (Optional but Recommended)
To play **age-restricted videos** and prevent certain YouTube errors (like `HTTP Error 429: Too Many Requests`), you need a `cookies.txt` file.

1.  Install a browser extension that can export cookies in the `Netscape` format. A good one for [Chrome](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) or [Firefox](https://addons.mozilla.org/en-US/firefox/addon/cookies-txt/) is **Get cookies.txt LOCALLY**.
2.  Log in to a YouTube account in your browser.
3.  Go to the YouTube homepage.
4.  Click the extension's icon and download the `cookies.txt` file.
5.  Place the downloaded `cookies.txt` file in the **root directory** of your bot project (the same place as your `.env` file).

### 7. Run the Bot
You can now start the bot using your main Python file (e.g., `main.py`).
```bash
python main.py
```

## 🤖 Usage & Commands

The default command prefix is `!`.

### Music Commands
| Command | Aliases | Description |
|---|---|---|
| `!play <song>` | `!p` | Plays a song from a URL or search query. |
| `!pause` | | Pauses the current song (requires votes). |
| `!stop` | | Stops the music and clears the queue (requires votes). |
| `!skip` | `!s` | Skips the current song (requires votes). |
| `!queue` | `!q` | Displays the current song queue. |
| `!nowplaying` | `!np` | Shows detailed info about the current song. |
| `!volume <0-200>`| `!vol` | Sets the music volume for the server. |
| `!loop [song/queue/off]` | | Sets the loop mode. Shows a menu if no mode is given. |
| `!shuffle` | `!shuf` | Shuffles the queue (requires votes). |
| `!remove <number>`| `!rm` | Removes a song from the queue by its number. |
| `!clear` | | Clears all songs from the queue (requires votes). |
| `!disconnect` | `!dc` | Disconnects the bot from the voice channel. |

### TaskBoard Commands
| Command | Description |
|---|---|
| `!addtask <@member> <YYYY-MM-DD> <task>` | Adds a new task for a member. |
| `!showtasks [@member]` | Shows your tasks or another member's tasks. |
| `!taskdone <number(s)>` | Marks one or more of your tasks as complete. |
| `!removetask <number(s)>` | Removes one or more of your tasks. |
| `!showalltasks` | **(Admin Only)** Shows all tasks on the server. |

### Admin Commands
| Command | Description |
|---|---|
| `!reload` | **(Bot Owner Only)** Reloads all cogs. |

## 📜 License
This project is licensed under the MIT License. See the `LICENSE` file for details.