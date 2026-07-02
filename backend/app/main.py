from datetime import datetime, timezone

from fastapi import FastAPI

app = FastAPI(title="ember", version="0.1.0")


@app.get("/health")
def health():
    return {
        "name": "ember",
        "status": "ok",
        "time": datetime.now(timezone.utc).isoformat(),
    }
