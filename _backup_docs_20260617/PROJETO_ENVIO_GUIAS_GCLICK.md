# Projeto — Envio automático de guias (Nescon) via G-Click + uazapi

Documento vivo. Decisões, pesquisa e log do projeto.

Última atualização: 2026-06-15

---

## 1. Objetivo

Distribuir automaticamente as guias em PDF (DARF, INSS/GPS, FGTS, DAS/Simples) aos clientes do escritório **Nescon** via WhatsApp, **sem** contratar gateways nativos do G-Click (DigiSac, Zappy, WhatsContábil, Onecode — ~R$ 500/mês, descartados). Construção própria em Python.

## 2. Arquitetura escolhida — Opção A (híbrida)

```
Omie.G-Click  ─►  Python (orquestrador)  ─►  uazapi  ─►  WhatsApp do cliente
   (fonte do dado)        (regras/log)        (envio)
```

- **G-Click = fonte do dado**: já entrega CNPJ, tipo de obrigação e URL do anexo (guia) estruturados.
- **uazapi = canal de envio**: endpoint `/send/media` com `type: document`, autenticação por header `token` da instância.
- **Fallback manual "arrastar e soltar"**: mantido para casos em que o G-Click não traz o anexo ou o cadastro do WhatsApp ainda não foi feito.

### Por que não a Opção B pura
A Opção B (ler PDF e deduzir CNPJ + tipo de guia) é frágil e desnecessária: o G-Click já entrega esses dois campos estruturados, sem precisar parsear o PDF.

## 3. Componentes externos

### 3.1 Omie.G-Click — API ✅ VALIDADA EM 2026-06-15
- Doc (Postman): https://documenter.getpostman.com/view/12417251/UV5TFeha
- **BASE_URL: `https://api.gclick.com.br`**
- **Auth: `POST /oauth/token`** — form-urlencoded com `client_id`, `client_secret`, `grant_type=client_credentials`.
  - Resposta: `access_token` (JWT), `token_type=Bearer`, `expires_in=3599` (1h).
  - **Sem `scope` no request** (a doc diz `scope: "write read"` na resposta, mas não exige no pedido).
  - `client_id`/`client_secret` armazenados em `API GLICK.txt` (local — **regenerar ao fim e mover para env var**).
  - JWT decodificado confirma vínculo com NESCON CONTABILIDADE (empresa.id=4781, cnpj 35736034000123).
- **Listar tarefas: `GET /tarefas`** — header `Authorization: Bearer <token>`.
  - Filtros úteis: `categoria=Obrigacao`, `clientesInscricoes=<CNPJ>`, `nome=<FGTS|DARF|DAS|...>`, `dataAcaoInicio=YYYY-MM-DD`, `dataVencimentoInicio/Fim`, `size`, `page`.
  - Resposta paginada: `{ content: [...], totalElements, totalPages }`.
  - Campos da tarefa: `id` (formato `calendario.evento`, ex.: `4.10216`), `status` (A=Aberto, C=Concluído, D=Dispensado, E=Retificando, O=Retificado, S=Aguardando), `dataAcao`, `dataVencimento`, `dataConclusao`, `nome`, `clienteId`, `clienteInscricao`, `clienteApelido`, `obrigacao.{id,nome,frequencia,departamento}`.
- **Listar atividades de uma tarefa: `GET /tarefas/{tarefaId}/atividades`**.
  - Retorna array. Cada atividade tem `id`, `nome`, `tipo` (P=Produto/upload, C=Checklist, E=Etapa, A=Um Arquivo, N=Múltiplos), `respondida`, `arquivos: [{ nome, url }]`.
  - **URL do PDF é um S3 pré-assinado** (`innubem-prod.s3.amazonaws.com/...?X-Amz-Signature=...&X-Amz-Expires=7199`) — vale 2h. Baixar GET simples, sem header.
- **🎯 PROVA-CHAVE RESPONDIDA**: a API entrega o anexo **mesmo em tarefas com status A (Aberta, NÃO concluída)**. Testado em `4.10216` (FGTS Nescon jun/2026, status=A) — PDF disponível e baixado com sucesso. O anexo aparece assim que a atividade de upload é respondida (`respondida=True`), independentemente do status geral da tarefa. **Isso destrava a "Opção A" — podemos enviar a guia ANTES do vencimento**.
- Anatomia da tarefa de obrigação (modelo Nescon): geralmente tem 4 atividades — `DCTF Web` (upload, PDF da guia DCTF), `FGTS` (upload, PDF da guia FGTS), `Validar Calculos` (checklist), `Enviar para o Cliente` (etapa). Filtrar pelo `nome` da atividade para separar DCTF de FGTS.

### 3.2 uazapi — envio WhatsApp
- Spec local: `uazapi-openapi-spec.yaml`
- Server: `https://{subdomain}.uazapi.com` — subdomínio da instância da Nescon a confirmar no painel uazapi.
- Endpoint de envio: `POST /send/media` com payload:
  ```json
  {
    "number": "55DDDNUMERO",
    "type": "document",
    "file": "https://.../guia.pdf",   // URL OU base64
    "docName": "DARF_05-2026.pdf",
    "text": "Segue a guia do mês."
  }
  ```
- Auth: header `token` da instância (uazapi).
- **Pendente**: pegar o `token` e o `subdomain` direto do painel da uazapi (Jeandson). O arquivo `Extensão Lion.txt` foi **descartado** — não vamos usar aquela extensão.

## 4. Artefatos do projeto

| Arquivo | Papel |
|---|---|
| `PROJETO_ENVIO_GUIAS_GCLICK.md` | este documento — decisões, pesquisa, log |
| `teste_extracao_gclick.py` | script Python — autentica no G-Click, descobre endpoints, extrai uma guia de teste |
| `API GLICK.txt` | credenciais G-Click (local, não commitar) |
| ~~`Extensão Lion.txt`~~ | **descartado** — não vamos usar a Extensão Lion |
| `uazapi-openapi-spec.yaml` | spec OpenAPI do envio |

## 5. Próximos passos

✅ **Concluídos em 2026-06-15** (Claude validou direto contra a API):
1. ~~Exportar coleção Postman~~ — feito via endpoint público do documenter.
2. ~~Fixar BASE_URL, token, tarefas, campos~~ — todos confirmados (seção 3.1).
3. ~~Extrair guia de teste FGTS CNPJ 35736034000123~~ — 6 PDFs baixados em `guias_baixadas/`.
4. ~~Validar extração em tarefa NÃO concluída~~ — sim, funciona (tarefa `4.10216` status=A, jun/2026).

🔜 **Próximos**:
5. Pegar `token` + `subdomain` da instância **uazapi** da Nescon direto no painel uazapi (Extensão Lion descartada).
6. Cadastro CNPJ ↔ número WhatsApp (sugestão: SQLite simples no início — colunas `cnpj`, `whatsapp`, `nome_responsavel`, `ativo`, `obrigacoes_aceitas`).
7. Orquestrador Python: para cada tarefa de obrigação com `dataVencimento` nos próximos N dias E atividade de upload `respondida=True` E ainda não enviada → enviar via uazapi `/send/media` (`type=document`, `file=<URL S3>`, `docName=<nome do PDF>`, `text=mensagem padronizada`).
8. Tabela de auditoria (LGPD): `enviado_em`, `cnpj`, `whatsapp`, `tarefa_id`, `atividade_id`, `arquivo_nome`, `uazapi_message_id`, `status_envio`.
9. Throttling: delay entre envios, limite por hora, retry em falha, alerta se número marcado pelo WhatsApp.
10. Modo manual "arrastar e soltar" como fallback (UI mínima — talvez Flet, que já está no ambiente).
11. **Regenerar `client_secret` no painel do G-Click** e mover para variável de ambiente / `.env` gitignored.

## 6. Decisões registradas

- **Gateways nativos do G-Click**: descartados (custo).
- **Opção B pura (parsear PDF)**: descartada (frágil).
- **Opção A híbrida**: escolhida. Manual "arrastar e soltar" fica como fallback.
- **Linguagem**: Python.
- **Extensão Lion**: descartada como canal de envio.
- **Canal de envio**: **uazapi** (`POST /send/media`).
- **Eixo de navegação da UI**: **por competência** (mês), usando `dataVencimentoInicio`/`Fim` como recorte. Filtros secundários: cliente, obrigação, status.
- **Agrupamento de envio**: **uma mensagem por guia** (cliente com 2+ obrigações no mês recebe N mensagens, cada PDF com sua legenda própria).
- **Stack do MVP da página**: **FastAPI + HTML simples** (servidor local, rota `/`, tabela renderizada server-side, ações via POST/HTMX-style — sem SPA).

## 7. Log

- **2026-06-15 (manhã)** — Projeto consolidado. Criados `PROJETO_ENVIO_GUIAS_GCLICK.md` e `teste_extracao_gclick.py` (versão descoberta).
- **2026-06-15 (tarde)** — Claude acessou direto a API do G-Click do próprio ambiente:
  - Coleção Postman baixada via `documenter.gw.postman.com/api/collections/12417251/UV5TFeha`.
  - Auth OAuth2 client_credentials funcionou no primeiro shot — `BASE_URL = https://api.gclick.com.br`.
  - Listadas 13 tarefas FGTS do CNPJ alvo (5 concluídas + 8 abertas).
  - Confirmado que **tarefa Aberta já entrega o anexo** se o upload foi feito (caso 4.10216, jun/2026).
  - 6 PDFs baixados em `guias_baixadas/`. PDF válido (`%PDF-1.4`, 180 KB cada).
  - Script `teste_extracao_gclick.py` reescrito como versão definitiva (sem modo descoberta).
