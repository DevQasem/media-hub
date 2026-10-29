# 🚀 Universal Media Hub

![Media Hub UI](https://img.shields.io/badge/UI-Modern%20%26%20Responsive-8b5cf6?style=flat-square)
![Docker](https://img.shields.io/badge/Docker-Supported-2496ED?logo=docker&style=flat-square)
![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&style=flat-square)
![AI Built](https://img.shields.io/badge/100%25-AI_Generated-FF4154?style=flat-square)

> 🤖 **100% AI-Generated Project** 
> *This entire application—from the responsive frontend and the robust Flask backend to the Docker architecture and this very README—was built end-to-end by an AI collaborator. It stands as a testament to the power of AI-assisted software engineering.*

Universal Media Hub is a sleek, heavily optimized, and fully Dockerized web interface for downloading high-quality media across the web. It bridges the gap between powerful command-line scraping tools and a seamless, beautiful user experience.

---

## ✨ Features

* 🎬 **Omnivorous Downloader:** Natively integrates `yt-dlp`, `gallery-dl`, and `snapchat-dl` to conquer YouTube, TikTok, Instagram (Posts/Reels/Stories), Snapchat, X/Twitter, and hundreds of other platforms.
* 🎨 **Studio-Grade Processing:** Built-in `ffmpeg` processing lets you apply cinematic color grading, crop to vertical formats, strip audio, or convert clips into looping GIFs and WhatsApp-sized WebP stickers on the fly.
* 👁️ **Relentless Watchlists:** Add your favorite creators to a watchlist and let the server quietly fetch their new posts in the background. 
* 📱 **Frictionless UI:** A gorgeous, app-like responsive design featuring dark/auto themes, drag-and-drop link support, clipboard auto-detection, and a mobile-optimized bottom tab bar.
* 🐳 **Containerized Perfection:** Fully Dockerized for zero-mess deployments. No Python version conflicts, no missing system packages, no headaches.

---

## 🛠️ The 60-Second Deploy (Fresh Ubuntu Server)

Starting with a brand-new Ubuntu VPS? You don't need to manually wrestle with Python or install `ffmpeg`. Docker handles the entire ecosystem for you.

Connect to your server via SSH and execute the following:

**1. Update the system and equip the basics**
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y curl git
