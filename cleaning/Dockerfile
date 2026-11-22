# Built from the REPO ROOT (cloudbuild: docker build -f cleaning/Dockerfile .) so it can ship config/units.json.
FROM python:3.12-slim
WORKDIR /app
COPY cleaning/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY cleaning/cleaning_sync ./cleaning_sync
COPY cleaning/payroll.json ./
COPY config/units.json ./units.json
ENV FEEDS_CONFIG=/app/units.json
ENTRYPOINT ["python", "-m", "cleaning_sync.job"]
