# Base image
FROM python:3.11-slim

# Set environment vars
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set working directory
WORKDIR /app

# Install OS dependencies
RUN apt-get update && apt-get install -y build-essential libffi-dev curl

# Copy requirements first for caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot and server code
COPY . .

# Expose port for FastAPI (used by Render)
EXPOSE 10000

# Run bot and server in parallel
CMD ["sh", "-c", "python3 bot.py & python3 server.py"]
