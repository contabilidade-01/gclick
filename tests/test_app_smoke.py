"""Smoke test do app montado: roteamento + render dos templates leves.

Não toca o G-Click — só as telas que dependem apenas do banco local.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.main import app


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def logado(client):
    client.cookies.set(auth.COOKIE, auth.gerar_cookie("admin"))
    return client


def test_login_get_renderiza(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "html" in r.text.lower()


def test_raiz_sem_login_redireciona(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_favicon():
    with TestClient(app) as c:
        assert c.get("/favicon.ico").status_code == 204


@pytest.mark.parametrize("rota", ["/auditoria", "/tipos", "/clientes", "/configuracoes", "/aprovacoes"])
def test_telas_leves_autenticadas_renderizam(logado, rota):
    # Telas que não dependem do G-Click devem responder 200 já autenticado.
    r = logado.get(rota, follow_redirects=False)
    assert r.status_code == 200, f"{rota} devolveu {r.status_code}"


def test_cliente_editar_renderiza(logado):
    # Regressão: a tela de edição dava 500 (sqlite3.Row não tem .get() no template).
    from app import db
    db.upsert_cliente("12345678000199", "ACME", whatsapp="5511963234599")
    r = logado.get("/clientes/12345678000199/editar", follow_redirects=False)
    assert r.status_code == 200


def test_filtro_enviaveis(logado):
    # Filtro "ativos com WhatsApp" responde 200.
    r = logado.get("/clientes?filtro=enviaveis", follow_redirects=False)
    assert r.status_code == 200
