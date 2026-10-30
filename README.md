# 🚀 Universal Media Hub

> 🤖 **100% AI-Generated Project**
> *This entire application—from the responsive frontend and the robust Flask backend to the Docker architecture and this very README—was built end-to-end by an AI collaborator. It stands as a testament to the power of AI-assisted software engineering.*

A powerful, self-hosted web application that downloads videos, audio, photos, and stories from platforms like YouTube, TikTok, Instagram, and Snapchat. It features a responsive UI with dynamic platform-specific color themes, automatic watchlist background downloading, and built-in media processing.

## ✨ Features
*   **Universal Support:** Works with YouTube, TikTok, Instagram (Posts/Stories/Profiles), Snapchat, X/Twitter, and more.
*   **Smart Fallbacks:** Automatically detects media types (e.g., TikTok photo slideshows vs. standard videos).
*   **Background Watchlist:** Set a timer to automatically download new posts from your favorite creators.
*   **Dynamic UI:** The interface seamlessly changes its accent colors to match the platform of the pasted link.
*   **Built-in Processing:** Crop, convert to MP3, create looping GIFs, or generate WhatsApp-ready stickers via `ffmpeg`.

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
git clone https://github.com/YOUR_USERNAME/universal-media-hub.git
cd universal-media-hub
```

### Step 2: Start the Server
Build and run the container in detached mode using Docker Compose:
```bash
docker-compose up -d --build
```
*Note: Docker will automatically pull a lightweight Python 3.11 image, install Gunicorn, `yt-dlp`, `gallery-dl`, `snapchat-dl`, and configure `ffmpeg`.*

### Step 3: Access the Hub
Open your web browser and navigate to:
```text
http://localhost:5000
```
All downloaded media, caches, and archive files are safely stored in the local `hub-data/` folder in your project directory.

---

## 💻 Manual Installation (Without Docker)

If you prefer to run the app natively on your host machine without Docker:

### Windows Native Setup
1. Install [Python 3.11+](https://www.python.org/downloads/) (Check "Add python.exe to PATH" during installation).
2. Install [FFmpeg for Windows](https://gyan.dev/ffmpeg/builds/) and add it to your System Environment PATH.
3. Open PowerShell as Administrator and install the required tools:
```powershell
pip install Flask gunicorn yt-dlp gallery-dl snapchat-dl
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
pip3 install Flask gunicorn yt-dlp gallery-dl snapchat-dl
```
3. Start the Flask application:
```bash
python3 app.py
```

---

## 🔧 Maintenance & Updating

To keep your downloaders (like `yt-dlp` and `gallery-dl`) working as platforms change their code, use the built-in **Tools (Maintenance)** tab in the web UI. Alternatively, to update the Docker container:
```bash
docker-compose pull
docker-compose up -d --build
```

---
