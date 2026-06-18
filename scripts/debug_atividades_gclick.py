"""
debug_atividades_gclick.py
--------------------------
Script para打印 o JSON COMPLETO de atividades com arquivo da GClick API.

Objetivo: encontrar campos de link PERMANENTE/PÚBLICO que não expirem,
como linkPublico, urlPublica, downloadUrl, hash, id de arquivo, etc.

O e-mail do G-Click usa um link estável que dura meses - descobri se a API
expõe esse mesmo link.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

# Garantir UTF-8 no stdout
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE_URL = "https://api.gclick.com.br"

# Credenciais via variáveis de ambiente
CLIENT_ID = os.environ.get("GCLICK_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GCLICK_CLIENT_SECRET", "")

# Alvo: NESCON + FGTS (tarefa que sabemos que tem arquivo)
CNPJ_ALVO = "35736034000123"
OBRIGACAO_ALVO = "FGTS"


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
            "size": 20,
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


def main() -> int:
    print("=" * 80)
    print("DEBUG: JSON COMPLETO de atividades da GClick API")
    print("=" * 80)
    print()

    if not CLIENT_ID or not CLIENT_SECRET:
        print("ERRO: Defina GCLICK_CLIENT_ID e GCLICK_CLIENT_SECRET")
        print("Exemplo:")
        print("  $ set GCLICK_CLIENT_ID=seu_id")
        print("  $ set GCLICK_CLIENT_SECRET=seu_secret")
        print("  $ python scripts/debug_atividades_gclick.py")
        return 1

    token = autenticar()
    print("✅ autenticado\n")

    tarefas = listar_tarefas(token, CNPJ_ALVO, OBRIGACAO_ALVO)
    print(f"{len(tarefas)} tarefa(s) encontrada(s):\n")

    if not tarefas:
        print("Nenhuma tarefa encontrada. Verifique CNPJ/obrigaçao.")
        return 1

    # Pegar a primeira tarefa com atividades que tenham arquivos
    for t in tarefas:
        print("-" * 80)
        print(f"TAREFA: {t['id']} - {t.get('nome', '?')}")
        print(f"Status: {t.get('status')} | Cliente: {t.get('clienteApelido', '?')}")
        print()

        ativs = listar_atividades(token, t["id"])
        print(f"Total de atividades: {len(ativs)}")
        print()

        # Procurar atividades com arquivos
        for a in ativs:
            arquivos = a.get("arquivos") or []
            if not arquivos:
                continue

            print("=" * 80)
            print(f"ATIVIDADE COM ARQUIVO: {a.get('id')} - {a.get('nome', '?')}")
            print(f"Tipo: {a.get('tipo')} | Respondida: {a.get('respondida')} | RespondidaEm: {a.get('respondidaEm')}")
            print()
            print("JSON COMPLETO DA ATIVIDADE:")
            print("-" * 80)
            print(json.dumps(a, indent=2, ensure_ascii=False))
            print()
            print("-" * 80)
            print("JSON COMPLETO DOS ARQUIVOS (cada item):")
            print("-" * 80)

            for i, arq in enumerate(arquivos):
                print(f"\n--- Arquivo {i+1} ---")
                print(json.dumps(arq, indent=2, ensure_ascii=False))

                # Mostrar todos os campos disponíveis no arquivo
                print("\nCampos disponíveis no arquivo:")
                for k, v in arq.items():
                    # Se for URL, mostrar se é S3 (com X-Amz) ou não
                    if k.lower() in ('url', 'link', 'downloadurl', 'uri'):
                        if isinstance(v, str):
                            if 'X-Amz' in v or 'Signature' in v:
                                print(f"  {k}: [URL S3 ASSINADA - expira em ~2h]")
                            else:
                                print(f"  {k}: [URL ESTÁVEL? Verificar]")
                    print(f"  {k}: {repr(v)[:200]}")

            print()
            print("=" * 80)
            print()

            # Só mostrar a primeira atividade com arquivo
            return 0

    print("Nenhuma atividade com arquivo encontrada.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
