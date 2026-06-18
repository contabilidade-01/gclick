"""Extração de dados de guias em PDF (vencimento, valor, competência).

Suporta os layouts mais comuns das guias da Receita/Caixa/Previdência:
- DARF (Receita Federal)
- GRF (FGTS — Caixa)
- GPS (INSS — Previdência)
- DAS (Simples Nacional)
- DCTF Web

A leitura é tolerante a OCR ruim: tenta vários padrões de regex próximos a
rótulos típicos (vencimento, data de pagamento, prazo, etc.).
"""

from __future__ import annotations

import io
import re
from datetime import date
from typing import Iterable

import pdfplumber

# Rótulos onde geralmente aparece a data de vencimento, em ordem de prioridade.
# Quanto mais específico, mais alto.
ROTULOS_VENCIMENTO = [
    r"Data\s+de\s+Vencimento",
    r"Data\s+do?\s+Vencimento",
    r"Pagar\s+este\s+documento\s+at[eé]",   # FGTS Digital (GFD)
    r"Pagar\s+at[eé]",
    r"Data\s+limite\s+(?:de|para)\s+pagamento",
    r"Data\s+de\s+Pagamento",
    r"Data\s+m[aá]xima\s+de\s+pagamento",
    r"Vencimento",                            # rótulo genérico — por último
]

# Quanto procurar depois do rótulo (alguns layouts intercalam outros campos antes da data)
_JANELA_BUSCA = 220

# Captura DD/MM/AAAA ou DD-MM-AAAA com tolerância a espaços
_RE_DATA = re.compile(r"(\b[0-3]?\d)[/\-\.\s]([01]?\d)[/\-\.\s]((?:19|20)\d{2})\b")


def _extrair_texto(pdf_bytes: bytes) -> str:
    """Concatena texto de todas as páginas. Tolerante a PDFs vazios/corrompidos."""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            partes = []
            for p in pdf.pages:
                t = p.extract_text() or ""
                partes.append(t)
            return "\n".join(partes)
    except Exception:
        return ""


def _parse_data(grupo_dia: str, grupo_mes: str, grupo_ano: str) -> date | None:
    try:
        return date(int(grupo_ano), int(grupo_mes), int(grupo_dia))
    except ValueError:
        return None


def extrair_vencimento(pdf_bytes: bytes) -> date | None:
    """Tenta achar a data de vencimento da guia. Retorna None se não conseguir.

    Estratégia:
    1. Para cada rótulo conhecido, procura uma data nas próximas ~80 chars.
    2. Se nenhum rótulo casar, devolve None (não chuta — é melhor errar para
       cima do que mandar data errada).
    """
    texto = _extrair_texto(pdf_bytes)
    if not texto:
        return None

    # Normaliza espaços e quebras de linha
    plano = re.sub(r"\s+", " ", texto)

    for rotulo in ROTULOS_VENCIMENTO:
        pat = re.compile(rf"{rotulo}.{{0,{_JANELA_BUSCA}}}", re.IGNORECASE)
        for m in pat.finditer(plano):
            trecho = m.group(0)
            # Pula a parte do rótulo em si para não capturar datas que vêm antes
            md = _RE_DATA.search(trecho[len(re.match(rotulo, trecho, re.IGNORECASE).group(0)):])
            if md:
                d = _parse_data(md.group(1), md.group(2), md.group(3))
                if d:
                    return d
    return None


def extrair_dados(pdf_bytes: bytes) -> dict:
    """Devolve um dict com {vencimento: date|None, texto_amostra: str}.
    Útil para auditoria e debug."""
    texto = _extrair_texto(pdf_bytes)
    return {
        "vencimento": extrair_vencimento(pdf_bytes),
        "texto_amostra": (texto[:500] if texto else ""),
    }
