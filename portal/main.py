"""
Portal — веб-интерфейс управления клиентом.
Предоставляет: вход по логину/паролю, выбор n8n или сервиса, управление аккаунтами.
"""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
import jwt
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Portal", docs_url=None, redoc_url=None)

# ── Конфигурация ──────────────────────────────────────────────────────────────
PORTAL_USERNAME = os.getenv("PORTAL_USERNAME", "admin")
PORTAL_PASSWORD = os.getenv("PORTAL_PASSWORD", "")
JWT_SECRET = os.getenv("JWT_SECRET", "change-me-please")
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "24"))
USERBOT_API_URL = os.getenv("USERBOT_API_URL", "http://userbot:8000")
N8N_URL = os.getenv("N8N_URL", "")
TOKEN_COOKIE = "portal_token"

STATIC_DIR = Path(__file__).parent / "static"

# ── JWT helpers ───────────────────────────────────────────────────────────────

def create_token(username: str) -> str:
    payload = {
        "sub": username,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def verify_token(token: str) -> Optional[str]:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return payload.get("sub")
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


def get_user(request: Request) -> Optional[str]:
    token = request.cookies.get(TOKEN_COOKIE)
    if not token:
        return None
    return verify_token(token)


def require_auth(request: Request) -> str:
    user = get_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return user


# ── Auth endpoints ────────────────────────────────────────────────────────────

@app.post("/api/login")
async def login(request: Request, response: Response):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    username = body.get("username", "").strip()
    password = body.get("password", "")

    if not PORTAL_PASSWORD:
        raise HTTPException(status_code=503, detail="PORTAL_PASSWORD не задан на сервере")

    if username == PORTAL_USERNAME and password == PORTAL_PASSWORD:
        token = create_token(username)
        response.set_cookie(
            TOKEN_COOKIE,
            token,
            httponly=True,
            secure=False,  # set True when running behind HTTPS
            samesite="lax",
            max_age=JWT_EXPIRE_HOURS * 3600,
            path="/",
        )
        return {"status": "ok", "username": username}

    raise HTTPException(status_code=401, detail="Неверный логин или пароль")


@app.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie(TOKEN_COOKIE, path="/")
    return {"status": "ok"}


@app.get("/api/me")
async def me(request: Request):
    user = get_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return {"username": user, "n8n_url": N8N_URL}


# ── Proxy → userbot service ───────────────────────────────────────────────────

async def _proxy(method: str, path: str, json_body=None, timeout=15.0):
    """Проксирует запрос к userbot-сервису."""
    url = f"{USERBOT_API_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, url, json=json_body)
        return JSONResponse(content=resp.json(), status_code=resp.status_code)
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail="Userbot-сервис недоступен")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Userbot-сервис не ответил вовремя")


@app.get("/api/accounts")
async def get_accounts(request: Request):
    require_auth(request)
    return await _proxy("GET", "/accounts")


@app.post("/api/accounts/{account_id}/auth/start")
async def auth_start(account_id: str, request: Request):
    require_auth(request)
    body = await request.json()
    return await _proxy("POST", f"/accounts/{account_id}/auth/start", json_body=body)


@app.post("/api/accounts/{account_id}/auth/resend")
async def auth_resend(account_id: str, request: Request):
    require_auth(request)
    return await _proxy("POST", f"/accounts/{account_id}/auth/resend")


@app.post("/api/accounts/{account_id}/auth/code")
async def auth_code(account_id: str, request: Request):
    require_auth(request)
    body = await request.json()
    return await _proxy("POST", f"/accounts/{account_id}/auth/code", json_body=body)


@app.post("/api/accounts/{account_id}/auth/qr")
async def auth_qr_start(account_id: str, request: Request):
    require_auth(request)
    return await _proxy("POST", f"/accounts/{account_id}/auth/qr")


@app.post("/api/accounts/{account_id}/auth/qr/refresh")
async def auth_qr_refresh(account_id: str, request: Request):
    require_auth(request)
    return await _proxy("POST", f"/accounts/{account_id}/auth/qr/refresh")


@app.post("/api/accounts/{account_id}/auth/qr/wait")
async def auth_qr_wait(account_id: str, request: Request):
    require_auth(request)
    body = await request.json()
    password = body.get("password")
    path = f"/accounts/{account_id}/auth/qr/wait"
    if password:
        path += f"?password={password}"
    return await _proxy("POST", path, timeout=35.0)


@app.delete("/api/accounts/{account_id}")
async def delete_account(account_id: str, request: Request):
    require_auth(request)
    return await _proxy("DELETE", f"/accounts/{account_id}")


# ── Static & page routes ──────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def root():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/service", response_class=HTMLResponse)
async def service():
    return FileResponse(STATIC_DIR / "service.html")
