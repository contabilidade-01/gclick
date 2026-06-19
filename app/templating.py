"""Objeto de templates Jinja2 compartilhado por todos os routers."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _aprovacoes_pendentes() -> int:
    """Contador para o badge da Caixa de Saída no menu. Falha silenciosa (nunca
    quebra a renderização de uma página)."""
    try:
        from . import db
        return db.contar_aprovacoes_pendentes()
    except Exception:  # noqa: BLE001
        return 0


# Disponível em qualquer template (ex.: badge no nav do base.html).
templates.env.globals["aprovacoes_pendentes"] = _aprovacoes_pendentes
