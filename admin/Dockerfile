FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DO_NOT_TRACK=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      curl \
      ca-certificates \
      zstd && \
    install -d /usr/share/postgresql-common/pgdg && \
    curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc && \
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" > /etc/apt/sources.list.d/pgdg.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends postgresql-client-16 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8090

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8090"]
