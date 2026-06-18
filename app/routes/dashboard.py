"""Dashboard (tela inicial `/`) — KPIs do mês, vencimentos próximos, saúde."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .. import auth, config, db, gclick, helpers, tipos as tipos_mod, uazapi
from ..templating import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, testar_uazapi: int = 0):
    # `def` (não async): chama o G-Click (httpx síncrono, ~10-15s). Como rota
    # síncrona roda num threadpool — NÃO trava o event loop, então as telas
    # leves (Clientes, Auditoria...) respondem na hora mesmo durante esta carga.
    if redir := auth.requer_login(request):
        return redir
    usuario = auth.usuario_da_requisicao(request)

    hoje = date.today()
    competencia = f"{hoje.year:04d}-{hoje.month:02d}"
    daqui_7d = hoje + timedelta(days=7)

    # Diagnóstico uazapi (sob demanda — botão "🩺 Testar conexão")
    uazapi_diag = uazapi.testar_conexao() if testar_uazapi else None

    enviadas_keys = db.chaves_enviadas()
    whatsapp_map = db.map_whatsapp_por_cnpj()
    ocultas_ativ, ocultas_tarefa = db.chaves_ocultas()
    todos_clientes = db.listar_clientes()
    total_clientes_local = len(todos_clientes)
    clientes_sem_whatsapp = sum(1 for c in todos_clientes if not (c["whatsapp"] or "").strip())
    # Carrega os tipos UMA vez (corta o N+1 de get_tipo_por_codigo por guia).
    tipos_map = helpers.mapa_tipos()

    erro_carga: str | None = None
    kpi_total = 0
    kpi_enviadas = 0
    kpi_prontas = 0
    kpi_sem_anexo = 0
    kpi_sem_whatsapp = 0
    vence_hoje: list[dict] = []
    proximas: list[dict] = []
    gclick_ok = True

    try:
        dados = helpers.carregar_tarefas_e_ativs(competencia, None)
        for t, ativs in dados:
            for g in gclick.extrair_guias_pendentes(t, ativs):
                cnpj = g["cnpj"] or ""
                # Pula ocultas (não entram em estatística nem urgência)
                if ((cnpj, g["tarefa_id"], g["atividade_id"]) in ocultas_ativ
                        or (cnpj, g["tarefa_id"]) in ocultas_tarefa):
                    continue
                wpp = whatsapp_map.get(cnpj)
                ja = (cnpj, g["tarefa_id"], g["atividade_id"]) in enviadas_keys
                tem_pdf = bool(g["arquivo_url"])
                kpi_total += 1
                if ja:
                    kpi_enviadas += 1
                    continue
                if not tem_pdf:
                    kpi_sem_anexo += 1
                    continue
                if not wpp:
                    kpi_sem_whatsapp += 1
                    continue
                kpi_prontas += 1
                # Documento sem vencimento (recibo, extrato) não entra em "urgência"
                cls = tipos_mod.classificar(g.get("atividade_nome") or "",
                                            g.get("obrigacao_nome") or "")
                tipo_row = tipos_map.get(cls[0]) if cls else None
                if tipo_row and not tipo_row["tem_vencimento"]:
                    continue
                # Está pronta e tem vencimento: pode entrar na fila de vencimento próximo
                venc = g.get("data_vencimento") or ""
                try:
                    venc_d = date.fromisoformat(venc[:10])
                except Exception:
                    continue
                if venc_d < hoje:
                    # Já venceu — entra como urgente também (atrasada)
                    atrasados_dias = (hoje - venc_d).days
                    item = {
                        **g, "whatsapp": wpp,
                        "chave": helpers.chave(g),
                        "venc_d": venc_d,
                        "venc_fmt": venc_d.strftime("%d/%m"),
                        "rotulo": f"⏰ atrasada {atrasados_dias}d",
                        "cor": "vermelho",
                        "data_vencimento_fmt": helpers.fmt_data(g["data_vencimento"]),
                    }
                    vence_hoje.append(item)
                elif venc_d == hoje:
                    vence_hoje.append({
                        **g, "whatsapp": wpp,
                        "chave": helpers.chave(g),
                        "venc_d": venc_d,
                        "venc_fmt": "hoje",
                        "rotulo": "🔴 vence hoje",
                        "cor": "vermelho",
                        "data_vencimento_fmt": helpers.fmt_data(g["data_vencimento"]),
                    })
                elif venc_d <= daqui_7d:
                    dias = (venc_d - hoje).days
                    cor = "amarelo" if dias <= 3 else "verde"
                    rotulo = "🟡 amanhã" if dias == 1 else ("🟡 em " + str(dias) + " dias" if dias <= 3 else "🟢 em " + str(dias) + " dias")
                    proximas.append({
                        **g, "whatsapp": wpp,
                        "chave": helpers.chave(g),
                        "venc_d": venc_d,
                        "venc_fmt": venc_d.strftime("%d/%m"),
                        "rotulo": rotulo,
                        "cor": cor,
                        "data_vencimento_fmt": helpers.fmt_data(g["data_vencimento"]),
                    })
        vence_hoje.sort(key=lambda x: x["venc_d"])
        proximas.sort(key=lambda x: x["venc_d"])
    except Exception as e:
        erro_carga = str(e)
        gclick_ok = False

    percentual = round(100 * kpi_enviadas / kpi_total) if kpi_total else 0

    # Atividade recente
    envios_recentes = []
    for e in db.listar_envios(8):
        envios_recentes.append({
            **dict(e),
            "enviado_em_fmt": helpers.fmt_dt(e["enviado_em"]),
        })

    return templates.TemplateResponse(request, "dashboard.html", {
        "request": request,
        "usuario": usuario,
        "active": "dashboard",
        "hoje": hoje,
        "hoje_fmt": hoje.strftime("%A, %d/%m/%Y").capitalize(),
        "competencia_fmt": helpers.competencia_label(competencia),
        "competencia": competencia,
        "kpi_total": kpi_total,
        "kpi_enviadas": kpi_enviadas,
        "kpi_prontas": kpi_prontas,
        "kpi_sem_anexo": kpi_sem_anexo,
        "kpi_sem_whatsapp": kpi_sem_whatsapp,
        "percentual": percentual,
        "vence_hoje": vence_hoje,
        "proximas": proximas[:12],
        "envios_recentes": envios_recentes,
        "uazapi_ok": config.uazapi_configurado(),
        "uazapi_diag": uazapi_diag,
        "gclick_ok": gclick_ok,
        "erro_carga": erro_carga,
        "total_clientes_local": total_clientes_local,
        "clientes_sem_whatsapp_local": clientes_sem_whatsapp,
    })


@router.get("/uazapi/teste")
async def uazapi_teste(request: Request):
    """Diagnóstico rápido. Redireciona pro dashboard com flag de teste."""
    from fastapi.responses import RedirectResponse
    if redir := auth.requer_login(request):
        return redir
    return RedirectResponse(url="/?testar_uazapi=1", status_code=303)
