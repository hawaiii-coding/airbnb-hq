# Rebuilt 2026-11-11: matches the deployed cleaning-sync-scratch image layout (code under the image workdir).
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY cleaning_sync ./cleaning_sync
COPY feeds.example.json rate_card.json payroll.json ./
ENV FEEDS_CONFIG=/app/feeds.example.json
ENTRYPOINT ["python", "-m", "cleaning_sync.job"]
