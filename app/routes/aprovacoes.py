"""Caixa de Saída — aprovação manual do envio automático.

O gatilho do G-Click NÃO envia direto: ele enfileira as guias elegíveis aqui.
Nada vai ao WhatsApp até o operador revisar e clicar "Liberar envio". É a trava
de segurança contra disparo em massa acidental.
"""

from __future__ import annotations

import threading

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import auth, config, db, helpers, uazapi
from ..templating import templates
from .envio import _processar_lote

router = APIRouter()


def _linha_para_guia(r) -> dict:
    """Reconstrói o dict 'guia' a partir de uma linha da fila (fallback quando o
    G-Click não devolve mais a guia fresca)."""
    return {
        "cnpj": r["cnpj"], "cliente_apelido": r["cliente_apelido"],
        "tarefa_id": r["tarefa_id"], "atividade_id": r["atividade_id"],
        "atividade_nome": r["atividade_nome"], "obrigacao_nome": r["obrigacao_nome"],
        "arquivo_nome": r["arquivo_nome"], "arquivo_url": r["arquivo_url"],
        "competencia": r["competencia"], "data_vencimento": r["data_vencimento"],
    }


@router.get("/aprovacoes", response_class=HTMLResponse)
def aprovacoes_get(request: Request, sucesso: str | None = None, erro: str | None = None):
    if redir := auth.requer_login(request):
        return redir
    usuario = auth.usuario_da_requisicao(request)
    pendentes = db.listar_aprovacoes_pendentes()
    itens = []
    for r in pendentes:
        g = _linha_para_guia(r)
        itens.append({
            **dict(r),
            "titulo": helpers.titulo_documento(g),
            "competencia_label": helpers.competencia_label(r["competencia"] or ""),
            "vencimento_fmt": helpers.fmt_data(r["data_vencimento"]),
            "whatsapp": db.map_whatsapp_por_cnpj().get(r["cnpj"] or ""),
            "detectado_fmt": helpers.fmt_dt(r["detectado_em"]),
        })
    return templates.TemplateResponse(request, "aprovacoes.html", {
        "request": request, "usuario": usuario, "active": "aprovacoes",
        "itens": itens, "piloto": config.get_piloto_runtime(),
        "sucesso": sucesso, "erro": erro,
    })


@router.post("/aprovacoes/enviar")
def aprovacoes_enviar(request: Request, ids: list[int] = Form(default=[])):
    """Libera as guias selecionadas: monta o lote e dispara o envio real (reusa
    todas as camadas de segurança de `_processar_lote`). Marca-as como aprovadas."""
    if redir := auth.requer_login(request):
        return redir
    rows = db.get_aprovacoes_por_ids(ids)
    if not rows:
        return RedirectResponse(url="/aprovacoes?erro=Nada+selecionado", status_code=303)

    if helpers.lote_ativo():
        return RedirectResponse(url="/enviar/progresso", status_code=303)

    # Re-busca guias frescas do G-Click (a URL S3 expira ~2h — garante PDF válido).
    fresh: dict[tuple, dict] = {}
    for comp in {r["competencia"] for r in rows if r["competencia"]}:
        try:
            for t, ativs in helpers.carregar_tarefas_e_ativs(comp, None, forcar=True):
                from .. import gclick
                for g in gclick.extrair_guias_pendentes(t, ativs):
                    fresh[((g["cnpj"] or ""), g["tarefa_id"], g["atividade_id"])] = g
        except Exception:  # noqa: BLE001 — sem rede, cai no fallback armazenado
            pass

    alvos = []
    for r in rows:
        chave = ((r["cnpj"] or ""), r["tarefa_id"], r["atividade_id"])
        alvos.append(fresh.get(chave) or _linha_para_guia(r))

    whatsapp_map = db.map_whatsapp_por_cnpj()
    # 🧪 Modo piloto: redireciona TODO o envio automático para o número de teste
    # (ignora os reais). Só aqui — o envio manual nunca passa por este ponto.
    piloto = config.get_piloto_runtime()
    if piloto["ativo"]:
        whatsapp_map = {(a.get("cnpj") or ""): piloto["numero"] for a in alvos}

    cfg_envio = config.get_throttle_runtime()

    # Pré-checagem da conexão para lote >1 (espelha o fluxo manual).
    if len(alvos) > 1 and config.uazapi_configurado():
        diag = uazapi.testar_conexao()
        if not diag.get("ok"):
            return RedirectResponse(
                url=f"/aprovacoes?erro=Conexao+uazapi+falhou+({diag.get('categoria','?')})",
                status_code=303,
            )

    db.resolver_aprovacoes([r["id"] for r in rows], "aprovado")
    helpers.lote_iniciar(total=len(alvos), destino="/aprovacoes")
    threading.Thread(
        target=_processar_lote,
        args=(alvos, whatsapp_map, cfg_envio),
        kwargs={"origem": "automatico"},
        daemon=True,
    ).start()
    return RedirectResponse(url="/enviar/progresso", status_code=303)


@router.post("/aprovacoes/descartar")
def aprovacoes_descartar(request: Request, ids: list[int] = Form(default=[])):
    """Descarta as guias selecionadas — não envia e não volta a aparecer na fila."""
    if redir := auth.requer_login(request):
        return redir
    n = db.resolver_aprovacoes(ids, "descartado")
    return RedirectResponse(
        url=f"/aprovacoes?sucesso={n}+descartada(s)", status_code=303,
    )
