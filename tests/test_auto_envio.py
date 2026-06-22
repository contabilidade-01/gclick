"""Testes da Fase 2 — envio automático por gatilho (lógica pura, sem rede)."""

from __future__ import annotations

from datetime import datetime, timedelta

from app import config, db, helpers


def _tarefa(status="A", venc="2026-06-20", concl=None):
    t = {"id": "T1", "status": status, "dataVencimento": venc,
         "clienteInscricao": "12345678000199", "clienteApelido": "ACME",
         "nome": "FGTS"}
    if concl:
        t["dataConclusao"] = concl
    return t


def _ativ_upload(respondida_em="2026-06-19 10:00"):
    return {"id": "A1", "nome": "Anexar guia FGTS", "tipo": "P",
            "respondida": True, "respondidaEm": respondida_em,
            "arquivos": [{"nome": "fgts.pdf", "url": "https://s3/fgts.pdf"}]}


def _ativ_enviar_cliente(respondida=True, respondida_em="2026-06-19 11:00"):
    return {"id": "A2", "nome": "Enviar para o Cliente", "tipo": "E",
            "respondida": respondida, "respondidaEm": respondida_em, "arquivos": []}


def test_gatilho_enviar_cliente_dispara(monkeypatch):
    dados = [(_tarefa(), [_ativ_upload(), _ativ_enviar_cliente()])]
    monkeypatch.setattr(helpers, "carregar_tarefas_e_ativs", lambda *a, **k: dados)
    elegiveis = helpers.guias_elegiveis_auto("2026-06", "enviar_cliente", "2026-06-01")
    assert len(elegiveis) == 1
    assert elegiveis[0]["arquivo_url"] == "https://s3/fgts.pdf"


def test_gatilho_nao_dispara_sem_enviar_cliente(monkeypatch):
    # Sem a atividade "Enviar para o Cliente" → não elegível no gatilho primário.
    dados = [(_tarefa(), [_ativ_upload()])]
    monkeypatch.setattr(helpers, "carregar_tarefas_e_ativs", lambda *a, **k: dados)
    assert helpers.guias_elegiveis_auto("2026-06", "enviar_cliente", "2026-06-01") == []


def test_gatilho_respeita_corte(monkeypatch):
    # Liberada em 10/06, corte em 15/06 → não dispara (evita histórico retroativo).
    dados = [(_tarefa(), [_ativ_upload(), _ativ_enviar_cliente(respondida_em="2026-06-10 09:00")])]
    monkeypatch.setattr(helpers, "carregar_tarefas_e_ativs", lambda *a, **k: dados)
    assert helpers.guias_elegiveis_auto("2026-06", "enviar_cliente", "2026-06-15") == []


def test_gatilho_concluida(monkeypatch):
    dados = [(_tarefa(status="C", concl="2026-06-19 12:00"), [_ativ_upload()])]
    monkeypatch.setattr(helpers, "carregar_tarefas_e_ativs", lambda *a, **k: dados)
    assert len(helpers.guias_elegiveis_auto("2026-06", "concluida", "2026-06-01")) == 1
    # Tarefa em aberto não dispara no gatilho "concluida".
    dados2 = [(_tarefa(status="A"), [_ativ_upload()])]
    monkeypatch.setattr(helpers, "carregar_tarefas_e_ativs", lambda *a, **k: dados2)
    assert helpers.guias_elegiveis_auto("2026-06", "concluida", "2026-06-01") == []


def test_auto_runtime_defaults():
    cfg = config.get_auto_envio_runtime()
    assert cfg == {"ativo": False, "gatilho": "enviar_cliente", "intervalo_min": 15}


def test_auto_runtime_le_config():
    db.set_config("auto_envio_ativo", "1")
    db.set_config("auto_gatilho", "concluida")
    db.set_config("auto_intervalo_min", "30")
    cfg = config.get_auto_envio_runtime()
    assert cfg == {"ativo": True, "gatilho": "concluida", "intervalo_min": 30}


# ---------- Caixa de Saída (aprovação manual) ----------

def _guia():
    return {"cnpj": "12345678000199", "cliente_apelido": "ACME", "tarefa_id": "T1",
            "atividade_id": "A1", "atividade_nome": "Anexar guia", "obrigacao_nome": "FGTS",
            "arquivo_nome": "fgts.pdf", "arquivo_url": "https://s3/fgts.pdf",
            "competencia": "2026-06", "data_vencimento": "2026-06-20"}


def test_enfileirar_idempotente():
    assert db.enfileirar_aprovacao(_guia()) is True   # 1ª vez insere
    assert db.enfileirar_aprovacao(_guia()) is False  # 2ª vez não duplica
    assert db.contar_aprovacoes_pendentes() == 1


def test_resolver_aprovacoes():
    db.enfileirar_aprovacao(_guia())
    pend = db.listar_aprovacoes_pendentes()
    assert len(pend) == 1
    assert db.resolver_aprovacoes([pend[0]["id"]], "descartado") == 1
    assert db.contar_aprovacoes_pendentes() == 0
    # Descartado não volta a ser enfileirado (UNIQUE protege).
    assert db.enfileirar_aprovacao(_guia()) is False


def test_ciclo_enfileira_nao_envia(monkeypatch):
    # Cliente com opt-in e WhatsApp; gatilho disparado; corte antigo.
    db.upsert_cliente("12345678000199", "ACME", whatsapp="5511963234599",
                      envio_automatico=1)
    db.set_config("auto_ativado_em", "2000-01-01")
    dados = [(_tarefa(), [_ativ_upload(), _ativ_enviar_cliente()])]
    monkeypatch.setattr(helpers, "carregar_tarefas_e_ativs", lambda *a, **k: dados)

    helpers._ciclo_auto_envio({"gatilho": "enviar_cliente"})
    assert db.contar_aprovacoes_pendentes() == 1   # foi para a fila
    # Nenhum envio foi registrado (nada saiu no WhatsApp).
    assert db.chaves_enviadas() == set()
    # Rodar de novo não duplica.
    helpers._ciclo_auto_envio({"gatilho": "enviar_cliente"})
    assert db.contar_aprovacoes_pendentes() == 1


def test_piloto_runtime():
    assert config.get_piloto_runtime() == {"ativo": False, "numero": ""}
    db.set_config("auto_piloto_numero", "5511984630568")
    db.set_config("auto_piloto_ativo", "1")
    assert config.get_piloto_runtime() == {"ativo": True, "numero": "5511984630568"}
    # Sem número, 'ativo' vira False (proteção contra ligar apontando p/ nada).
    db.set_config("auto_piloto_numero", "")
    assert config.get_piloto_runtime()["ativo"] is False


# ---------- limpeza de PDFs antigos ----------

def _envio_com_pdf(monkeypatch, tmp_path, enviado_em):
    """Cria um envio + arquivo PDF físico, com data de envio forçada."""
    monkeypatch.setattr(config, "PASTA_GUIAS", tmp_path / "guias")
    config.PASTA_GUIAS.mkdir(parents=True, exist_ok=True)
    eid = db.registrar_envio(cnpj="123", whatsapp="5511963234599", tarefa_id="T",
                             atividade_id="A", arquivo_nome="g.pdf",
                             competencia="2026-06", uazapi_message_id="m", status="ok")
    arq = config.PASTA_GUIAS / f"{eid:06d}_g.pdf"
    arq.write_bytes(b"%PDF-fake")
    db.set_envio_pdf_local(eid, f"guias/{eid:06d}_g.pdf")
    with db.conn() as c:
        c.execute("UPDATE envios SET enviado_em=? WHERE id=?", (enviado_em, eid))
    return eid, arq


def test_limpeza_apaga_antigos_preserva_novos(monkeypatch, tmp_path):
    # 1 antigo (8 meses) e 1 recente (hoje).
    antigo_dt = (datetime.now() - timedelta(days=8 * 30)).isoformat()
    eid_velho, arq_velho = _envio_com_pdf(monkeypatch, tmp_path, antigo_dt)
    eid_novo, arq_novo = _envio_com_pdf(monkeypatch, tmp_path, datetime.now().isoformat())

    assert helpers.limpar_pdfs_antigos() == 1
    assert not arq_velho.exists()                      # antigo apagado
    assert arq_novo.exists()                           # recente preservado
    # Vínculo limpo no banco do antigo; registro permanece.
    assert db.get_envio(eid_velho)["pdf_local_path"] is None
    assert db.get_envio(eid_velho) is not None


def test_excluir_pdf_manual(monkeypatch, tmp_path):
    eid, arq = _envio_com_pdf(monkeypatch, tmp_path, datetime.now().isoformat())
    assert helpers.excluir_pdf_envio(eid) is True
    assert not arq.exists()
    assert db.get_envio(eid)["pdf_local_path"] is None


def test_limpeza_runtime_defaults():
    cfg = config.get_limpeza_runtime()
    assert cfg["ativa"] is True and cfg["intervalo_h"] == 24 and cfg["retencao_meses"] == 6
