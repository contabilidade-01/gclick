"""Carrega configuração do .env."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


GCLICK_BASE_URL = "https://api.gclick.com.br"

# Valores do .env (fallback). As funções *_credentials() consultam o SQLite
# primeiro — assim a UI pode atualizar credenciais sem reiniciar o servidor e
# sem nada hardcoded no código (que vai para o Git).
GCLICK_CLIENT_ID_ENV = _env("GCLICK_CLIENT_ID")
GCLICK_CLIENT_SECRET_ENV = _env("GCLICK_CLIENT_SECRET")
UAZAPI_SUBDOMAIN_ENV = _env("UAZAPI_SUBDOMAIN")
UAZAPI_TOKEN_ENV = _env("UAZAPI_TOKEN")


def gclick_credentials() -> tuple[str, str]:
    """Retorna (client_id, client_secret). SQLite tem prioridade, .env é fallback.
    Import tardio de `db` para evitar ciclo de import."""
    from . import db
    cid = db.get_config("gclick_client_id", GCLICK_CLIENT_ID_ENV) or ""
    csec = db.get_config("gclick_client_secret", GCLICK_CLIENT_SECRET_ENV) or ""
    return cid, csec


def gclick_configurado() -> bool:
    cid, csec = gclick_credentials()
    return bool(cid and csec)


def uazapi_credentials() -> tuple[str, str]:
    """Retorna (subdomain, token). SQLite tem prioridade, .env é fallback.
    Import tardio de `db` para evitar ciclo de import.
    """
    from . import db
    sub = db.get_config("uazapi_subdomain", UAZAPI_SUBDOMAIN_ENV) or ""
    tok = db.get_config("uazapi_token", UAZAPI_TOKEN_ENV) or ""
    return sub, tok


# Aliases legados (read-only). A verdade ao longo do app são as funções
# *_credentials() acima (banco > .env).
GCLICK_CLIENT_ID = GCLICK_CLIENT_ID_ENV
GCLICK_CLIENT_SECRET = GCLICK_CLIENT_SECRET_ENV
UAZAPI_SUBDOMAIN = UAZAPI_SUBDOMAIN_ENV
UAZAPI_TOKEN = UAZAPI_TOKEN_ENV

APP_USER = _env("APP_USER", "admin")
APP_PASSWORD_HASH = _env("APP_PASSWORD_HASH")
SECRET_KEY = _env("SECRET_KEY", "dev-trocar-no-deploy")

APP_HOST = _env("APP_HOST", "127.0.0.1")
APP_PORT = int(_env("APP_PORT", "8000"))

# URL pública do app (produção). Usada para montar os links rastreados
# (/g/{token}) que vão no WhatsApp. Local: vazio → deriva do host da requisição.
# VPS: setar ex. PUBLIC_BASE_URL=https://guias.gestaoempresa.com
PUBLIC_BASE_URL = (_env("PUBLIC_BASE_URL") or "").rstrip("/")

# Anti-bloqueio do número no WhatsApp:
# - throttle: pausa (segundos) entre dois envios reais consecutivos.
# - teto/hora: máximo de envios reais por hora (janela deslizante em memória).
ENVIO_THROTTLE_S = float(_env("ENVIO_THROTTLE_S", "0.6"))
ENVIO_MAX_POR_HORA = int(_env("ENVIO_MAX_POR_HORA", "180"))

# Atualizador periódico do cache de guias do mês atual (em horas).
# 0 = desligado (padrão local). Em VPS, ex.: REFRESH_INTERVAL_H=3 mantém o mês
# atual sempre "quente" — as telas abrem rápido sem ninguém ter que recarregar.
REFRESH_INTERVAL_H = float(_env("REFRESH_INTERVAL_H", "0"))

# Diretório de dados PERSISTENTES (banco SQLite + PDFs das guias).
# Tudo que precisa sobreviver a redeploy fica aqui — assim UM único volume no
# EasyPanel (montado em /app/data) persiste o sistema inteiro.
# Local: default ROOT/data. VPS: setar env DATA_DIR=/app/data.
DATA_DIR = Path(_env("DATA_DIR") or (ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "dados.db"
PASTA_GUIAS = DATA_DIR / "guias"
PASTA_GUIAS.mkdir(parents=True, exist_ok=True)


def url_publica(path: str, request=None) -> str:
    """URL absoluta pública para `path` (ex.: '/g/abc' → 'https://.../g/abc').

    Usa PUBLIC_BASE_URL (deploy) quando setado; senão deriva do host da
    requisição (`request.base_url`) — útil em desenvolvimento local.
    """
    if not path.startswith("/"):
        path = "/" + path
    base = PUBLIC_BASE_URL
    if not base and request is not None:
        base = str(request.base_url).rstrip("/")
    return f"{base}{path}"


def uazapi_configurado() -> bool:
    sub, tok = uazapi_credentials()
    return bool(sub and tok)


def get_auto_envio_runtime() -> dict:
    """Configuração do envio automático por gatilho do G-Click (SQLite > default).

    Chaves no config_runtime:
      auto_envio_ativo    — '1'/'0' (default '0', desligado)
      auto_gatilho        — 'enviar_cliente' (default) | 'concluida'
      auto_intervalo_min  — minutos entre ciclos do worker (default 15)
      auto_ativado_em     — ISO do momento em que ligou (data de corte; lida no worker)
    """
    from . import db
    ativo = (db.get_config("auto_envio_ativo", "0") or "0") == "1"
    gatilho = db.get_config("auto_gatilho", "enviar_cliente") or "enviar_cliente"
    if gatilho not in ("enviar_cliente", "concluida"):
        gatilho = "enviar_cliente"
    try:
        intervalo = int(db.get_config("auto_intervalo_min", "15"))
    except (TypeError, ValueError):
        intervalo = 15
    return {"ativo": ativo, "gatilho": gatilho, "intervalo_min": max(1, intervalo)}


# Retenção dos PDFs guardados na VPS: arquivos com mais que isto são apagados
# pela limpeza automática (o REGISTRO de auditoria permanece — só o arquivo sai).
RETENCAO_PDF_MESES = 6


def get_limpeza_runtime() -> dict:
    """Config da limpeza automática de PDFs antigos (SQLite > default).
    Chaves: limpeza_ativa ('1'/'0'), limpeza_intervalo_h (de quanto em quanto
    tempo o worker roda). A idade de retenção é fixa (RETENCAO_PDF_MESES)."""
    from . import db
    ativa = (db.get_config("limpeza_ativa", "1") or "1") == "1"
    try:
        intervalo = int(db.get_config("limpeza_intervalo_h", "24"))
    except (TypeError, ValueError):
        intervalo = 24
    return {"ativa": ativa, "intervalo_h": max(1, intervalo),
            "retencao_meses": RETENCAO_PDF_MESES}


def get_piloto_runtime() -> dict:
    """Modo piloto do envio AUTOMÁTICO: quando ativo, toda liberação da Caixa de
    Saída é redirecionada para um número de teste (ignora os reais). NÃO afeta o
    envio manual. Chaves: auto_piloto_ativo ('1'/'0'), auto_piloto_numero."""
    from . import db
    ativo = (db.get_config("auto_piloto_ativo", "0") or "0") == "1"
    numero = (db.get_config("auto_piloto_numero", "") or "").strip()
    return {"ativo": ativo and bool(numero), "numero": numero}


def get_throttle_runtime() -> dict:
    """Configurações de ritmo de envio. SQLite (config_runtime) tem prioridade;
    fallback nas constantes derivadas do .env.

    Chaves no SQLite:
      envio_throttle_s      — pausa entre envios reais (segundos, float)
      envio_max_por_hora    — teto de envios por hora (int)
      envio_delay_uazapi_ms — "digitando..." antes do envio (ms, int)

    O `modo_envio` é FIXO em 'link' (link rastreado) — não é mais configurável.
    """
    from . import db
    try:
        throttle_s = float(db.get_config("envio_throttle_s", str(ENVIO_THROTTLE_S)))
    except (TypeError, ValueError):
        throttle_s = ENVIO_THROTTLE_S
    try:
        max_por_hora = int(db.get_config("envio_max_por_hora", str(ENVIO_MAX_POR_HORA)))
    except (TypeError, ValueError):
        max_por_hora = ENVIO_MAX_POR_HORA
    try:
        delay_ms = int(db.get_config("envio_delay_uazapi_ms", "0"))
    except (TypeError, ValueError):
        delay_ms = 0
    return {
        "throttle_s": max(0.0, throttle_s),
        "max_por_hora": max(1, max_por_hora),
        "delay_uazapi_ms": max(0, delay_ms),
        "modo_envio": "link",  # fixo — sempre link rastreado
    }
