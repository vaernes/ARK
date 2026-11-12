FROM python:3.12-slim-bookworm

ARG ARK_VERSION=2026.11.0
ARG ARK_GIT_COMMIT=unknown
ARG ARK_BUILD_DATE=unknown

LABEL org.opencontainers.image.title="ark-admin" \
      org.opencontainers.image.description="ARK (Air-gapped Retrieval & Knowledge) Control Plane & API Gateway" \
      org.opencontainers.image.version="${ARK_VERSION}" \
      org.opencontainers.image.revision="${ARK_GIT_COMMIT}" \
      org.opencontainers.image.created="${ARK_BUILD_DATE}" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/vaernes/ARK"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DO_NOT_TRACK=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ARK_VERSION=${ARK_VERSION} \
    ARK_GIT_COMMIT=${ARK_GIT_COMMIT} \
    ARK_BUILD_DATE=${ARK_BUILD_DATE}

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
