"""
teste_extracao_gclick.py
------------------------
Extração de guias do Omie.G-Click — endpoints reais, validados em 2026-06-15.

Fluxo:
1. POST /oauth/token   (client_credentials)               → access_token (1h)
2. GET  /tarefas?categoria=Obrigacao&clientesInscricoes=&nome=...
3. GET  /tarefas/{tarefaId}/atividades                   → arquivos[].url (PDF S3)
4. GET  na URL S3 assinada                                → baixa o PDF

⚠ O client_secret está em API GLICK.txt (NÃO commitar). Regenerar no painel
   ao fim do projeto e mover para variável de ambiente.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import requests

# Garantir UTF-8 no stdout (Windows default = cp1252 quebra emojis/setas)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE_URL = "https://api.gclick.com.br"

# Sem defaults hardcoded — este script vai para o Git. Defina as variáveis de
# ambiente GCLICK_CLIENT_ID / GCLICK_CLIENT_SECRET antes de rodar.
CLIENT_ID = os.environ.get("GCLICK_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GCLICK_CLIENT_SECRET", "")

CNPJ_ALVO = "35736034000123"           # NESCON CONTABILIDADE
OBRIGACAO_ALVO = "FGTS"                # filtra por substring no campo `nome`
DATA_ACAO_INICIO = "2026-01-01"

PASTA_SAIDA = Path(__file__).parent.parent / "data" / "guias"
PASTA_SAIDA.mkdir(exist_ok=True)


def autenticar() -> str:
    r = requests.post(
        f"{BASE_URL}/oauth/token",
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def listar_tarefas(token: str, cnpj: str, nome_obrigacao: str) -> list[dict]:
    r = requests.get(
        f"{BASE_URL}/tarefas",
        headers={"Authorization": f"Bearer {token}"},
        params={
            "categoria": "Obrigacao",
            "clientesInscricoes": cnpj,
            "nome": nome_obrigacao,
            "dataAcaoInicio": DATA_ACAO_INICIO,
            "size": 50,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("content", [])


def listar_atividades(token: str, tarefa_id: str) -> list[dict]:
    r = requests.get(
        f"{BASE_URL}/tarefas/{tarefa_id}/atividades",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def baixar(url: str, destino: Path) -> int:
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    destino.write_bytes(r.content)
    return len(r.content)


def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")


def main() -> int:
    print(f"=== G-Click → {OBRIGACAO_ALVO} para CNPJ {CNPJ_ALVO} ===\n")

    token = autenticar()
    print("✅ autenticado\n")

    tarefas = listar_tarefas(token, CNPJ_ALVO, OBRIGACAO_ALVO)
    print(f"{len(tarefas)} tarefa(s) encontrada(s):")
    # Status: A=Aberto, C=Concluído, D=Dispensado, E=Retificando, O=Retificado, S=Aguardando
    for t in tarefas:
        print(f"  {t['id']:10}  status={t['status']}  ação={t['dataAcao']}  venc={t['dataVencimento']}  {t['nome']}")
    print()

    baixados = 0
    for t in tarefas:
        ativs = listar_atividades(token, t["id"])
        # A guia FGTS fica em uma atividade chamada "FGTS" (tipo P = Produto/upload).
        # DARF de DCTF Web em "DCTF Web". O nome ajuda a separar.
        for a in ativs:
            for f in a.get("arquivos") or []:
                # Pula DCTF Web quando o alvo é FGTS (filtra pelo nome da atividade).
                if OBRIGACAO_ALVO.upper() not in a["nome"].upper():
                    continue
                nome_base = f"{slug(t['clienteApelido'])}_{t['id']}_{slug(f['nome'])}"
                destino = PASTA_SAIDA / nome_base
                tamanho = baixar(f["url"], destino)
                print(f"  ⬇ {destino.name}  ({tamanho/1024:.1f} KB)  status={t['status']}")
                baixados += 1

    print(f"\n✅ {baixados} arquivo(s) baixado(s) em {PASTA_SAIDA}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
