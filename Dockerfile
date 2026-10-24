FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt \
    && groupadd --gid 10001 api \
    && useradd --uid 10001 --gid api --no-create-home api
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY deploy/start-api.sh /app/start-api.sh
RUN mkdir -p uploads generated-websites data \
    && chown -R api:api uploads generated-websites data
USER api
EXPOSE 8000
CMD ["sh", "/app/start-api.sh"]
