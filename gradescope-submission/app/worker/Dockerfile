# Build from the repository root: docker build -f worker/Dockerfile .
FROM python:3.12-slim
WORKDIR /app
COPY worker/requirements.txt worker/requirements.txt
RUN pip install --no-cache-dir -r worker/requirements.txt
COPY core core
COPY worker worker
USER nobody
CMD ["python", "-m", "worker.main"]
