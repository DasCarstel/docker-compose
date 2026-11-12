FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# Abhängigkeiten zuerst (Layer-Caching)
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Anwendungscode
COPY src /app/src

# Non-root User (uid 10001)
RUN adduser --uid 10001 --disabled-password --no-create-home --gecos "" bons-mcp

USER 10001

EXPOSE 8000

CMD ["uvicorn", "bons_mcp.server:app", "--host", "0.0.0.0", "--port", "8000"]
