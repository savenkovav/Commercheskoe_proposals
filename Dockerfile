ARG PYTHON_IMAGE=mirror.gcr.io/library/python:3.11-slim
FROM ${PYTHON_IMAGE}

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    fonts-dejavu-core \
    antiword \
    tesseract-ocr \
    tesseract-ocr-rus \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --default-timeout=120 -r requirements.txt

COPY . .

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

RUN mkdir -p output logs

CMD ["python", "-m", "src.web.server"]
