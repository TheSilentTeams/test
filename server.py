# server.py
import os
import uvicorn
import threading
import time
import requests
from fastapi import FastAPI

app = FastAPI()
PORT = int(os.getenv("PORT", 10000))  # Render sets PORT automatically

@app.get("/")
async def root():
    return {"status": "ok", "message": "Bot alive"}

def ping_self():
    url = os.getenv("SELF_URL")  # e.g., https://your-render-url.onrender.com
    while True:
        if url:
            try:
                print(f"Pinging {url}")
                requests.get(url, timeout=5)
            except Exception as e:
                print(f"Ping failed: {e}")
        time.sleep(100)  # every 1.4 minutes

if __name__ == "__main__":
    threading.Thread(target=ping_self, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=PORT)
