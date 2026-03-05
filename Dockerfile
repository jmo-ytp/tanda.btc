FROM python:3.11-slim

WORKDIR /app

# gcc is needed to build coincurve from source if no wheel is available
RUN apt-get update && apt-get install -y --no-install-recommends gcc && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-demo.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-demo.txt

COPY tanda/ ./tanda/
COPY scripts/ ./scripts/

ENV PYTHONPATH=/app
