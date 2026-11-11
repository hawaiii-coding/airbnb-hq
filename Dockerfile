FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY config ./config
COPY hq ./hq
COPY jobs ./jobs
ENTRYPOINT ["python", "-m", "jobs.live_availability"]
