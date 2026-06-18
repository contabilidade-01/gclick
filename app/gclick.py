"""Cliente da API Omie.G-Click — endpoints validados em 2026-06-15."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from . import config


@dataclass
class _TokenCache:
    valor: str = ""
    expira_em: float = 0.0


_cache = _TokenCache()

# Cliente HTTP compartilhado: mantem pool de conexoes vivas (keep-alive),
# elimina TLS handshake a cada chamada (era o gargalo nas 50+ chamadas paralelas).
_client = httpx.Client(
    base_url=config.GCLICK_BASE_URL,
    timeout=30,
    limits=httpx.Limits(max_keepalive_connections=20, max_connections=30),
    http2=False,
)


def _autenticar() -> str:
    """Devolve access_token válido, com cache (renova ~30s antes de expirar)."""
    agora = time.time()
    if _cache.valor and _cache.expira_em - 30 > agora:
        return _cache.valor

    cid, csec = config.gclick_credentials()
    r = _client.post(
        "/oauth/token",
        data={
            "client_id": cid,
            "client_secret": csec,
            "grant_type": "client_credentials",
        },
    )
    r.raise_for_status()
    j = r.json()
    _cache.valor = j["access_token"]
    _cache.expira_em = agora + int(j.get("expires_in", 3599))
    return _cache.valor


def _get(path: str, params: dict | None = None) -> Any:
    token = _autenticar()
    r = _client.get(
        path,
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    )
    r.raise_for_status()
    return r.json()


def listar_tarefas_obrigacoes(
    *,
    data_vencimento_inicio: str,
    data_vencimento_fim: str,
    nome: str | None = None,
    clientes_inscricoes: str | None = None,
    size: int = 500,
) -> list[dict]:
    """Lista tarefas de obrigação por vencimento. Paralela paginação.

    `nome` filtra pelo nome da obrigação (suporta múltiplos separados por vírgula).
    `clientes_inscricoes` filtra por CNPJ (idem múltiplos).
    """
    from concurrent.futures import ThreadPoolExecutor

    base_params: dict[str, Any] = {
        "categoria": "Obrigacao",
        "dataVencimentoInicio": data_vencimento_inicio,
        "dataVencimentoFim": data_vencimento_fim,
        "size": size,
    }
    if nome:
        base_params["nome"] = nome
    if clientes_inscricoes:
        base_params["clientesInscricoes"] = clientes_inscricoes

    # Página 0 para descobrir totalPages
    j0 = _get("/tarefas", {**base_params, "page": 0})
    todos: list[dict] = list(j0.get("content", []))
    total_pages = j0.get("totalPages", 1)
    if total_pages <= 1:
        return todos

    # Páginas restantes em paralelo
    with ThreadPoolExecutor(max_workers=8) as pool:
        pags = list(pool.map(
            lambda p: _get("/tarefas", {**base_params, "page": p}),
            range(1, total_pages),
        ))
    for j in pags:
        todos.extend(j.get("content", []))
    return todos


def listar_atividades(tarefa_id: str) -> list[dict]:
    return _get(f"/tarefas/{tarefa_id}/atividades")


def listar_clientes(size: int = 200) -> list[dict]:
    """Lista todos os clientes do G-Click. Pagina automaticamente em paralelo.
    Default size=200 normalmente fecha tudo em 1 página.
    """
    from concurrent.futures import ThreadPoolExecutor

    j0 = _get("/clientes", {"size": size, "page": 0})
    if not isinstance(j0, dict):
        return j0 if isinstance(j0, list) else []
    todos: list[dict] = list(j0.get("content", []))
    total_pages = j0.get("totalPages", 1)
    if total_pages <= 1:
        return todos

    with ThreadPoolExecutor(max_workers=4) as pool:
        pags = list(pool.map(
            lambda p: _get("/clientes", {"size": size, "page": p}),
            range(1, total_pages),
        ))
    for j in pags:
        if isinstance(j, dict):
            todos.extend(j.get("content", []))
    return todos


def extrair_dados_cliente(c: dict) -> dict:
    """Normaliza os campos de um cliente do G-Click para o nosso modelo.

    - whatsapp: pega o 1o telefone, prefixa com '55' se não tiver DDI.
    - email: pega o 1o email com categoria 'Departamento Pessoal' (preferido),
      senão o primeiro.
    - responsavel_nome: nome do contato no telefone (ou no email).
    """
    cnpj = (c.get("inscricao") or "").strip()
    apelido = (c.get("apelido") or c.get("nome") or "").strip()
    nome_completo = (c.get("nome") or apelido).strip()
    status = c.get("status") or ""

    tels = c.get("telefones") or []
    whatsapp = None
    responsavel_nome = None
    if tels:
        primeiro = tels[0]
        num_bruto = "".join(ch for ch in (primeiro.get("numero") or "") if ch.isdigit())
        if num_bruto:
            # Se tem 10 ou 11 dígitos, é número BR sem DDI — prefixa 55.
            if len(num_bruto) in (10, 11) and not num_bruto.startswith("55"):
                whatsapp = "55" + num_bruto
            else:
                whatsapp = num_bruto
        responsavel_nome = (primeiro.get("nome") or "").strip() or None

    emails = c.get("emails") or []
    email = None
    if emails:
        # Preferir o email categorizado como Departamento Pessoal (categoriaIds inclui 1)
        dp = next((e for e in emails if 1 in (e.get("categoriaIds") or [])), None)
        escolhido = dp or emails[0]
        email = (escolhido.get("email") or "").strip() or None
        if not responsavel_nome:
            responsavel_nome = (escolhido.get("nome") or "").strip() or None

    return {
        "cnpj": cnpj,
        "apelido": apelido,
        "nome_completo": nome_completo,
        "whatsapp": whatsapp,
        "email": email,
        "responsavel_nome": responsavel_nome,
        "status_gclick": status,
    }


def baixar_pdf(url: str) -> bytes:
    """Baixa o PDF da URL S3 pré-assinada do G-Click. Não precisa de header.
    Usa httpx puro porque a URL é externa (S3), nao a base_url do client."""
    r = httpx.get(url, timeout=60)
    r.raise_for_status()
    return r.content


# ---------- helpers de domínio ----------

def extrair_guias_pendentes(tarefa: dict, atividades: list[dict]) -> list[dict]:
    """De uma tarefa + suas atividades, devolve a lista de "guias" (atividade com arquivo).

    Modelo de retificação no G-Click: quando há retificação, uma NOVA atividade
    é criada com o mesmo `nome` (ex.: "Anexar recibo de pagamento" original +
    "Anexar recibo de pagamento" retificadora). Esta função agrupa por nome e
    devolve apenas a versão mais recente (maior `respondidaEm`), anotando se
    houve retificação e quantas versões anteriores existem.

    Cada item: {
      tarefa_id, atividade_id, atividade_nome, arquivo_nome, arquivo_url,
      cnpj, cliente_apelido, obrigacao_nome, data_vencimento, status_tarefa,
      competencia, eh_retificada (bool), num_versoes (int >= 1),
      versoes_anteriores: [{atividade_id, respondidaEm, arquivo_nome, arquivo_url}]
    }
    """
    competencia = (tarefa.get("dataVencimento") or "")[:7]  # YYYY-MM

    # Agrupa atividades de upload (tem arquivos e respondida) pelo `nome`.
    por_nome: dict[str, list[dict]] = {}
    for a in atividades:
        arqs = a.get("arquivos") or []
        if not arqs or not a.get("respondida"):
            continue
        por_nome.setdefault(a["nome"], []).append(a)

    guias: list[dict] = []
    for nome, lista in por_nome.items():
        # Ordena por respondidaEm crescente (mais antigo primeiro); fallback id
        lista.sort(key=lambda x: (x.get("respondidaEm") or "", str(x.get("id") or "")))
        mais_recente = lista[-1]
        versoes_anteriores = lista[:-1]
        for arq in mais_recente.get("arquivos", []):
            guias.append({
                "tarefa_id": tarefa["id"],
                "atividade_id": mais_recente["id"],
                "atividade_nome": mais_recente["nome"],
                "arquivo_nome": arq.get("nome"),
                "arquivo_url": arq.get("url"),
                "cnpj": tarefa.get("clienteInscricao"),
                "cliente_apelido": tarefa.get("clienteApelido"),
                "obrigacao_nome": tarefa.get("nome"),
                "data_vencimento": tarefa.get("dataVencimento"),
                "status_tarefa": tarefa.get("status"),
                "competencia": competencia,
                "respondida_em": mais_recente.get("respondidaEm"),
                "eh_retificada": len(versoes_anteriores) > 0,
                "num_versoes": len(lista),
                "versoes_anteriores": [
                    {
                        "atividade_id": v["id"],
                        "respondida_em": v.get("respondidaEm"),
                        "arquivos": [{"nome": a.get("nome"), "url": a.get("url")}
                                     for a in v.get("arquivos") or []],
                    }
                    for v in versoes_anteriores
                ],
            })
    return guias
