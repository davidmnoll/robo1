import os
from fastapi import FastAPI, Header, HTTPException


def allowed_keys() -> set[str]:
    """Comma-separated list of allowed API keys injected via env."""
    raw = os.getenv("ROS_PROXY_KEYS", "")
    candidates = [value.strip() for value in raw.split(",")]
    return {value for value in candidates if value}


app = FastAPI(title="ROS Key Proxy", version="0.1.0")


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/authorize")
def authorize(x_api_key: str = Header(default="")) -> dict[str, bool]:
    keys = allowed_keys()
    if keys and x_api_key not in keys:
        raise HTTPException(status_code=403, detail="invalid key")
    return {"authorized": True}
