"""Login simples: usuário/senha único, hash bcrypt, cookie de sessão assinado."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, URLSafeSerializer
from passlib.hash import bcrypt

from . import config

COOKIE = "gclick_sess"
_signer = URLSafeSerializer(config.SECRET_KEY, salt="gclick-login")


def verificar_credenciais(usuario: str, senha: str) -> bool:
    if usuario != config.APP_USER:
        return False
    try:
        return bcrypt.verify(senha, config.APP_PASSWORD_HASH)
    except Exception:
        return False


def gerar_cookie(usuario: str) -> str:
    return _signer.dumps({"u": usuario})


def usuario_da_requisicao(request: Request) -> str | None:
    raw = request.cookies.get(COOKIE)
    if not raw:
        return None
    try:
        data = _signer.loads(raw)
        return data.get("u")
    except BadSignature:
        return None


def requer_login(request: Request) -> RedirectResponse | None:
    """Retorna RedirectResponse se NÃO logado, senão None."""
    if usuario_da_requisicao(request):
        return None
    return RedirectResponse(url="/login", status_code=303)
