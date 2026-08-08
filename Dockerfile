FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv/hamdoor

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static
COPY docker-entrypoint.sh /usr/local/bin/hamdoor-entrypoint.sh

RUN useradd --create-home --uid 10001 hamdoor \
    && mkdir -p /data \
    && chown -R hamdoor:hamdoor /data /srv/hamdoor \
    && chmod +x /usr/local/bin/hamdoor-entrypoint.sh
# Container starts as root so the entrypoint can fix /data ownership when a
# host bind mount is used; it drops to the hamdoor user before exec'ing CMD.

ENV DATABASE_URL=sqlite:////data/hamdoor.db
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"

ENTRYPOINT ["hamdoor-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
