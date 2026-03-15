FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY bot.py README.md LICENSE DEPLOY.md .env.example /app/
COPY deploy /app/deploy

CMD ["python3", "bot.py"]
