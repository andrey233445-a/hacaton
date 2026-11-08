from __future__ import annotations

import logging
import secrets
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.datastructures import FormData

from app.config import ADMIN_PASSWORD, ADMIN_USERNAME
from app.db.crud import (
    change_account_role,
    delete_account,
    list_accounts,
)
from app.db.models import RequestStatus, UserRole
from app.mini_web.api import router as mini_router

logger = logging.getLogger(__name__)
security = HTTPBasic()
templates = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(("html", "xml")),
)
app = FastAPI(title="Школьный штаб — панель управления")
# Мини веб-приложение для учеников/учителей (/mini и /mini/api/*)
app.include_router(mini_router)
ACCOUNT_PAGE_SIZE = 50

ROLE_LABELS = {
    UserRole.STUDENT.value: "Ученик",
    UserRole.TEACHER.value: "Учитель",
    UserRole.PARENT.value: "Родитель",
    UserRole.PSYCHOLOGIST.value: "Психолог",
    UserRole.UNSELECTED.value: "Не выбрана",
}


def _require_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    host = request.headers.get("host", "").lower()
    source = origin or referer
    if source is None or urlsplit(source).netloc.lower() != host:
        raise HTTPException(status_code=403, detail="Запрос отклонен: неверный источник.")


def _accounts_redirect(request: Request, form: FormData) -> RedirectResponse:
    query = urlencode(
        {
            key: str(form.get(key, request.query_params.get(key, "")))
            for key in ("search", "role_filter", "status_filter", "page")
            if form.get(key, request.query_params.get(key))
        }
    )
    url = "/admin/accounts"
    if query:
        url = f"{url}?{query}"
    return RedirectResponse(url, status_code=303)


def require_admin(
    credentials: HTTPBasicCredentials = Depends(security),
) -> str:
    if not ADMIN_USERNAME or not ADMIN_PASSWORD:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Доступ к панели не настроен: задайте ADMIN_USERNAME и ADMIN_PASSWORD.",
        )
    valid = secrets.compare_digest(
        credentials.username.encode(), ADMIN_USERNAME.encode()
    ) and secrets.compare_digest(
        credentials.password.encode(), ADMIN_PASSWORD.encode()
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверные учетные данные.",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


@app.get("/admin/accounts", response_class=HTMLResponse)
async def accounts_page(
    search: str = Query(default="", max_length=100),
    role_filter: str = "",
    status_filter: str = "",
    page: int = Query(default=1, ge=1),
    _: str = Depends(require_admin),
) -> HTMLResponse:
    valid_roles = {role.value for role in UserRole}
    valid_statuses = {item.value for item in RequestStatus}
    if role_filter and role_filter not in valid_roles:
        raise HTTPException(status_code=400, detail="Неизвестная роль.")
    if status_filter and status_filter not in valid_statuses:
        raise HTTPException(status_code=400, detail="Неизвестный статус.")

    offset = (page - 1) * ACCOUNT_PAGE_SIZE
    users, total = await list_accounts(
        search=search.strip(),
        role_filter=role_filter,
        status_filter=status_filter,
        limit=ACCOUNT_PAGE_SIZE,
        offset=offset,
    )
    total_pages = max(1, (total + ACCOUNT_PAGE_SIZE - 1) // ACCOUNT_PAGE_SIZE)
    template = templates.get_template("accounts.html")
    return HTMLResponse(
        template.render(
            accounts=users,
            search=search,
            role_filter=role_filter,
            status_filter=status_filter,
            roles=ROLE_LABELS,
            page=page,
            total=total,
            total_pages=total_pages,
        ),
        headers={"Cache-Control": "no-store"},
    )


@app.post("/admin/accounts/{user_id}/role")
async def update_account_role(
    user_id: int,
    request: Request,
    _: str = Depends(require_admin),
) -> RedirectResponse:
    _require_same_origin(request)
    form = await request.form()
    role_value = str(form.get("role", ""))
    try:
        role = UserRole(role_value)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Неизвестная роль.") from error

    try:
        user = await change_account_role(user_id, role)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if user is None:
        raise HTTPException(status_code=404, detail="Аккаунт не найден.")
    return _accounts_redirect(request, form)


@app.post("/admin/accounts/{user_id}/delete")
async def remove_account(
    user_id: int,
    request: Request,
    _: str = Depends(require_admin),
) -> RedirectResponse:
    _require_same_origin(request)
    form = await request.form()
    if form.get("confirm") != "delete":
        raise HTTPException(status_code=400, detail="Подтвердите удаление аккаунта.")
    if not await delete_account(user_id):
        raise HTTPException(status_code=404, detail="Аккаунт не найден.")
    return _accounts_redirect(request, form)
