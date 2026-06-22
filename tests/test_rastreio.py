"""Testes da camada de link rastreado (token público + acessos do documento)."""

from __future__ import annotations

from app import db


def _novo_envio() -> int:
    return db.registrar_envio(
        cnpj="12345678000199", whatsapp="5511963234599", tarefa_id="T1",
        atividade_id="A1", arquivo_nome="DARF.pdf", competencia="2026-06",
        uazapi_message_id="msg1", status="ok",
    )


def test_token_publico_idempotente():
    envio_id = _novo_envio()
    t1 = db.garantir_token_publico(envio_id)
    t2 = db.garantir_token_publico(envio_id)
    assert t1 and t1 == t2  # mesmo token nas duas chamadas
    assert db.get_envio_por_token(t1)["id"] == envio_id


def test_get_envio_por_token_invalido():
    assert db.get_envio_por_token("nao-existe") is None
    assert db.get_envio_por_token("") is None


def test_acessos_conta_aberturas_e_downloads():
    envio_id = _novo_envio()
    token = db.garantir_token_publico(envio_id)
    db.registrar_acesso(envio_id=envio_id, token=token, evento="pagina", ip="8.8.8.8")
    db.registrar_acesso(envio_id=envio_id, token=token, evento="pagina", ip="8.8.8.8")
    db.registrar_acesso(envio_id=envio_id, token=token, evento="download", ip="187.1.2.3")

    resumo = db.acessos_por_envio([envio_id])[envio_id]
    assert resumo["aberturas"] == 2
    assert resumo["downloads"] == 1
    assert resumo["ultimo_ip"] == "187.1.2.3"  # IP do último acesso


def test_acessos_ignora_bots():
    envio_id = _novo_envio()
    token = db.garantir_token_publico(envio_id)
    # Preview do WhatsApp/Meta — marcado como bot, NÃO conta como abertura real.
    db.registrar_acesso(envio_id=envio_id, token=token, evento="pagina",
                        ip="1.1.1.1", eh_bot=1)
    assert db.acessos_por_envio([envio_id]) == {}  # nenhum acesso humano


def test_baixa_manual_sai_de_pendentes():
    # Baixa manual marca como resolvida sem enviar — sai de chaves_enviadas/ja_enviado.
    assert db.ja_enviado("123", "T1", "A1") is False
    assert db.dar_baixa_manual(cnpj="123", tarefa_id="T1", atividade_id="A1",
                               arquivo_nome="g.pdf", competencia="2026-06") is True
    assert db.ja_enviado("123", "T1", "A1") is True
    assert ("123", "T1", "A1") in db.chaves_enviadas()
    # Idempotente: não dá baixa de novo.
    assert db.dar_baixa_manual(cnpj="123", tarefa_id="T1", atividade_id="A1",
                               arquivo_nome=None, competencia="2026-06") is False


def test_acessos_por_envio_em_lote():
    e1, e2 = _novo_envio(), _novo_envio()
    for eid in (e1, e2):
        db.registrar_acesso(envio_id=eid, token=db.garantir_token_publico(eid),
                            evento="pagina", ip="8.8.8.8")
    resumo = db.acessos_por_envio([e1, e2])
    assert set(resumo) == {e1, e2}
    assert db.acessos_por_envio([]) == {}
