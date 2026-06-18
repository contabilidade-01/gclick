"""Fila do mês (`/fila`) + ações de ocultar/desocultar guias."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import auth, config, db, gclick, helpers
from ..templating import templates

router = APIRouter()


@router.get("/fila", response_class=HTMLResponse)
def fila(request: Request, competencia: str | None = None,
         obrigacao: str | None = None, cliente: str | None = None,
         status: str | None = None,
         mostrar_ocultas: int = 0, refresh: int = 0,
         sucesso: str | None = None, erro: str | None = None):
    # `def` (não async): consulta o G-Click — roda no threadpool, não trava o app.
    if redir := auth.requer_login(request):
        return redir
    usuario = auth.usuario_da_requisicao(request)

    hoje = date.today()
    competencia = competencia or f"{hoje.year:04d}-{hoje.month:02d}"
    obrigacao = (obrigacao or "").strip() or None
    cliente_q = (cliente or "").strip().lower()
    status_filtro = (status or "").strip().lower() or "todas"

    erro_carga: str | None = None
    guias_view: list[dict] = []
    enviadas_keys = db.chaves_enviadas()
    whatsapp_map = db.map_whatsapp_por_cnpj()
    ocultas_ativ, ocultas_tarefa = db.chaves_ocultas()
    qtd_ocultas_na_carga = 0

    try:
        dados = helpers.carregar_tarefas_e_ativs(competencia, obrigacao, forcar=bool(refresh))
        for t, ativs in dados:
            for g in gclick.extrair_guias_pendentes(t, ativs):
                cnpj = g["cnpj"] or ""
                # Filtro de oculta
                oculto = (
                    (cnpj, g["tarefa_id"], g["atividade_id"]) in ocultas_ativ
                    or (cnpj, g["tarefa_id"]) in ocultas_tarefa
                )
                if oculto:
                    qtd_ocultas_na_carga += 1
                    if not mostrar_ocultas:
                        continue
                # Filtro por cliente (substring no apelido ou CNPJ)
                if cliente_q:
                    alvo = f"{(g.get('cliente_apelido') or '').lower()} {cnpj}"
                    if cliente_q not in alvo:
                        continue
                ja = (cnpj, g["tarefa_id"], g["atividade_id"]) in enviadas_keys
                # Filtro por status
                if status_filtro == "enviadas" and not ja:
                    continue
                if status_filtro == "pendentes" and ja:
                    continue
                wpp = whatsapp_map.get(cnpj)
                tem_pdf = bool(g["arquivo_url"])
                pode = tem_pdf and bool(wpp) and not ja
                guias_view.append({
                    **g,
                    "data_vencimento_fmt": helpers.fmt_data(g["data_vencimento"]),
                    "whatsapp": wpp,
                    "whatsapp_fmt": helpers.fmt_whatsapp(wpp),
                    "tem_pdf": tem_pdf,
                    "ja_enviado": ja,
                    "pode_enviar": pode,
                    "oculto": oculto,
                    "chave": helpers.chave(g),
                })
        guias_view.sort(key=lambda x: (x["data_vencimento"] or "", x["cliente_apelido"] or ""))
    except Exception as e:
        erro_carga = str(e)

    prontas = sum(1 for g in guias_view if g["pode_enviar"])
    enviadas = sum(1 for g in guias_view if g["ja_enviado"])
    pendentes = prontas  # prontas + pendentes de envio

    return templates.TemplateResponse(request, "fila.html", {
        "request": request,
        "usuario": usuario,
        "active": "fila",
        "competencia": competencia,
        "competencias_opcoes": helpers.competencias_opcoes(competencia),
        "obrigacao": obrigacao,
        "cliente": cliente or "",
        "status_filtro": status_filtro,
        "mostrar_ocultas": mostrar_ocultas,
        "qtd_ocultas_na_carga": qtd_ocultas_na_carga,
        "guias": guias_view,
        "total": len(guias_view),
        "prontas": prontas,
        "enviadas": enviadas,
        "pendentes": pendentes,
        "uazapi_ok": config.uazapi_configurado(),
        "erro_carga": erro_carga,
        "sucesso": sucesso,
        "erro": erro,
    })


@router.post("/ocultar")
async def ocultar_post(request: Request,
                       chave: str = Form(...),
                       cnpj: str = Form(...),
                       competencia: str = Form(...),
                       origem: str = Form("fila"),
                       motivo: str = Form("")):
    """Oculta uma atividade específica (formato chave: 'tarefa_id|atividade_id')."""
    if redir := auth.requer_login(request):
        return redir
    try:
        tarefa_id, atividade_id = chave.split("|", 1)
    except ValueError:
        return RedirectResponse(url="/fila", status_code=303)
    db.ocultar(cnpj=cnpj, tarefa_id=tarefa_id, atividade_id=atividade_id,
               motivo=motivo or "Oculto na fila")
    destino = "/" if origem == "dashboard" else f"/fila?competencia={competencia}"
    return RedirectResponse(url=destino, status_code=303)


@router.post("/desocultar")
async def desocultar_post(request: Request,
                          cnpj: str = Form(...),
                          tarefa_id: str = Form(...),
                          atividade_id: str = Form(""),
                          voltar: str = Form("/fila")):
    if redir := auth.requer_login(request):
        return redir
    db.desocultar(cnpj=cnpj, tarefa_id=tarefa_id,
                  atividade_id=(atividade_id or None))
    return RedirectResponse(url=voltar, status_code=303)


@router.post("/ocultar/cliente")
async def ocultar_cliente_post(request: Request,
                               cnpj: str = Form(...),
                               competencia: str = Form(...),
                               tarefa_ids: str = Form(""),
                               motivo: str = Form("Ignorado em lote")):
    """Oculta todas as tarefas do cliente na competência. `tarefa_ids` vem como
    string separada por vírgula (montada no template a partir das guias visíveis)."""
    if redir := auth.requer_login(request):
        return redir
    ids = [t.strip() for t in tarefa_ids.split(",") if t.strip()]
    db.ocultar_cliente_competencia(cnpj=cnpj, tarefa_ids=ids, motivo=motivo)
    return RedirectResponse(url=f"/fila?competencia={competencia}", status_code=303)


@router.post("/ocultar/lote")
async def ocultar_lote_post(request: Request,
                            pares: list[str] = Form(default=[]),
                            competencia: str = Form(...),
                            motivo: str = Form("Oculto em lote pela fila")):
    """Oculta várias guias selecionadas na fila.

    `pares`: lista de strings no formato `cnpj|tarefa_id|atividade_id`.
    Vinda do form oculto montado pelo JS `ocultarSelecionadas()`.
    """
    if redir := auth.requer_login(request):
        return redir
    triplas: list[tuple[str, str, str | None]] = []
    for p in pares:
        try:
            cnpj, tarefa_id, atividade_id = p.split("|", 2)
        except ValueError:
            continue
        cnpj = cnpj.strip()
        tarefa_id = tarefa_id.strip()
        atividade_id = atividade_id.strip() or None
        if not cnpj or not tarefa_id:
            continue
        triplas.append((cnpj, tarefa_id, atividade_id))

    if not triplas:
        return RedirectResponse(
            url=f"/fila?competencia={competencia}&erro=Nenhuma+guia+valida+para+ocultar",
            status_code=303,
        )

    n = db.ocultar_em_lote(triplas, motivo=motivo or "Oculto em lote pela fila")
    return RedirectResponse(
        url=f"/fila?competencia={competencia}&sucesso={n}+guia(s)+ocultas",
        status_code=303,
    )
