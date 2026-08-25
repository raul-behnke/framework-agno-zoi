# Imagem do agente. Um processo por bot: o tenant vem por argumento.
FROM python:3.12-slim

# uv resolve e instala em segundos; o build inteiro cabe em duas camadas.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# git é dependência de BUILD, não de runtime: `zoi-routine` é instalado do
# repositório. A imagem slim não o traz, e o erro só aparece no build limpo —
# localmente o pacote já estava em cache.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Dependências primeiro: mudar código não invalida esta camada.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src/ ./src/
COPY app.py ./
COPY tenants/ ./tenants/
RUN uv sync --frozen --no-dev

# Estado da conversa e esperas pendentes vivem aqui — é o volume.
VOLUME ["/app/.dados"]

ENV PATH="/app/.venv/bin:$PATH"

# O tenant é escolhido no compose. Sem argumento, falha com a lista dos
# disponíveis, que é mais útil que um default arbitrário.
ENTRYPOINT ["python", "app.py"]
