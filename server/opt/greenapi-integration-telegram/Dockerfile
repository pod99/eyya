FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN groupadd -r appuser -g 10001 \
    && useradd -r -g appuser -u 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app

EXPOSE 3000

USER appuser

CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3000"]
