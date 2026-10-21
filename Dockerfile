# Official Playwright image: Chromium and its system libraries preinstalled.
# Its version must match playwright== in requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
ENV PYTHONUNBUFFERED=1

# Runs forever, checking the inbox every AMY_POLL_SECONDS. The mode comes
# from AMY_MODE (dry-run | test | live), set in the host's variables.
CMD ["python", "main.py", "--loop"]
