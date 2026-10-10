FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py db.py prompt.txt ./
COPY static ./static

RUN mkdir -p uploaded_images

# PORT задає Cloud Run, але локально за замовчуванням 8080
ENV PORT=8080

CMD ["bash", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8080}"]

