"""Self-service credential settings + admin user management.

``/settings`` lets a logged-in user store their own Garmin login, Anthropic key and
Telegram chat id (secrets are Fernet-encrypted via ``app.core.crypto`` on write and
never rendered back). ``/admin/users`` lets an admin create further accounts.
"""
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user, require_admin
from app.core.crypto import decrypt, encrypt, hash_password
from app.db import users
from app.db.models import User
from app.dependencies import get_session

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["settings"])


def _safe_decrypt(token):
    if not token:
        return ""
    try:
        return decrypt(token)
    except Exception:
        return ""  # wrong/missing key — don't blow up the page


@router.get("/settings", response_class=HTMLResponse)
async def settings_form(request: Request, user: User = Depends(current_user)):
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "user": user,
            "garmin_email": _safe_decrypt(user.garmin_email_enc),
            "has_garmin_password": bool(user.garmin_password_enc),
            "has_anthropic": bool(user.anthropic_key_enc),
            "has_garth_token": bool(user.garth_token_enc),
            "telegram_chat_id": user.telegram_chat_id or "",
            "saved": request.query_params.get("saved") == "1",
        },
    )


@router.post("/settings")
async def settings_save(
    request: Request,
    garmin_email: str = Form(""),
    garmin_password: str = Form(""),
    anthropic_key: str = Form(""),
    telegram_chat_id: str = Form(""),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    # current_user and this route share the request session, so `user` is editable here.
    garmin_email = garmin_email.strip()
    if garmin_email:
        if garmin_email != _safe_decrypt(user.garmin_email_enc):
            user.garth_token_enc = None  # account changed → drop the saved session
        user.garmin_email_enc = encrypt(garmin_email)
    if garmin_password.strip():
        user.garmin_password_enc = encrypt(garmin_password.strip())
        user.garth_token_enc = None  # new password → re-login next time
    if anthropic_key.strip():
        user.anthropic_key_enc = encrypt(anthropic_key.strip())

    tci = telegram_chat_id.strip()
    user.telegram_chat_id = int(tci) if tci.lstrip("-").isdigit() else None

    await session.commit()
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.get("/admin/users", response_class=HTMLResponse)
async def users_list(
    request: Request,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    rows = (await session.execute(select(User).order_by(User.id))).scalars().all()
    return templates.TemplateResponse(
        "users.html",
        {"request": request, "users": rows, "error": None},
    )


@router.post("/admin/users")
async def users_create(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    is_admin: str = Form(""),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    email = email.strip().lower()
    if await users.get_by_email(session, email):
        rows = (await session.execute(select(User).order_by(User.id))).scalars().all()
        return templates.TemplateResponse(
            "users.html",
            {"request": request, "users": rows, "error": f"Користувач {email} вже існує."},
            status_code=409,
        )
    await users.create_user(
        session, email=email, password_hash=hash_password(password), is_admin=bool(is_admin)
    )
    return RedirectResponse("/admin/users", status_code=303)
