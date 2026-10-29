# 🚀 Universal Media Hub

A sleek, heavily optimized, and fully Dockerized web interface for downloading high-quality media across the web. Built with Python (Flask) and a beautiful modern frontend, it bridges the gap between powerful command-line scraping tools and a seamless user experience.

![Media Hub UI](https://img.shields.io/badge/UI-Modern%20%26%20Responsive-8b5cf6?style=flat-square)
![Docker](https://img.shields.io/badge/Docker-Supported-2496ED?logo=docker&style=flat-square)
![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&style=flat-square)

## ✨ Features

* 🎬 **Multi-Platform Power:** Natively integrates `yt-dlp`, `gallery-dl`, and `snapchat-dl` to support YouTube, TikTok, Instagram (Posts/Reels/Stories), Snapchat, X/Twitter, and hundreds of other sites.
* 🎨 **Live Video Effects:** Built-in `ffmpeg` processing lets you apply cinematic color grading, crop to vertical formats, remove audio, or convert clips into looping GIFs and WhatsApp-sized WebP stickers on the fly.
* 👁️ **Automated Watchlists:** Add creators to your watchlist and let the server automatically fetch their new posts in the background. 
* 📱 **App-Like Frontend:** Responsive design featuring dark/auto themes, drag-and-drop links, clipboard auto-detection, and a mobile-friendly bottom tab bar.
* 🐳 **Zero-Mess Setup:** Fully Dockerized. No Python version conflicts, no missing system packages. 

---

## 🛠️ One-Command Installation (Fresh Ubuntu Server)

If you are starting with a brand-new Ubuntu VPS, you don't need to manually configure Python or install `ffmpeg`. Docker handles the entire environment for you.

Connect to your server via SSH and run these steps:

**1. Update the system and install basic tools**
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y curl git
