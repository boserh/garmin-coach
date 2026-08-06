"""Login / logout / self-registration routes for the web UI."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import login_session, logout_session
from app.core.config import settings
from app.core.crypto import hash_password_async, verify_password_async
from app.core.ratelimit import RateLimiter
from app.db import users
from app.demo import ensure_demo_user
from app.dependencies import get_session
from app.templating import create_templates

templates = create_templates()

router = APIRouter(tags=["auth"])

# In-memory brute-force / signup-spam guards (SEC-01). Per-process by design — see
# app.core.ratelimit. Login is keyed per-IP AND per-email; register per-IP.
_login_limiter = RateLimiter(settings.LOGIN_RATE_LIMIT, settings.LOGIN_RATE_WINDOW_S)
_register_limiter = RateLimiter(settings.LOGIN_RATE_LIMIT, settings.LOGIN_RATE_WINDOW_S)

_RATE_LIMIT_MSG = "Забагато спроб. Зачекай кілька хвилин і спробуй знову."


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _login_page(request: Request, *, error=None, info=None, status_code=200):
    return templates.TemplateResponse(
        request, "login.html",
        # A missing APP_SECRET_KEY means sessions are signed with an ephemeral
        # per-process key (see app.main) — warn the operator right on the page.
        {"error": error, "info": info, "insecure_secret": not settings.APP_SECRET_KEY},
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
    email_key = email.strip().lower()
    if not _login_limiter.allow(f"ip:{_client_ip(request)}") or not _login_limiter.allow(
        f"email:{email_key}"
    ):
        return _login_page(request, error=_RATE_LIMIT_MSG, status_code=429)
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
    if user.is_admin:
        return RedirectResponse("/ui", status_code=303)
    # EP-04: a non-admin lands on the dashboard (readiness/trends/plan/cost at a
    # glance) instead of the raw settings form — unless there's no Garmin data to
    # show yet, in which case /settings (enter creds) is the more useful landing.
    if not user.has_garmin_setup:
        return RedirectResponse("/settings", status_code=303)
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/logout")
async def logout(request: Request):
    logout_session(request)
    return RedirectResponse("/login", status_code=303)


@router.get("/logout")
async def logout_get():
    # Logout must be a POST (a state change) so a cross-site `<img src=/logout>`
    # can't silently sign the user out. A stray GET just lands on /settings (which
    # bounces to /login if the session is already gone) — it never clears state.
    return RedirectResponse("/settings", status_code=303)


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
    if not _register_limiter.allow(f"ip:{_client_ip(request)}"):
        return templates.TemplateResponse(
            request, "register.html",
            {"error": _RATE_LIMIT_MSG},
            status_code=429,
        )
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


@router.post("/demo-login")
async def demo_login(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """One-click walkthrough with seeded fake data — no registration, no real Garmin/
    Claude call ever (see app.core.demo + app.demo). Reuses the login rate limiter
    (per-IP) so it can't be hammered into repeated DB writes."""
    if not _login_limiter.allow(f"ip:{_client_ip(request)}"):
        return _login_page(request, error=_RATE_LIMIT_MSG, status_code=429)
    user = await ensure_demo_user(session)
    login_session(request, user)
    return RedirectResponse("/dashboard", status_code=303)
