# Revisão técnica aprofundada — GCLICK

**Data:** 2026-06-16  
**Escopo:** robustez, arquitetura, boas práticas, desempenho, adequação ao objetivo. Segurança fora do escopo (dev local).  
**Skill consultada:** `skill-GCLICK-ENVIO-GUIAS-WHATSAPP.md` (não alterada).  
**Projeto:** `C:\Users\Jeandson\OneDrive\01_Jean\00_Claude\00_PROJETOS\GCLICK`

---

## Sumário executivo (leigo)

O sistema **já cumpre o trabalho** para o escritório Nescon: busca guias no G-Click, cruza com WhatsApp do cliente, envia, audita. Não está “mal feito” — está **maduro para MVP local**, com regras de negócio pensadas (retificação, PDF como fonte de vencimento, bloqueios antes de enviar).

O que incomoda no dia a dia — **demora ao abrir/trocar telas** — tem causa identificável: o G-Click é lento e o app consulta centenas de vezes a cada tela pesada, além de gerar HTML grande demais na Fila. Isso é **corrigível sem reescrever o projeto**.

Veredicto neutro: **7/10 para o estágio atual**. Bom o suficiente para operar; precisa de camada de cache persistente + ajustes de arquitetura antes de crescer ou ir para hosting.

---

## 1. Objetivo e estado de maturidade

### 1.1 Fim do projeto

Automatizar a distribuição de guias fiscais (FGTS, DARF/INSS, DAS, recibos, extratos) da Nescon:

```
G-Click (PDF + metadados) → Python/FastAPI (regras + UI) → uazapi → WhatsApp do cliente
```

Substitui gateways pagos do G-Click (~R$ 500/mês). Fallback manual (arrastar PDF) ainda **não implementado**.

### 1.2 O que já existe (inventário real)

| Camada | Arquivos | Estado |
|--------|----------|--------|
| API G-Click | `gclick.py` | Completo: auth cache, paginação paralela, extração de guias, sync clientes |
| WhatsApp | `uazapi.py` | Completo: envio, diagnóstico tipado, token inválido |
| Persistência | `db.py` + `dados.db` | 91 clientes, 17 envios, 8 tipos — schema evolutivo (ALTER idempotente) |
| Classificação | `tipos.py` | Regex configurável, cache LRU |
| PDF | `pdf_parser.py` | Vencimento por regex (pdfplumber) |
| UI | 10 templates Jinja2 | 7 telas operacionais + login + editar cliente |
| Orquestração | `main.py` (1304 linhas) | Monólito: rotas + regra de negócio + montagem de view |

**Skill vs código:** a skill descreve bem o projeto, mas está ~1 dia atrás em alguns detalhes (HTMX planejado, não implementado; 3 telas viraram 7+).

---

## 2. Arquitetura — análise crítica

### 2.1 Diagrama do fluxo de dados

```
                    ┌─────────────────────────────────────────┐
                    │              main.py (1304 lin)          │
                    │  dashboard │ fila │ check │ enviar │ …  │
                    └─────┬───────────┬───────────┬───────────┘
                          │           │           │
              _carregar_tarefas_e_ativs (cache 60s)
                          │           │           │
                    ┌─────▼─────┐ ┌───▼───┐ ┌─────▼─────┐
                    │ gclick.py │ │ db.py │ │ uazapi.py │
                    │  httpx    │ │SQLite │ │  httpx    │
                    └─────┬─────┘ └───┬───┘ └─────┬─────┘
                          │           │           │
                    api.gclick.com.br  dados.db   free.uazapi.com
```

### 2.2 O que está bem separado

- **Integrações externas** isoladas (`gclick`, `uazapi`) — fácil mockar/testar.
- **Domínio G-Click** em `extrair_guias_pendentes()` — consolida retificação, monta dict padronizado.
- **Migrações SQLite** idempotentes — projeto evolui sem quebrar banco existente.
- **Config runtime** (`config_runtime`) — token uazapi atualizável pela UI sem restart.

### 2.3 O que concentra risco

**`main.py` absorveu tudo:** montagem de KPIs, matriz do check, validação pré-envio, download de PDF, geração de legenda, redirects. Com 1304 linhas, qualquer mudança na Fila pode quebrar o Dashboard silenciosamente — porque **compartilham a mesma função `_carregar_tarefas_e_ativs` e loops quase idênticos** (linhas 342–417 vs 475–511 vs 1221–1272).

**Rotas `async` com I/O síncrono:** todas as rotas são `async def`, mas chamam `httpx` síncrono, `ThreadPoolExecutor`, `sqlite3` síncrono. O FastAPI não ganha concorrência real; só adiciona complexidade. Para 1 usuário local, irrelevante; para hosting com 3+ usuários, vira gargalo.

**Duplicação de lógica de classificação:** `_legenda()` usa ordem `arquivo > atividade > obrigação` (correto, iter 7 da skill). Dashboard e Check usam ordem diferente — ver seção 4 (bugs).

---

## 3. Desempenho — diagnóstico detalhado

### 3.1 Onde o tempo vai (telas pesadas: `/`, `/fila`, `/check`)

| Etapa | O que faz | Tempo típico | Controlável? |
|-------|-----------|--------------|--------------|
| A | `GET /tarefas` (paginado, paralelo) | 2–8 s | Parcial — latência do G-Click |
| B | N × `GET /tarefas/{id}/atividades` (N≈130–240, 16 threads) | 5–10 s | Parcial — N fixo pelo volume Nescon |
| C | Processamento Python (extrair guias, classificar, KPIs) | 0,2–0,5 s | Sim |
| D | Queries SQLite (`chaves_enviadas`, `map_whatsapp`, etc.) | <0,05 s | Sim |
| E | Render Jinja2 + transfer HTML | 0,1–2 s | Sim — Fila é o pior caso |

**Total medido (skill, iter performance):** 1ª carga ~37 s → otimizado para ~10–15 s. Com cache 60 s: ~2 s.

### 3.2 Gargalo #1 — API G-Click (externo, structural)

O endpoint `/tarefas` devolve **todas** as obrigações do mês (~237 tarefas jun/2026). Para cada uma, precisa de `/atividades` — **não existe endpoint batch**. Mesmo com paralelismo agressivo (16 workers), são ~150+ round-trips HTTP.

O código já aplicou as otimizações óbvias:
- Pool HTTP keep-alive (`gclick.py:24-28`)
- Paginação paralela de tarefas (`gclick.py:98-104`)
- Atividades em paralelo (`main.py:72-73`)
- Cache TTL 60 s (`main.py:53-76`)

**Conclusão:** sem cache persistente ou snapshot local, **10 s na 1ª carga é piso realista**, não bug.

### 3.3 Gargalo #2 — HTML da Fila (~384 KB documentado na skill)

Cada linha da Fila inclui link direto para URL S3 pré-assinada:

```html
<a href="{{ g.arquivo_url }}" ...>  <!-- URL com ~400-600 caracteres -->
```

131 guias × URL longa + metadados = página HTML enorme. Efeitos:
- Browser demora para parsear DOM
- OneDrive/antivírus podem agravar
- URLs S3 são estáveis

**Correção:** proxy local `/preview/{tarefa_id}/{atividade_id}` ou botão que busca URL on-demand via AJAX — não embutir S3 no HTML.

### 3.4 Gargalo #3 — Navegação full-page reload

Cada clique no menu (Dashboard → Fila → Check) dispara **novo request HTTP completo**:
- Re-renderiza `base.html` + CSS inline (70 linhas repetidas)
- Re-executa `_carregar_tarefas_e_ativs` (cache ajuda só se mesma competência+obrigação e <60 s)
- Check usa competência do filtro; Dashboard **sempre mês corrente** — caches diferentes, miss frequente

HTMX foi decidido na skill, **nunca implementado** — só spinner JS na Fila/Check.

### 3.5 Gargalo #4 — Envio em lote (`POST /enviar`)

Loop síncrono (`main.py:620-708`). Para cada guia com `tem_vencimento=1`:
1. Baixa PDF inteiro do S3 (`_validar_vencimento_no_pdf`)
2. Parseia com pdfplumber
3. Chama uazapi
4. Baixa PDF de novo para backup (reusa bytes se OK)

**Sem throttling** (`time.sleep`) — risco de bloqueio WhatsApp + sobrecarga. Enviar 12 guias pode levar **minutos** e travar a aba do browser até terminar.

### 3.6 Telas que deveriam ser instantâneas

| Rota | Consulta G-Click? | Observação |
|------|-------------------|------------|
| `/clientes` | Só se `?sync=1` | 91 linhas × 8 colunas — leve |
| `/auditoria` | Não | 500 envios — OK |
| `/tipos` | Não | 8 tipos — OK |
| `/configuracoes` | Só se `?testar=1` | OK |
| `/login` | Não | OK |

Se essas também parecem lentas, investigar: **uvicorn --reload** (reimporta app a cada request em dev), OneDrive sync na pasta do projeto, ou antivírus escaneando `dados.db`.

### 3.7 Ineficiências menores (não críticas hoje)

- `ThreadPoolExecutor` **criado e destruído** a cada `_carregar_tarefas_e_ativs` — overhead de threads
- `db.listar_clientes()` chamado 3× em fluxos de clientes (linhas 1043, 1074) — deveria `get_cliente(cnpj)`
- `_invalidar_cache_guias()` **existe mas nunca é chamada** (linha 79) — código morto
- `uazapi.py` não reutiliza client httpx (diferente de `gclick.py`)
- `pdf_parser.extrair_dados()` parseia PDF **duas vezes** (linhas 91-97)

---

## 4. Bugs e inconsistências encontrados

### 4.1 Classificador com ordem diferente entre telas (médio)

Regra de negócio acordada (skill iter 7): **arquivo > atividade > obrigação**.

| Local | Chamada | Ordem |
|-------|---------|-------|
| `_legenda()` / `/enviar` | `classificar(arquivo, atividade, obrigacao)` | ✅ Correto |
| Dashboard KPI urgência | `classificar(atividade, obrigacao)` | ❌ Ignora nome do arquivo |
| `/check` matriz | `classificar(atividade, obrigacao)` | ❌ Ignora nome do arquivo |

**Impacto real:** cliente com recibo anexado na atividade "Extrato" aparece na matriz como EXTRATO_FOLHA, mas ao enviar a mensagem diz "recibo de pagamento". O detector de divergência no Check **partialmente compensa**, mas a matriz em si pode mentir.

### 4.2 Dashboard preso no mês corrente

`competencia = f"{hoje.year}-{hoje.month}"` (linha 319) — sem seletor. Operador que quer ver maio precisa ir à Fila. KPIs do dashboard e fila podem **divergir** se o usuário filtra outro mês na Fila.

### 4.3 Cache não invalida após ocultar/enviar

Comentário linha 710: "Não invalida o cache — só a tabela envios muda". Status `ja_enviado` vem do DB fresco, então **fila atualiza OK**. Mas lista de guias do G-Click no cache pode incluir atividades que mudaram no G-Click (novo upload) por até 60 s — operador precisa "Forçar atualização".

### 4.4 CNPJ duplicado no sync (documentado, não resolvido)

Dois clientes G-Click com mesmo CNPJ (JEANDSON N + NESCON) — PRIMARY KEY sobrescreve. Pode causar envio para WhatsApp errado em edge case.

### 4.5 Template auditoria desatualizado

Banner token inválido ainda diz "atualize `.env`" — mas `/configuracoes` já permite trocar token no SQLite sem restart.

---

## 5. Robustez e regras de negócio

### 5.1 Pontos fortes (merecem manter)

1. **Retificação G-Click:** `extrair_guias_pendentes` agrupa por nome, pega versão mais recente — reduziu 148→131 guias.
2. **PDF > G-Click para vencimento:** decisão correta (FGTS Digital: 10 dias de diferença documentados). Se parser falha, **omite** vencimento na mensagem — não mente pro cliente.
3. **Camadas pré-envio:** formato WhatsApp, anti-auto-envio, `obrigacoes_aceitas`, validação PDF.
4. **Anti-duplicidade:** `(cnpj, tarefa_id, atividade_id)` com status ok.
5. **Backup PDF local + reenvio resiliente:** G-Click down → usa `/d/{id}`.
6. **Sync conservador:** não pisa edição manual unless `sobrescrever=1`.
7. **Modo simulado:** uazapi ausente não quebra fluxo.

### 5.2 Lacunas operacionais

| Lacuna | Risco | Prioridade |
|--------|-------|------------|
| Sem throttling entre envios | Bloqueio número WhatsApp | Alta antes de produção |
| Sem retry automático | Retrabalho manual na auditoria | Média |
| Sem preview antes "Enviar todas" | Envio acidental em lote | Média |
| Sem fila assíncrona de envio | Browser trava minutos | Alta |
| Sem arrastar-e-soltar PDF | Fallback prometido ausente | Baixa (edge case) |
| Sem testes automatizados | Regressão silenciosa (ex.: classificar) | Média |

### 5.3 Qualidade de código — avaliação por módulo

| Módulo | Nota | Comentário |
|--------|------|------------|
| `gclick.py` | 8/10 | Limpo, bem documentado, paralelismo correto |
| `db.py` | 7/10 | Sólido; seção duplicada "cliente detalhado"; sem WAL/index extras |
| `uazapi.py` | 7/10 | Diagnóstico excelente; falta client compartilhado |
| `tipos.py` | 8/10 | Simples e eficaz |
| `pdf_parser.py` | 7/10 | Regex robustas; falta testes com PDFs reais versionados |
| `auth.py` | 7/10 | Adequado pro escopo |
| `main.py` | 5/10 | Funciona, mas monólito, duplicação, inconsistências |
| Templates | 6/10 | CSS inline repetido; HTML inválido (form dentro de tr em clientes) |

---

## 6. O que é suficiente vs o que falta

### Suficiente para operação local diária ✅

- Fila + envio individual/lote
- Dashboard com alertas de vencimento
- Check matricial
- Cadastro/sync clientes
- Auditoria + reenvio
- Tipos configuráveis
- Config uazapi pela UI

### Necessário antes de "produção real" ⚠️

- Cache persistente (sobrevive restart)
- Envio assíncrono + throttling
- Unificar ordem do classificador
- Senha/secret reais, HTTPS (hosting)
- Testes mínimos automatizados

### Nice-to-have (skill backlog) 📋

- HTMX / navegação parcial
- Tracking jurídico de acesso ao PDF (base em `/d/{id}` pronta)
- Modo manual upload PDF
- Paginação/virtualização da Fila

---

## 7. Plano de melhorias (priorizado com rationale)

### Fase 1 — Velocidade percebida (impacto imediato, ~2-3 dias)

| # | Ação | Por quê | Esforço |
|---|------|---------|---------|
| 1.1 | Tabela SQLite `snapshot_guias (competencia, json, atualizado_em)` | Elimina 10s de G-Click a cada navegação; sobrevive restart | M |
| 1.2 | Telas leem snapshot primeiro; refresh G-Click em background ou no botão existente | Operador vê tela em <1s | M |
| 1.3 | Remover URL S3 do HTML da Fila; link via rota proxy ou botão AJAX | Corta ~300KB do HTML | B |
| 1.4 | TTL configurável (5-10 min) + badge "dados de HH:MM" | Menos surpresa pro operador | B |
| 1.5 | Pré-aquecer snapshot no startup | 1ª tela após ligar PC já rápida | B |
| 1.6 | Spinner no Dashboard (já existe na Fila) | Feedback psicológico | B |

### Fase 2 — Correções de consistência (~1-2 dias)

| # | Ação | Por quê |
|---|------|---------|
| 2.1 | Unificar `classificar(arquivo, atividade, obrigacao)` em Dashboard e Check | Matriz e mensagem alinhadas |
| 2.2 | Seletor de competência no Dashboard | Coerência com Fila |
| 2.3 | Atualizar textos auditoria → apontar `/configuracoes` | UX desatualizada |
| 2.4 | Remover ou usar `_invalidar_cache_guias()` | Código morto confunde |

### Fase 3 — Arquitetura e envio (~3-5 dias)

| # | Ação | Por quê |
|---|------|---------|
| 3.1 | Extrair `services/guias.py` + routers FastAPI | `main.py` insustentável acima de 1500 linhas |
| 3.2 | Fila de envio assíncrona (BackgroundTasks ou tabela `envios_pendentes`) | Browser não trava |
| 3.3 | Throttling 0,5-1s entre envios uazapi | Proteção WhatsApp |
| 3.4 | `run_in_threadpool` para chamadas G-Click | Preparar hosting |
| 3.5 | Testes: `extrair_guias_pendentes`, `classificar`, `pdf_parser` | Evitar regressão iter 7 |

### Fase 4 — UX avançada (opcional)

- HTMX no menu (só troca `<main>`)
- Paginação Fila 50/página
- Preview modal antes de lote
- Retry automático status=falha

---

## 8. O que NÃO recomendo

| Proposta | Motivo |
|----------|--------|
| Reescrever em React/Vue | Complexidade 10×; problema é I/O externo, não frontend |
| Microserviços | 1 escritório, 1-2 operadores |
| Parsear PDF para CNPJ (Opção B) | Descartado corretamente — G-Click já entrega estruturado |
| Cache infinito sem refresh | Dados ficam stale; operador perde confiança |

---

## 9. Checklist para aprovação (copiar na skill depois)

- [ ] Fase 1 aprovada
- [ ] Fase 2 aprovada  
- [ ] Fase 3 aprovada
- [ ] Fase 4 selecionada (opcional)

---

## 10. Referências de código

Cache G-Click:
```53:76:app/main.py
_CACHE_TTL_S = 60
_cache_guias: dict[tuple[str, str], tuple[float, list[tuple[dict, list[dict]]]]] = {}

def _carregar_tarefas_e_ativs(...):
    ...
    with ThreadPoolExecutor(max_workers=16) as pool:
        ativs_por_tarefa = list(pool.map(lambda t: gclick.listar_atividades(t["id"]), tarefas))
```

Classificação inconsistente no Check:
```1227:1228:app/main.py
cls = tipos_mod.classificar(g["atividade_nome"], g["obrigacao_nome"])
```

Classificação correta na legenda:
```796:796:app/main.py
cls = tipos_mod.classificar(arquivo, atividade, g.get("obrigacao_nome") or "")
```

URL S3 embutida na Fila:
```85:86:app/templates/fila.html
<a href="{{ g.arquivo_url }}" target="_blank" ...>
```
