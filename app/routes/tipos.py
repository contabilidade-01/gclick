"""CRUD dos tipos de documento padrão (classificador)."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import auth, db, tipos as tipos_mod
from ..templating import templates

router = APIRouter()


@router.get("/tipos", response_class=HTMLResponse)
async def tipos_get(request: Request, sucesso: str | None = None):
    if redir := auth.requer_login(request):
        return redir
    return templates.TemplateResponse(request, "tipos.html", {
        "request": request,
        "usuario": auth.usuario_da_requisicao(request),
        "active": "tipos",
        "tipos": db.listar_tipos(),
        "sucesso": sucesso,
    })


@router.post("/tipos/salvar")
async def tipos_salvar(request: Request,
                       id: int | None = Form(None),
                       codigo: str = Form(...),
                       nome: str = Form(...),
                       matchers: str = Form(...),
                       ordem: int = Form(0),
                       ativo: int = Form(1),
                       template_mensagem: str = Form(""),
                       tem_vencimento: int = Form(1)):
    if redir := auth.requer_login(request):
        return redir
    db.upsert_tipo(id=id, codigo=codigo.strip().upper(), nome=nome.strip(),
                   matchers=matchers.strip(), ativo=ativo, ordem=ordem,
                   template_mensagem=template_mensagem.strip() or None,
                   tem_vencimento=tem_vencimento)
    tipos_mod.invalidar()
    return RedirectResponse(url="/tipos?sucesso=Tipo+salvo", status_code=303)


@router.post("/tipos/deletar")
async def tipos_deletar(request: Request, id: int = Form(...)):
    if redir := auth.requer_login(request):
        return redir
    db.deletar_tipo(id)
    tipos_mod.invalidar()
    return RedirectResponse(url="/tipos?sucesso=Tipo+excluido", status_code=303)
