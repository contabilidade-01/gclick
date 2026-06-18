# GCLICK — Envio Automatizado de Guias Fiscais por WhatsApp

Sistema que **busca as guias fiscais no Omie.G-Click e as distribui aos clientes do escritório via WhatsApp**, automaticamente. Substitui os gateways pagos do G-Click (DigiSac/Zappy/WhatsContábil/Onecode, ~R$ 500/mês) por uma solução própria.

> **Uso interno — Nescon Contabilidade.** MVP em produção (local). Projeto preparado para deploy em VPS (EasyPanel).

---

## 1. Visão geral

```
Omie.G-Click  ──►  Python / FastAPI  ──►  uazapi  ──►  WhatsApp do cliente
 (fonte do dado)    (regras + UI + log)    (envio)
```

- **G-Click = fonte do dado**: entrega CNPJ, tipo de obrigação e o PDF da guia (URL S3), tudo estruturado via API.
- **Python/FastAPI = orquestrador**: classifica, valida, aplica regras de negócio, registra auditoria, serve a interface web.
- **uazapi = canal de envio**: API de WhatsApp. Envia o PDF como **anexo** ou como **botão de link** (configurável).

## 2. Stack

| Camada | Tecnologia |
|---|---|
| Backend | FastAPI + Uvicorn |
| Banco | SQLite (modo WAL) |
| Templates | Jinja2 (server-side, sem SPA) |
| Envio WhatsApp | uazapi (`/send/media`, `/send/menu`) |
| PDF | pdfplumber (extração de vencimento) |
| Auth | bcrypt + cookie assinado (itsdangerous) |
| Testes | pytest |

## 3. Estrutura do projeto

```
GCLICK/
├── app/
│   ├── main.py            # FastAPI app + lifespan (init DB, prewarm cache)
│   ├── config.py          # Lê .env + config_runtime (SQLite sobrepõe .env)
│   ├── db.py              # SQLite: schema, migrações idempotentes, helpers
│   ├── gclick.py          # Cliente G-Click (auth cache, paginação paralela)
│   ├── uazapi.py          # Cliente uazapi (anexo, botão de URL, diagnóstico)
│   ├── pdf_parser.py      # Extrai data de vencimento do PDF (pdfplumber)
│   ├── tipos.py           # Classificação de guias por tipo (regex + cache)
│   ├── helpers.py         # Legenda, encurtador, throttle, estado do lote, etc.
│   ├── auth.py            # Login bcrypt + cookie de sessão
│   ├── templating.py      # Config Jinja2
│   ├── routes/            # Um router por área (ver §6)
│   └── templates/         # HTML por tela
├── data/                  # Dados PERSISTENTES (volume na VPS) — NÃO versionar
│   ├── dados.db           # Banco SQLite (dados de cliente — LGPD)
│   └── guias/             # PDFs baixados (backup local de cada envio)
├── docs/                  # Documentação e planos (ver §10)
├── scripts/               # Scripts utilitários
├── tests/                 # pytest
├── Dockerfile             # Deploy (EasyPanel/VPS)
├── .dockerignore          # Mantém dados/segredos fora da imagem
├── requirements.txt
└── .env                   # Credenciais (NÃO versionar)
```

## 4. Instalação

```bash
pip install -r requirements.txt
cp .env.example .env          # depois edite com suas credenciais
uvicorn app.main:app --host 127.0.0.1 --port 8000
# abra http://localhost:8000  (login padrão dev: admin / admin)
```

Variáveis do `.env` (ver `.env.example` para o modelo completo):

| Variável | Para quê |
|---|---|
| `GCLICK_CLIENT_ID` / `GCLICK_CLIENT_SECRET` | API do G-Click (painel Omie) |
| `UAZAPI_SUBDOMAIN` / `UAZAPI_TOKEN` | Instância uazapi (também editável pela UI) |
| `APP_USER` / `APP_PASSWORD_HASH` | Login (hash bcrypt — gerar com passlib) |
| `SECRET_KEY` | Assinatura do cookie de sessão (32+ chars) |
| `PUBLIC_BASE_URL` | URL pública (para deploy; link rastreado futuro) |
| `ENVIO_THROTTLE_S` / `ENVIO_MAX_POR_HORA` | Ritmo de envio (anti-bloqueio) |

> **uazapi pela UI**: subdomain e token podem ser salvos em **Configurações** (persistem no SQLite e **sobrepõem** o `.env`, sem reiniciar o servidor).

## 5. As telas

| Tela | O que faz |
|---|---|
| **📊 Dashboard** (`/`) | Visão do dia: alerta de vencimentos de hoje, KPIs do mês, próximos vencimentos, últimos envios, saúde (uazapi/G-Click) |
| **Fila do mês** (`/fila`) | Lista as guias da competência. Filtros: competência, obrigação, cliente, status. Enviar individual, selecionadas ou todas. Ocultar (individual e em lote) |
| **Check de enviados** (`/check`) | Matriz cliente × tipo. Detecta divergência (arquivo ≠ atividade) e guias não classificadas |
| **Tipos de documentos** (`/tipos`) | Catálogo de tipos: nome amigável, termos de classificação (matchers), template, "tem vencimento" |
| **Clientes** (`/clientes`) | Cadastro CNPJ ↔ WhatsApp. Sincroniza do G-Click (preserva edições manuais). Filtros (sem WhatsApp, desativados) |
| **Auditoria** (`/auditoria`) | Histórico de envios com status. Reenvio. Alerta de token inválido nas últimas 24h |
| **⚙ Configurações** (`/configuracoes`) | Credenciais uazapi, teste de conexão, ritmo de envio, **modo de envio (anexo/link)** |
| **Progresso** (`/enviar/progresso`) | Acompanhamento ao vivo do lote (X/N, cliente atual, OK/bloqueado/falha) |

## 6. Regras de negócio (o que importa entender)

Estas são as decisões que tornam o sistema confiável — não óbvias no código:

- **Vencimento vem do PDF, não do G-Click.** O G-Click guarda uma data de vencimento que está sistematicamente errada (caso FGTS Digital: ~10 dias de diferença). O sistema **lê a data dentro do PDF** (`pdf_parser.py`) e usa essa. Se o PDF não traz data confiável, a mensagem **omite** o vencimento (nunca chuta).

- **Retificação = versão mais recente.** Quando uma folha/guia é retificada, o G-Click cria uma **nova atividade com o mesmo nome**. O sistema agrupa por nome e usa só a versão mais recente (`gclick.extrair_guias_pendentes`), marcando "🔄 Retificada".

- **Título amigável por tipo.** A mensagem ao cliente usa o **nome do tipo classificado** (`FGTS`, `INSS (DCTF Web)`, `Recibos da Folha`, `Extrato da Folha`), nunca a descrição interna da tarefa. Editável em **Tipos**.

- **Classificação por arquivo > atividade > obrigação.** Se a equipe subiu o arquivo errado na atividade, o que vale é o **conteúdo do PDF** (nome do arquivo). O Check sinaliza essas divergências.

- **Modo de envio: anexo ou link** (Configurações).
  - **Anexo**: manda o PDF direto (WhatsApp hospeda; não expira; sem rastreio).
  - **Link**: manda mensagem com **botão "📄 Abrir documento"** apontando pro PDF do G-Click. Profissional, sem encurtador. *Limitação: botões aparecem só no app do celular, não no WhatsApp Web.*

- **Camadas de segurança antes de cada envio** (`routes/envio.py`):
  1. WhatsApp em branco → falha.
  2. **Número fixo** (12 dígitos, sem o "9") → bloqueado com aviso (WhatsApp só funciona em celular).
  3. Auto-envio (destino = número da instância) → bloqueado.
  4. `obrigacoes_aceitas` do cliente → respeitado.
  5. Teto de envios/hora → bloqueado se estourar.

- **Anti-duplicidade.** Não reenvia uma guia já enviada com sucesso (`(cnpj, tarefa_id, atividade_id)`).

- **Reenvio pega o número ATUAL do cadastro** (não o congelado no envio antigo) e respeita o modo de envio vigente.

- **Throttling.** Pausa entre envios + teto/hora (configurável) para não bloquear o número no WhatsApp.

## 7. Banco de dados (resumo)

SQLite (`dados.db`), schema evolutivo via `ALTER TABLE` idempotente no boot.

| Tabela | Função |
|---|---|
| `clientes` | CNPJ, apelido, whatsapp, email, responsável, ativo, origem_dados (gclick/manual) |
| `envios` | Auditoria: cnpj, whatsapp, tarefa/atividade, arquivo, status, msg_id, vencimentos, pdf local |
| `tipos_padrao` | Tipos: codigo, nome, matchers, template, tem_vencimento |
| `tarefas_ocultas` | Guias escondidas do nosso sistema (não toca no G-Click) |
| `config_runtime` | Config editável pela UI (uazapi, ritmo, modo de envio) — sobrepõe `.env` |

Localização: **`data/dados.db`** (controlado por `DATA_DIR`; na VPS, o volume persistente é montado em `/app/data`). Os PDFs ficam em `data/guias/`.

> ⚠️ `data/dados.db` contém dados de cliente (LGPD). **Está no `.gitignore`. Faça backup antes de qualquer operação destrutiva.**

## 8. Deploy

Destino: **VPS via EasyPanel** (HTTPS automático, 24/7). Passo a passo completo em [`docs/PLANO_DEPLOY_EASYPANEL.md`](docs/PLANO_DEPLOY_EASYPANEL.md).

Essencial: **volume persistente** para `data/` (banco + PDFs), variáveis de ambiente no painel, domínio + HTTPS. Antes de expor: senha forte (não `admin/admin`), `SECRET_KEY` forte, regenerar `client_secret` do G-Click.

## 9. Roadmap

- **Link rastreado** (saber se o cliente abriu/baixou, quando, por onde — paridade com o e-mail do G-Click). Exige servidor público 24/7 (VPS). Plano em [`docs/PLANO_ENVIO_LINK_RASTREIO.md`](docs/PLANO_ENVIO_LINK_RASTREIO.md).
- Fallback manual (arrastar PDF) para quando o G-Click ainda não tem o anexo.

## 10. Documentação

| Arquivo | Conteúdo |
|---|---|
| `README.md` | Este — visão geral, uso, regras, arquitetura |
| `docs/PLANO_DEPLOY_EASYPANEL.md` | Deploy na VPS passo a passo |
| `docs/PLANO_ENVIO_LINK_RASTREIO.md` | Plano do link rastreado (item futuro) |
| `docs/REVISAO_E_PLANO_MELHORIAS.md` | Revisão técnica (performance, arquitetura) |
| `docs/uazapi-openapi-spec.yaml` | Spec da API uazapi |
| Skill viva (OneDrive `00_Skills/`) | Log completo de decisões/iterações (ponto de retomada) |

## 11. Troubleshooting

| Sintoma | Causa / solução |
|---|---|
| `401 Invalid token` na uazapi | Token expirou/rotacionou. Atualize em **Configurações** (não precisa reiniciar). No plano free a instância é descartada em 1h — use plano pago. |
| Envio falha com `500` para vários | Provável **número fixo** no cadastro (sem o "9"). Veja no aviso da auditoria; corrija o WhatsApp do cliente. |
| Telas demoram a abrir | 1ª carga consulta o G-Click (lento). Cache de 60s aquece; navegação seguinte é instantânea. |
| Reenvio foi pro número errado | Corrigido: reenvio usa o número atual do cadastro. Confirme que o cliente foi salvo. |
| Botão de link não aparece | WhatsApp **Web** não mostra botões — abra no **celular**. |

## 12. Desenvolvimento e testes

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000   # hot-reload
pytest -q                                                    # testes
```

## Segurança

Credenciais fora do código (`.env`/`config_runtime`, no `.gitignore`). Senhas com bcrypt, cookies assinados, validação de WhatsApp (celular BR), anti-duplicidade, rate-limiting. Antes de ir para internet, ver checklist de hardening no plano de deploy.

---

_Uso interno — Nescon Contabilidade._
