FROM python:3.12-slim AS runtime

# Unbuffered output so logs reach `docker logs` immediately.
ENV PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app
COPY app.py ./

# Run as an unprivileged user.
RUN useradd --system --no-create-home appuser
USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"

CMD ["python", "app.py"]

# Tests run on top of the exact runtime layers, but are never shipped.
# Build with `--target test`; a plain build skips this stage.
FROM runtime AS test
COPY test_app.py ./
ENV SKIP_INTEGRATION=1
RUN python -m unittest -v

# Last stage is the default target, so a plain `docker build` gives the runtime image.
FROM runtime
