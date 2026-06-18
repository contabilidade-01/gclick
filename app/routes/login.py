"""Rotas de autenticação: login, logout."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import auth
from ..templating import templates

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    return templates.TemplateResponse(request, "login.html", {"request": request, "usuario": None, "erro": None})


@router.post("/login")
def login_post(request: Request, usuario: str = Form(...), senha: str = Form(...)):
    # `def` (não async): bcrypt.verify é CPU-bound (~100-300ms). Como rota
    # síncrona, o FastAPI a roda num threadpool e não trava o event loop.
    if not auth.verificar_credenciais(usuario, senha):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "usuario": None, "erro": "Usuario ou senha invalidos."},
            status_code=401,
        )
    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie(auth.COOKIE, auth.gerar_cookie(usuario), httponly=True, samesite="lax")
    return resp


@router.get("/logout")
async def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp
