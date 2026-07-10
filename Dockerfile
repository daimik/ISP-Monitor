FROM python:3.11-slim

ARG HAILO_BUILD=false

# Install system dependencies + Ookla Speedtest CLI + iperf3
RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends curl gnupg ca-certificates iperf3 && \
    curl -s https://packagecloud.io/install/repositories/ookla/speedtest-cli/script.deb.sh | bash && \
    apt-get install -y --no-install-recommends speedtest && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data

ENV DB_PATH=/data/speedtest.db \
    MEASUREMENT_INTERVAL=600 \
    MAX_SERVER_ATTEMPTS=5 \
    SPEEDTEST_TIMEOUT=120 \
    PYTHONUNBUFFERED=1

EXPOSE 5000 5201

VOLUME ["/data"]

CMD ["sh", "-c", \
  "(while true; do python speedtest_monitor.py; echo 'Monitor exited, restarting in 10s...'; sleep 10; done) & exec python dashboard.py"]
