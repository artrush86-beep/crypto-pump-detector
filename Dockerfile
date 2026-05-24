FROM python:3.11-slim

ARG CACHE_BUST=2

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p logs data

CMD ["python", "main.py"]
