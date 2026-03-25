FROM python:3.11-slim

# Install librdkafka (required by confluent-kafka)
RUN apt-get update && apt-get install -y --no-install-recommends \
    librdkafka-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY logpose/ logpose/

ENV PYTHONUNBUFFERED=1

# Default entrypoint — override in K8s/OpenShift with the specific consumer command
CMD ["python", "-m", "logpose"]
