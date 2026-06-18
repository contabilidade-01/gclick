"""Classificacao de guias do G-Click em tipos padrao."""

from __future__ import annotations

import re
from functools import lru_cache

from . import db


@lru_cache(maxsize=1)
def _carregar_padroes() -> list[tuple[str, str, re.Pattern]]:
    """Carrega tipos ativos como [(codigo, nome, regex_compilada), ...].
    Cache resetado quando invalidar() é chamado."""
    out: list[tuple[str, str, re.Pattern]] = []
    for t in db.listar_tipos(ativos_apenas=True):
        matchers = (t["matchers"] or "").strip()
        if not matchers:
            continue
        # Cada matcher e tratado como literal, separados por | viram alternativas
        partes = [re.escape(p.strip()) for p in matchers.split("|") if p.strip()]
        if not partes:
            continue
        pat = re.compile("|".join(partes), re.IGNORECASE)
        out.append((t["codigo"], t["nome"], pat))
    return out


def invalidar() -> None:
    _carregar_padroes.cache_clear()


def classificar(*nomes: str) -> tuple[str, str] | None:
    """Recebe um ou mais nomes (atividade, obrigacao, arquivo) e retorna
    (codigo, nome) do primeiro tipo que casa.

    A ordem dos argumentos importa: classifica primeiro pelo nome mais
    específico (atividade), só cai para os demais (obrigação) se nenhum
    tipo casar com o primeiro. Isso evita que uma obrigação com nome
    genérico tipo "FGTS, DCTF Web" pegue todas as suas atividades como FGTS.
    """
    padroes = _carregar_padroes()
    for txt in nomes:
        if not txt:
            continue
        for codigo, nome, pat in padroes:
            if pat.search(txt):
                return codigo, nome
    return None
