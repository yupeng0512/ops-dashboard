FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py models.py notifier.py probes.py ops_reporter.py repair_engine.py ./
COPY static/ ./static/
COPY genes/ ./genes/

RUN mkdir -p /app/data

ENV PYTHONUNBUFFERED=1

EXPOSE 9090

CMD ["python", "main.py"]
