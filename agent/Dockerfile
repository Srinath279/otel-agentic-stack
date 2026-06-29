FROM python:3.11-slim

WORKDIR /app

# Install the reusable instrumentation package first (better layer caching).
COPY obs /tmp/obs
RUN pip install --no-cache-dir /tmp/obs

COPY agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/ .

EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
