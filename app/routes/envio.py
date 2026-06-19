"""Envio e reenvio de guias pela uazapi + download local do PDF."""

from __future__ import annotations

import logging
import secrets
import threading
import time

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from .. import auth, config, db, gclick, helpers, tipos as tipos_mod, uazapi
from ..templating import templates

router = APIRouter()
logger = logging.getLogger("gclick.app")


@router.post("/enviar")
def enviar(request: Request,
           competencia: str = Form(...),
           obrigacao: str = Form(""),
           acao: str | None = Form(None),
           chave: list[str] = Form(default=[]),
           chave_unica: str | None = Form(None),
           origem: str = Form("fila")):
    """Dispara o lote em thread de fundo e redireciona pra tela de progresso.

    A rota retorna IMEDIATO (não fica bloqueada durante o envio) — a thread
    atualiza o estado em `helpers.lote_state` e o JS no progresso faz polling.
    """
    if redir := auth.requer_login(request):
        return redir

    # Se já tem lote rodando, só leva o usuário pra tela dele (sem iniciar 2º)
    if helpers.lote_ativo():
        return RedirectResponse(url="/enviar/progresso", status_code=303)

    chaves_selecionadas = set(chave)
    if chave_unica:
        chaves_selecionadas = {chave_unica}
        acao = "lote"

    # Usa o cache de tarefas+atividades (recém-populado pela /)
    dados = helpers.carregar_tarefas_e_ativs(competencia, (obrigacao or None))
    guias_disp: dict[str, dict] = {}
    for t, ativs in dados:
        for g in gclick.extrair_guias_pendentes(t, ativs):
            guias_disp[helpers.chave(g)] = g

    whatsapp_map = db.map_whatsapp_por_cnpj()
    enviadas_keys = db.chaves_enviadas()
    cfg_envio = config.get_throttle_runtime()

    if acao == "todas":
        alvos = [g for g in guias_disp.values()
                 if g["arquivo_url"] and whatsapp_map.get(g["cnpj"] or "")
                 and (g["cnpj"] or "", g["tarefa_id"], g["atividade_id"]) not in enviadas_keys]
    else:
        alvos = [guias_disp[k] for k in chaves_selecionadas if k in guias_disp]

    if not alvos:
        return RedirectResponse(
            url=f"/fila?competencia={competencia}&erro=Nenhuma+guia+selecionada+para+enviar",
            status_code=303,
        )

    # 🛡 Pré-checagem da conexão uazapi quando o lote tem >1 alvo.
    if len(alvos) > 1 and config.uazapi_configurado():
        diag = uazapi.testar_conexao()
        if not diag.get("ok"):
            cat = diag.get("categoria", "?")
            logger.warning("Lote abortado pre-envio: %s (%s alvos)",
                           diag.get("mensagem"), len(alvos))
            return RedirectResponse(
                url=f"/configuracoes?testar=1&erro=Lote+abortado+({cat})",
                status_code=303,
            )

    # Calcula a URL de destino final (após o lote terminar) para o JS redirecionar.
    if origem == "dashboard":
        destino = "/"
    else:
        qs = f"competencia={competencia}"
        if obrigacao:
            qs += f"&obrigacao={obrigacao}"
        destino = f"/fila?{qs}"

    helpers.lote_iniciar(total=len(alvos), destino=destino)
    threading.Thread(
        target=_processar_lote,
        args=(alvos, whatsapp_map, cfg_envio),
        daemon=True,
    ).start()
    return RedirectResponse(url="/enviar/progresso", status_code=303)


def _processar_lote(alvos: list[dict],
                    whatsapp_map: dict[str, str],
                    cfg_envio: dict) -> None:
    """Loop de envio executado em thread de fundo. Atualiza `helpers.lote_*`
    a cada passo para a tela de progresso refletir."""
    throttle_s = cfg_envio["throttle_s"]
    teto_hora = cfg_envio["max_por_hora"]
    delay_uazapi_ms = cfg_envio["delay_uazapi_ms"]
    modo_envio = cfg_envio.get("modo_envio", "anexo")
    abortar_por_token = False

    try:
        for idx, g in enumerate(alvos, start=1):
            if abortar_por_token:
                break
            cnpj = g["cnpj"] or ""
            cliente = g.get("cliente_apelido") or cnpj or "?"
            arquivo = g.get("arquivo_nome") or "(sem nome)"
            helpers.lote_set_atual(idx, cliente, arquivo)

            wpp = whatsapp_map.get(cnpj)
            if not wpp:
                db.registrar_envio(cnpj=cnpj, whatsapp=None,
                                   tarefa_id=g["tarefa_id"], atividade_id=g["atividade_id"],
                                   arquivo_nome=g["arquivo_nome"], competencia=g["competencia"],
                                   uazapi_message_id=None, status="falha",
                                   erro="WhatsApp nao cadastrado")
                helpers.lote_marcar_resultado("falha", f"❌ {cliente} — sem WhatsApp cadastrado")
                continue

            # 🛡 Camada 1: formato do número
            ok, motivo = helpers.validar_whatsapp_br(wpp)
            if not ok:
                db.registrar_envio(cnpj=cnpj, whatsapp=wpp,
                                   tarefa_id=g["tarefa_id"], atividade_id=g["atividade_id"],
                                   arquivo_nome=g["arquivo_nome"], competencia=g["competencia"],
                                   uazapi_message_id=None, status="bloqueado",
                                   erro=f"Validacao: {motivo}")
                helpers.lote_marcar_resultado("bloqueado", f"⚠ {cliente} — WhatsApp inválido")
                continue

            # 🛡 Camada 2: auto-envio
            if helpers.eh_proprio_numero(wpp):
                db.registrar_envio(cnpj=cnpj, whatsapp=wpp,
                                   tarefa_id=g["tarefa_id"], atividade_id=g["atividade_id"],
                                   arquivo_nome=g["arquivo_nome"], competencia=g["competencia"],
                                   uazapi_message_id=None, status="bloqueado",
                                   erro="Destino igual ao numero da instancia")
                helpers.lote_marcar_resultado("bloqueado", f"⚠ {cliente} — destino = nosso próprio número")
                continue

            # 🛡 Camada 3: obrigacoes_aceitas
            cls = tipos_mod.classificar(g.get("arquivo_nome") or "",
                                        g.get("atividade_nome") or "",
                                        g.get("obrigacao_nome") or "")
            codigo_tipo = cls[0] if cls else None
            if not helpers.cliente_aceita_tipo(cnpj, codigo_tipo):
                db.registrar_envio(cnpj=cnpj, whatsapp=wpp,
                                   tarefa_id=g["tarefa_id"], atividade_id=g["atividade_id"],
                                   arquivo_nome=g["arquivo_nome"], competencia=g["competencia"],
                                   uazapi_message_id=None, status="bloqueado",
                                   erro=f"Cliente nao aceita o tipo {codigo_tipo or '(nao classificado)'}")
                helpers.lote_marcar_resultado(
                    "bloqueado",
                    f"⚠ {cliente} — não aceita {codigo_tipo or 'tipo desconhecido'}")
                continue

            # 🛡 Camada 4: valida vencimento no PDF
            pdf_bytes: bytes | None = None
            venc_pdf_str: str | None = None
            venc_gclick_str = (g.get("data_vencimento") or "")[:10]
            tipo_row = db.get_tipo_por_codigo(codigo_tipo) if codigo_tipo else None
            if tipo_row and tipo_row["tem_vencimento"]:
                pdf_bytes, venc_pdf_str = helpers.validar_vencimento_no_pdf(g)

            caption = helpers.legenda(g)
            if not config.uazapi_configurado():
                db.registrar_envio(cnpj=cnpj, whatsapp=wpp,
                                   tarefa_id=g["tarefa_id"], atividade_id=g["atividade_id"],
                                   arquivo_nome=g["arquivo_nome"], competencia=g["competencia"],
                                   uazapi_message_id=None, status="simulado",
                                   erro="uazapi nao configurada (.env)")
                helpers.lote_marcar_resultado("bloqueado", f"⚠ {cliente} — uazapi não configurada (simulado)")
                continue

            # Teto/hora
            if not helpers.sob_teto_horario():
                db.registrar_envio(cnpj=cnpj, whatsapp=wpp,
                                   tarefa_id=g["tarefa_id"], atividade_id=g["atividade_id"],
                                   arquivo_nome=g["arquivo_nome"], competencia=g["competencia"],
                                   uazapi_message_id=None, status="bloqueado",
                                   erro=f"Teto de {teto_hora} envios/hora atingido")
                helpers.lote_marcar_resultado(
                    "bloqueado",
                    f"⚠ {cliente} — teto de {teto_hora}/h atingido")
                continue

            try:
                # Decide modo: anexo (PDF) ou link (texto com link curto clicável)
                if modo_envio == "link":
                    nome_arquivo = g["arquivo_nome"] or "Documento"
                    # Link RASTREADO próprio (/g/{token}) quando há domínio público
                    # (PUBLIC_BASE_URL). Vantagens: NUNCA expira (serve a cópia local)
                    # e registra quem abriu/baixou, quando e de onde. Sem domínio
                    # público (dev local), cai no link S3 cru do G-Click (expira
                    # ~2h, sem rastreio) — degradação segura.
                    if config.PUBLIC_BASE_URL:
                        token = secrets.token_urlsafe(16)
                        link = config.url_publica(f"/g/{token}")
                    else:
                        token = None
                        link = g["arquivo_url"] or ""
                    msg_texto = helpers.mensagem_documento(g)
                    resp = helpers.enviar_botao_com_retry(
                        numero=wpp, texto=msg_texto,
                        botao_texto="📄 Abrir documento",
                        url=link,
                        footer="NESCON CONTABILIDADE",
                        delay_ms=delay_uazapi_ms,
                    )
                    msg_id = (resp.get("messageid") or resp.get("id")
                              or (resp.get("message", {}) or {}).get("id"))
                    envio_id = db.registrar_envio(
                        cnpj=cnpj, whatsapp=wpp,
                        tarefa_id=g["tarefa_id"], atividade_id=g["atividade_id"],
                        arquivo_nome=nome_arquivo, competencia=g["competencia"],
                        uazapi_message_id=str(msg_id) if msg_id else None,
                        status="ok",
                    )
                    if token:
                        db.set_envio_token(envio_id, token)
                    # Backup local do PDF — é o que o /g/{token}/ver vai servir
                    # (permanente, nunca expira).
                    helpers.baixar_pdf_local(
                        envio_id, g["arquivo_url"] or "", nome_arquivo,
                        bytes_ja_baixados=pdf_bytes,
                    )
                else:
                    # Modo anexo: envia PDF diretamente
                    resp = helpers.enviar_com_retry(
                        numero=wpp, file_url=g["arquivo_url"],
                        doc_name=g["arquivo_nome"] or "guia.pdf", caption=caption,
                        delay_ms=delay_uazapi_ms,
                    )
                    msg_id = (resp.get("messageid") or resp.get("id")
                              or (resp.get("message", {}) or {}).get("id"))
                    envio_id = db.registrar_envio(
                        cnpj=cnpj, whatsapp=wpp,
                        tarefa_id=g["tarefa_id"], atividade_id=g["atividade_id"],
                        arquivo_nome=g["arquivo_nome"], competencia=g["competencia"],
                        uazapi_message_id=str(msg_id) if msg_id else None,
                        status="ok",
                    )
                    helpers.baixar_pdf_local(
                        envio_id, g["arquivo_url"], g["arquivo_nome"] or "documento.pdf",
                        bytes_ja_baixados=pdf_bytes,
                    )

                if venc_pdf_str or venc_gclick_str:
                    db.set_envio_vencimentos(envio_id, venc_pdf_str, venc_gclick_str)
                helpers.lote_marcar_resultado("ok", f"✅ {cliente} — {arquivo} ({modo_envio})")
            except uazapi.UazapiTokenInvalido as e:
                db.registrar_envio(cnpj=cnpj, whatsapp=wpp,
                                   tarefa_id=g["tarefa_id"], atividade_id=g["atividade_id"],
                                   arquivo_nome=g["arquivo_nome"], competencia=g["competencia"],
                                   uazapi_message_id=None, status="token_invalido",
                                   erro=str(e)[:500])
                helpers.lote_marcar_resultado(
                    "token_invalido",
                    f"🔑 {cliente} — token uazapi inválido (lote interrompido)")
                abortar_por_token = True
            except Exception as e:  # noqa: BLE001
                db.registrar_envio(cnpj=cnpj, whatsapp=wpp,
                                   tarefa_id=g["tarefa_id"], atividade_id=g["atividade_id"],
                                   arquivo_nome=g["arquivo_nome"], competencia=g["competencia"],
                                   uazapi_message_id=None, status="falha",
                                   erro=str(e)[:500])
                helpers.lote_marcar_resultado("falha", f"❌ {cliente} — {str(e)[:100]}")
            finally:
                helpers.marcar_envio_realizado()
                if throttle_s > 0:
                    time.sleep(throttle_s)
    except Exception as e:  # noqa: BLE001 — não deixa thread morrer silenciosa
        logger.exception("Erro fatal no lote: %s", e)
        helpers.lote_finalizar(erro_fatal=f"Erro fatal: {e}")
        return

    # Se foi por token inválido, manda direto pras configurações
    if abortar_por_token:
        from .. import helpers as _h
        _h._lote_state["destino"] = (
            "/configuracoes?testar=1&erro=Lote+interrompido+por+token+invalido+da+uazapi"
        )
    helpers.lote_finalizar()


@router.get("/enviar/status", response_class=JSONResponse)
def enviar_status(request: Request):
    """Snapshot JSON do lote em curso. Polled pelo JS do progresso."""
    if redir := auth.requer_login(request):
        return JSONResponse({"erro": "nao_autenticado"}, status_code=401)
    return JSONResponse(helpers.lote_snapshot())


@router.get("/enviar/progresso", response_class=Response)
def enviar_progresso(request: Request):
    """Tela com barra de progresso + cliente atual + lista de eventos.
    Se não há lote ativo nem recente, manda o usuário pra fila."""
    if redir := auth.requer_login(request):
        return redir
    snap = helpers.lote_snapshot()
    # Se nada rodou desde o boot, redireciona pra fila (não tem o que mostrar)
    if not snap.get("iniciado_em"):
        return RedirectResponse(url="/fila", status_code=303)
    usuario = auth.usuario_da_requisicao(request)
    return templates.TemplateResponse(request, "progresso.html", {
        "request": request,
        "usuario": usuario,
        "active": "fila",
        "snap": snap,
    })


@router.get("/d/{envio_id}")
async def baixar_pdf_local(envio_id: int, request: Request):
    """Serve o PDF salvo no envio. Acessível SEM login porque precisa ser
    baixado pelos servidores do WhatsApp/uazapi quando enviamos por link.
    Registra o acesso (IP, user-agent) para fins de auditoria."""
    envio = db.get_envio(envio_id)
    if not envio:
        return Response("Documento não encontrado.", status_code=404)
    pdf_path = envio["pdf_local_path"] if "pdf_local_path" in envio.keys() else None
    if not pdf_path:
        return Response("Documento não tem cópia local.", status_code=404)
    caminho = config.PASTA_GUIAS.parent / pdf_path
    if not caminho.exists():
        return Response("Arquivo não encontrado no disco.", status_code=410)

    # Log do acesso (preparação para tracking jurídico — item 7)
    ip = request.client.host if request.client else "?"
    ua = request.headers.get("user-agent", "")[:300]
    logger.info("[/d/%s] acesso IP=%s UA=%s", envio_id, ip, ua[:80])

    nome_dl = (envio["arquivo_nome"] or "documento.pdf")
    return FileResponse(
        caminho, media_type="application/pdf",
        filename=nome_dl,
        headers={"Content-Disposition": f'inline; filename="{nome_dl}"'},
    )


# ===================== LINK RASTREADO (/g/{token}) =====================
# Página pública de visualização + serviço do PDF, com registro de acesso
# (IP, geo, user-agent), filtro de bot/preview e token opaco (não-enumerável).

def _registrar_acesso_async(envio_id: int, token: str, evento: str, request: Request) -> None:
    """Registra o acesso em background — não atrasa a resposta ao cliente.
    Extrai IP/UA na hora (thread-safe); geo-IP + insert vão na thread."""
    ip = helpers.ip_do_request(request)
    ua = request.headers.get("user-agent", "")
    eh_bot = 1 if helpers.eh_user_agent_bot(ua) else 0

    def _job() -> None:
        try:
            geo = {} if eh_bot else helpers.geo_ip(ip)
            db.registrar_acesso(
                envio_id=envio_id, token=token, evento=evento, ip=ip,
                cidade=geo.get("cidade"), estado=geo.get("estado"),
                pais=geo.get("pais"), user_agent=ua, eh_bot=eh_bot,
            )
        except Exception:  # noqa: BLE001
            logger.exception("falha ao registrar acesso ao documento (envio %s)", envio_id)

    threading.Thread(target=_job, daemon=True).start()


@router.get("/g/{token}")
def documento_pagina(token: str, request: Request):
    """Página de visualização do documento (pública, SEM login — é o cliente).
    Mostra a guia + botão para abrir o PDF. Registra a abertura (filtra preview)."""
    envio = db.get_envio_por_token(token)
    if not envio:
        return Response("Documento não encontrado ou link inválido.", status_code=404)
    _registrar_acesso_async(envio["id"], token, "pagina", request)

    g = {
        "arquivo_nome": envio["arquivo_nome"], "atividade_nome": "", "obrigacao_nome": "",
        "competencia": envio["competencia"], "data_vencimento": envio["vencimento_pdf"] or "",
    }
    venc_iso = envio["vencimento_pdf"] if "vencimento_pdf" in envio.keys() else None
    return templates.TemplateResponse(request, "documento_publico.html", {
        "request": request,
        "titulo": helpers.titulo_documento(g),
        "competencia": helpers.competencia_label(envio["competencia"] or ""),
        "vencimento": helpers.fmt_data(venc_iso) if venc_iso else "",
        "token": token,
    })


@router.get("/g/{token}/ver")
def documento_pdf(token: str, request: Request):
    """Serve o PDF (cópia local, permanente) e registra o download."""
    envio = db.get_envio_por_token(token)
    if not envio:
        return Response("Documento não encontrado.", status_code=404)
    pdf_path = envio["pdf_local_path"] if "pdf_local_path" in envio.keys() else None
    if not pdf_path:
        return Response("Documento sem cópia disponível.", status_code=404)
    caminho = config.PASTA_GUIAS.parent / pdf_path
    if not caminho.exists():
        return Response("Arquivo não encontrado.", status_code=410)
    _registrar_acesso_async(envio["id"], token, "download", request)
    nome_dl = envio["arquivo_nome"] or "documento.pdf"
    return FileResponse(
        caminho, media_type="application/pdf", filename=nome_dl,
        headers={"Content-Disposition": f'inline; filename="{nome_dl}"'},
    )


@router.post("/reenviar")
def reenviar(request: Request, envio_id: int = Form(...)):
    """Refaz o envio buscando uma URL fresca do PDF no G-Click."""
    if redir := auth.requer_login(request):
        return redir
    original = db.get_envio(envio_id)
    if not original:
        return RedirectResponse(url="/auditoria?erro=Envio+nao+encontrado", status_code=303)

    cnpj = original["cnpj"]
    tarefa_id = original["tarefa_id"]
    atividade_id = original["atividade_id"]
    competencia = original["competencia"] or ""
    arquivo_nome_original = original["arquivo_nome"] or "documento.pdf"

    # 1) Estratégia: tenta URL fresca do G-Click primeiro.
    #    Se G-Click estiver fora ou anexo removido, fallback para PDF salvo localmente.
    file_url: str | None = None
    arquivo_nome = arquivo_nome_original
    fonte = "G-Click"
    atividade = None
    try:
        ativs = gclick.listar_atividades(tarefa_id)
        atividade = next((a for a in ativs if str(a["id"]) == atividade_id), None)
        if atividade and atividade.get("arquivos"):
            arquivo = atividade["arquivos"][0]
            file_url = arquivo["url"]
            arquivo_nome = arquivo.get("nome") or arquivo_nome_original
    except Exception as e:  # noqa: BLE001
        logger.warning("reenvio: falha ao buscar URL fresca no G-Click: %s", e)

    if not file_url:
        # Fallback: PDF salvo localmente no envio original
        pdf_local = original["pdf_local_path"] if "pdf_local_path" in original.keys() else None
        if pdf_local:
            local_path = config.PASTA_GUIAS.parent / pdf_local
            if local_path.exists():
                # Servimos via rota /d/{envio_id} pra uazapi conseguir baixar
                base = str(request.base_url).rstrip("/")
                file_url = f"{base}/d/{envio_id}"
                fonte = "PDF local"
        if not file_url:
            db.registrar_envio(cnpj=cnpj, whatsapp=original["whatsapp"],
                                tarefa_id=tarefa_id, atividade_id=atividade_id,
                                arquivo_nome=arquivo_nome_original, competencia=competencia,
                                uazapi_message_id=None, status="falha",
                                erro="Reenvio: G-Click sem anexo e nenhum PDF local salvo")
            return RedirectResponse(url="/auditoria?erro=Sem+PDF+disponivel", status_code=303)

    # 2) Reconstrói o `g` para gerar a legenda com template correto
    if atividade:
        g = {
            "tarefa_id": tarefa_id, "atividade_id": atividade_id,
            "atividade_nome": atividade.get("nome") or "",
            "obrigacao_nome": "",  # desconhecido sem buscar tarefa — legenda cai no fallback
            "arquivo_nome": arquivo_nome, "arquivo_url": file_url,
            "cnpj": cnpj, "cliente_apelido": "",
            "data_vencimento": "", "competencia": competencia,
            "status_tarefa": "",
        }
    else:
        g = {
            "tarefa_id": tarefa_id, "atividade_id": atividade_id,
            "atividade_nome": "", "obrigacao_nome": "",
            "arquivo_nome": arquivo_nome, "arquivo_url": file_url,
            "cnpj": cnpj, "cliente_apelido": "",
            "data_vencimento": "", "competencia": competencia,
            "status_tarefa": "",
        }

    # Valida o vencimento no PDF (mesma política do /enviar) antes de gerar legenda.
    venc_pdf_str: str | None = None
    venc_gclick_str = (g.get("data_vencimento") or "")[:10]
    cls_re = tipos_mod.classificar(g.get("arquivo_nome") or "",
                                   g.get("atividade_nome") or "",
                                   g.get("obrigacao_nome") or "")
    tipo_row_re = db.get_tipo_por_codigo(cls_re[0]) if cls_re else None
    if tipo_row_re and tipo_row_re["tem_vencimento"]:
        _, venc_pdf_str = helpers.validar_vencimento_no_pdf(g)

    caption = helpers.legenda(g)

    # Número de destino: SEMPRE o atual do cadastro do cliente (o usuário pode
    # ter corrigido o telefone depois do envio original — reenvio precisa pegar
    # a versão nova, não o número congelado no registro antigo). Fallback no
    # número do envio original só se o cliente sumiu do cadastro.
    cli_atual = db.get_cliente(cnpj)
    wpp_atual = (cli_atual["whatsapp"] or "").strip() if cli_atual else ""
    wpp = wpp_atual or (original["whatsapp"] or "")
    if not wpp:
        return RedirectResponse(url="/auditoria?erro=Envio+sem+WhatsApp", status_code=303)

    # Valida o formato (barra fixo/inválido antes de reenviar errado de novo).
    ok_num, motivo_num = helpers.validar_whatsapp_br(wpp)
    if not ok_num:
        db.registrar_envio(cnpj=cnpj, whatsapp=wpp,
                            tarefa_id=tarefa_id, atividade_id=atividade_id,
                            arquivo_nome=arquivo_nome, competencia=competencia,
                            uazapi_message_id=None, status="bloqueado",
                            erro=f"Reenvio bloqueado: {motivo_num}")
        return RedirectResponse(
            url="/auditoria?erro=Numero+invalido+%E2%80%94+corrija+o+cadastro+do+cliente",
            status_code=303,
        )

    if not config.uazapi_configurado():
        db.registrar_envio(cnpj=cnpj, whatsapp=wpp,
                            tarefa_id=tarefa_id, atividade_id=atividade_id,
                            arquivo_nome=arquivo_nome, competencia=competencia,
                            uazapi_message_id=None, status="simulado",
                            erro="Reenvio simulado — uazapi nao configurada")
        return RedirectResponse(url="/auditoria?sucesso=Reenvio+simulado", status_code=303)

    try:
        cfg = config.get_throttle_runtime()
        modo_envio = cfg.get("modo_envio", "anexo")
        delay_ms = cfg["delay_uazapi_ms"]
        if modo_envio == "link":
            # Mesmo formato da fila: botão "Abrir documento" com link RASTREADO
            # (/g/{token}) quando há domínio público; senão cai no S3 cru (dev).
            msg_texto = helpers.mensagem_documento(g)
            if config.PUBLIC_BASE_URL:
                token = secrets.token_urlsafe(16)
                link = config.url_publica(f"/g/{token}")
            else:
                token = None
                link = file_url
            resp = helpers.enviar_botao_com_retry(
                numero=wpp, texto=msg_texto,
                botao_texto="📄 Abrir documento",
                url=link,
                footer="NESCON CONTABILIDADE",
                delay_ms=delay_ms,
            )
        else:
            token = None
            resp = helpers.enviar_com_retry(
                numero=wpp, file_url=file_url,
                doc_name=arquivo_nome, caption=caption,
                delay_ms=delay_ms,
            )
        msg_id = (resp.get("messageid") or resp.get("id")
                  or (resp.get("message", {}) or {}).get("id"))
        novo_envio_id = db.registrar_envio(cnpj=cnpj, whatsapp=wpp,
                            tarefa_id=tarefa_id, atividade_id=atividade_id,
                            arquivo_nome=arquivo_nome, competencia=competencia,
                            uazapi_message_id=str(msg_id) if msg_id else None,
                            status="ok", erro=f"Reenvio {modo_envio} ({fonte})")
        # Backup local também no reenvio (se veio do G-Click; do local já está salvo).
        # É o que o /g/{token}/ver vai servir (permanente).
        if fonte == "G-Click":
            helpers.baixar_pdf_local(novo_envio_id, file_url, arquivo_nome)
        if token:
            db.set_envio_token(novo_envio_id, token)
        # Grava vencimentos auditados
        if venc_pdf_str or venc_gclick_str:
            db.set_envio_vencimentos(novo_envio_id, venc_pdf_str, venc_gclick_str)
        helpers.marcar_envio_realizado()
        return RedirectResponse(url=f"/auditoria?sucesso=Reenviado+via+{fonte}",
                                status_code=303)
    except uazapi.UazapiTokenInvalido as e:
        db.registrar_envio(cnpj=cnpj, whatsapp=wpp,
                            tarefa_id=tarefa_id, atividade_id=atividade_id,
                            arquivo_nome=arquivo_nome, competencia=competencia,
                            uazapi_message_id=None, status="token_invalido",
                            erro=f"Reenvio: {str(e)[:400]}")
        return RedirectResponse(
            url="/auditoria?erro=Token+uazapi+invalido+%E2%80%94+atualize+o+token+no+.env",
            status_code=303,
        )
    except Exception as e:
        db.registrar_envio(cnpj=cnpj, whatsapp=wpp,
                            tarefa_id=tarefa_id, atividade_id=atividade_id,
                            arquivo_nome=arquivo_nome, competencia=competencia,
                            uazapi_message_id=None, status="falha",
                            erro=f"Reenvio falhou: {str(e)[:400]}")
        return RedirectResponse(url="/auditoria?erro=Falhou", status_code=303)
