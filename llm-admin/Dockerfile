FROM python:3.12-slim-bookworm

ARG ARK_VERSION=2026.11.0
ARG ARK_GIT_COMMIT=unknown
ARG ARK_BUILD_DATE=unknown

LABEL org.opencontainers.image.title="ark-llm-admin" \
      org.opencontainers.image.description="ARK LLM Backend Management Console" \
      org.opencontainers.image.version="${ARK_VERSION}" \
      org.opencontainers.image.revision="${ARK_GIT_COMMIT}" \
      org.opencontainers.image.created="${ARK_BUILD_DATE}" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DO_NOT_TRACK=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ARK_VERSION=${ARK_VERSION}

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl zstd && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8095

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8095"]
