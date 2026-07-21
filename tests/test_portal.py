"""Testes da integração com o Portal do Cliente.

Neste desenho o portal busca os documentos sozinho no G-Click; este sistema só
LIBERA os documentos e avisa o cliente. Nenhum teste toca a rede.
"""

from __future__ import annotations

import pytest

from app import config, helpers, portal


def _guia(**over) -> dict:
    g = {
        "tarefa_id": "4.10216",
        "atividade_id": "99",
        "atividade_nome": "Anexar guia FGTS",
        "obrigacao_nome": "FGTS",
        "arquivo_nome": "fgts.pdf",
        "cnpj": "35736034000123",
        "competencia": "2026-07",
        "data_vencimento": "2026-07-20",
    }
    g.update(over)
    return g


@pytest.fixture
def portal_ligado(monkeypatch):
    monkeypatch.setattr(
        config, "portal_credentials", lambda: ("https://portal.test/api/fiscal", "chave")
    )
    monkeypatch.setattr(config, "portal_configurado", lambda: True)


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


# --- URL base -------------------------------------------------------------

def test_url_base_aceita_raiz(portal_ligado):
    assert portal._url_base() == "https://portal.test/api/fiscal"


def test_url_base_tolera_sufixo_antigo(monkeypatch):
    """Quem já tinha .../ingest no .env não precisa mexer na configuração."""
    monkeypatch.setattr(
        config, "portal_credentials", lambda: ("https://portal.test/api/fiscal/ingest", "k")
    )
    assert portal._url_base() == "https://portal.test/api/fiscal"


# --- liberação ------------------------------------------------------------

def test_liberar_desconfigurado_devolve_none(monkeypatch):
    monkeypatch.setattr(config, "portal_credentials", lambda: ("", ""))
    assert portal.liberar("35736034000123", [_guia()]) is None


def test_liberar_cnpj_invalido(portal_ligado):
    assert portal.liberar("123", [_guia()]) is None


def test_liberar_sem_guias(portal_ligado):
    assert portal.liberar("35736034000123", []) is None


def test_liberar_identifica_por_tarefa_e_NOME_da_atividade(portal_ligado, monkeypatch):
    """Retificação cria atividade NOVA com o mesmo nome.

    Se a identidade fosse o atividade_id, a versão retificada viraria um documento
    separado e o cliente veria as duas. Por nome, ela substitui a anterior.
    """
    capturado = {}

    def _post(url, json=None, headers=None, timeout=None):
        capturado["url"] = url
        capturado["body"] = json
        capturado["key"] = headers["X-Ingest-Key"]
        return _Resp(payload={"liberados_agora": 1, "total_liberados": 5,
                              "portal_url": "https://p/"})

    monkeypatch.setattr(portal.httpx, "post", _post)
    out = portal.liberar("35.736.034/0001-23", [_guia(atividade_id="777")])

    assert capturado["url"] == "https://portal.test/api/fiscal/release"
    assert capturado["key"] == "chave"
    assert capturado["body"]["cnpj"] == "35736034000123"  # máscara removida
    item = capturado["body"]["itens"][0]
    assert item == {"tarefa_id": "4.10216", "atividade_nome": "Anexar guia FGTS"}
    assert "atividade_id" not in item
    assert out["liberados_agora"] == 1


def test_liberar_descarta_guia_sem_identidade(portal_ligado, monkeypatch):
    capturado = {}
    monkeypatch.setattr(
        portal.httpx, "post",
        lambda url, json=None, **k: (capturado.update(json), _Resp(payload={}))[1],
    )
    portal.liberar("35736034000123", [_guia(), _guia(atividade_nome=None), _guia(tarefa_id=None)])
    assert len(capturado["itens"]) == 1


def test_liberar_erro_http_devolve_none(portal_ligado, monkeypatch):
    monkeypatch.setattr(portal.httpx, "post",
                        lambda *a, **k: _Resp(status_code=404, text="sem empresa"))
    assert portal.liberar("35736034000123", [_guia()]) is None


def test_liberar_falha_de_rede_devolve_none(portal_ligado, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("timeout")

    monkeypatch.setattr(portal.httpx, "post", _boom)
    assert portal.liberar("35736034000123", [_guia()]) is None


# --- mensagem consolidada -------------------------------------------------

def test_mensagem_uma_guia():
    txt = helpers.mensagem_portal([_guia()])
    assert "1 novo documento no seu portal" in txt
    assert "FGTS" in txt
    assert "Julho/2026" in txt
    assert "20/07/2026" in txt


def test_mensagem_plural_e_resumo_de_lista_longa():
    guias = [_guia(atividade_nome=f"Guia {i}", arquivo_nome=f"doc{i}.pdf") for i in range(9)]
    txt = helpers.mensagem_portal(guias)
    assert "9 novos documentos no seu portal" in txt
    # Lista longa é cortada para o WhatsApp não virar paredão de texto.
    assert "e mais 3" in txt


def test_mensagem_cita_o_acervo_quando_ha_mais_no_portal():
    txt = helpers.mensagem_portal([_guia()], total_no_portal=12)
    assert "12 no total" in txt


def test_mensagem_nao_leva_link_no_texto():
    """O endereço vai no BOTÃO; link solto no corpo aumenta chance de spam."""
    txt = helpers.mensagem_portal([_guia()])
    assert "http" not in txt


# --- sincronização de clientes -------------------------------------------

def _cliente(**over) -> dict:
    c = {
        "cnpj": "35736034000123",
        "apelido": "NESCON",
        "nome_completo": "NESCON CONTABILIDADE LTDA",
        "whatsapp": "5511999998888",
        "email": "contato@nescon.com",
        "ativo": 1,
    }
    c.update(over)
    return c


def test_sync_clientes_manda_razao_social(portal_ligado, monkeypatch):
    capturado = {}
    monkeypatch.setattr(
        portal.httpx, "post",
        lambda url, json=None, **k: (capturado.update({"url": url, **json}), _Resp(payload={}))[1],
    )
    portal.sincronizar_clientes([_cliente()])
    assert capturado["url"] == "https://portal.test/api/fiscal/sync-companies"
    item = capturado["companies"][0]
    assert item["name"] == "NESCON CONTABILIDADE LTDA"
    assert item["phone"] == "5511999998888"


def test_sync_clientes_descarta_invalidos(portal_ligado, monkeypatch):
    capturado = {}
    monkeypatch.setattr(
        portal.httpx, "post",
        lambda url, json=None, **k: (capturado.update(json), _Resp(payload={}))[1],
    )
    portal.sincronizar_clientes([
        _cliente(),
        _cliente(cnpj="123"),
        _cliente(nome_completo="", apelido=""),
    ])
    assert len(capturado["companies"]) == 1


# --- aberturas (auditoria como painel único) ------------------------------

def test_acessos_sem_ids_nao_chama_a_rede(portal_ligado, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("não devia chamar a rede sem ids")

    monkeypatch.setattr(portal.httpx, "post", _boom)
    assert portal.acessos_por_deliverable([]) == {}


def test_acessos_devolve_resumo_e_deduplica(portal_ligado, monkeypatch):
    capturado = {}

    def _post(url, json=None, headers=None, timeout=None):
        capturado["ids"] = json["ids"]
        return _Resp(payload={"abc": {"aberturas": 2, "downloads": 1,
                                      "ultimo_em": "2026-07-16T18:53:50",
                                      "ultimo_ip": "1.2.3.4"}})

    monkeypatch.setattr(portal.httpx, "post", _post)
    out = portal.acessos_por_deliverable(["abc", "abc", "def"])
    assert capturado["ids"] == ["abc", "def"]
    assert set(out["abc"]) == {"aberturas", "downloads", "ultimo_em", "ultimo_ip"}


def test_acessos_falha_devolve_vazio(portal_ligado, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("portal fora")

    monkeypatch.setattr(portal.httpx, "post", _boom)
    assert portal.acessos_por_deliverable(["abc"]) == {}
