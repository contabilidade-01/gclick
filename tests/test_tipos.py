"""Testes do classificador de tipos (usa o banco temporário com TIPOS_SEED)."""

from __future__ import annotations

from app import tipos


def test_classifica_guias_fiscais():
    assert tipos.classificar("FGTS")[0] == "FGTS"
    assert tipos.classificar("INSS")[0] == "INSS"
    assert tipos.classificar("DAS Simples")[0] == "DAS"


def test_classifica_recibo_e_extrato():
    assert tipos.classificar("Anexar recibo de pagamento")[0] == "RECIBO_PAGTO"
    assert tipos.classificar("Anexar Folha de Pagamento (Extrato)")[0] == "EXTRATO_FOLHA"


def test_ordem_dos_argumentos_atividade_vence_obrigacao():
    # Obrigação genérica "FGTS, DCTF Web" não deve fazer a atividade DCTF virar FGTS.
    assert tipos.classificar("DCTF Web", "FGTS, DCTF Web")[0] == "DCTF_WEB"


def test_sem_match_retorna_none():
    assert tipos.classificar("documento qualquer sem padrão") is None
    assert tipos.classificar("") is None
