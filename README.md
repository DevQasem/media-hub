# 🚀 Universal Media Hub

> 🤖 **100% AI-Generated Project**
> *This entire application—from the responsive frontend and the robust Flask backend to the Docker architecture and this very README—was built end-to-end by an AI collaborator. It stands as a testament to the power of AI-assisted software engineering.*

Universal Media Hub is a sleek, heavily optimized, and fully Dockerized web interface for downloading high-quality media across the web. It bridges the gap between powerful command-line scraping tools and a seamless, beautiful user experience.

## ✨ Features

* 🎬 **Omnivorous Downloader:** Natively integrates `yt-dlp`, `gallery-dl`, and `snapchat-dl` to conquer YouTube, TikTok, Instagram (Posts/Reels/Stories), Snapchat, X/Twitter, and hundreds of other platforms.

* 🎨 **Studio-Grade Processing:** Built-in `ffmpeg` processing lets you apply cinematic color grading, crop to vertical formats, strip audio, or convert clips into looping GIFs and WhatsApp-sized WebP stickers on the fly.

* 👁️ **Relentless Watchlists:** Add your favorite creators to a watchlist and let the server quietly fetch their new posts in the background.

* 📱 **Frictionless UI:** A gorgeous, app-like responsive design featuring dark/auto themes, drag-and-drop link support, clipboard auto-detection, and a mobile-optimized bottom tab bar.

* 🐳 **Containerized Perfection:** Fully Dockerized for zero-mess deployments. No Python version conflicts, no missing system packages, no headaches.

## 🛠️ The 60-Second Deploy (Fresh Ubuntu Server)

Starting with a brand-new Ubuntu VPS? You don't need to manually wrestle with Python or install `ffmpeg`. Docker handles the entire ecosystem for you.

Connect to your server via SSH and execute the following:

**1. Update the system and equip the basics**

```
sudo apt update && sudo apt upgrade -y
sudo apt install -y curl git

```

**2. Summon Docker & Docker Compose**

```
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

```

**3. Clone the Hub**

```
git clone https://github.com/DevQasem/media-hub.git
cd media-hub

```

**4. Ignite the Engine!**

```
sudo docker compose up -d --build

```

### 🎉 You're Live!

Once the build completes, open your web browser and navigate to:
`http://<YOUR_SERVER_IP>:5000`

## 🌍 Going Global (Zero-Trust Security)

Want to access your Media Hub from anywhere without exposing your server's true IP address or opening dangerous firewall ports? Deploy a **Cloudflare Tunnel**.

1. Create a free account at the [Cloudflare Zero Trust Dashboard](https://one.dash.cloudflare.com/).

2. Navigate to **Networks** > **Tunnels** and click **Create a tunnel**.

3. Name your tunnel and copy the provided installation command for Linux/Debian.

4. Paste that command directly into your Ubuntu server terminal.

5. In the Cloudflare dashboard, map your slick public domain (like `hub.yourdomain.com`) to your local app at `http://localhost:5000`.

Your app is now securely accessible worldwide via HTTPS!

## 🗂️ The Vault (Where your files live)

Thanks to Docker volumes, your downloaded videos, configurations, and application states are safely bridged directly to your host machine. Look inside the newly created `hub-data` folder right next to your code:

```
media-hub/
 ┗ 📂 hub-data/           
    ┣ 📂 youtube/         # Downloaded YouTube/TikTok videos
    ┣ 📂 instagram/       # Gallery-dl media captures
    ┣ 📂 snapchat/        # Snapchat story archives
    ┗ 📜 cookies.txt      # Your securely mapped Instagram login

```

## 📸 The Instagram Checkpoint

To extract Instagram posts, stories, or profiles, the hub needs a `cookies.txt` file from a throwaway Instagram account. You can easily upload this authentication file directly through the **Settings (⚙️)** menu in the Media Hub web UI.
