"""Testes do parser de vencimento do PDF (foco em tolerância a entrada ruim)."""

from __future__ import annotations

from datetime import date

from app import pdf_parser


def test_entrada_invalida_nao_quebra():
    # Bytes que não são PDF → None (parser é tolerante, não levanta exceção).
    assert pdf_parser.extrair_vencimento(b"isso nao e um pdf") is None
    assert pdf_parser.extrair_vencimento(b"") is None


def test_parse_data_interno():
    assert pdf_parser._parse_data("29", "06", "2026") == date(2026, 6, 29)
    assert pdf_parser._parse_data("31", "02", "2026") is None  # data impossível


def test_extrair_dados_retorna_estrutura():
    d = pdf_parser.extrair_dados(b"nao-pdf")
    assert "vencimento" in d and "texto_amostra" in d
    assert d["vencimento"] is None
