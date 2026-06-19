"""FastAPI — app de envio de guias.

Este módulo só monta o app: configura logging, o ciclo de vida (lifespan) e
inclui os routers de `app/routes/`. A lógica de cada tela vive no seu router;
os helpers de domínio (cache, validações, legenda) vivem em `app/helpers.py`.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from . import config, db, helpers
from .routes import (
    aprovacoes,
    auditoria,
    check,
    clientes,
    configuracoes,
    dashboard,
    envio,
    fila,
    login,
    tipos as tipos_routes,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Substitui o antigo @app.on_event("startup") (deprecado no FastAPI atual).
    db.init()
    helpers.prewarm()  # aquece o cache do mês atual em background (não bloqueia)
    helpers.iniciar_atualizador_periodico()  # VPS: mantém o mês atual quente
    helpers.iniciar_worker_auto_envio()  # Fase 2: envio automático por gatilho (opt-in)
    yield


app = FastAPI(title="Envio de Guias - Nescon", lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/favicon.ico")
async def _favicon() -> Response:
    return Response(status_code=204)


for _router in (
    login.router,
    dashboard.router,
    fila.router,
    envio.router,
    clientes.router,
    tipos_routes.router,
    configuracoes.router,
    auditoria.router,
    aprovacoes.router,
    check.router,
):
    app.include_router(_router)


if __name__ == "__main__":
    import uvicorn

    # reload=False de propósito: no uso diário (especialmente com a pasta no
    # OneDrive) o --reload reinicia o servidor a cada arquivo sincronizado e
    # derruba o cache quente, deixando tudo lento. Para desenvolver, rode:
    #   python -m uvicorn app.main:app --reload
    uvicorn.run("app.main:app", host=config.APP_HOST, port=config.APP_PORT, reload=False)
