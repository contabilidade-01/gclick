# GCLICK - Envio Automatizado de Guias Fiscais

Sistema de distribuição automatizada de guias fiscais (DARF, FGTS, INSS, DAS) da Omie.G-Click para clientes via WhatsApp.

## Stack

- **Backend**: FastAPI + Uvicorn
- **Database**: SQLite (modo WAL)
- **Templates**: Jinja2 (server-side rendering)
- **Envio**: uazapi (WhatsApp Business)
- **Autenticação**: bcrypt + cookie assinado

## Instalação

```bash
# Clonar o repositório
git clone https://github.com/seu-usuario/gclick.git
cd gclick

# Instalar dependências
pip install -r requirements.txt

# Criar arquivo .env
cp .env.example .env
# Editar .env com suas credenciais

# Iniciar servidor
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## Configuração

Edite o arquivo `.env` com suas credenciais:

```env
# G-Click API (obter no painel Omie)
GCLICK_CLIENT_ID=sua_client_id
GCLICK_CLIENT_SECRET=sua_client_secret

# uazapi (WhatsApp)
UAZAPI_SUBDOMAIN=seu_subdomain
UAZAPI_TOKEN=seu_token

# Login
APP_USER=admin
APP_PASSWORD_HASH=$2b$12$hash_bcrypt_da_senha

# Segurança
SECRET_KEY=sua_chave_secreta_minimo_32_chars
```

## Uso

1. Acesse `http://localhost:8000`
2. Login com as credenciais configuradas
3. **Fila**: lista guias do mês com filtros
4. **Clientes**: gerenciar CNPJ ↔ WhatsApp
5. **Auditoria**: histórico de envios
6. **Configurações**: credenciais e ritmo de envio

### Filtros da Fila

- **Competência**: mês/ano de vencimento
- **Obrigação**: tipo (FGTS, DARF, DAS...)
- **Cliente**: nome ou CNPJ
- **Status**: Todas / Pendentes / Enviadas

## Deploy no EasyPanel

Consulte `docs/PLANO_DEPLOY_EASYPANEL.md` para instruções detalhadas de deploy.

```bash
# Build da imagem Docker
docker build -t gclick .

# Ou usar diretamente com uvicorn
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Desenvolvimento

```bash
# Rodar com hot-reload
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

# Rodar testes
pytest -q
```

## Segurança

- Credenciais **NÃO** são commitadas (`.env` no gitignore)
- Senhas hashadas com bcrypt
- Cookies assinados com itsdangerous
- Validação de formato WhatsApp (celular brasileiro)
- Anti-duplicidade de envios
- Rate-limiting configurável

## Estrutura

```
GCLICK/
├── app/
│   ├── routes/             # Rotas FastAPI
│   ├── templates/          # Templates Jinja2
│   ├── db.py               # SQLite
│   ├── gclick.py           # Cliente G-Click
│   ├── uazapi.py           # Cliente uazapi
│   └── helpers.py          # Funções auxiliares
├── data/guias/             # PDFs baixados
├── docs/                   # Documentação
├── scripts/                # Scripts utilitários
├── tests/                  # Testes pytest
├── Dockerfile              # Deploy Docker
└── requirements.txt        # Dependências
```

## Licença

Uso interno - Nescon Contabilidade
