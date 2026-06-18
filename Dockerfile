# =============================================================================
# GCLICK — Dockerfile para deploy no EasyPanel (SQLite persistente)
# =============================================================================
# - Dados (banco + PDFs) ficam em /app/data → montar UM volume persistente lá.
# - Credenciais vêm das ENV VARS do EasyPanel (não da imagem).
# =============================================================================

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    DATA_DIR=/app/data

# Usuário não-root (segurança)
RUN groupadd --gid 1000 appgroup && \
    useradd --uid 1000 --gid 1000 --create-home appuser

WORKDIR /app

# Dependências Python primeiro (aproveita cache de camada do Docker)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Apenas o código da aplicação. Dados e segredos NÃO entram na imagem
# (ver .dockerignore) — vêm do volume e das env vars do EasyPanel.
COPY app/ ./app/

# Diretório de dados persistentes — o volume do EasyPanel é montado aqui.
RUN mkdir -p /app/data/guias && chown -R appuser:appgroup /app

USER appuser

# Health check leve (/login é público e não consulta o G-Click)
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/login').status==200 else 1)" || exit 1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
