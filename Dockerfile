FROM python:3.12-slim

WORKDIR /app

# Кириллица для PDF (data/fonts в git не лежит)
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-liberation fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY run.sh .
RUN mkdir -p data && chmod +x run.sh

ENV PYTHONUNBUFFERED=1
ENV HOST=0.0.0.0
ENV PORT=8000
EXPOSE 8000

RUN chmod +x scripts/docker-entrypoint.sh
CMD ["./scripts/docker-entrypoint.sh"]
