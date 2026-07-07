"""Login / logout / self-registration routes for the web UI."""
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import login_session, logout_session
from app.core.crypto import hash_password_async, verify_password_async
from app.db import users
from app.dependencies import get_session

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["auth"])


def _login_page(request: Request, *, error=None, info=None, status_code=200):
    return templates.TemplateResponse(
        request, "login.html",
        {"error": error, "info": info},
        status_code=status_code,
    )


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return _login_page(request)


@router.post("/login")
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    user = await users.get_by_email(session, email)
    if user is None or not await verify_password_async(password, user.password_hash):
        return _login_page(request, error="Невірний email або пароль.", status_code=401)
    if not user.is_approved:
        return _login_page(
            request,
            error="Акаунт ще не підтверджено адміністратором.",
            status_code=403,
        )
    if not user.is_active:
        return _login_page(request, error="Акаунт деактивовано.", status_code=403)
    login_session(request, user)
    return RedirectResponse("/ui" if user.is_admin else "/settings", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    logout_session(request)
    return RedirectResponse("/login", status_code=303)


@router.get("/register", response_class=HTMLResponse)
async def register_form(request: Request):
    return templates.TemplateResponse(
        request, "register.html", {"error": None}
    )


@router.post("/register")
async def register_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    email = email.strip().lower()
    if len(password) < 6:
        return templates.TemplateResponse(
            request, "register.html",
            {"error": "Пароль має бути щонайменше 6 символів."},
            status_code=400,
        )
    if await users.get_by_email(session, email):
        return templates.TemplateResponse(
            request, "register.html",
            {"error": "Цей email вже зареєстровано."},
            status_code=409,
        )
    await users.create_user(
        session, email=email, password_hash=await hash_password_async(password),
        is_admin=False, is_approved=False,
    )
    return _login_page(
        request,
        info="Реєстрацію надіслано. Увійти можна буде після підтвердження адміністратором.",
    )
