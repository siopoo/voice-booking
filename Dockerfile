FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PAWPILOT_HOST=0.0.0.0 \
    PAWPILOT_PORT=8000 \
    PAWPILOT_DATABASE_PATH=/app/data/appointments.db \
    PAWPILOT_BUSINESS_CONFIG_PATH=/app/config/business.json

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/data && useradd --create-home pawpilot && chown -R pawpilot:pawpilot /app
USER pawpilot

EXPOSE 8000
VOLUME ["/app/data"]
HEALTHCHECK --interval=20s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2)"

CMD ["python", "server.py"]
