# 🚀 Universal Media Hub

> 🤖 **100% AI-Generated Project**
> *This entire application, from the responsive frontend and the robust Flask backend to the Telegram bot, the Docker architecture and this very README, was built end-to-end by an AI collaborator. It stands as a testament to the power of AI-assisted software engineering.*

A powerful, self-hosted web application that downloads videos, audio, photos, and stories from platforms like YouTube, TikTok, Instagram, and Snapchat. It features a responsive UI with dynamic platform-specific color themes, automatic watchlist background downloading, built-in media processing, a private file manager, and an optional **Telegram bot** (English + العربية).

## ✨ Features
*   **Universal Support:** Works with YouTube, TikTok, Instagram (Posts/Stories/Profiles), Snapchat, X/Twitter, and more.
*   **Smart Fallbacks:** Automatically detects media types (e.g., TikTok photo slideshows vs. standard videos).
*   **Snapchat Story Picker:** Download all stories of a user, or pick only the ones you want (for example #27 and #66) and choose whether to keep or delete the rest.
*   **Background Watchlist:** Set a timer to automatically download new posts from your favorite creators.
*   **Dynamic UI:** The interface seamlessly changes its accent colors to match the platform of the pasted link.
*   **Built-in Processing:** Crop, convert to MP3, create looping GIFs, generate WhatsApp-ready stickers, or apply color filters via `ffmpeg`.
*   **Private File Manager:** Browse, search, tag, preview, and play everything you downloaded, protected by a file password.
*   **Telegram Bot (optional):** Send a link to your bot, tap a button, and the Hub does the rest. See [Telegram Bot](#-telegram-bot-optional).

---

## 🛠️ Prerequisites

Before you begin, ensure your system has the following installed:

**For Windows:**
1. Download and install [Git for Windows](https://gitforwindows.org/).
2. Download and install [Docker Desktop](https://www.docker.com/products/docker-desktop/). Ensure it is running in the background.

**For Linux (Ubuntu/Debian):**
Open your terminal and install Git, Docker, and Docker Compose:
```bash
sudo apt update && sudo apt install git docker.io docker-compose -y
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
# (Log out and log back in to apply Docker permissions)
```

---

## 🚀 Installation & Setup (Recommended)

Since the Universal Media Hub is containerized, the installation process is identical for both Windows and Linux using Docker.

### Step 1: Clone the Repository
Open your terminal (or Command Prompt / PowerShell on Windows) and run:
```bash
git clone https://github.com/DevQasem/media-hub.git
cd media-hub
```

### Step 2: Create your settings file
Your passwords and tokens live in a private `.env` file that is **never uploaded to GitHub**. Copy the example and fill it in:
```bash
cp .env.example .env        # Windows PowerShell: copy .env.example .env
```
Open `.env` and set:
```env
BOT_TOKEN=            # from @BotFather (leave empty if you do not want the bot)
ALLOWED_IDS=          # your numeric Telegram ID from @userinfobot
HUB_TOKEN=            # optional: a long random string that locks the whole web page
HUB_FILES_PIN=        # password for the Files screen (use 8+ characters)
```
Write values without quotes and without spaces around `=`.

> ⚠️ Without `HUB_TOKEN` and `HUB_FILES_PIN`, anyone on your network can open your Hub and your files. The server prints a security warning at startup until you set them.

### Step 3: Start the Server
Build and run the container in detached mode using Docker Compose:
```bash
docker compose up -d --build
```
*(On older installs the command is `docker-compose`.)*

*Note: Docker will automatically pull a lightweight Python 3.11 image, install Gunicorn, `yt-dlp`, `gallery-dl`, `snapchat-dl`, the Telegram bot library, and configure `ffmpeg`.*

### Step 4: Access the Hub
Open your web browser and navigate to:
```text
http://localhost:5000
```
All downloaded media, caches, settings, and archive files are safely stored in the local `hub-data/` folder in your project directory. This folder is excluded from Git, so back it up yourself.

---

## 🤖 Telegram Bot (optional)

The bot runs inside the same container as the Hub, so no extra container is needed.

1. In Telegram, message **@BotFather**, send `/newbot`, and copy the token into `BOT_TOKEN`.
2. Message **@userinfobot** to get your numeric ID and put it in `ALLOWED_IDS` (comma-separate several people). Only these IDs can use the bot.
3. Run `docker compose up -d --build` and message your bot.

**What it can do**
*   📎 Send any link, then tap **Video**, **Audio**, or **Cover**.
*   👻 `/snap username` for Snapchat stories, with **All stories** or **🎯 Pick stories** (tap numbers or type `27 66 70-75`, with thumbnail previews).
*   ✨ Filters: black & white, sepia, vivid, warm, cool, vintage, cinematic (or tap 🎲 for a surprise).
*   ⚙️ `/settings`: language, type, quality, filter, whether to send the files to Telegram (ask, always, never), and sending as uncompressed files.
*   🌐 `/lang`: switch between English and العربية.
*   📤 When a download finishes, the bot asks if you also want the files sent to Telegram. Files always stay saved in the Hub.

> Telegram bots can upload files up to 50 MB. Bigger files stay in the Hub and the bot tells you.

---

## 🏠 Home Server / CasaOS

1. Clone the repo on your server and create the `.env` file as above.
2. Copy your existing `hub-data/` folder next to `docker-compose.yml` so your files and settings come along (its `downloads/` folder is your library).
3. Run `docker compose up -d --build`.

For CasaOS, build the image once with `docker build -t media-hub:latest .`, then use **+ → Install a customized app → Import** with a compose file that uses `image: media-hub:latest` and mounts `/DATA/AppData/media-hub/hub-data` to `/root/media-downloader`.

For access from outside your home, use a VPN such as Tailscale instead of opening port 5000 on your router.

---

## 💻 Manual Installation (Without Docker)

If you prefer to run the app natively on your host machine without Docker:

### Windows Native Setup
1. Install [Python 3.11+](https://www.python.org/downloads/) (Check "Add python.exe to PATH" during installation).
2. Install [FFmpeg for Windows](https://gyan.dev/ffmpeg/builds/) and add it to your System Environment PATH.
3. Open PowerShell as Administrator and install the required tools:
```powershell
pip install Flask gunicorn yt-dlp gallery-dl snapchat-dl python-telegram-bot httpx
```
4. Start the Flask application:
```powershell
python app.py
```

### Linux Native Setup
1. Install Python, pip, and FFmpeg:
```bash
sudo apt update
sudo apt install python3 python3-pip ffmpeg -y
```
2. Install the required Python packages (use `--break-system-packages` if required by newer pip versions, or set up a `venv`):
```bash
pip3 install Flask gunicorn yt-dlp gallery-dl snapchat-dl python-telegram-bot httpx
```
3. Start the Flask application:
```bash
python3 app.py
```

To use the Telegram bot without Docker, set `BOT_TOKEN`, `ALLOWED_IDS`, `HUB_URL=http://127.0.0.1:5000` and `HUB_FILES_PIN` as environment variables, then run `python3 bot.py` in a second terminal.

---

## 🔧 Maintenance & Updating

To keep your downloaders (like `yt-dlp` and `gallery-dl`) working as platforms change their code, use the built-in **Tools (Maintenance)** tab in the web UI. To update the Hub itself to the latest version from GitHub:
```bash
git pull
docker compose up -d --build
```

---

## 🔒 Security Notes
*   Never commit your `.env` file. It is already listed in `.gitignore`.
*   If a bot token is ever exposed, revoke it with `/revoke` in @BotFather and use the new one.
*   Set `HUB_TOKEN` and a strong `HUB_FILES_PIN`. You can add `HUB_STRICT=1` to make the server refuse to start while they are weak.
