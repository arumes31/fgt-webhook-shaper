# syntax=docker/dockerfile:1.19.0@sha256:b6afd42430b15f2d2a4c5a02b919e98a525b785b1aaff16747d2f623364e39b6

FROM python:3.14-slim@sha256:cae66f2ef0ec51a9891263eeee7f987dacf0a9879e8aa9353d5606e0530619a5 AS builder

ENV VIRTUAL_ENV=/opt/venv
RUN python -m venv "$VIRTUAL_ENV"
COPY requirements.txt /tmp/requirements.txt
RUN "$VIRTUAL_ENV/bin/pip" install --require-hashes -r /tmp/requirements.txt

FROM python:3.14-slim@sha256:cae66f2ef0ec51a9891263eeee7f987dacf0a9879e8aa9353d5606e0530619a5

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 65532 shaper \
    && useradd --uid 65532 --gid shaper --no-create-home --shell /usr/sbin/nologin shaper

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --chown=65532:65532 app.py config.py service.py ./
RUN rm -rf /opt/venv/bin/pip* \
        /opt/venv/lib/python3.14/site-packages/pip* \
        /usr/local/lib/python3.14/ensurepip \
        /usr/local/lib/python3.14/site-packages/pip*

USER 65532:65532
EXPOSE 25001
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:25001/healthz', timeout=2)"

ENTRYPOINT ["gunicorn"]
CMD ["--workers", "1", "--threads", "4", "--timeout", "30", "--graceful-timeout", "20", "--keep-alive", "2", "--max-requests", "1000", "--max-requests-jitter", "100", "--bind", "0.0.0.0:25001", "app:create_app()"]
