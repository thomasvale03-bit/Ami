# AMY Slack worker (separate Railway service). Socket Mode: no public port.
# Build context is the repo root; only slack_worker/ is copied.
FROM python:3.12-slim
WORKDIR /app
COPY slack_worker/requirements.txt slack_worker/requirements.txt
RUN pip install --no-cache-dir -r slack_worker/requirements.txt
COPY slack_worker/ slack_worker/
ENV PYTHONUNBUFFERED=1 TZ=America/Phoenix
CMD ["python", "-m", "slack_worker"]
