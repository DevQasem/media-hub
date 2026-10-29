FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y ffmpeg curl && rm -rf /var/lib/apt/lists/*

# Install yt-dlp from official release
RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp \
    && chmod a+rx /usr/local/bin/yt-dlp

# Install Python packages (including gunicorn for production)
RUN pip install --no-cache-dir flask gunicorn snapchat-dl gallery-dl

# Copy your code into the container
COPY . .

EXPOSE 5000

# Run in production mode using Gunicorn with 1 worker and 8 threads
CMD ["gunicorn", "-w", "1", "--threads", "8", "-b", "0.0.0.0:5000", "app:app"]