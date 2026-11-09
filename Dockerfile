# Use a lightweight Python environment
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# Install system dependencies (ffmpeg is required for media conversions)
RUN apt-get update && apt-get install -y ffmpeg curl && rm -rf /var/lib/apt/lists/*

# Install the yt-dlp command-line executable from the official source
RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp \
    && chmod a+rx /usr/local/bin/yt-dlp

# Install Python packages (yt-dlp added here to fix the ModuleNotFoundError!)
RUN pip install --no-cache-dir flask gunicorn snapchat-dl gallery-dl yt-dlp "python-telegram-bot>=21,<23" httpx curl_cffi

# Copy your project files into the container
COPY . .

# Expose the port Gunicorn uses
EXPOSE 5000

# The command to start your production server
CMD ["sh", "-c", "(while true; do python bot.py; sleep 5; done) & exec gunicorn -w 1 --threads 8 -b 0.0.0.0:5000 app:app"]