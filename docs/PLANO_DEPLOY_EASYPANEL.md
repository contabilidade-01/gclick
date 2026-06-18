# Plano de Deploy — GCLICK no EasyPanel (SQLite persistente)

> Passo a passo de produção. Banco: **SQLite com volume persistente** (decidido em 2026-06-18).
> Atualizado em 2026-06-18. Referência EasyPanel: `00_Skills/skill-CRMCONTADOR-DEPLOY.md`.

---

## ⚠️ REGRAS DE ISOLAMENTO (INEGOCIÁVEIS)

1. **BANCO ISOLADO**: o sistema usa SEU PRÓPRIO SQLite em `/app/data`. NUNCA conecta/leia/grave no Supabase ou Postgres do queijeiros. O `.env` do GCLICK não deve ter nenhuma variável do Supabase.
2. **MESMA VPS/SERVIDOR**: o GCLICK roda como App separado no EasyPanel (mesmo servidor do queijeiros), não como parte do docker-compose.
3. **DOCKER-COMPOSE DO QUEIJEIROS**: NÃO ALTERAR. Apenas adicionar item de menu no front-end do queijeiros.

---

## 0. Resumo da arquitetura de deploy

```
GitHub (código)  ──►  EasyPanel (build do Dockerfile)  ──►  Container FastAPI
                                                              │
                          Volume persistente  ───────────────┘
                          /app/data  (dados.db + guias/*.pdf)
```

- **Imagem Docker**: só o código (`app/`) + dependências. **Sem dados, sem segredos.**
- **Volume persistente** montado em **`/app/data`**: guarda o banco SQLite **e** os PDFs.
  É o único ponto que precisa sobreviver a redeploys.
- **Credenciais**: variáveis de ambiente no painel do EasyPanel (não vão no Git nem na imagem).
- **HTTPS + domínio**: EasyPanel resolve com Let's Encrypt automático.

> **Por que SQLite e não Postgres**: 1 operador + ~100 clientes = baixo volume, single-writer.
> SQLite é SQL completo, zero reescrita, backup = copiar 1 arquivo. Migração para Postgres/
> Supabase fica documentada para quando houver multiusuário (ver `PLANO_ENVIO_LINK_RASTREIO.md`
> §arquitetura). A camada `db.py` está isolada — a migração futura não afeta o resto.

---

## 1. Preparação local (já feito em 2026-06-18)

- [x] Banco unificado em `data/` → `config.DATA_DIR` (env, default `ROOT/data`); `DB_PATH` e
      `PASTA_GUIAS` derivam dele. Local e VPS usam a mesma estrutura.
- [x] `Dockerfile` limpo: copia só `app/`, instala `requirements.txt`, roda como usuário
      não-root, `DATA_DIR=/app/data`, healthcheck em `/login`.
- [x] `.dockerignore` criado: exclui `.env`, `data/`, `*.db`, backups, `docs/`, `tests/`,
      `__pycache__`, `.git` — nada de dado/segredo entra na imagem.
- [x] Backups do banco: `data/dados.db` + `dados.db.bak-deploy-*` (raiz).

---

## 2. Segurança ANTES de expor (hardening obrigatório)

```powershell
# 1) SECRET_KEY forte (cookies de sessão)
python -c "import secrets; print(secrets.token_urlsafe(32))"

# 2) Hash bcrypt de uma senha FORTE (não admin/admin)
python -c "from passlib.hash import bcrypt; print(bcrypt.hash('SUA_SENHA_FORTE'))"

# 3) Regenerar o client_secret do G-Click no painel Omie (ficou exposto no dev)
```

- [ ] Senha admin **não** é `admin/admin`
- [ ] `SECRET_KEY` aleatória (32+ chars)
- [ ] `client_secret` do G-Click regenerado
- [ ] Backup do volume agendado (ver §7)

---

## 3. Subir o código no GitHub

```powershell
cd "C:\Users\Jeandson\OneDrive\01_Jean\00_Claude\00_PROJETOS\GCLICK"
git init                      # se ainda não for repo
git add .                     # .gitignore já bloqueia .env, *.db, data/, backups
git commit -m "GCLICK: deploy inicial EasyPanel (SQLite persistente)"
git branch -M main
git remote add origin https://github.com/<seu-usuario>/gclick.git
git push -u origin main
```

> Confirme no GitHub que **NÃO** subiram: `.env`, `data/`, `dados.db*`, `API GLICK.txt`,
> `_backup_docs_*`. (O `.gitignore` já cobre — mas confira.)

---

## 4. Criar o serviço no EasyPanel

1. **EasyPanel → Create → App**
2. **Source**: GitHub → selecionar o repo `gclick`, branch `main`
3. **Build**: **Dockerfile** (detectado na raiz)
4. **Port**: `8000`

### 4.1 Variáveis de ambiente (App → Environment)

```env
# G-Click
GCLICK_CLIENT_ID=<seu_client_id_do_painel_Omie>
GCLICK_CLIENT_SECRET=<seu_client_secret_REGENERADO_no_painel_Omie>

# uazapi (WhatsApp - também editável depois pela UI em /configuracoes)
UAZAPI_SUBDOMAIN=<seu_subdomain>
UAZAPI_TOKEN=<token_da_instancia>

# Login (gerar com os comandos do §2)
APP_USER=admin
APP_PASSWORD_HASH=<hash_bcrypt_da_sua_senha_forte>
SECRET_KEY=<chave_aleatoria_32_chars>

# Dados persistentes + URL pública
DATA_DIR=/app/data
PUBLIC_BASE_URL=https://guias.gestaoempresa.com
APP_HOST=0.0.0.0
APP_PORT=8000
```

### 4.2 Volume persistente (CRÍTICO — sem isso, redeploy apaga tudo)

**App → Mounts → Add Volume**:

| Tipo | Nome do volume | Mount path |
|---|---|---|
| Volume | `gclick-data` | `/app/data` |

> **Um único volume** em `/app/data` cobre o banco (`/app/data/dados.db`) **e** os PDFs
> (`/app/data/guias/`). É só este que precisa de persistência.

### 4.3 Domínio + HTTPS

1. **App → Domains → Add Domain**: `guias.gestaoempresa.com`
2. Apontar o DNS do domínio (registro A) para o IP da VPS do EasyPanel.
3. EasyPanel emite o certificado Let's Encrypt automaticamente (aguardar propagação do DNS).
4. Confirmar que `PUBLIC_BASE_URL` (env) == o domínio com `https://`.

---

## 5. Primeiro deploy + levar os dados atuais

1. **Deploy** (EasyPanel builda a imagem e sobe). O volume começa **vazio** → o app cria um
   `dados.db` novo (em branco) no primeiro boot.
2. **Levar o banco atual** (com os 90 clientes, tipos, histórico) — escolha UMA opção:

   **Opção A — Upload do banco (preserva tudo):**
   - EasyPanel → App → **Files/Console** (ou via volume) → enviar o arquivo local
     `data/dados.db` para `/app/data/dados.db` (sobrescrevendo o vazio).
   - **Reiniciar** o app. Pronto — todos os dados aparecem.

   **Opção B — Começar limpo (mais simples, perde histórico de envios):**
   - Login no app novo → **Clientes → 🔄 Sincronizar do G-Click** (repopula os 90 clientes).
   - **Configurações → uazapi** (cola subdomain + token) → **🩺 Testar conexão**.
   - Os tipos de documento são recriados pelo seed automaticamente.

   > Recomendado: **Opção A** se quiser manter o histórico de auditoria; **Opção B** se um
   > recomeço limpo estiver ok.

---

## 6. Verificação pós-deploy

1. `https://guias.gestaoempresa.com/login` abre com cadeado (HTTPS válido).
2. Login com a senha **nova** (admin/admin NÃO funciona).
3. **Configurações → 🩺 Testar conexão** uazapi = `connected`.
4. **Clientes**: os clientes aparecem (Opção A) ou sincroniza (Opção B).
5. **Fila do mês**: carrega as guias da competência.
6. Enviar 1 guia de teste para um número próprio.
7. **Redeploy de teste** (push qualquer no Git → EasyPanel redeploy) e confirmar que o banco
   e os PDFs **persistem** (volume funcionando).

---

## 7. Backup do volume (rotina)

- EasyPanel → App → **Backups** (se disponível no plano) → agendar **diário**, retenção 7 dias.
- Alternativa manual: baixar periodicamente `/app/data/dados.db` (Console/Files).
- O `dados.db` é pequeno (~70 KB hoje) — backup é instantâneo.

---

## 8. Atualizações futuras

```powershell
# Local: alterar código → commit → push
git add . && git commit -m "ajuste X" && git push
# EasyPanel: Redeploy (ou auto-deploy se configurado). Volume NÃO é afetado.
```

---

## 9. Troubleshooting

| Problema | Solução |
|---|---|
| Banco "vazio" após deploy | O volume começou vazio. Fazer Opção A (upload do `data/dados.db`) ou Opção B (sincronizar). |
| Dados sumiram no redeploy | Volume **não** está montado em `/app/data`. Conferir §4.2. |
| Login não funciona | `APP_USER`/`APP_PASSWORD_HASH` errados nas env vars. |
| `401 Invalid token` uazapi | Atualizar token em **Configurações** (persiste no banco/volume). |
| Guias não carregam | `GCLICK_CLIENT_ID/SECRET` errados ou `client_secret` não regenerado. |
| Link/botão não abre no cliente | Confirmar `PUBLIC_BASE_URL` e que o domínio resolve (DNS + HTTPS). |
| SSL inválido | DNS ainda propagando; EasyPanel reemite o certificado. |

---

## 10. Referências

- `README.md` — visão geral e regras de negócio
- `docs/PLANO_ENVIO_LINK_RASTREIO.md` — próximo passo (link rastreado, usa `PUBLIC_BASE_URL`)
- `00_Skills/skill-CRMCONTADOR-DEPLOY.md` — método de deploy EasyPanel já usado
- `00_Skills/skill-GCLICK-ENVIO-GUIAS-WHATSAPP.md` — log/decisões do projeto
