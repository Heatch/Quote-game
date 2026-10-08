FROM python:3.11-slim
ARG HOST_UID=1000
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u ${HOST_UID} bot
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py database.py schema.sql quote_ingester.py quoteparse.py test_migration.py ./
USER bot
STOPSIGNAL SIGINT
CMD ["python", "main.py"]