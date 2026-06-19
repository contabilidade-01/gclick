"""SQLite — clientes (CNPJ↔WhatsApp) e auditoria de envios."""

from __future__ import annotations

import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from . import config


def agora_iso() -> str:
    """Timestamp UTC no formato 'YYYY-MM-DDTHH:MM:SS' (sem offset).

    Usa datetime.now(timezone.utc) — datetime.utcnow() está deprecado no
    Python 3.12+. O .replace(tzinfo=None) preserva o MESMO formato textual
    de antes (sem '+00:00'), para não quebrar registros nem comparações já
    gravadas no banco.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")

SCHEMA = """
CREATE TABLE IF NOT EXISTS clientes (
  cnpj TEXT PRIMARY KEY,
  apelido TEXT,
  whatsapp TEXT,
  ativo INTEGER DEFAULT 1,
  obrigacoes_aceitas TEXT,
  atualizado_em TEXT
);

CREATE TABLE IF NOT EXISTS envios (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  enviado_em TEXT NOT NULL,
  cnpj TEXT NOT NULL,
  whatsapp TEXT,
  tarefa_id TEXT NOT NULL,
  atividade_id TEXT NOT NULL,
  arquivo_nome TEXT,
  competencia TEXT,
  uazapi_message_id TEXT,
  status TEXT NOT NULL,
  erro TEXT,
  pdf_local_path TEXT
);

CREATE INDEX IF NOT EXISTS idx_envios_chave
  ON envios(cnpj, tarefa_id, atividade_id, status);

CREATE TABLE IF NOT EXISTS tipos_padrao (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  codigo TEXT UNIQUE NOT NULL,
  nome TEXT NOT NULL,
  matchers TEXT NOT NULL,
  ativo INTEGER DEFAULT 1,
  ordem INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tarefas_ocultas (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cnpj TEXT NOT NULL,
  tarefa_id TEXT NOT NULL,
  atividade_id TEXT,
  motivo TEXT,
  oculto_em TEXT NOT NULL,
  UNIQUE(cnpj, tarefa_id, atividade_id)
);

CREATE TABLE IF NOT EXISTS config_runtime (
  chave TEXT PRIMARY KEY,
  valor TEXT,
  atualizado_em TEXT,
  atualizado_por TEXT
);

CREATE INDEX IF NOT EXISTS idx_ocultas_cnpj
  ON tarefas_ocultas(cnpj, tarefa_id);

-- Rastreio de acesso aos documentos enviados por LINK (/g/{token}).
-- Append-only: é a prova de quando/onde o cliente abriu/baixou. Nunca apagar.
CREATE TABLE IF NOT EXISTS acessos_documento (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  envio_id INTEGER NOT NULL,
  token TEXT NOT NULL,
  evento TEXT NOT NULL,          -- 'pagina' (abriu a página) | 'download' (baixou o PDF)
  ip TEXT,
  cidade TEXT,
  estado TEXT,
  pais TEXT,
  user_agent TEXT,
  eh_bot INTEGER DEFAULT 0,      -- 1 = preview do WhatsApp/Meta (não conta como abertura real)
  acessado_em TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_acessos_envio
  ON acessos_documento(envio_id, evento, eh_bot);

-- Caixa de Saída do envio automático: o gatilho NÃO envia direto; ele enfileira
-- aqui as guias elegíveis e o operador APROVA antes de ir ao WhatsApp.
-- UNIQUE garante que a mesma guia nunca é enfileirada 2× (dedup automático).
CREATE TABLE IF NOT EXISTS aprovacoes_pendentes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cnpj TEXT NOT NULL,
  cliente_apelido TEXT,
  tarefa_id TEXT NOT NULL,
  atividade_id TEXT NOT NULL,
  atividade_nome TEXT,
  obrigacao_nome TEXT,
  arquivo_nome TEXT,
  arquivo_url TEXT,
  competencia TEXT,
  data_vencimento TEXT,
  detectado_em TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pendente',   -- 'pendente' | 'aprovado' | 'descartado'
  resolvido_em TEXT,
  UNIQUE(cnpj, tarefa_id, atividade_id)
);

CREATE INDEX IF NOT EXISTS idx_aprovacoes_status
  ON aprovacoes_pendentes(status);
"""

# Tipos padrao pre-populados com matchers iniciais baseados nos nomes que
# aparecem no G-Click da Nescon. O usuario edita pela tela /tipos.
# Formato: (codigo, nome, matchers, ordem, template_mensagem, tem_vencimento)
TIPOS_SEED = [
    ("FGTS",          "FGTS",                   "FGTS",
        10, None, 1),
    ("DCTF_WEB",      "INSS (DCTF Web)",        "DCTF Web|DCTFWeb|DCTF-Web",
        20, None, 1),
    ("INSS",          "INSS / GPS",             "INSS|GPS",
        30, None, 1),
    ("DAS",           "DAS / Simples",          "DAS Simples|DAS|Simples Nacional",
        40, None, 1),
    ("ICMS",          "ICMS",                   "ICMS",
        50, None, 1),
    ("ISS",           "ISS",                    "ISS|ISSQN",
        60, None, 1),
    ("RECIBO_PAGTO",  "Recibos da Folha",
        "Anexar recibo de pagamento|Recibo de Pagamento|Recibo de Adiantamento",
        100,
        "Olá! Segue o *recibo da folha* referente a {competencia}.\n"
        "Qualquer dúvida estamos à disposição.",
        0),
    ("EXTRATO_FOLHA", "Extrato da Folha",
        "Anexar Folha de Pagamento (Extrato)|Folha de Pagamento (Extrato)",
        110,
        "Olá! Segue o *extrato da folha de pagamento* referente a {competencia}.\n"
        "Qualquer dúvida estamos à disposição.",
        0),
]

# Colunas extras de clientes (acrescentadas via ALTER, idempotente)
_COLUNAS_EXTRAS_CLIENTES = [
    ("responsavel_nome", "TEXT"),
    ("observacoes", "TEXT"),
    ("email", "TEXT"),
    ("nome_completo", "TEXT"),       # razao social vinda do G-Click
    ("status_gclick", "TEXT"),       # ATIVO / DESATIVADO no G-Click
    ("origem_dados", "TEXT"),        # "gclick" se veio da sync, "manual" se editado
    ("envio_automatico", "INTEGER DEFAULT 0"),  # 1 = recebe envio automatico
]


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(config.DB_PATH, timeout=5.0)
    c.row_factory = sqlite3.Row
    # WAL: leituras não bloqueiam escritas (e vice-versa) — telas que tocam o
    # banco ficam mais responsivas. synchronous=NORMAL é seguro sob WAL.
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init() -> None:
    with conn() as c:
        c.executescript(SCHEMA)
        # Colunas extras em `clientes`
        cols_cli = {r["name"] for r in c.execute("PRAGMA table_info(clientes)")}
        for col, tipo in _COLUNAS_EXTRAS_CLIENTES:
            if col not in cols_cli:
                c.execute(f"ALTER TABLE clientes ADD COLUMN {col} {tipo}")
        # Colunas extras em envios (idempotente)
        cols_env = {r["name"] for r in c.execute("PRAGMA table_info(envios)")}
        if "pdf_local_path" not in cols_env:
            c.execute("ALTER TABLE envios ADD COLUMN pdf_local_path TEXT")
        if "vencimento_pdf" not in cols_env:
            c.execute("ALTER TABLE envios ADD COLUMN vencimento_pdf TEXT")
        if "vencimento_gclick" not in cols_env:
            c.execute("ALTER TABLE envios ADD COLUMN vencimento_gclick TEXT")
        if "origem" not in cols_env:
            c.execute("ALTER TABLE envios ADD COLUMN origem TEXT DEFAULT 'manual'")
        if "token_publico" not in cols_env:
            c.execute("ALTER TABLE envios ADD COLUMN token_publico TEXT")
        # Colunas extras em `tipos_padrao` (template + flag de vencimento)
        cols_tp = {r["name"] for r in c.execute("PRAGMA table_info(tipos_padrao)")}
        if "template_mensagem" not in cols_tp:
            c.execute("ALTER TABLE tipos_padrao ADD COLUMN template_mensagem TEXT")
        if "tem_vencimento" not in cols_tp:
            c.execute("ALTER TABLE tipos_padrao ADD COLUMN tem_vencimento INTEGER DEFAULT 1")
        # Seed inicial dos tipos padrão (só insere se a tabela está vazia)
        n_tipos = c.execute("SELECT COUNT(*) FROM tipos_padrao").fetchone()[0]
        if n_tipos == 0:
            c.executemany(
                """INSERT INTO tipos_padrao
                   (codigo, nome, matchers, ordem, template_mensagem, tem_vencimento)
                   VALUES (?,?,?,?,?,?)""",
                TIPOS_SEED,
            )
        # Migracao: se ja havia tipos antes (sem template), atualiza os 2 novos
        # se ainda nao existirem
        seed_extra_codigos = [t[0] for t in TIPOS_SEED if t[0] in ("RECIBO_PAGTO", "EXTRATO_FOLHA")]
        for cod in seed_extra_codigos:
            ja = c.execute("SELECT 1 FROM tipos_padrao WHERE codigo=?", (cod,)).fetchone()
            if not ja:
                seed = next(t for t in TIPOS_SEED if t[0] == cod)
                c.execute(
                    """INSERT INTO tipos_padrao
                       (codigo, nome, matchers, ordem, template_mensagem, tem_vencimento)
                       VALUES (?,?,?,?,?,?)""",
                    seed,
                )


# ---------- clientes ----------

def upsert_cliente(cnpj: str, apelido: str, whatsapp: str | None = None,
                   ativo: int = 1, obrigacoes_aceitas: str | None = None,
                   envio_automatico: int | None = None) -> None:
    agora = agora_iso()
    with conn() as c:
        # Busca valor atual para não sobrescrever se nao informado
        atual = c.execute("SELECT envio_automatico FROM clientes WHERE cnpj=?", (cnpj,)).fetchone()
        auto_val = envio_automatico if envio_automatico is not None else (atual["envio_automatico"] if atual else 0)
        c.execute(
            """
            INSERT INTO clientes (cnpj, apelido, whatsapp, ativo, obrigacoes_aceitas, atualizado_em, envio_automatico)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cnpj) DO UPDATE SET
              apelido=excluded.apelido,
              whatsapp=COALESCE(excluded.whatsapp, clientes.whatsapp),
              ativo=excluded.ativo,
              obrigacoes_aceitas=excluded.obrigacoes_aceitas,
              atualizado_em=excluded.atualizado_em,
              envio_automatico=COALESCE(excluded.envio_automatico, clientes.envio_automatico)
            """,
            (cnpj, apelido, whatsapp, ativo, obrigacoes_aceitas, agora, auto_val),
        )


def set_whatsapp(cnpj: str, whatsapp: str) -> None:
    agora = agora_iso()
    with conn() as c:
        c.execute(
            "UPDATE clientes SET whatsapp=?, atualizado_em=? WHERE cnpj=?",
            (whatsapp, agora, cnpj),
        )


def listar_clientes() -> list[sqlite3.Row]:
    with conn() as c:
        return list(c.execute("SELECT * FROM clientes ORDER BY apelido"))


def map_whatsapp_por_cnpj() -> dict[str, str]:
    with conn() as c:
        rows = c.execute("SELECT cnpj, whatsapp FROM clientes WHERE ativo=1 AND whatsapp IS NOT NULL AND whatsapp != ''")
        return {r["cnpj"]: r["whatsapp"] for r in rows}


# ---------- envios ----------

def registrar_envio(*, cnpj: str, whatsapp: str | None, tarefa_id: str,
                    atividade_id: str, arquivo_nome: str | None,
                    competencia: str | None, uazapi_message_id: str | None,
                    status: str, erro: str | None = None,
                    origem: str = "manual") -> int:
    agora = agora_iso()
    with conn() as c:
        cur = c.execute(
            """
            INSERT INTO envios
              (enviado_em, cnpj, whatsapp, tarefa_id, atividade_id, arquivo_nome,
               competencia, uazapi_message_id, status, erro, origem)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (agora, cnpj, whatsapp, tarefa_id, atividade_id, arquivo_nome,
             competencia, uazapi_message_id, status, erro, origem),
        )
        return cur.lastrowid


def ja_enviado(cnpj: str, tarefa_id: str, atividade_id: str) -> bool:
    with conn() as c:
        row = c.execute(
            "SELECT 1 FROM envios WHERE cnpj=? AND tarefa_id=? AND atividade_id=? AND status='ok' LIMIT 1",
            (cnpj, tarefa_id, atividade_id),
        ).fetchone()
        return row is not None


def chaves_enviadas() -> set[tuple[str, str, str]]:
    """Retorna {(cnpj, tarefa_id, atividade_id)} de tudo que já foi enviado com sucesso."""
    with conn() as c:
        rows = c.execute(
            "SELECT cnpj, tarefa_id, atividade_id FROM envios WHERE status='ok'"
        )
        return {(r["cnpj"], r["tarefa_id"], r["atividade_id"]) for r in rows}


def set_envio_pdf_local(envio_id: int, caminho: str) -> None:
    with conn() as c:
        c.execute("UPDATE envios SET pdf_local_path=? WHERE id=?", (caminho, envio_id))


def set_envio_token(envio_id: int, token: str) -> None:
    """Grava o token público (link rastreado) gerado para este envio."""
    with conn() as c:
        c.execute("UPDATE envios SET token_publico=? WHERE id=?", (token, envio_id))


def set_envio_vencimentos(envio_id: int, venc_pdf: str | None,
                          venc_gclick: str | None) -> None:
    with conn() as c:
        c.execute(
            "UPDATE envios SET vencimento_pdf=?, vencimento_gclick=? WHERE id=?",
            (venc_pdf, venc_gclick, envio_id),
        )


def get_envio(envio_id: int) -> sqlite3.Row | None:
    with conn() as c:
        return c.execute("SELECT * FROM envios WHERE id=?", (envio_id,)).fetchone()


def listar_envios(limit: int = 200) -> list[sqlite3.Row]:
    with conn() as c:
        return list(c.execute(
            "SELECT * FROM envios ORDER BY id DESC LIMIT ?", (limit,)
        ))


# ---------- rastreio de documentos (link /g/{token}) ----------

def garantir_token_publico(envio_id: int) -> str:
    """Garante que o envio tenha um token público (opaco, não-enumerável) e o
    retorna. Idempotente: se já existe, devolve o mesmo. Usado para montar o
    link rastreado que vai no WhatsApp."""
    with conn() as c:
        row = c.execute(
            "SELECT token_publico FROM envios WHERE id=?", (envio_id,)
        ).fetchone()
        if row and row["token_publico"]:
            return row["token_publico"]
        token = secrets.token_urlsafe(16)
        c.execute("UPDATE envios SET token_publico=? WHERE id=?", (token, envio_id))
        return token


def get_envio_por_token(token: str) -> sqlite3.Row | None:
    if not token:
        return None
    with conn() as c:
        return c.execute(
            "SELECT * FROM envios WHERE token_publico=?", (token,)
        ).fetchone()


def registrar_acesso(*, envio_id: int, token: str, evento: str,
                     ip: str | None = None, cidade: str | None = None,
                     estado: str | None = None, pais: str | None = None,
                     user_agent: str | None = None, eh_bot: int = 0) -> None:
    """Grava um acesso ao documento (append-only). `evento`: 'pagina'|'download'."""
    with conn() as c:
        c.execute(
            """INSERT INTO acessos_documento
               (envio_id, token, evento, ip, cidade, estado, pais,
                user_agent, eh_bot, acessado_em)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (envio_id, token, evento, ip, cidade, estado, pais,
             (user_agent or "")[:300], eh_bot, agora_iso()),
        )


def acessos_por_envio(envio_ids: list[int]) -> dict[int, dict]:
    """Resumo de acessos REAIS (não-bot) para vários envios — 1 query (evita N+1).
    Retorna {envio_id: {aberturas, downloads, ultimo_em, ultima_cidade}}."""
    if not envio_ids:
        return {}
    placeholders = ",".join("?" * len(envio_ids))
    with conn() as c:
        rows = list(c.execute(
            f"""SELECT envio_id, evento, cidade, estado, acessado_em
                FROM acessos_documento
                WHERE eh_bot=0 AND envio_id IN ({placeholders})
                ORDER BY acessado_em""",
            envio_ids,
        ))
    out: dict[int, dict] = {}
    for r in rows:
        d = out.setdefault(r["envio_id"], {
            "aberturas": 0, "downloads": 0, "ultimo_em": None, "ultima_cidade": None,
        })
        if r["evento"] == "pagina":
            d["aberturas"] += 1
        elif r["evento"] == "download":
            d["downloads"] += 1
        d["ultimo_em"] = r["acessado_em"]
        if r["cidade"]:
            d["ultima_cidade"] = f"{r['cidade']}/{r['estado']}" if r["estado"] else r["cidade"]
    return out


def listar_acessos(envio_id: int) -> list[sqlite3.Row]:
    """Timeline completa de acessos de um envio (inclui os marcados como bot)."""
    with conn() as c:
        return list(c.execute(
            "SELECT * FROM acessos_documento WHERE envio_id=? ORDER BY acessado_em DESC",
            (envio_id,),
        ))


# ---------- Caixa de Saída (aprovação manual do envio automático) ----------

def enfileirar_aprovacao(g: dict) -> bool:
    """Coloca uma guia elegível na fila de aprovação. Idempotente: se a guia já
    foi enfileirada antes (qualquer status), o INSERT OR IGNORE não duplica.
    Retorna True se inseriu uma nova linha."""
    with conn() as c:
        cur = c.execute(
            """INSERT OR IGNORE INTO aprovacoes_pendentes
               (cnpj, cliente_apelido, tarefa_id, atividade_id, atividade_nome,
                obrigacao_nome, arquivo_nome, arquivo_url, competencia,
                data_vencimento, detectado_em, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?, 'pendente')""",
            (g.get("cnpj") or "", g.get("cliente_apelido"), g["tarefa_id"],
             g["atividade_id"], g.get("atividade_nome"), g.get("obrigacao_nome"),
             g.get("arquivo_nome"), g.get("arquivo_url"), g.get("competencia"),
             g.get("data_vencimento"), agora_iso()),
        )
        return cur.rowcount > 0


def listar_aprovacoes_pendentes() -> list[sqlite3.Row]:
    with conn() as c:
        return list(c.execute(
            "SELECT * FROM aprovacoes_pendentes WHERE status='pendente' "
            "ORDER BY detectado_em DESC"
        ))


def contar_aprovacoes_pendentes() -> int:
    with conn() as c:
        return c.execute(
            "SELECT COUNT(*) FROM aprovacoes_pendentes WHERE status='pendente'"
        ).fetchone()[0]


def get_aprovacoes_por_ids(ids: list[int]) -> list[sqlite3.Row]:
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    with conn() as c:
        return list(c.execute(
            f"SELECT * FROM aprovacoes_pendentes WHERE id IN ({ph}) AND status='pendente'",
            ids,
        ))


def resolver_aprovacoes(ids: list[int], status: str) -> int:
    """Marca linhas da fila como 'aprovado' ou 'descartado'. Retorna nº afetado."""
    if not ids or status not in ("aprovado", "descartado"):
        return 0
    ph = ",".join("?" * len(ids))
    with conn() as c:
        cur = c.execute(
            f"UPDATE aprovacoes_pendentes SET status=?, resolvido_em=? "
            f"WHERE id IN ({ph}) AND status='pendente'",
            [status, agora_iso(), *ids],
        )
        return cur.rowcount


# ---------- tipos padrao ----------

def listar_tipos(ativos_apenas: bool = False) -> list[sqlite3.Row]:
    with conn() as c:
        sql = "SELECT * FROM tipos_padrao"
        if ativos_apenas:
            sql += " WHERE ativo=1"
        sql += " ORDER BY ordem, nome"
        return list(c.execute(sql))


def upsert_tipo(*, id: int | None, codigo: str, nome: str, matchers: str,
                ativo: int = 1, ordem: int = 0,
                template_mensagem: str | None = None,
                tem_vencimento: int = 1) -> None:
    with conn() as c:
        if id:
            c.execute(
                """UPDATE tipos_padrao
                   SET codigo=?, nome=?, matchers=?, ativo=?, ordem=?,
                       template_mensagem=?, tem_vencimento=?
                   WHERE id=?""",
                (codigo, nome, matchers, ativo, ordem,
                 template_mensagem, tem_vencimento, id),
            )
        else:
            c.execute(
                """INSERT INTO tipos_padrao
                   (codigo, nome, matchers, ativo, ordem, template_mensagem, tem_vencimento)
                   VALUES (?,?,?,?,?,?,?)""",
                (codigo, nome, matchers, ativo, ordem,
                 template_mensagem, tem_vencimento),
            )


def get_tipo_por_codigo(codigo: str) -> sqlite3.Row | None:
    with conn() as c:
        return c.execute("SELECT * FROM tipos_padrao WHERE codigo=?", (codigo,)).fetchone()


def deletar_tipo(id: int) -> None:
    with conn() as c:
        c.execute("DELETE FROM tipos_padrao WHERE id=?", (id,))


# ---------- cliente detalhado ----------

def get_cliente(cnpj: str) -> sqlite3.Row | None:
    with conn() as c:
        return c.execute("SELECT * FROM clientes WHERE cnpj=?", (cnpj,)).fetchone()


# ---------- envio automatico (opt-in por cliente) ----------

def set_envio_automatico(cnpj: str, ativo: int) -> None:
    """Liga/desliga envio automatico para um cliente."""
    agora = agora_iso()
    with conn() as c:
        c.execute(
            "UPDATE clientes SET envio_automatico=?, atualizado_em=? WHERE cnpj=?",
            (ativo, agora, cnpj),
        )


def set_envio_automatico_lote(cnpjs: list[str], ativo: int) -> int:
    """Marca/desmarca envio automatico para varios clientes. Retorna quantos afetados."""
    if not cnpjs:
        return 0
    agora = agora_iso()
    placeholders = ",".join("?" * len(cnpjs))
    with conn() as c:
        c.execute(
            f"UPDATE clientes SET envio_automatico=?, atualizado_em=? WHERE cnpj IN ({placeholders})",
            [ativo, agora] + cnpjs,
        )
        return c.rowcount


def listar_clientes_auto() -> list[sqlite3.Row]:
    """Lista apenas clientes com envio_automatico=1."""
    with conn() as c:
        return list(c.execute(
            "SELECT * FROM clientes WHERE ativo=1 AND envio_automatico=1 AND whatsapp IS NOT NULL AND whatsapp != '' ORDER BY apelido"
        ))


# ---------- configuracao runtime (sobrepoe .env quando presente) ----------

def get_config(chave: str, default: str | None = None) -> str | None:
    with conn() as c:
        row = c.execute("SELECT valor FROM config_runtime WHERE chave=?", (chave,)).fetchone()
        return row["valor"] if row and row["valor"] is not None else default


def set_config(chave: str, valor: str | None, usuario: str | None = None) -> None:
    agora = agora_iso()
    with conn() as c:
        c.execute(
            """INSERT INTO config_runtime (chave, valor, atualizado_em, atualizado_por)
               VALUES (?,?,?,?)
               ON CONFLICT(chave) DO UPDATE SET
                 valor=excluded.valor,
                 atualizado_em=excluded.atualizado_em,
                 atualizado_por=excluded.atualizado_por""",
            (chave, valor, agora, usuario),
        )


def get_config_row(chave: str) -> sqlite3.Row | None:
    with conn() as c:
        return c.execute("SELECT * FROM config_runtime WHERE chave=?", (chave,)).fetchone()


# ---------- tarefas ocultas ----------

def ocultar(*, cnpj: str, tarefa_id: str, atividade_id: str | None = None,
            motivo: str | None = None) -> None:
    """Oculta uma atividade específica (atividade_id != None) ou a tarefa toda
    (atividade_id None). Idempotente — se já está oculta, não faz nada."""
    agora = agora_iso()
    with conn() as c:
        c.execute(
            """INSERT OR IGNORE INTO tarefas_ocultas
               (cnpj, tarefa_id, atividade_id, motivo, oculto_em)
               VALUES (?,?,?,?,?)""",
            (cnpj, tarefa_id, atividade_id, motivo, agora),
        )


def desocultar(*, cnpj: str, tarefa_id: str, atividade_id: str | None = None) -> None:
    with conn() as c:
        if atividade_id is None:
            c.execute(
                "DELETE FROM tarefas_ocultas WHERE cnpj=? AND tarefa_id=? AND atividade_id IS NULL",
                (cnpj, tarefa_id),
            )
        else:
            c.execute(
                "DELETE FROM tarefas_ocultas WHERE cnpj=? AND tarefa_id=? AND atividade_id=?",
                (cnpj, tarefa_id, atividade_id),
            )


def chaves_ocultas() -> tuple[set[tuple[str, str]], set[str]]:
    """Retorna ({(cnpj, tarefa_id)}_atividade_nula, {cnpj}_cliente_todo).
    Implementação simples: dois sets — um de (cnpj, tarefa_id, atividade_id), outro de (cnpj, tarefa_id) quando a atividade_id é NULL."""
    with conn() as c:
        rows = list(c.execute(
            "SELECT cnpj, tarefa_id, atividade_id FROM tarefas_ocultas"
        ))
    individuais: set[tuple[str, str, str]] = set()
    tarefa_inteira: set[tuple[str, str]] = set()
    for r in rows:
        if r["atividade_id"]:
            individuais.add((r["cnpj"], r["tarefa_id"], r["atividade_id"]))
        else:
            tarefa_inteira.add((r["cnpj"], r["tarefa_id"]))
    return individuais, tarefa_inteira


def listar_ocultas() -> list[sqlite3.Row]:
    with conn() as c:
        return list(c.execute("SELECT * FROM tarefas_ocultas ORDER BY oculto_em DESC"))


def ocultar_cliente_competencia(cnpj: str, tarefa_ids: list[str],
                                 motivo: str = "Ignorado em lote") -> int:
    """Oculta todas as tarefas listadas para o cliente. Retorna quantas inseriu."""
    agora = agora_iso()
    n = 0
    with conn() as c:
        for tid in tarefa_ids:
            cur = c.execute(
                """INSERT OR IGNORE INTO tarefas_ocultas
                   (cnpj, tarefa_id, atividade_id, motivo, oculto_em)
                   VALUES (?,?,NULL,?,?)""",
                (cnpj, tid, motivo, agora),
            )
            n += cur.rowcount
    return n


def ocultar_em_lote(pares: list[tuple[str, str, str | None]],
                    motivo: str = "Oculto em lote") -> int:
    """Oculta múltiplas atividades de uma vez em uma única transação.

    `pares`: lista de (cnpj, tarefa_id, atividade_id). `atividade_id=None`
    oculta a tarefa inteira (semântica idêntica à do `ocultar()`).
    Idempotente — usa INSERT OR IGNORE.
    Retorna quantas linhas foram efetivamente inseridas (ignora duplicatas).
    """
    if not pares:
        return 0
    agora = agora_iso()
    n = 0
    with conn() as c:
        for cnpj, tarefa_id, atividade_id in pares:
            cur = c.execute(
                """INSERT OR IGNORE INTO tarefas_ocultas
                   (cnpj, tarefa_id, atividade_id, motivo, oculto_em)
                   VALUES (?,?,?,?,?)""",
                (cnpj, tarefa_id, atividade_id, motivo, agora),
            )
            n += cur.rowcount
    return n


# ---------- cliente detalhado ----------

def atualizar_cliente_detalhado(*, cnpj: str, apelido: str, whatsapp: str | None,
                                 ativo: int, responsavel_nome: str | None,
                                 observacoes: str | None, email: str | None,
                                 obrigacoes_aceitas: str | None,
                                 envio_automatico: int = 0) -> None:
    agora = agora_iso()
    with conn() as c:
        c.execute(
            """
            UPDATE clientes SET
              apelido=?, whatsapp=?, ativo=?, responsavel_nome=?,
              observacoes=?, email=?, obrigacoes_aceitas=?, atualizado_em=?,
              origem_dados='manual', envio_automatico=?
            WHERE cnpj=?
            """,
            (apelido, whatsapp, ativo, responsavel_nome, observacoes,
             email, obrigacoes_aceitas, agora, envio_automatico, cnpj),
        )


def sync_cliente_do_gclick(*, cnpj: str, apelido: str, nome_completo: str,
                            whatsapp: str | None, email: str | None,
                            responsavel_nome: str | None, status_gclick: str,
                            sobrescrever: bool = False) -> str:
    """Sincroniza UM cliente do G-Click.

    Política em duas camadas:
      1) Sync padrão (sobrescrever=False): preenche apenas onde o local está vazio.
      2) "Forçar sobrescrita" (sobrescrever=True): sobrescreve o que veio do G-Click,
         mas **ainda preserva** os campos do cliente quando `origem_dados='manual'`.
         Ou seja: edição manual pela UI vira "trava" permanente. Isso protege
         contra o caso de o G-Click ter dado errado (ex.: número fixo sem o 9)
         e o usuário ter corrigido aqui — mesmo um Forçar não destrói a correção.

    Sempre atualiza: nome_completo, status_gclick.
    ativo: se G-Click marca DESATIVADO e nosso ativo=1, marca como 0.

    Retorna: 'novo' | 'atualizado'.
    """
    agora = agora_iso()
    with conn() as c:
        atual = c.execute("SELECT * FROM clientes WHERE cnpj=?", (cnpj,)).fetchone()
        ativo_calc = 0 if status_gclick == "DESATIVADO" else 1

        if atual is None:
            c.execute(
                """
                INSERT INTO clientes
                  (cnpj, apelido, nome_completo, whatsapp, email, responsavel_nome,
                   ativo, status_gclick, atualizado_em, origem_dados)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (cnpj, apelido, nome_completo, whatsapp, email, responsavel_nome,
                 ativo_calc, status_gclick, agora, "gclick"),
            )
            return "novo"

        # Edições manuais (qualquer campo editado por /clientes/.../editar)
        # ficam imunes mesmo no Forçar sobrescrita.
        origem = atual["origem_dados"] if "origem_dados" in atual.keys() else None
        protegido_por_edicao_manual = (origem == "manual")

        def pick(campo: str, novo_valor):
            # Edição manual sempre vence
            if protegido_por_edicao_manual:
                atual_v = atual[campo] if campo in atual.keys() else None
                if atual_v not in (None, ""):
                    return atual_v
            if sobrescrever:
                return novo_valor
            atual_v = atual[campo] if campo in atual.keys() else None
            return atual_v if atual_v not in (None, "") else novo_valor

        novo_apelido = pick("apelido", apelido) or apelido
        novo_wpp = pick("whatsapp", whatsapp)
        novo_email = pick("email", email)
        novo_resp = pick("responsavel_nome", responsavel_nome)
        # Se G-Click marcou DESATIVADO, força inativo. Caso contrário mantém o atual.
        novo_ativo = 0 if status_gclick == "DESATIVADO" else atual["ativo"]
        # Preserva envio_automatico se existir
        if "envio_automatico" in atual.keys():
            novo_auto = atual["envio_automatico"]
        else:
            novo_auto = 0

        c.execute(
            """
            UPDATE clientes SET
              apelido=?, nome_completo=?, whatsapp=?, email=?, responsavel_nome=?,
              ativo=?, status_gclick=?, atualizado_em=?, envio_automatico=?
            WHERE cnpj=?
            """,
            (novo_apelido, nome_completo, novo_wpp, novo_email, novo_resp,
             novo_ativo, status_gclick, agora, novo_auto, cnpj),
        )
        return "atualizado"
