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
from app.core.config import settings
from app.core.crypto import decrypt, encrypt, hash_password, verify_password
from app.db import users
from app.db.models import User
from app.dependencies import get_session
from app.weather import geocode

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


@router.get("/info", response_class=HTMLResponse)
async def info_page(request: Request, user: User = Depends(current_user)):
    return templates.TemplateResponse(
        request, "info.html",
        {"user": user, "bot_username": settings.TELEGRAM_BOT_USERNAME},
    )


@router.get("/settings", response_class=HTMLResponse)
async def settings_form(request: Request, user: User = Depends(current_user)):
    return templates.TemplateResponse(
        request, "settings.html",
        {
            "user": user,
            "garmin_email": _safe_decrypt(user.garmin_email_enc),
            "has_garmin_password": bool(user.garmin_password_enc),
            "has_anthropic": bool(user.anthropic_key_enc),
            "has_garth_token": bool(user.garth_token_enc),
            "telegram_chat_id": user.telegram_chat_id or "",
            "weather_location": user.weather_location or "",
            "saved": request.query_params.get("saved") == "1",
            "geo": request.query_params.get("geo"),
            "pw": request.query_params.get("pw"),
            "bot_username": settings.TELEGRAM_BOT_USERNAME,
        },
    )


@router.post("/settings")
async def settings_save(
    request: Request,
    garmin_email: str = Form(""),
    garmin_password: str = Form(""),
    anthropic_key: str = Form(""),
    telegram_chat_id: str = Form(""),
    weather_location: str = Form(""),
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

    # Weather location: geocode once on change so the morning job has lat/lon directly.
    geo_failed = False
    loc = weather_location.strip()
    if not loc:
        user.weather_location = user.latitude = user.longitude = None
    elif loc != (user.weather_location or ""):
        hit = geocode(loc)
        if hit:
            user.latitude, user.longitude, user.weather_location = hit
        else:
            geo_failed = True  # keep the previous location, tell the user

    await session.commit()
    if geo_failed:
        return RedirectResponse("/settings?saved=1&geo=fail", status_code=303)
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/password")
async def change_password(
    current_password: str = Form(...),
    new_password: str = Form(...),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    if not verify_password(current_password, user.password_hash):
        return RedirectResponse("/settings?pw=wrong", status_code=303)
    if len(new_password) < 6:
        return RedirectResponse("/settings?pw=short", status_code=303)
    user.password_hash = hash_password(new_password)
    await session.commit()
    return RedirectResponse("/settings?pw=ok", status_code=303)


@router.get("/admin/users", response_class=HTMLResponse)
async def users_list(
    request: Request,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    rows = (await session.execute(select(User).order_by(User.id))).scalars().all()
    return templates.TemplateResponse(
        request, "users.html",
        {"users": rows, "error": None, "current_user_id": admin.id, "user": admin},
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
            request, "users.html",
            {"users": rows, "current_user_id": admin.id, "user": admin,
             "error": f"Користувач {email} вже існує."},
            status_code=409,
        )
    await users.create_user(
        session, email=email, password_hash=hash_password(password),
        is_admin=bool(is_admin), is_approved=True,  # admin-created → active immediately
    )
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/approve")
async def users_approve(
    user_id: int,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    u = await session.get(User, user_id)
    if u is not None:
        u.is_approved = True
        await session.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/active")
async def users_set_active(
    user_id: int,
    active: str = Form(...),  # "1" to activate, "0" to deactivate
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    u = await session.get(User, user_id)
    if u is not None and u.id != admin.id:  # never deactivate yourself
        u.is_active = active == "1"
        await session.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/delete")
async def users_delete(
    user_id: int,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    u = await session.get(User, user_id)
    if u is not None and u.id != admin.id:  # never delete yourself
        await session.delete(u)
        await session.commit()
    return RedirectResponse("/admin/users", status_code=303)
