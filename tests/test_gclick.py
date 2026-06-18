"""Testes de gclick.extrair_guias_pendentes (consolidação de retificações)."""

from __future__ import annotations

from app import gclick


def _tarefa():
    return {
        "id": "4.1",
        "clienteInscricao": "35736034000123",
        "clienteApelido": "NESCON",
        "nome": "FGTS + DCTF Web",
        "dataVencimento": "2026-06-29",
        "status": "A",
    }


def test_consolida_retificacao_pega_versao_mais_recente():
    ativs = [
        {"id": "4.10", "nome": "FGTS", "respondida": True,
         "respondidaEm": "2026-06-01T10:00:00", "arquivos": [{"nome": "fgts_v1.pdf", "url": "u1"}]},
        {"id": "4.11", "nome": "FGTS", "respondida": True,  # retificadora (mais nova)
         "respondidaEm": "2026-06-02T10:00:00", "arquivos": [{"nome": "fgts_v2.pdf", "url": "u2"}]},
    ]
    guias = gclick.extrair_guias_pendentes(_tarefa(), ativs)
    assert len(guias) == 1
    g = guias[0]
    assert g["arquivo_nome"] == "fgts_v2.pdf"   # vence a mais recente
    assert g["eh_retificada"] is True
    assert g["num_versoes"] == 2
    assert g["competencia"] == "2026-06"
    assert g["cnpj"] == "35736034000123"


def test_ignora_atividades_sem_arquivo_ou_nao_respondidas():
    ativs = [
        {"id": "4.12", "nome": "Validar Calculos", "respondida": True, "arquivos": []},
        {"id": "4.13", "nome": "DAS", "respondida": False,
         "arquivos": [{"nome": "das.pdf", "url": "u3"}]},
    ]
    assert gclick.extrair_guias_pendentes(_tarefa(), ativs) == []


def test_duas_obrigacoes_geram_duas_guias():
    ativs = [
        {"id": "4.10", "nome": "FGTS", "respondida": True,
         "respondidaEm": "2026-06-01T10:00:00", "arquivos": [{"nome": "fgts.pdf", "url": "u1"}]},
        {"id": "4.20", "nome": "DCTF Web", "respondida": True,
         "respondidaEm": "2026-06-01T10:00:00", "arquivos": [{"nome": "darf.pdf", "url": "u2"}]},
    ]
    guias = gclick.extrair_guias_pendentes(_tarefa(), ativs)
    nomes = sorted(g["arquivo_nome"] for g in guias)
    assert nomes == ["darf.pdf", "fgts.pdf"]
