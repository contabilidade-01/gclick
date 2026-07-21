"""Cadastro de clientes (CNPJ↔WhatsApp) + sync com o G-Click + edição detalhada."""

from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import auth, config, db, gclick, portal
from ..templating import templates

router = APIRouter()


@router.get("/clientes", response_class=HTMLResponse)
def clientes_get(request: Request, sync: str | None = None,
                 sobrescrever: int = 0, focus: str | None = None,
                 filtro: str | None = None, sucesso: str | None = None):
    # `def`: quando sync=1, consulta o G-Click (bloqueante) — roda no threadpool.
    if redir := auth.requer_login(request):
        return redir
    usuario = auth.usuario_da_requisicao(request)
    # `sucesso` pode chegar por querystring (redirect de outra ação, ex.: sync do portal).

    if sync == "1":
        try:
            externos = gclick.listar_clientes()
            novos = 0
            atualizados = 0
            com_whatsapp_gclick = 0
            sem_whatsapp_gclick: list[str] = []
            for c in externos:
                d = gclick.extrair_dados_cliente(c)
                if not d["cnpj"] or d["cnpj"] == "0":
                    continue
                if d["whatsapp"]:
                    com_whatsapp_gclick += 1
                else:
                    sem_whatsapp_gclick.append(d["apelido"] or d["cnpj"])
                result = db.sync_cliente_do_gclick(
                    cnpj=d["cnpj"],
                    apelido=d["apelido"],
                    nome_completo=d["nome_completo"],
                    whatsapp=d["whatsapp"],
                    email=d["email"],
                    responsavel_nome=d["responsavel_nome"],
                    status_gclick=d["status_gclick"],
                    sobrescrever=bool(sobrescrever),
                )
                if result == "novo":
                    novos += 1
                else:
                    atualizados += 1
            modo = " (FORÇADO — sobrescreveu edições manuais)" if sobrescrever else ""
            partes = [f"✅ Sincronizado{modo}: {novos} novo(s), {atualizados} atualizado(s)."]
            partes.append(f"📱 {com_whatsapp_gclick} cliente(s) com telefone no G-Click.")
            if sem_whatsapp_gclick:
                amostra = ", ".join(sem_whatsapp_gclick[:5])
                if len(sem_whatsapp_gclick) > 5:
                    amostra += f" (+{len(sem_whatsapp_gclick)-5})"
                partes.append(f"⚠ {len(sem_whatsapp_gclick)} ainda SEM telefone no G-Click: {amostra}")
            sucesso = " ".join(partes)
        except Exception as e:
            sucesso = f"❌ Erro ao sincronizar: {e}"

    # Filtro: tudo (padrao) | sem_whatsapp | desativados | fora_padrão
    todos = db.listar_clientes()

    # Converte sqlite3.Row para dict
    def to_dict(row):
        return dict(row) if hasattr(row, 'keys') else row

    # Cálculo de dígitos para identificar números fora do padrão
    def contar_digitos(w: str | None) -> int:
        if not w:
            return 0
        return len("".join(ch for ch in w if ch.isdigit()))

    def fora_padrão_whatsapp(c: dict) -> bool:
        digitos = contar_digitos(c.get("whatsapp"))
        return digitos > 0 and digitos != 13

    # Pré-calcular dígitos para cada cliente
    clientes_view = []
    for c in todos:
        c_dict = to_dict(c)
        digitos = contar_digitos(c_dict.get("whatsapp"))
        clientes_view.append({**c_dict, "_digitos": digitos})

    if filtro == "sem_whatsapp":
        clientes_f = [c for c in clientes_view if not (c.get("whatsapp") or "").strip()]
    elif filtro == "desativados":
        clientes_f = [c for c in clientes_view if not c.get("ativo")]
    elif filtro == "fora_padrao":
        clientes_f = [c for c in clientes_view if fora_padrão_whatsapp(c)]
    elif filtro == "auto":
        clientes_f = [c for c in clientes_view if c.get("envio_automatico")]
    elif filtro == "enviaveis":
        # Ativos E com WhatsApp — sem os desativados e sem os sem-telefone.
        clientes_f = [c for c in clientes_view
                      if c.get("ativo") and (c.get("whatsapp") or "").strip()]
    else:
        clientes_f = clientes_view

    total_sem_whatsapp = sum(1 for c in clientes_view if not (c.get("whatsapp") or "").strip())
    total_desativados = sum(1 for c in clientes_view if not c.get("ativo"))
    total_fora_padrao = sum(1 for c in clientes_view if fora_padrão_whatsapp(c))
    total_auto = sum(1 for c in clientes_view if c.get("envio_automatico"))
    total_enviaveis = sum(1 for c in clientes_view
                          if c.get("ativo") and (c.get("whatsapp") or "").strip())

    return templates.TemplateResponse(request, "clientes.html", {
        "request": request,
        "usuario": usuario,
        "active": "clientes",
        "clientes": clientes_f,
        "total_geral": len(clientes_view),
        "total_sem_whatsapp": total_sem_whatsapp,
        "total_desativados": total_desativados,
        "total_fora_padrao": total_fora_padrao,
        "total_auto": total_auto,
        "total_enviaveis": total_enviaveis,
        "filtro": filtro or "",
        "sucesso": sucesso,
        "focus": focus,
    })


@router.post("/clientes/sincronizar-portal")
def sincronizar_portal(request: Request):
    """Cria no Portal do Cliente as empresas que faltam (só clientes ATIVOS).

    `def` (não async): a chamada ao portal é bloqueante — o FastAPI roda no threadpool.
    O portal não sobrescreve empresa existente; este botão só preenche o que falta.
    """
    if redir := auth.requer_login(request):
        return redir

    def _volta(msg: str):
        return RedirectResponse(url=f"/clientes?sucesso={quote_plus(msg)}", status_code=303)

    if not config.portal_configurado():
        return _volta("❌ Portal não configurado — defina PORTAL_INGEST_URL e PORTAL_INGEST_KEY.")

    ativos = [dict(c) for c in db.listar_clientes() if c["ativo"]]
    out = portal.sincronizar_clientes(ativos)
    if out is None:
        return _volta("❌ Falha ao falar com o portal — veja os logs.")

    partes = [f"✅ Portal sincronizado: {out['criadas']} empresa(s) criada(s), "
              f"{out['existentes']} já existia(m)."]
    if out.get("erros"):
        partes.append(f"⚠ {out['erros']} com erro (CNPJ inválido ou sem razão social).")
    partes.append("Empresas novas nascem só com as seções de entregas; "
                  "o Departamento Pessoal você liga no painel do portal.")
    return _volta(" ".join(partes))


@router.post("/clientes/salvar")
async def clientes_salvar(request: Request,
                          cnpj: str = Form(...),
                          whatsapp: str = Form(""),
                          ativo: int = Form(1)):
    if redir := auth.requer_login(request):
        return redir
    wpp = "".join(ch for ch in whatsapp if ch.isdigit()) or None
    # Mantém apelido existente
    atual = next((c for c in db.listar_clientes() if c["cnpj"] == cnpj), None)
    apelido = atual["apelido"] if atual else ""
    db.upsert_cliente(cnpj=cnpj, apelido=apelido, whatsapp=wpp, ativo=ativo)
    return RedirectResponse(url=f"/clientes?focus={cnpj}", status_code=303)


@router.get("/clientes/{cnpj}/editar", response_class=HTMLResponse)
async def cliente_editar_get(request: Request, cnpj: str, sucesso: str | None = None):
    if redir := auth.requer_login(request):
        return redir
    cli = db.get_cliente(cnpj)
    if not cli:
        return RedirectResponse(url="/clientes", status_code=303)
    # dict() para o template poder usar .get() (sqlite3.Row não tem .get()).
    return templates.TemplateResponse(request, "cliente_editar.html", {
        "request": request,
        "usuario": auth.usuario_da_requisicao(request),
        "active": "clientes",
        "cliente": dict(cli),
        "sucesso": sucesso,
    })


@router.post("/clientes/{cnpj}/editar")
async def cliente_editar_post(request: Request, cnpj: str,
                              apelido: str = Form(""),
                              whatsapp: str = Form(""),
                              email: str = Form(""),
                              ativo: int = Form(1),
                              responsavel_nome: str = Form(""),
                              observacoes: str = Form(""),
                              obrigacoes_aceitas: str = Form(""),
                              envio_automatico: int = Form(0)):
    if redir := auth.requer_login(request):
        return redir
    wpp = "".join(ch for ch in whatsapp if ch.isdigit()) or None
    db.atualizar_cliente_detalhado(
        cnpj=cnpj,
        apelido=apelido.strip(),
        whatsapp=wpp,
        email=email.strip() or None,
        ativo=ativo,
        responsavel_nome=responsavel_nome.strip() or None,
        observacoes=observacoes.strip() or None,
        obrigacoes_aceitas=obrigacoes_aceitas.strip().upper() or None,
        envio_automatico=envio_automatico,
    )
    return RedirectResponse(url=f"/clientes/{cnpj}/editar?sucesso=Cliente+atualizado", status_code=303)


@router.post("/clientes/marcar-auto")
async def clientes_marcar_auto(request: Request, ativo: int = Form(...)):
    """Marca ou desmarca envio automatico para TODOS os clientes."""
    if redir := auth.requer_login(request):
        return redir
    todos = db.listar_clientes()
    cnpjs = [c["cnpj"] for c in todos]
    n = db.set_envio_automatico_lote(cnpjs, ativo)
    return RedirectResponse(url=f"/clientes?sucesso={n}+cliente(s)+marcado(s)+para+envio+automatico", status_code=303)


@router.post("/clientes/{cnpj}/auto")
async def cliente_auto_toggle(request: Request, cnpj: str, ativo: int = Form(...)):
    """Marca ou desmarca envio automatico para um cliente (API via JS)."""
    if redir := auth.requer_login(request):
        return RedirectResponse(url="/login", status_code=303)
    db.set_envio_automatico(cnpj, ativo)
    return {"ok": True, "cnpj": cnpj, "envio_automatico": ativo}
