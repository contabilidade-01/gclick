"""Histórico de envios (auditoria)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .. import auth, db, helpers
from ..templating import templates

router = APIRouter()


@router.get("/auditoria", response_class=HTMLResponse)
async def auditoria(request: Request,
                    sucesso: str | None = None,
                    erro: str | None = None):
    if redir := auth.requer_login(request):
        return redir
    usuario = auth.usuario_da_requisicao(request)
    envios = db.listar_envios(500)
    envios_view = [{**dict(e), "enviado_em_fmt": helpers.fmt_dt(e["enviado_em"])} for e in envios]
    # Banner crítico — quantos token_invalido nas últimas 24h
    limite = (datetime.now(timezone.utc).replace(tzinfo=None)
              - timedelta(hours=24)).isoformat(timespec="seconds")
    qtd_token_invalido_24h = sum(
        1 for e in envios_view
        if e["status"] == "token_invalido" and (e["enviado_em"] or "") >= limite
    )
    return templates.TemplateResponse(request, "auditoria.html", {
        "request": request,
        "usuario": usuario,
        "active": "auditoria",
        "envios": envios_view,
        "qtd_token_invalido_24h": qtd_token_invalido_24h,
        "sucesso": sucesso,
        "erro": erro,
    })
