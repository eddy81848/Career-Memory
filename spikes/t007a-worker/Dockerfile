# T-007A prototype image. NOT built or run in this task (Docker daemon unavailable);
# kept only as the minimal container procedure to be verified later. Do not push.
#   docker build -t t007a-spike:local .
#   docker run --rm -e T007A_ROLE=worker -e T007A_DATABASE_URL=... -p 8081:8080 t007a-spike:local
#   docker run --rm -e T007A_ROLE=api -e T007A_DATABASE_URL=... \
#       -e T007A_WORKER_WAKE_URL=http://host.docker.internal:8081/internal/t007a/wake \
#       -p 8080:8080 t007a-spike:local
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY t007a ./t007a
ENV PORT=8080 T007A_ROLE=api
USER nobody
CMD ["sh", "-c", "if [ \"$T007A_ROLE\" = worker ]; then F=t007a.worker:create_worker_app; else F=t007a.api:create_api; fi; exec uvicorn \"$F\" --factory --host 0.0.0.0 --port \"$PORT\""]
