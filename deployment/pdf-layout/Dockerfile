ARG PYTHON_BASE_IMAGE=python:3.13-slim
FROM ${PYTHON_BASE_IMAGE}
RUN pip install --no-cache-dir pdfplumber==0.11.10
WORKDIR /app
COPY server.py .
USER 65534:65534
CMD ["python", "-u", "server.py", "--host", "0.0.0.0"]
