ARG ALPINE_IMAGE=docker.1ms.run/library/alpine:3.20
FROM ${ALPINE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST_ROOT=/host \
    STATE_DIR=/state \
    PORT=9365 \
    BIND_ADDRESS=0.0.0.0

RUN sed -i 's#https://dl-cdn.alpinelinux.org/alpine#https://mirrors.aliyun.com/alpine#g' /etc/apk/repositories \
    && apk add --no-cache python3 \
    && addgroup -S -g 10001 devicekeeper \
    && adduser -S -D -H -u 10001 -G devicekeeper devicekeeper \
    && mkdir -p /app /state \
    && chown -R 10001:10001 /app /state

WORKDIR /app
COPY --chown=10001:10001 app /app/app
COPY --chown=10001:10001 ui /app/ui

USER 10001:10001
EXPOSE 9365

HEALTHCHECK --interval=15s --timeout=3s --start-period=25s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9365/api/v1/health', timeout=2)"

CMD ["python3", "-m", "app.main"]
