"""Helpers de domínio compartilhados pelas rotas.

Tudo que antes vivia solto no `main.py` (cache de guias, formatadores de data,
validações pré-envio, montagem de legenda, backup local do PDF) foi centralizado
aqui para que cada router em `app/routes/` fique enxuto.
"""

from __future__ import annotations

import logging
import re
import threading
import time as _time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import httpx

from . import config, db, gclick, tipos as tipos_mod, uazapi

logger = logging.getLogger("gclick.app")

MESES_PT = ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
            "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"]


def competencia_label(comp: str) -> str:
    """'2026-06' -> 'Junho/2026'"""
    try:
        y, m = comp.split("-")
        return f"{MESES_PT[int(m)-1]}/{y}"
    except Exception:
        return comp


# ---------- cache em memória das guias do G-Click ----------
# (competencia, obrigacao) -> (timestamp, [(tarefa, atividades), ...])
_CACHE_TTL_S = 60
_cache_guias: dict[tuple[str, str], tuple[float, list[tuple[dict, list[dict]]]]] = {}


def carregar_tarefas_e_ativs(competencia: str, obrigacao: str | None,
                             forcar: bool = False) -> list[tuple[dict, list[dict]]]:
    """Busca tarefas + atividades no G-Click, com cache de TTL curto.

    O cache (60s) só evita refazer a MESMA consulta quando o usuário alterna
    entre Dashboard/Fila/Check em menos de 1 minuto — não mostra dado "velho"
    de propósito. `forcar=True` (botão Atualizar) ignora o cache.
    """
    chave = (competencia, obrigacao or "")
    agora = _time.time()
    if not forcar:
        cached = _cache_guias.get(chave)
        if cached and agora - cached[0] < _CACHE_TTL_S:
            return cached[1]
    d_ini, d_fim = range_competencia(competencia)
    tarefas = gclick.listar_tarefas_obrigacoes(
        data_vencimento_inicio=d_ini,
        data_vencimento_fim=d_fim,
        nome=obrigacao,
    )
    with ThreadPoolExecutor(max_workers=16) as pool:
        ativs_por_tarefa = list(pool.map(lambda t: gclick.listar_atividades(t["id"]), tarefas))
    dados = list(zip(tarefas, ativs_por_tarefa))
    _cache_guias[chave] = (agora, dados)
    return dados


def invalidar_cache_guias() -> None:
    _cache_guias.clear()


def prewarm() -> None:
    """Pré-aquece o cache do mês atual numa thread de fundo, no startup.

    Assim a 1ª visita ao Dashboard/Fila/Check já encontra o dado quente, sem
    bloquear a subida do servidor. Falhas (G-Click fora) são silenciosas.
    """
    def _run() -> None:
        try:
            hoje = date.today()
            comp = f"{hoje.year:04d}-{hoje.month:02d}"
            carregar_tarefas_e_ativs(comp, None)
            logger.info("prewarm do cache de guias concluído (%s)", comp)
        except Exception as e:  # noqa: BLE001 — best-effort
            logger.warning("prewarm do cache de guias falhou: %s", e)

    threading.Thread(target=_run, name="prewarm-guias", daemon=True).start()


def iniciar_atualizador_periodico() -> None:
    """Re-busca o mês atual no G-Click a cada `REFRESH_INTERVAL_H` horas.

    Mantém o cache do mês corrente sempre quente — pensado para a VPS, onde o
    servidor fica de pé o tempo todo: ninguém precisa "esperar carregar". Local,
    fica desligado por padrão (REFRESH_INTERVAL_H=0). Cada ciclo usa `forcar`
    para realmente trazer dado fresco do G-Click.
    """
    horas = config.REFRESH_INTERVAL_H
    if horas <= 0:
        return
    intervalo_s = horas * 3600

    def _loop() -> None:
        while True:
            _time.sleep(intervalo_s)
            try:
                hoje = date.today()
                comp = f"{hoje.year:04d}-{hoje.month:02d}"
                carregar_tarefas_e_ativs(comp, None, forcar=True)
                logger.info("atualização periódica do cache concluída (%s)", comp)
            except Exception as e:  # noqa: BLE001 — best-effort
                logger.warning("atualização periódica do cache falhou: %s", e)

    threading.Thread(target=_loop, name="refresh-guias", daemon=True).start()
    logger.info("atualizador periódico ligado: a cada %.1fh", horas)


# ---------- envio automático por gatilho do G-Click (Fase 2) ----------
# Gatilho primário: atividade "Enviar para o Cliente" respondida (= liberada ao
# cliente, o "e-mail enviado"). Fallback: tarefa concluída (status="C").

_PADROES_ENVIAR_CLIENTE = ("enviar para o cliente", "enviar ao cliente",
                           "enviado ao cliente", "enviar cliente")


def _norm_ts(s: str | None) -> str:
    """Normaliza timestamp do G-Click ('2026-01-15 09:52' ou ISO com 'T') para
    comparação lexical estável até o minuto."""
    return (s or "").replace("T", " ")[:16]


def _gatilho_disparado(tarefa: dict, ativs: list[dict], gatilho: str) -> str | None:
    """Devolve o timestamp do gatilho (string) se disparou, senão None."""
    if gatilho == "concluida":
        if (tarefa.get("status") or "") == "C":
            return tarefa.get("dataConclusao") or tarefa.get("dataAcao") or ""
        return None
    # Primário: atividade "Enviar para o Cliente" respondida.
    for a in ativs:
        nome = (a.get("nome") or "").strip().lower()
        if a.get("respondida") and any(p in nome for p in _PADROES_ENVIAR_CLIENTE):
            return a.get("respondidaEm") or ""
    return None


def guias_elegiveis_auto(competencia: str, gatilho: str, corte_iso: str) -> list[dict]:
    """Guias cuja tarefa já disparou o gatilho com timestamp >= corte.
    NÃO filtra opt-in/enviadas — isso é responsabilidade do worker."""
    corte = _norm_ts(corte_iso)
    dados = carregar_tarefas_e_ativs(competencia, None)
    elegiveis: list[dict] = []
    for tarefa, ativs in dados:
        ts = _gatilho_disparado(tarefa, ativs, gatilho)
        if ts is None or _norm_ts(ts) < corte:
            continue
        elegiveis.extend(gclick.extrair_guias_pendentes(tarefa, ativs))
    return elegiveis


def _ciclo_auto_envio(cfg: dict) -> None:
    """Um ciclo do worker: identifica guias elegíveis (gatilho + opt-in + não
    enviadas) e as coloca na CAIXA DE SAÍDA para aprovação manual.
    NÃO envia nada — o envio só acontece quando o operador aprova em /aprovacoes."""
    hoje = date.today()
    comp = f"{hoje.year:04d}-{hoje.month:02d}"
    corte = db.get_config("auto_ativado_em") or hoje.isoformat()

    elegiveis = guias_elegiveis_auto(comp, cfg["gatilho"], corte)
    auto_cnpjs = {c["cnpj"] for c in db.listar_clientes_auto()}
    whatsapp_map = db.map_whatsapp_por_cnpj()
    enviadas = db.chaves_enviadas()
    alvos = [
        g for g in elegiveis
        if (g["cnpj"] or "") in auto_cnpjs
        and g.get("arquivo_url") and whatsapp_map.get(g["cnpj"] or "")
        and ((g["cnpj"] or ""), g["tarefa_id"], g["atividade_id"]) not in enviadas
    ]
    # Enfileira (idempotente — a UNIQUE da tabela evita duplicar).
    novas = sum(1 for g in alvos if db.enfileirar_aprovacao(g))
    pendentes = db.contar_aprovacoes_pendentes()
    db.set_config("auto_ultima_exec", db.agora_iso())
    db.set_config(
        "auto_ultimo_resultado",
        f"{novas} nova(s) na Caixa de Saída · {pendentes} aguardando aprovação"
        if novas else f"nada novo · {pendentes} aguardando aprovação",
    )
    logger.info("ciclo auto-envio: %d nova(s) enfileirada(s), %d pendente(s) (gatilho=%s)",
                novas, pendentes, cfg["gatilho"])


def iniciar_worker_auto_envio() -> None:
    """Worker de polling do envio automático. Autocontrolado por `auto_envio_ativo`
    (desligado = só dorme). Erros nunca derrubam a thread."""
    def _loop() -> None:
        _time.sleep(30)  # deixa o boot/prewarm respirarem
        while True:
            intervalo_s = 900
            try:
                cfg = config.get_auto_envio_runtime()
                intervalo_s = max(60, int(cfg["intervalo_min"]) * 60)
                if cfg["ativo"]:
                    _ciclo_auto_envio(cfg)
            except Exception as e:  # noqa: BLE001 — best-effort, não morre
                logger.warning("worker auto-envio: ciclo falhou: %s", e)
                db.set_config("auto_ultimo_resultado", f"erro: {str(e)[:120]}")
            _time.sleep(intervalo_s)

    threading.Thread(target=_loop, name="auto-envio", daemon=True).start()
    logger.info("worker de envio automático ligado")


def mapa_tipos() -> dict:
    """Todos os tipos padrão indexados por código — 1 query em vez de N.

    Evita o N+1 de `db.get_tipo_por_codigo()` chamado dentro do loop de guias
    (o Dashboard fazia ~130 conexões SQLite por carga).
    """
    return {t["codigo"]: t for t in db.listar_tipos()}


def competencias_opcoes(ref: str) -> list[dict]:
    """Gera lista de 12 meses centrada na data de hoje."""
    hoje = date.today()
    base_ano, base_mes = hoje.year, hoje.month
    opcoes = []
    for offset in range(-6, 6):
        m = base_mes + offset
        y = base_ano + (m - 1) // 12
        m = ((m - 1) % 12) + 1
        valor = f"{y:04d}-{m:02d}"
        label = f"{MESES_PT[m-1]}/{y}"
        opcoes.append({"valor": valor, "label": label})
    if not any(o["valor"] == ref for o in opcoes):
        y, m = int(ref[:4]), int(ref[5:7])
        opcoes.insert(0, {"valor": ref, "label": f"{MESES_PT[m-1]}/{y}"})
    return opcoes


def range_competencia(comp: str) -> tuple[str, str]:
    """'2026-06' -> ('2026-06-01', '2026-06-30')."""
    y, m = int(comp[:4]), int(comp[5:7])
    inicio = date(y, m, 1)
    if m == 12:
        fim = date(y, 12, 31)
    else:
        from datetime import timedelta
        fim = date(y, m + 1, 1) - timedelta(days=1)
    return inicio.isoformat(), fim.isoformat()


def fmt_data(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        d = date.fromisoformat(iso[:10])
        return d.strftime("%d/%m/%Y")
    except Exception:
        return iso


def fmt_dt(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return iso


def fmt_whatsapp(num: str | None) -> str:
    if not num or len(num) < 12:
        return num or ""
    return f"+{num[:2]} ({num[2:4]}) {num[4:9]}-{num[9:]}"


def chave(g: dict) -> str:
    return f"{g['tarefa_id']}|{g['atividade_id']}"


# ---------- camadas de segurança antes do envio real ----------
# 1) Formato do número (55 + DDD + 8-9 dígitos).
# 2) Anti-auto-envio (não mandar pro número conectado na própria instância).
# 3) `obrigacoes_aceitas` do cliente (se preenchido, só envia tipos listados).

# Celular brasileiro: 55 + DDD + 9 + 8 dígitos = 13 dígitos.
# WhatsApp só funciona em celular — números fixos (12 dígitos, sem o "9")
# são reconhecidos separadamente para devolver mensagem amigável.
_RE_WPP_CELULAR = re.compile(r"^55(?:1[1-9]|[2-9][1-9])9\d{8}$")
_RE_WPP_FIXO_BR = re.compile(r"^55(?:1[1-9]|[2-9][1-9])\d{8}$")

# Cache simples do owner da instância (consultado uma vez por bloco de envios)
_owner_cache: dict = {"valor": None, "ts": 0.0}


def owner_uazapi() -> str | None:
    if _time.time() - _owner_cache["ts"] < 120:
        return _owner_cache["valor"]
    try:
        d = uazapi.testar_conexao()
        owner = (d.get("detalhes") or {}).get("owner")
    except Exception:
        owner = None
    _owner_cache["valor"] = owner
    _owner_cache["ts"] = _time.time()
    return owner


def validar_whatsapp_br(numero: str | None) -> tuple[bool, str]:
    """Aceita só celular brasileiro com 13 dígitos (55+DDD+9+8 dígitos).
    Rejeita fixo com mensagem específica para a auditoria ficar clara.
    Retorna (ok, motivo)."""
    if not numero:
        return False, "WhatsApp em branco"
    digits = "".join(ch for ch in numero if ch.isdigit())
    if _RE_WPP_CELULAR.match(digits):
        return True, ""
    if _RE_WPP_FIXO_BR.match(digits):
        return False, (
            "Provável telefone FIXO (12 dígitos sem o 9). WhatsApp só funciona"
            " em celular. Atualize o cadastro do cliente com o número de celular."
        )
    return False, f"Formato inválido (esperado 55+DDD+9+8 dígitos, recebido {digits!r})"


def eh_proprio_numero(numero: str) -> bool:
    """Evita enviar pro número conectado na instância uazapi (self-send falha silenciosa)."""
    owner = owner_uazapi()
    return bool(owner) and owner == numero


def cliente_aceita_tipo(cnpj: str, codigo_tipo: str | None) -> bool:
    """Se o cliente tem `obrigacoes_aceitas` definido, só aceita os tipos listados.
    Vazio/NULL = aceita tudo."""
    cli = db.get_cliente(cnpj)
    if not cli or not cli["obrigacoes_aceitas"]:
        return True
    aceitos = {c.strip().upper() for c in cli["obrigacoes_aceitas"].split(",") if c.strip()}
    if not aceitos:
        return True
    return (codigo_tipo or "").upper() in aceitos


def baixar_pdf_local(envio_id: int, file_url: str, arquivo_nome: str,
                     bytes_ja_baixados: bytes | None = None) -> str | None:
    """Salva o PDF em data/guias/{envio_id:06d}_{nome}. Se `bytes_ja_baixados`
    for fornecido, evita uma nova requisição (já temos o conteúdo)."""
    try:
        conteudo = bytes_ja_baixados if bytes_ja_baixados is not None else gclick.baixar_pdf(file_url)
    except Exception as e:  # noqa: BLE001
        logger.warning("falha ao baixar PDF para backup local (envio %s): %s", envio_id, e)
        return None
    nome_seguro = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", arquivo_nome or "documento.pdf")
    destino = config.PASTA_GUIAS / f"{envio_id:06d}_{nome_seguro}"
    try:
        destino.write_bytes(conteudo)
    except Exception as e:  # noqa: BLE001
        logger.warning("falha ao gravar PDF local (envio %s): %s", envio_id, e)
        return None
    rel = str(destino.relative_to(config.PASTA_GUIAS.parent))
    db.set_envio_pdf_local(envio_id, rel)
    return rel


# ---------- rastreio de acesso: detecção de bot + geo-IP ----------

# User-agents de previews/crawlers — NÃO é o cliente abrindo de verdade.
# O preview do WhatsApp/Meta bate no link ao receber a mensagem; isso não pode
# contar como "abertura real".
_BOT_UAS = ("whatsapp", "facebookexternalhit", "facebot", "telegrambot",
            "twitterbot", "slackbot", "discordbot", "linkedinbot", "bingbot",
            "googlebot", "bot", "crawler", "spider", "preview", "curl",
            "wget", "python-httpx", "headless")


def eh_user_agent_bot(ua: str | None) -> bool:
    """True se o user-agent parece preview/crawler (WhatsApp/Meta/etc.).
    Sem user-agent também é tratado como suspeito (não conta como abertura real)."""
    if not ua:
        return True
    u = ua.lower()
    return any(b in u for b in _BOT_UAS)


_geo_cache: dict[str, dict] = {}


def geo_ip(ip: str | None) -> dict:
    """Cidade/estado/país de um IP via ip-api.com (grátis, sem chave).
    Best-effort: cacheado por IP, timeout curto, falha silenciosa. NUNCA quebra
    o acesso ao documento — geo é só enriquecimento."""
    vazio = {"cidade": None, "estado": None, "pais": None}
    if not ip or ip in ("127.0.0.1", "::1", "?", "localhost"):
        return vazio
    if ip in _geo_cache:
        return _geo_cache[ip]
    try:
        r = httpx.get(
            f"http://ip-api.com/json/{ip}",
            params={"fields": "status,country,regionName,city", "lang": "pt-BR"},
            timeout=4,
        )
        j = r.json() if r.status_code == 200 else {}
        res = ({"cidade": j.get("city"), "estado": j.get("regionName"),
                "pais": j.get("country")}
               if j.get("status") == "success" else vazio)
    except Exception:  # noqa: BLE001 — best-effort
        res = vazio
    _geo_cache[ip] = res
    return res


def ip_do_request(request) -> str:
    """IP real do cliente, considerando o proxy do EasyPanel (X-Forwarded-For)."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


def validar_vencimento_no_pdf(g: dict) -> tuple[bytes | None, str | None]:
    """Baixa o PDF, tenta parsear o vencimento.
    Se diferente do que G-Click diz, SOBRESCREVE g['data_vencimento'] e
    retorna (bytes_baixados, venc_pdf_iso) para reuso.
    Falhas são silenciosas — mantém o vencimento do G-Click."""
    url = g.get("arquivo_url")
    if not url:
        return None, None
    try:
        conteudo = gclick.baixar_pdf(url)
    except Exception as e:  # noqa: BLE001
        logger.warning("falha ao baixar PDF para validar vencimento: %s", e)
        return None, None
    try:
        from . import pdf_parser
        venc = pdf_parser.extrair_vencimento(conteudo)
    except Exception as e:  # noqa: BLE001
        logger.warning("falha ao parsear vencimento do PDF: %s", e)
        return conteudo, None
    if not venc:
        # Sem data confiável no PDF — política: NÃO usar a do G-Click (que é
        # divergente e confunde o cliente). Mantém só para auditoria interna.
        g["_venc_gclick_original"] = (g.get("data_vencimento") or "")[:10]
        g["data_vencimento"] = ""
        g["_venc_pdf"] = None
        g["_venc_divergente"] = True
        return conteudo, None
    venc_iso = venc.isoformat()
    venc_gclick = (g.get("data_vencimento") or "")[:10]
    if venc_iso != venc_gclick:
        g["data_vencimento"] = venc_iso
        g["_venc_divergente"] = True
        g["_venc_gclick_original"] = venc_gclick
    g["_venc_pdf"] = venc_iso
    return conteudo, venc_iso


# ---------- estado do lote de envio (progresso em tempo real) ----------
# Compartilhado entre a thread que processa o lote e a rota /enviar/status
# que devolve o snapshot pro JS poll. Como o app é single-user (uvicorn 1
# worker), 1 lote por vez basta — se uma segunda chamada chega com lote
# ativo, redireciona pro progresso em vez de iniciar outro.

import threading as _threading

_lote_lock = _threading.Lock()
_lote_state: dict = {
    "ativo": False,
    "iniciado_em": None,
    "destino": "/fila",          # URL para onde redirecionar ao terminar
    "total": 0,
    "feitos": 0,                 # processados (sucesso ou não)
    "enviados": 0,               # status=ok
    "bloqueados": 0,             # status=bloqueado/token_invalido
    "falhas": 0,                 # status=falha
    "atual_idx": 0,
    "atual_cliente": "",
    "atual_arquivo": "",
    "erro_fatal": None,
    "mensagens": [],             # lista de {ts, texto, tipo}
}
_LOTE_MSG_MAX = 30


def lote_ativo() -> bool:
    with _lote_lock:
        return bool(_lote_state.get("ativo"))


def lote_iniciar(total: int, destino: str = "/fila") -> None:
    """Reinicia o estado do lote para um novo bloco de envios."""
    with _lote_lock:
        _lote_state.clear()
        _lote_state.update({
            "ativo": True,
            "iniciado_em": _time.time(),
            "destino": destino,
            "total": total,
            "feitos": 0,
            "enviados": 0,
            "bloqueados": 0,
            "falhas": 0,
            "atual_idx": 0,
            "atual_cliente": "",
            "atual_arquivo": "",
            "erro_fatal": None,
            "mensagens": [],
        })


def lote_set_atual(idx: int, cliente: str, arquivo: str) -> None:
    """Sinaliza qual guia está sendo processada agora (mostra na tela)."""
    with _lote_lock:
        _lote_state["atual_idx"] = idx
        _lote_state["atual_cliente"] = cliente or ""
        _lote_state["atual_arquivo"] = arquivo or ""


def lote_marcar_resultado(status: str, mensagem: str | None = None) -> None:
    """Conta o resultado da guia atual e (opcional) registra mensagem amigável."""
    with _lote_lock:
        _lote_state["feitos"] += 1
        if status == "ok":
            _lote_state["enviados"] += 1
            tipo = "ok"
        elif status in ("bloqueado", "token_invalido"):
            _lote_state["bloqueados"] += 1
            tipo = "aviso"
        else:
            _lote_state["falhas"] += 1
            tipo = "erro"
        if mensagem:
            _lote_state["mensagens"].append({
                "ts": _time.time(),
                "texto": mensagem[:200],
                "tipo": tipo,
            })
            if len(_lote_state["mensagens"]) > _LOTE_MSG_MAX:
                _lote_state["mensagens"] = _lote_state["mensagens"][-_LOTE_MSG_MAX:]


def lote_finalizar(erro_fatal: str | None = None) -> None:
    """Fecha o lote — JS detecta `ativo=False` e dispara o redirect."""
    with _lote_lock:
        _lote_state["ativo"] = False
        _lote_state["erro_fatal"] = erro_fatal
        _lote_state["atual_cliente"] = ""
        _lote_state["atual_arquivo"] = ""


def lote_snapshot() -> dict:
    """Cópia do estado atual para JSON. Acrescenta tempo decorrido."""
    with _lote_lock:
        s = dict(_lote_state)
    if s.get("iniciado_em"):
        s["decorrido_s"] = round(_time.time() - s["iniciado_em"], 1)
    return s


# ---------- controle de ritmo de envio (anti-bloqueio do número) ----------

_envios_ts: list[float] = []


def sob_teto_horario() -> bool:
    """True se ainda dá pra enviar dentro do teto da última hora.
    Lê o teto da configuração runtime (UI tem prioridade sobre .env)."""
    agora = _time.time()
    while _envios_ts and agora - _envios_ts[0] > 3600:
        _envios_ts.pop(0)
    teto = config.get_throttle_runtime()["max_por_hora"]
    return len(_envios_ts) < teto


def marcar_envio_realizado() -> None:
    """Registra o instante de um envio real para a contagem do teto/hora."""
    _envios_ts.append(_time.time())


def enviar_com_retry(*, numero: str, file_url: str, doc_name: str,
                     caption: str | None, tentativas: int = 2,
                     delay_ms: int | None = None) -> dict:
    """Envia pela uazapi com 1 re-tentativa em falha transitória (rede/5xx).

    NÃO repete em token inválido/instância desconectada (inútil — precisa de
    ação humana). Lança a última exceção se todas as tentativas falharem.
    `delay_ms` (opcional): repassa pra uazapi pra mostrar "digitando..." antes.
    """
    ultimo_erro: Exception | None = None
    for i in range(max(1, tentativas)):
        try:
            return uazapi.enviar_documento(
                numero=numero, file_url=file_url, doc_name=doc_name,
                caption=caption, delay_ms=delay_ms,
            )
        except uazapi.UazapiTokenInvalido:
            raise  # não abatei repetir
        except Exception as e:  # noqa: BLE001 — falha transitória, tenta de novo
            ultimo_erro = e
            logger.warning("envio falhou (tentativa %d/%d): %s", i + 1, tentativas, e)
            if i + 1 < tentativas:
                _time.sleep(0.8 * (i + 1))
    assert ultimo_erro is not None
    raise ultimo_erro


def enviar_link_com_retry(*, numero: str, texto: str,
                          link_url: str | None = None,
                          link_title: str | None = None,
                          tentativas: int = 2,
                          delay_ms: int | None = None) -> dict:
    """Envia texto com hyperlink pela uazapi com 1 re-tentativa.

    `texto`: mensagem com o link embedado.
    `link_url`: URL do hyperlink (se None, extrai do texto).
    `link_title`: título personalizado do preview.
    """
    ultimo_erro: Exception | None = None
    for i in range(max(1, tentativas)):
        try:
            return uazapi.enviar_texto(
                numero=numero, texto=texto,
                link_url=link_url, link_title=link_title,
                delay_ms=delay_ms,
            )
        except uazapi.UazapiTokenInvalido:
            raise
        except Exception as e:  # noqa: BLE001
            ultimo_erro = e
            logger.warning("envio link falhou (tentativa %d/%d): %s", i + 1, tentativas, e)
            if i + 1 < tentativas:
                _time.sleep(0.8 * (i + 1))
    assert ultimo_erro is not None
    raise ultimo_erro


# Cache simples de URLs já encurtadas (a S3 do G-Click muda a cada envio, mas
# dentro de um mesmo lote a mesma URL pode reaparecer; evita 2ª chamada à API).
_short_cache: dict[str, str] = {}


def encurtar_url(url: str) -> str:
    """Encurta uma URL longa. tinyurl como primário, is.gd como fallback.

    Por que: no WhatsApp um link só é clicável se a URL aparecer em texto puro.
    A URL S3 do G-Click tem ~300+ chars (poluiria a mensagem). Encurtar permite
    um link curto e clicável. Se ambos os encurtadores falharem, devolve a URL
    original (melhor um link grande que nenhum link).
    """
    if not url:
        return url
    if url in _short_cache:
        return _short_cache[url]

    enc = urllib.parse.quote(url, safe="")
    # 1) tinyurl (sem chave, retorna o short em texto puro)
    try:
        r = httpx.get(f"https://tinyurl.com/api-create.php?url={enc}", timeout=10)
        if r.status_code == 200 and r.text.strip().startswith("http"):
            curto = r.text.strip()
            _short_cache[url] = curto
            return curto
    except Exception as e:  # noqa: BLE001
        logger.warning("tinyurl falhou: %s", e)
    # 2) is.gd (fallback)
    try:
        r = httpx.get(f"https://is.gd/create.php?format=simple&url={enc}", timeout=10)
        if r.status_code == 200 and r.text.strip().startswith("http"):
            curto = r.text.strip()
            _short_cache[url] = curto
            return curto
    except Exception as e:  # noqa: BLE001
        logger.warning("is.gd falhou: %s", e)
    # 3) fallback: a URL original (grande, mas funcional)
    logger.warning("encurtadores indisponíveis; usando URL original")
    return url


def mensagem_link(g: dict, link: str) -> str:
    """[legado] Mensagem de texto com o link no corpo (encurtado).
    Mantida para fallback; o fluxo atual usa o BOTÃO de URL (mensagem_documento)."""
    competencia = competencia_label(g.get("competencia") or "")
    tipo_doc = g.get("obrigacao_nome") or g.get("atividade_nome") or "Documento"
    venc = fmt_data(g.get("data_vencimento"))

    linhas = [f"📄 *{tipo_doc}* — {competencia}", ""]
    if venc:
        linhas.append(f"🗓 Vencimento: {venc}")
        linhas.append("")
    linhas.append("Acesse o documento pelo link abaixo:")
    linhas.append(link)
    linhas.append("")
    linhas.append("Qualquer dúvida, estamos à disposição.")
    return "\n".join(linhas)


def titulo_documento(g: dict) -> str:
    """Título amigável para o cliente (NÃO a descrição interna da tarefa).

    Usa o `nome` do tipo classificado (FGTS, INSS (DCTF Web), Extrato da Folha,
    Recibos da Folha...). Classifica por arquivo > atividade > obrigação — o
    mesmo critério da legenda do anexo. Fallback: nome do arquivo ou "Documento".
    """
    cls = tipos_mod.classificar(
        g.get("arquivo_nome") or "",
        g.get("atividade_nome") or "",
        g.get("obrigacao_nome") or "",
    )
    if cls:
        return cls[1]  # nome amigável do tipo
    return g.get("arquivo_nome") or "Documento"


def mensagem_documento(g: dict) -> str:
    """Texto informativo para envio por BOTÃO de URL.

    O link NÃO entra aqui — vai no botão "Abrir documento". Este texto só
    contextualiza (tipo, competência, vencimento). Mesma regra de vencimento do
    anexo: só mostra a data se veio do PDF (já tratada em validar_vencimento_no_pdf).
    """
    competencia = competencia_label(g.get("competencia") or "")
    tipo_doc = titulo_documento(g)
    venc = fmt_data(g.get("data_vencimento"))

    linhas = [f"📄 *{tipo_doc}* — {competencia}", ""]
    if venc:
        linhas.append(f"🗓 Vencimento: {venc}")
        linhas.append("")
    linhas.append("Sua guia está disponível. Toque no botão abaixo para abrir o documento.")
    return "\n".join(linhas)


def enviar_botao_com_retry(*, numero: str, texto: str, botao_texto: str, url: str,
                           footer: str | None = None, tentativas: int = 2,
                           delay_ms: int | None = None) -> dict:
    """Envia mensagem com botão de URL pela uazapi, com 1 re-tentativa em falha
    transitória. Não repete em token inválido."""
    ultimo_erro: Exception | None = None
    for i in range(max(1, tentativas)):
        try:
            return uazapi.enviar_botao_url(
                numero=numero, texto=texto, botao_texto=botao_texto,
                url=url, footer=footer, delay_ms=delay_ms,
            )
        except uazapi.UazapiTokenInvalido:
            raise
        except Exception as e:  # noqa: BLE001
            ultimo_erro = e
            logger.warning("envio botão falhou (tentativa %d/%d): %s", i + 1, tentativas, e)
            if i + 1 < tentativas:
                _time.sleep(0.8 * (i + 1))
    assert ultimo_erro is not None
    raise ultimo_erro


def legenda(g: dict) -> str:
    """Monta a legenda da mensagem.
    Se a guia foi classificada num tipo padrão com `template_mensagem`, usa esse
    template (formatado com placeholders). Senão, usa um fallback:
    - tem_vencimento: "Segue a guia X referente a... Vencimento: ..."
    - sem vencimento: "Segue o X referente a..."
    """
    comp = g.get("competencia") or ""
    if len(comp) == 7:
        y, m = comp.split("-")
        comp_fmt = f"{MESES_PT[int(m)-1]}/{y}"
    else:
        comp_fmt = comp

    venc_fmt = fmt_data(g.get("data_vencimento"))
    cliente = g.get("cliente_apelido") or ""
    arquivo = g.get("arquivo_nome") or ""
    atividade = g.get("atividade_nome") or ""

    # Classifica para pegar template e tem_vencimento.
    # Ordem: arquivo (o que o cliente VÊ) > atividade > obrigação.
    cls = tipos_mod.classificar(arquivo, atividade, g.get("obrigacao_nome") or "")
    tipo_row = db.get_tipo_por_codigo(cls[0]) if cls else None
    tipo_nome = tipo_row["nome"] if tipo_row else atividade
    template = (tipo_row["template_mensagem"] if tipo_row else None) or ""
    tem_venc = bool(tipo_row["tem_vencimento"]) if tipo_row else True

    placeholders = {
        "cliente": cliente, "competencia": comp_fmt, "vencimento": venc_fmt,
        "tipo": tipo_nome, "arquivo": arquivo, "atividade": atividade,
    }
    if template.strip():
        # Se não temos vencimento confiável, remove qualquer linha que
        # mencione {vencimento} — evita "Vencimento: ." vazio na mensagem.
        eff = template
        if not venc_fmt:
            eff = "\n".join(l for l in eff.split("\n") if "{vencimento}" not in l)
        try:
            return eff.format(**placeholders)
        except KeyError:
            pass  # fallback abaixo

    if tem_venc and venc_fmt:
        return (f"Olá! Segue a guia *{tipo_nome}* referente a {comp_fmt}.\n"
                f"Vencimento: {venc_fmt}.\n"
                f"Qualquer dúvida estamos à disposição.")
    # Sem vencimento (não-tributário OU PDF não trouxe data confiável)
    return (f"Olá! Segue o *{tipo_nome}* referente a {comp_fmt}.\n"
            f"Qualquer dúvida estamos à disposição.")
