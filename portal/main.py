"""
Portal — веб-интерфейс управления клиентом.
Предоставляет: вход по логину/паролю, выбор n8n или сервиса, управление аккаунтами.
"""
import asyncio
import json
import os

try:
    import gspread
    from google.oauth2.service_account import Credentials as SACredentials
    _GSPREAD_OK = True
except ImportError:
    _GSPREAD_OK = False
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
PM2_USERBOT_NAME = os.getenv("PM2_USERBOT_NAME", "userbot")
N8N_URL = os.getenv("N8N_URL", "")
N8N_INTERNAL_URL = os.getenv("N8N_INTERNAL_URL", "http://n8n:5678")
GOOGLE_SA_JSON    = os.getenv("GOOGLE_SA_JSON", "")
GOOGLE_SHEET_ID   = os.getenv("GOOGLE_SHEET_ID", "")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Клиенты")
N8N_OWNER_EMAIL = os.getenv("N8N_OWNER_EMAIL", "")
N8N_OWNER_PASSWORD = os.getenv("N8N_OWNER_PASSWORD", "")
TOKEN_COOKIE = "portal_token"
N8N_COOKIE = "n8n-auth"

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


# ── n8n SSO ───────────────────────────────────────────────────────────────────

@app.get("/go/n8n")
async def go_n8n(request: Request, response: Response):
    """
    SSO-переход в n8n: проверяет сессию портала, логинится в n8n на сервере,
    передаёт n8n-auth cookie браузеру и редиректит на интерфейс n8n.
    """
    require_auth(request)

    if not N8N_OWNER_EMAIL or not N8N_OWNER_PASSWORD:
        # SSO не настроен — просто редирект, пользователь войдёт сам
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=N8N_URL or "/n8n/")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{N8N_INTERNAL_URL}/rest/login",
                json={"emailOrLdapLoginId": N8N_OWNER_EMAIL, "password": N8N_OWNER_PASSWORD},
                headers={"Content-Type": "application/json"},
            )
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail="n8n недоступен")

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="n8n: ошибка авторизации")

    # Находим n8n-auth cookie в ответе
    n8n_cookie_value = None
    for set_cookie in resp.headers.get_list("set-cookie"):
        if set_cookie.startswith("n8n-auth="):
            n8n_cookie_value = set_cookie.split("=", 1)[1].split(";")[0]
            break

    if not n8n_cookie_value:
        raise HTTPException(status_code=502, detail="n8n не вернул cookie")

    from fastapi.responses import RedirectResponse
    redirect = RedirectResponse(url=N8N_URL or "/n8n/", status_code=302)
    redirect.set_cookie(
        N8N_COOKIE,
        n8n_cookie_value,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
        max_age=604800,
    )
    return redirect


# ── Proxy → userbot service ───────────────────────────────────────────────────

async def _proxy(method: str, path: str, json_body=None, timeout=15.0):
    """Проксирует запрос к userbot-сервису."""
    url = f"{USERBOT_API_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, url, json=json_body)
        try:
            content = resp.json()
        except Exception:
            raise HTTPException(status_code=502, detail="Userbot вернул пустой ответ")
        return JSONResponse(content=content, status_code=resp.status_code)
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


@app.get("/api/accounts/{account_id}/dialogs")
async def proxy_dialogs(account_id: str, request: Request, limit: int = 30, sent_only: bool = False):
    require_auth(request)
    return await _proxy("GET", f"/accounts/{account_id}/dialogs?limit={limit}&sent_only={str(sent_only).lower()}", timeout=60.0)


@app.post("/api/accounts/{account_id}/dialogs/{chat_id}/read")
async def proxy_mark_read(account_id: str, chat_id: int, request: Request):
    require_auth(request)
    return await _proxy("POST", f"/accounts/{account_id}/dialogs/{chat_id}/read")


@app.get("/api/accounts/{account_id}/dialogs/{chat_id}/messages")
async def proxy_messages(account_id: str, chat_id: int, request: Request, limit: int = 50, offset_id: int = 0):
    require_auth(request)
    return await _proxy("GET", f"/accounts/{account_id}/dialogs/{chat_id}/messages?limit={limit}&offset_id={offset_id}", timeout=30.0)


@app.post("/api/accounts/{account_id}/dialogs/{chat_id}/send")
async def proxy_chat_send(account_id: str, chat_id: int, request: Request):
    require_auth(request)
    body = await request.json()
    return await _proxy("POST", "/send", json_body={
        "account_id": account_id,
        "chat_id": chat_id,
        "text": body.get("text", ""),
        "instant": True,
    }, timeout=30.0)


@app.delete("/api/accounts/{account_id}/dialogs/{chat_id}/messages/{message_id}")
async def proxy_delete_message(account_id: str, chat_id: int, message_id: int, request: Request):
    require_auth(request)
    return await _proxy("DELETE", f"/accounts/{account_id}/dialogs/{chat_id}/messages/{message_id}")


@app.patch("/api/accounts/{account_id}/dialogs/{chat_id}/messages/{message_id}")
async def proxy_edit_message(account_id: str, chat_id: int, message_id: int, request: Request):
    require_auth(request)
    body = await request.json()
    return await _proxy("PATCH", f"/accounts/{account_id}/dialogs/{chat_id}/messages/{message_id}", json_body=body)


@app.get("/api/accounts/{account_id}/resolve/{username}")
async def proxy_resolve_username(account_id: str, username: str, request: Request):
    require_auth(request)
    return await _proxy("GET", f"/accounts/{account_id}/resolve/{username}", timeout=15.0)


@app.patch("/api/accounts/{account_id}/typing")
async def update_typing(account_id: str, request: Request):
    require_auth(request)
    body = await request.json()
    return await _proxy("PATCH", f"/accounts/{account_id}/typing", json_body=body)


@app.patch("/api/accounts/{account_id}/link_preview")
async def update_link_preview(account_id: str, request: Request):
    require_auth(request)
    body = await request.json()
    return await _proxy("PATCH", f"/accounts/{account_id}/link_preview", json_body=body)


@app.patch("/api/accounts/{account_id}/proxy")
async def set_proxy(account_id: str, request: Request):
    require_auth(request)
    body = await request.json()
    return await _proxy("PATCH", f"/accounts/{account_id}/proxy", json_body=body)


# ── Proxy pool ────────────────────────────────────────────────────────────────

@app.get("/api/proxies")
async def get_proxies(request: Request):
    require_auth(request)
    return await _proxy("GET", "/proxies")

@app.post("/api/proxies/assign")
async def assign_proxies(request: Request):
    require_auth(request)
    return await _proxy("POST", "/proxies/assign", timeout=30.0)

@app.post("/api/proxies/import")
async def import_proxies(request: Request):
    require_auth(request)
    return await _proxy("POST", "/proxies/import", timeout=60.0)

@app.get("/api/proxies/settings")
async def get_proxy_settings(request: Request):
    require_auth(request)
    return await _proxy("GET", "/proxies/settings")

@app.patch("/api/proxies/settings")
async def update_proxy_settings(request: Request):
    require_auth(request)
    body = await request.json()
    return await _proxy("PATCH", "/proxies/settings", json_body=body)

@app.post("/api/proxies/check-all")
async def check_all_proxies(request: Request):
    require_auth(request)
    return await _proxy("POST", "/proxies/check-all", timeout=120.0)

@app.post("/api/proxies/{proxy_id}/check")
async def check_proxy(proxy_id: str, request: Request):
    require_auth(request)
    return await _proxy("POST", f"/proxies/{proxy_id}/check", timeout=15.0)

@app.delete("/api/proxies/{proxy_id}")
async def delete_proxy(proxy_id: str, request: Request):
    require_auth(request)
    return await _proxy("DELETE", f"/proxies/{proxy_id}")

@app.post("/api/proxies")
async def add_proxy(request: Request):
    require_auth(request)
    body = await request.json()
    return await _proxy("POST", "/proxies", json_body=body)


@app.patch("/api/accounts/{account_id}/warmup_initiator")
async def update_warmup_initiator(account_id: str, request: Request):
    require_auth(request)
    body = await request.json()
    return await _proxy("PATCH", f"/accounts/{account_id}/warmup_initiator", json_body=body)


@app.patch("/api/accounts/{account_id}/warmup_receiver")
async def update_warmup_receiver(account_id: str, request: Request):
    require_auth(request)
    body = await request.json()
    return await _proxy("PATCH", f"/accounts/{account_id}/warmup_receiver", json_body=body)


@app.patch("/api/accounts/{account_id}/spam_ban_auto")
async def update_spam_ban_auto(account_id: str, request: Request):
    require_auth(request)
    body = await request.json()
    return await _proxy("PATCH", f"/accounts/{account_id}/spam_ban_auto", json_body=body)


@app.delete("/api/accounts/{account_id}")
async def delete_account(account_id: str, request: Request):
    require_auth(request)
    return await _proxy("DELETE", f"/accounts/{account_id}")


# ── Google Sheets contacts ────────────────────────────────────────────────────

def _read_sheet_contacts() -> list[dict]:
    if not _GSPREAD_OK or not GOOGLE_SA_JSON or not GOOGLE_SHEET_ID:
        return []
    creds = SACredentials.from_service_account_file(
        GOOGLE_SA_JSON,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    gc = gspread.authorize(creds)
    ws = gc.open_by_key(GOOGLE_SHEET_ID).worksheet(GOOGLE_SHEET_NAME)
    records = ws.get_all_records()
    result = []
    for r in records:
        if r.get("Статус"):
            try:
                user_id = int(r["User_id"])
            except (ValueError, KeyError):
                continue
            result.append({
                "user_id":  user_id,
                "username": r.get("Username", ""),
                "name":     r.get("Имя", ""),
                "account":  r.get("Аккаунт", ""),
                "status":   r.get("Статус", ""),
            })
    return result


@app.get("/api/contacts")
async def get_contacts(request: Request):
    require_auth(request)
    try:
        loop = asyncio.get_event_loop()
        contacts = await loop.run_in_executor(None, _read_sheet_contacts)
        return contacts
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка чтения таблицы: {e}")


# ── Resend message to n8n ─────────────────────────────────────────────────────

@app.post("/api/resend-to-n8n")
async def resend_to_n8n(request: Request):
    require_auth(request)
    body = await request.json()
    account_id = body["account_id"]
    chat_id = body["chat_id"]
    payload = {
        "account_id": account_id,
        "chat_id": chat_id,
        "message_id": body["message_id"],
        "from_user_id": body.get("from_user_id"),
        "from_username": body.get("from_username"),
        "from_name": body.get("from_name", ""),
        "message_text": body["message_text"],
        "timestamp": body["timestamp"],
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{N8N_INTERNAL_URL}/webhook/telegram-incoming", json=payload)
            resp.raise_for_status()
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail="n8n недоступен")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"n8n вернул {e.response.status_code}")

    # Помечаем чат как прочитанный
    await _proxy("POST", f"/accounts/{account_id}/dialogs/{chat_id}/read")

    return {"ok": True}


# ── PM2 control (userbot start/stop) ──────────────────────────────────────────

async def _pm2_status(name: str) -> dict:
    proc = await asyncio.create_subprocess_exec(
        "pm2", "jlist",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return {"running": False, "status": "error"}
    processes = json.loads(stdout.decode())
    p = next((x for x in processes if x["name"] == name), None)
    if not p:
        return {"running": False, "status": "not_found"}
    status = p["pm2_env"]["status"]
    return {"running": status == "online", "status": status}


async def _pm2_action(action: str, name: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "pm2", action, name,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    await proc.communicate()
    return proc.returncode == 0


@app.get("/api/service/status")
async def service_status(request: Request):
    require_auth(request)
    return await _pm2_status(PM2_USERBOT_NAME)


@app.post("/api/service/start")
async def service_start(request: Request):
    require_auth(request)
    ok = await _pm2_action("restart", PM2_USERBOT_NAME)
    if ok:
        return {"ok": True}
    raise HTTPException(status_code=500, detail="Не удалось запустить сервис")


@app.post("/api/service/stop")
async def service_stop(request: Request):
    require_auth(request)
    ok = await _pm2_action("stop", PM2_USERBOT_NAME)
    if ok:
        return {"ok": True}
    raise HTTPException(status_code=500, detail="Не удалось остановить сервис")


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


@app.get("/chat", response_class=HTMLResponse)
async def chat_page():
    return FileResponse(STATIC_DIR / "chat.html")
