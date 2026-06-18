"""Configurações da uazapi pela UI (atualiza subdomain/token sem reiniciar)."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import auth, config, db, uazapi
from ..templating import templates

router = APIRouter()


@router.get("/configuracoes", response_class=HTMLResponse)
def configuracoes_get(request: Request,
                      sucesso: str | None = None,
                      erro: str | None = None,
                      testar: int = 0):
    # `def`: "Testar conexão" faz chamada de rede à uazapi — roda no threadpool.
    if redir := auth.requer_login(request):
        return redir
    usuario = auth.usuario_da_requisicao(request)

    sub_atual, tok_atual = config.uazapi_credentials()
    sub_row = db.get_config_row("uazapi_subdomain")
    tok_row = db.get_config_row("uazapi_token")
    sub_origem = "Banco (UI)" if sub_row else (".env" if config.UAZAPI_SUBDOMAIN_ENV else "—")
    tok_origem = "Banco (UI)" if tok_row else (".env" if config.UAZAPI_TOKEN_ENV else "—")

    # G-Click — mesmo modelo (banco > .env), editável pela UI
    gc_id, gc_sec = config.gclick_credentials()
    gc_id_row = db.get_config_row("gclick_client_id")
    gc_sec_row = db.get_config_row("gclick_client_secret")
    gc_id_origem = "Banco (UI)" if gc_id_row else (".env" if config.GCLICK_CLIENT_ID_ENV else "—")
    gc_sec_origem = "Banco (UI)" if gc_sec_row else (".env" if config.GCLICK_CLIENT_SECRET_ENV else "—")

    diag = uazapi.testar_conexao() if testar else None
    # Estado da fila de envio (consulta leve à uazapi). Só pega se as
    # credenciais existem — não vale a pena tomar 401 toda vez que o
    # usuário abre a tela.
    fila_uazapi = uazapi.consultar_fila() if config.uazapi_configurado() else None

    # Configurações de ritmo atuais (origem: banco se UI já gravou, senão .env)
    throttle = config.get_throttle_runtime()
    throttle_origem = {
        "throttle_s": "Banco (UI)" if db.get_config_row("envio_throttle_s") else ".env",
        "max_por_hora": "Banco (UI)" if db.get_config_row("envio_max_por_hora") else ".env",
        "delay_uazapi_ms": "Banco (UI)" if db.get_config_row("envio_delay_uazapi_ms") else "padrão",
    }

    return templates.TemplateResponse(request, "configuracoes.html", {
        "request": request,
        "usuario": usuario,
        "active": "configuracoes",
        "subdomain_atual": sub_atual,
        "token_atual": tok_atual,  # valor real (campo password + olho)
        "token_atual_mascarado": (tok_atual[:8] + "…" + tok_atual[-4:]) if tok_atual else "",
        "sub_origem": sub_origem,
        "tok_origem": tok_origem,
        # G-Click
        "gclick_client_id": gc_id,
        "gclick_client_secret": gc_sec,
        "gc_id_origem": gc_id_origem,
        "gc_sec_origem": gc_sec_origem,
        "sub_atualizado_em": (sub_row["atualizado_em"] if sub_row else None),
        "tok_atualizado_em": (tok_row["atualizado_em"] if tok_row else None),
        "sub_atualizado_por": (sub_row["atualizado_por"] if sub_row else None),
        "tok_atualizado_por": (tok_row["atualizado_por"] if tok_row else None),
        "diag": diag,
        "throttle": throttle,
        "throttle_origem": throttle_origem,
        "fila_uazapi": fila_uazapi,
        "sucesso": sucesso,
        "erro": erro,
    })


@router.post("/configuracoes/uazapi")
async def configuracoes_uazapi_post(request: Request,
                                    subdomain: str = Form(""),
                                    token: str = Form(""),
                                    testar: int = Form(0)):
    if redir := auth.requer_login(request):
        return redir
    usuario = auth.usuario_da_requisicao(request) or "?"
    sub = subdomain.strip()
    tok = token.strip()

    if not sub and not tok:
        return RedirectResponse(
            url="/configuracoes?erro=Preencha+pelo+menos+um+campo",
            status_code=303,
        )

    if sub:
        db.set_config("uazapi_subdomain", sub, usuario)
    if tok:
        db.set_config("uazapi_token", tok, usuario)

    qs = "sucesso=Credenciais+salvas"
    if testar:
        qs += "&testar=1"
    return RedirectResponse(url=f"/configuracoes?{qs}", status_code=303)


@router.post("/configuracoes/gclick")
async def configuracoes_gclick_post(request: Request,
                                    client_id: str = Form(""),
                                    client_secret: str = Form("")):
    """Salva as credenciais do G-Click no banco (config_runtime), sobrepondo o .env.
    Tira o secret do código/.env — fica persistido no banco da VPS."""
    if redir := auth.requer_login(request):
        return redir
    usuario = auth.usuario_da_requisicao(request) or "?"
    cid = client_id.strip()
    csec = client_secret.strip()
    if not cid and not csec:
        return RedirectResponse(
            url="/configuracoes?erro=Preencha+pelo+menos+um+campo+do+G-Click",
            status_code=303,
        )
    if cid:
        db.set_config("gclick_client_id", cid, usuario)
    if csec:
        db.set_config("gclick_client_secret", csec, usuario)
    return RedirectResponse(
        url="/configuracoes?sucesso=Credenciais+G-Click+salvas", status_code=303,
    )


@router.post("/configuracoes/gclick/limpar")
async def configuracoes_gclick_limpar(request: Request, campo: str = Form(...)):
    """Remove a sobreposição do banco para um campo do G-Click — volta ao .env."""
    if redir := auth.requer_login(request):
        return redir
    if campo not in ("gclick_client_id", "gclick_client_secret"):
        return RedirectResponse(url="/configuracoes", status_code=303)
    with db.conn() as c:
        c.execute("DELETE FROM config_runtime WHERE chave=?", (campo,))
    return RedirectResponse(
        url="/configuracoes?sucesso=Valor+G-Click+removido%2C+voltou+ao+.env",
        status_code=303,
    )


@router.post("/configuracoes/uazapi/limpar")
async def configuracoes_uazapi_limpar(request: Request, campo: str = Form(...)):
    """Remove a sobreposição do banco para um campo — volta ao valor do .env."""
    if redir := auth.requer_login(request):
        return redir
    if campo not in ("uazapi_subdomain", "uazapi_token"):
        return RedirectResponse(url="/configuracoes", status_code=303)
    with db.conn() as c:
        c.execute("DELETE FROM config_runtime WHERE chave=?", (campo,))
    return RedirectResponse(
        url="/configuracoes?sucesso=Valor+do+banco+removido%2C+voltou+ao+.env",
        status_code=303,
    )


@router.post("/configuracoes/throttle")
async def configuracoes_throttle_post(request: Request,
                                      throttle_s: str = Form(""),
                                      max_por_hora: str = Form(""),
                                      delay_uazapi_ms: str = Form("")):
    """Persiste as 3 configurações de ritmo de envio. Campos em branco mantêm
    o valor atual (a função `get_throttle_runtime` continua usando o banco/.env)."""
    if redir := auth.requer_login(request):
        return redir
    usuario = auth.usuario_da_requisicao(request) or "?"

    # Cada campo só é gravado se vier preenchido e parseável.
    salvos = 0
    if throttle_s.strip():
        try:
            v = max(0.0, float(throttle_s.replace(",", ".")))
            db.set_config("envio_throttle_s", str(v), usuario)
            salvos += 1
        except ValueError:
            pass
    if max_por_hora.strip():
        try:
            v = max(1, int(max_por_hora))
            db.set_config("envio_max_por_hora", str(v), usuario)
            salvos += 1
        except ValueError:
            pass
    if delay_uazapi_ms.strip():
        try:
            v = max(0, int(delay_uazapi_ms))
            db.set_config("envio_delay_uazapi_ms", str(v), usuario)
            salvos += 1
        except ValueError:
            pass

    if not salvos:
        return RedirectResponse(
            url="/configuracoes?erro=Preencha+pelo+menos+um+campo+valido",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/configuracoes?sucesso=Ritmo+de+envio+salvo+({salvos}+campo(s))",
        status_code=303,
    )


@router.post("/configuracoes/throttle/restaurar")
async def configuracoes_throttle_restaurar(request: Request, campo: str = Form(...)):
    """Remove a sobreposição do banco — volta ao valor do .env (ou default)."""
    if redir := auth.requer_login(request):
        return redir
    if campo not in ("envio_throttle_s", "envio_max_por_hora", "envio_delay_uazapi_ms"):
        return RedirectResponse(url="/configuracoes", status_code=303)
    with db.conn() as c:
        c.execute("DELETE FROM config_runtime WHERE chave=?", (campo,))
    return RedirectResponse(
        url="/configuracoes?sucesso=Restaurado+ao+padrao+do+.env",
        status_code=303,
    )


@router.post("/configuracoes/uazapi/limpar-fila")
async def configuracoes_uazapi_limpar_fila(request: Request):
    """Reset de emergência — apaga toda a fila async pendente na uazapi."""
    if redir := auth.requer_login(request):
        return redir
    r = uazapi.limpar_fila()
    if r["ok"]:
        return RedirectResponse(
            url=f"/configuracoes?sucesso=Fila+uazapi+limpa",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/configuracoes?erro=Falhou+ao+limpar+fila+({r.get('categoria')})",
        status_code=303,
    )


@router.post("/configuracoes/modo-envio")
async def configuracoes_modo_envio(request: Request,
                                  modo_envio: str = Form("anexo")):
    """Salva o modo de envio: 'anexo' (PDF) ou 'link' (texto com hyperlink)."""
    if redir := auth.requer_login(request):
        return redir
    usuario = auth.usuario_da_requisicao(request) or "?"
    modo = modo_envio.strip().lower()
    if modo not in ("anexo", "link"):
        return RedirectResponse(
            url="/configuracoes?erro=Modo+invalido",
            status_code=303,
        )
    db.set_config("modo_envio", modo, usuario)
    label = "📎 Anexo (PDF)" if modo == "anexo" else "🔗 Link (hyperlink)"
    return RedirectResponse(
        url=f"/configuracoes?sucesso=Modo+de+envio+alterado+para+{label}",
        status_code=303,
    )
