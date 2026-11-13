FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY wsgi.py .

RUN useradd --create-home buddy && mkdir -p /app/instance && chown -R buddy /app
USER buddy

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=5 CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["gunicorn", "--preload", "-w", "2", "--threads", "8", "-b", "0.0.0.0:8000", "--access-logfile", "-", "wsgi:app"]
