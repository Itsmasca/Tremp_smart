FROM python:3.13-slim

WORKDIR /app

# Instalar uv para resolución rápida de dependencias
COPY --from=ghcr.io/astral-sh/uv:0.5.1 /uv /usr/local/bin/uv

# Copiar solo lo necesario para instalar dependencias primero (cache layer)
COPY pyproject.toml .

# Instalar en el sistema (sin venv dentro del container) las dependencias declaradas
# en pyproject.toml — única fuente de verdad, sin listas duplicadas aquí
RUN uv pip install --system --no-cache -r pyproject.toml

# Copiar código fuente
COPY app/ app/
COPY main.py .

# Servidor HTTP interno (POST /match, GET /health) + consumer de Redis Streams
EXPOSE 8000
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

CMD ["python", "main.py"]
