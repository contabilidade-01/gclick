"""Integração com o Portal do Cliente.

O portal busca os documentos sozinho na API do G-Click — este sistema NÃO envia mais
arquivo nenhum. Aqui só fazemos duas coisas:

1. `liberar()` — dizer ao portal quais documentos daquele cliente podem ficar visíveis
   (o portal sincroniza antes de liberar, então o aviso nunca aponta para portal vazio);
2. `acessos_por_deliverable()` — trazer as aberturas de volta para a tela de auditoria.

Falha aqui nunca derruba a tela: as funções devolvem None e o chamador decide.
"""

from __future__ import annotations

import logging

import httpx

from . import config

logger = logging.getLogger(__name__)

TIMEOUT_S = 180.0  # liberar dispara um sync no portal — pode demorar


def _url_base() -> str:
    """Raiz da API do portal, derivada da URL configurada (.../api/fiscal)."""
    url, _ = config.portal_credentials()
    if not url:
        return ""
    for sufixo in ("/ingest", "/release"):
        if url.endswith(sufixo):
            return url[: -len(sufixo)]
    return url.rstrip("/")


def _post(caminho: str, payload: dict, timeout: float = TIMEOUT_S) -> dict | None:
    _, key = config.portal_credentials()
    base = _url_base()
    if not base or not key:
        return None
    try:
        resp = httpx.post(
            f"{base}{caminho}",
            json=payload,
            headers={"X-Ingest-Key": key},
            timeout=timeout,
        )
        if resp.status_code >= 400:
            logger.warning("portal %s: HTTP %s — %s", caminho, resp.status_code, resp.text[:200])
            return None
        return resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("portal %s: %s", caminho, e)
        return None


def liberar(cnpj: str, guias: list[dict]) -> dict | None:
    """Libera no portal os documentos das `guias` deste CNPJ.

    A identidade do documento é (tarefa_id, nome da atividade) — NÃO o atividade_id,
    porque uma retificação no G-Click cria uma atividade nova com o mesmo nome. Assim a
    versão retificada substitui a anterior no portal em vez de duplicar.

    Devolve {liberados_agora, total_liberados, portal_url, ...} ou None se falhar.
    """
    digitos = "".join(ch for ch in str(cnpj or "") if ch.isdigit())
    if len(digitos) != 14:
        logger.warning("portal: CNPJ inválido para liberação (%r)", cnpj)
        return None

    itens = [
        {"tarefa_id": g.get("tarefa_id"), "atividade_nome": g.get("atividade_nome")}
        for g in guias
        if g.get("tarefa_id") and g.get("atividade_nome")
    ]
    if not itens:
        return None

    return _post("/release", {"cnpj": digitos, "itens": itens})


def acessos_por_deliverable(deliverable_ids: list[str]) -> dict[str, dict]:
    """Aberturas/downloads das entregas no portal, em 1 chamada.

    Mesmo formato de `db.acessos_por_envio` ({aberturas, downloads, ultimo_em,
    ultimo_ip}) para a auditoria não precisar saber a origem. Falha → {}.
    """
    ids = [i for i in dict.fromkeys(deliverable_ids or []) if i]
    if not ids:
        return {}
    out = _post("/access-stats", {"ids": ids}, timeout=15.0)
    return out or {}


def sincronizar_clientes(clientes: list[dict]) -> dict | None:
    """Cria no portal as empresas que faltam (casa por CNPJ).

    O portal também cria empresas sozinho ao puxar os documentos; este botão serve
    para adiantar o cadastro com os dados curados daqui (WhatsApp/e-mail corrigidos
    à mão). O portal NÃO sobrescreve empresa existente.
    """
    payload = []
    for c in clientes:
        cnpj = "".join(ch for ch in str(c.get("cnpj") or "") if ch.isdigit())
        if len(cnpj) != 14:
            continue
        # nome_completo é a razão social; apelido é o nome curto do dia a dia.
        nome = (c.get("nome_completo") or c.get("apelido") or "").strip()
        if not nome:
            continue
        item = {"cnpj": cnpj, "name": nome}
        if c.get("email"):
            item["email"] = c["email"]
        if c.get("whatsapp"):
            item["phone"] = c["whatsapp"]
        payload.append(item)

    if not payload:
        return {"criadas": 0, "existentes": 0, "erros": 0, "detalhe": {}}
    return _post("/sync-companies", {"companies": payload}, timeout=120.0)
