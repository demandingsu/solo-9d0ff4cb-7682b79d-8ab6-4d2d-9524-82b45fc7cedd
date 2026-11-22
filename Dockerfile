FROM python:3.12-slim

WORKDIR /srv/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY run.py .

ENV MSO_DB=/data/manuscript.db
VOLUME ["/data"]
EXPOSE 8000

# 单进程多线程：SQLite 写者由 BEGIN IMMEDIATE 串行化
CMD ["gunicorn", "--workers", "1", "--threads", "8", "--bind", "0.0.0.0:8000", "run:app"]
