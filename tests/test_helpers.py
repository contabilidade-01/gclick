"""Testes das funções puras de app/helpers.py."""

from __future__ import annotations

from app import helpers


def test_validar_whatsapp_br_validos():
    ok, _ = helpers.validar_whatsapp_br("5511963234599")   # celular 9 dígitos
    assert ok
    ok, _ = helpers.validar_whatsapp_br("+55 (11) 96323-4599")  # com máscara
    assert ok
    ok, _ = helpers.validar_whatsapp_br("553133334444")    # fixo 8 dígitos
    assert ok


def test_validar_whatsapp_br_invalidos():
    assert not helpers.validar_whatsapp_br("")[0]            # vazio
    assert not helpers.validar_whatsapp_br(None)[0]          # None
    assert not helpers.validar_whatsapp_br("11963234599")[0]  # sem DDI 55
    assert not helpers.validar_whatsapp_br("5500963234599")[0]  # DDD inválido


def test_range_competencia():
    assert helpers.range_competencia("2026-06") == ("2026-06-01", "2026-06-30")
    assert helpers.range_competencia("2026-12") == ("2026-12-01", "2026-12-31")
    assert helpers.range_competencia("2026-02") == ("2026-02-01", "2026-02-28")
    assert helpers.range_competencia("2024-02") == ("2024-02-01", "2024-02-29")  # bissexto


def test_competencia_label():
    assert helpers.competencia_label("2026-06") == "Junho/2026"
    assert helpers.competencia_label("2026-01") == "Janeiro/2026"
    assert helpers.competencia_label("lixo") == "lixo"  # entrada inválida volta igual


def test_competencias_opcoes_inclui_ref_e_tem_12():
    opcoes = helpers.competencias_opcoes("2026-06")
    assert all({"valor", "label"} <= set(o) for o in opcoes)
    assert any(o["valor"] == "2026-06" for o in opcoes)
    # Uma referência fora da janela de 12 meses é inserida no topo.
    opcoes2 = helpers.competencias_opcoes("2000-01")
    assert opcoes2[0]["valor"] == "2000-01"


def test_fmt_data_e_whatsapp():
    assert helpers.fmt_data("2026-06-29") == "29/06/2026"
    assert helpers.fmt_data("") == ""
    assert helpers.fmt_whatsapp("5511963234599") == "+55 (11) 96323-4599"
    assert helpers.fmt_whatsapp("123") == "123"  # curto demais, volta igual
