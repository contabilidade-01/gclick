# Plano — Envio por link com rastreio de abertura/download (item 7)

> Documento de retomada. Se o limite de uso do Claude acabar, este arquivo tem o
> plano inteiro para continuar de onde paramos. Status: **planejado, não iniciado.**
> Criado em 2026-06-16.

---

## Context (por que estamos fazendo)

Hoje o sistema envia o **PDF anexo** pelo WhatsApp via uazapi `/send/media`. O usuário
quer replicar o que o **G-Click já faz por e-mail**: enviar um **link** em vez do anexo e
**rastrear se o cliente abriu, quando e por onde** (cidade/IP). Isso dá comprovação de
entrega — valor operacional (cobrança: "a guia foi aberta dia X") e jurídico.

**Pré-condição que destrava tudo**: o link só abre no celular do cliente se o servidor
estiver acessível pela internet. Hoje roda em `127.0.0.1`. O usuário vai subir na **VPS via
EasyPanel** (já usa EasyPanel para outros deploys — ver `skill-CRMCONTADOR-DEPLOY`). Sem URL
pública, nada do item 7 funciona.

## O que JÁ existe (fundação ~80% pronta)

| Peça | Onde | Estado |
|---|---|---|
| Rota pública que serve PDF local | `app/routes/envio.py` `/d/{envio_id}` | ✅ existe; já extrai IP+UA mas só loga (`logger.info`), não persiste |
| PDF preservado em disco | `data/guias/{id:06d}_nome.pdf`, path em `envios.pdf_local_path` | ✅ |
| `uazapi.enviar_documento(delay_ms=...)` | `app/uazapi.py` | ✅ (anexo) |
| **`/send/text` com linkPreview** | uazapi spec `docs/uazapi-openapi-spec.yaml:4119` | ✅ na API, ainda não usamos. Campos: `linkPreview`, `linkPreviewTitle`, `linkPreviewDescription`, `linkPreviewImage`, `linkPreviewLarge` |
| SQLite + padrão de helpers | `app/db.py` (`conn()`, `agora_iso()`, `INSERT OR IGNORE`) | ✅ |
| Config runtime (UI sobrepõe .env) | `config_runtime` + `db.get_config/set_config` | ✅ |

## Decisões de design (cravadas)

1. **Token opaco por envio** (não usar id sequencial). `secrets.token_urlsafe(16)` numa
   nova coluna `envios.token_publico`. Motivo: hoje `/d/1`, `/d/2`… deixa qualquer um
   enumerar e ver guia de outro cliente (CNPJ, valores) — **falha de LGPD**. Link vira
   `/g/{token}` não-adivinhável.

2. **Página de visualização intermediária** (não link direto pro PDF). O cliente abre uma
   página HTML nossa (logo Nescon, nome do documento, competência, vencimento, botão
   "Baixar PDF"). Vantagens:
   - Separa **"abriu a página"** de **"baixou o PDF"** (dois eventos distintos = mais força jurídica).
   - O preview do WhatsApp busca a **página leve** (não baixa o PDF inteiro à toa).
   - Branding + aviso "documento disponibilizado em DD/MM".

3. **Filtro de bots/preview**. Quando manda link, o **próprio WhatsApp/Meta faz um acesso**
   para gerar o preview (IP da Meta, UA tipo `WhatsApp/2.x`, `facebookexternalhit`). Sem
   filtrar, polui o rastreio com falso positivo. Marca `eh_bot=1` e a contagem de
   "aberturas reais" ignora bots. Lista de UA: `whatsapp`, `facebookexternalhit`,
   `telegrambot`, `twitterbot`, `bot`, `crawler`, `preview`.

4. **Geo-IP "por onde"**. Resolve cidade/estado/país do IP via **ip-api.com** (grátis,
   45 req/min, sem chave) com cache por IP em memória/SQLite. LGPD: finalidade =
   comprovação de entrega de documento fiscal ao próprio titular (base legal: execução de
   obrigação contratual + legítimo interesse). Registrar no termo de privacidade do escritório.

5. **Escopo configurável por tipo**. Nova coluna `tipos_padrao.modo_envio` (`'anexo'|'link'`).
   Seed: tipos com `tem_vencimento=1` (FGTS, INSS, DAS, DARF) → `'link'`; recibo/extrato →
   `'anexo'`. Editável na tela `/tipos`.

6. **`PUBLIC_BASE_URL`** (env var). Se setada (ex.: `https://guias.nescon.com.br`), usa pra
   montar os links. Fallback `request.base_url` (local). **Crítico** — sem isso o link
   aponta pra localhost e não abre no cliente.

## Esquema novo de banco (`app/db.py`)

```sql
-- coluna nova em envios
ALTER TABLE envios ADD COLUMN token_publico TEXT;   -- secrets.token_urlsafe(16)
ALTER TABLE envios ADD COLUMN modo_envio TEXT;       -- 'anexo' | 'link' (como foi enviado)
ALTER TABLE envios ADD COLUMN pdf_sha256 TEXT;       -- integridade do que foi entregue (fase 3)

-- coluna nova em tipos_padrao
ALTER TABLE tipos_padrao ADD COLUMN modo_envio TEXT DEFAULT 'anexo';

-- tabela nova de acessos (append-only — nunca deletar; é a prova)
CREATE TABLE IF NOT EXISTS acessos_documento (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  envio_id INTEGER NOT NULL,
  token TEXT NOT NULL,
  evento TEXT NOT NULL,            -- 'pagina' | 'download'
  ip TEXT,
  cidade TEXT,
  estado TEXT,
  pais TEXT,
  user_agent TEXT,
  eh_bot INTEGER DEFAULT 0,
  acessado_em TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_acessos_envio ON acessos_documento(envio_id, evento, eh_bot);
```

Helpers a adicionar em `db.py` (seguir padrão `conn()`/`agora_iso()`):
- `set_token_publico(envio_id, token)`, `get_envio_por_token(token)`.
- `registrar_acesso(envio_id, token, evento, ip, cidade, estado, pais, ua, eh_bot)`.
- `contar_acessos(envio_id)` → `{paginas_reais, downloads_reais, primeiro_acesso, ultimo_acesso, ultima_cidade}`.

## Fases (cada uma entrega valor sozinha)

### Fase 0 — Deploy base no EasyPanel (PRÉ-REQUISITO)
Sem isso, link não funciona. Itens:
- **Dockerfile** (não existe ainda): `python:3.12-slim` → `pip install -r requirements.txt` →
  `uvicorn app.main:app --host 0.0.0.0 --port 8000`.
- **Volume persistente** no EasyPanel para `/app/dados.db` E `/app/data/guias/` (senão
  redeploy apaga banco e PDFs).
- **Env vars** no EasyPanel: `GCLICK_CLIENT_ID/SECRET`, `APP_USER`, `APP_PASSWORD_HASH`
  (senha forte — NÃO admin/admin), `SECRET_KEY` (forte), `PUBLIC_BASE_URL`,
  `UAZAPI_SUBDOMAIN/TOKEN` (ou deixar no `config_runtime` via UI).
- **Domínio + HTTPS**: EasyPanel resolve Let's Encrypt automático. Definir subdomínio
  (ex.: `guias.nescon.com.br`).
- **Hardening** (checklist obrigatório antes de expor): senha forte, SECRET_KEY forte,
  regenerar `client_secret` do G-Click (ficou exposto no dev), rate-limit no `/login`,
  backup automático do `dados.db`.
- Referência de deploy EasyPanel: `skill-CRMCONTADOR-DEPLOY.md` (mesmo método já usado).

### Fase 1 — Núcleo do rastreio (link + página + acessos)
- `db.py`: migrações idempotentes (colunas + tabela acima) + helpers.
- `config.py`: ler `PUBLIC_BASE_URL`; helper `url_publica(path)` (usa PUBLIC_BASE_URL ou request.base_url).
- `app/uazapi.py`: nova `enviar_texto(numero, texto, link_preview=True, titulo=None, descricao=None, delay_ms=None)` usando `POST /send/text`.
- `app/routes/` (novo arquivo `documento.py` ou dentro de `envio.py`):
  - `GET /g/{token}` → renderiza `documento.html` (página de visualização). Registra evento `pagina` (com filtro bot).
  - `GET /g/{token}/pdf` → serve o PDF (`FileResponse`). Registra evento `download` (com filtro bot).
  - Manter `/d/{envio_id}` por compat, mas novos envios usam `/g/{token}`.
- `app/templates/documento.html`: página pública com branding Nescon, nome do doc,
  competência, vencimento (se `tem_vencimento`), botão "📄 Baixar PDF", rodapé "documento
  disponibilizado por NESCON CONTABILIDADE em DD/MM/AAAA".
- **Fluxo de envio** (`_processar_lote` em `app/routes/envio.py`): se o tipo tem
  `modo_envio='link'`, gera token, salva, monta `url = config.url_publica(f"/g/{token}")`,
  chama `uazapi.enviar_texto(...)` com a URL + preview. Senão, mantém `enviar_documento` (anexo).
- `app/templates/tipos.html`: dropdown `modo_envio` (anexo|link) por tipo.

### Fase 2 — Inteligência do rastreio (geo + auditoria visual)
- Geo-IP: helper `geo_ip(ip)` → ip-api.com com cache (tabela `geo_cache` ou dict + TTL).
- Filtro de bots robusto em `registrar_acesso`.
- `app/templates/auditoria.html`: coluna "👁 Aberturas" = downloads reais (não-bot); tooltip com último acesso (cidade · hora).
- Nova tela `GET /auditoria/{envio_id}` → timeline completa de acessos (todos os eventos, com bot separado).

### Fase 3 — Reforço jurídico (opcional)
- `pdf_sha256` no envio: grava hash do PDF entregue (prova de integridade do que foi enviado).
- **Webhook uazapi** (`POST /webhook/uazapi`): recebe status de leitura (✓✓ azul) da
  mensagem WhatsApp — outra camada de evidência ("mensagem lida em DD/MM"). Configurar via
  painel uazapi (botão "Configurar Webhook" já visto no painel).
- Página de visualização com aceite opcional ("Confirmo recebimento") gravando IP+hora.

## Comparação com o G-Click (meta de paridade)
G-Click por e-mail informa: **baixou? por onde? quando?** Nós replicamos com:
- baixou? → evento `download` na `acessos_documento`.
- por onde? → geo-IP (cidade/estado) — Fase 2.
- quando? → `acessado_em` de cada evento.
- **bônus** que o e-mail não tem: status de leitura do WhatsApp (✓✓) na Fase 3.

## Verificação end-to-end (quando implementar)
1. Marcar um tipo (ex.: FGTS) como `modo_envio='link'` em `/tipos`.
2. Enviar para um número de teste real. Conferir que chega **mensagem com link** (não anexo) + preview.
3. Abrir o link no celular → conferir página de visualização (branding, nome, botão).
4. Clicar "Baixar PDF" → PDF abre.
5. `/auditoria/{envio_id}`: deve mostrar 1 evento `pagina` + 1 `download`, com IP e cidade, e o acesso do preview do WhatsApp marcado como bot (separado).
6. Tentar `/g/{token_invalido}` → 404. Tentar enumerar → impossível (token aleatório).
7. Em produção (EasyPanel): redeploy não pode perder `dados.db` nem PDFs (testar volume).

## Arquivos a tocar (resumo)
- `app/db.py` — migrações + helpers de token/acessos.
- `app/config.py` — `PUBLIC_BASE_URL` + `url_publica()`.
- `app/uazapi.py` — `enviar_texto()`.
- `app/routes/envio.py` (ou novo `documento.py`) — rotas `/g/{token}`, `/g/{token}/pdf`; lógica de modo no `_processar_lote`.
- `app/routes/auditoria.py` — contagem + tela detalhe.
- `app/templates/` — `documento.html` (novo), `tipos.html`, `auditoria.html`.
- raiz — `Dockerfile` (novo), ajustes EasyPanel.

## Alternativas de URL pública SEM VPS (abordagem interina)

O link só abre no celular do cliente se o servidor for público. Antes/sem a VPS, há 3 caminhos:

### A) Link S3 do próprio G-Click (escolha INTERINA atual — 2026-06-16)
- O PDF do G-Click já vem com URL S3 pública e assinada. Dá pra mandar essa URL como link,
  sem VPS, e abre no celular.
- **Perdas (importante)**:
  - **Sem rastreio nosso** — o acesso vai direto pra AWS, não passa pelo nosso servidor.
    Não sabemos se abriu/baixou/quando/onde. ⚠ Isso é DIFERENTE do rastreio que o G-Click
    faz no e-mail dele (lá o link é rastreado pelo produto de e-mail deles; o link S3 cru
    que NÓS extraímos da API NÃO é).
- **Quando usar**: como ponte rápida pra mandar link leve em vez de PDF, ciente de que
  NÃO há rastreio nosso.

### B) Cloudflare Tunnel (recomendado para validar rastreio sem VPS)
- Expõe `localhost:8000` com URL https pública, grátis, sem cartão, em minutos.
- App continua na máquina local; o túnel só cria a ponte. **Rastreio funciona completo**
  (o acesso do cliente passa pelo nosso servidor via túnel).
- Dois modos:
  - **Efêmero** (`cloudflared tunnel --url http://localhost:8000` → `*.trycloudflare.com`):
    zero config, mas a URL muda a cada reinício.
  - **Nomeado** (conta Cloudflare grátis + domínio próprio): URL fixa e estável
    (`https://guias.seudominio.com`). Esse serve para piloto real.
- **Limitação honesta**: a máquina precisa estar ligada com app + túnel rodando enquanto os
  clientes acessam. Se desligar o PC, links param. Por isso a VPS continua sendo o destino
  final (24/7).
- **Migração sem retrabalho**: implementa-se a Fase 1 apontando `PUBLIC_BASE_URL` para a URL
  do túnel; ao migrar pra VPS/EasyPanel, muda-se só o `PUBLIC_BASE_URL`. Código idêntico.

### C) VPS/EasyPanel (destino final — Fase 0 do plano principal)
- 24/7, URL estável, profissional. É o alvo. Detalhado nas Fases acima.

**Ordem de evolução pretendida**: A (agora, link G-Click cru) → B (Cloudflare Tunnel, quando
quiser validar o rastreio de verdade) → C (VPS/EasyPanel, produção).

## Log
- 2026-06-16 — Plano criado. Aguardando dados de acesso da VPS (EasyPanel) para iniciar Fase 0.
- 2026-06-16 — Adicionada seção "Alternativas sem VPS" (Cloudflare Tunnel detalhado).
  Decisão interina do usuário: **usar o link S3 do G-Click por ora** (ciente: sem rastreio
  nosso e link expira). Túnel/VPS ficam para quando o rastreio for prioridade.
