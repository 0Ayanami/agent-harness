ARG PYTHON_BASE_IMAGE=python:3.13-slim
FROM ${PYTHON_BASE_IMAGE}
WORKDIR /app
RUN mkdir /state && chown 65534:65534 /state
COPY core/models.py agent_harness/core/models.py
COPY services/scheduler.py agent_harness/services/scheduler.py
COPY infrastructure/cron_store.py agent_harness/infrastructure/cron_store.py
COPY deployment/cron/server.py server.py
USER 65534:65534
CMD ["python", "-u", "server.py"]
