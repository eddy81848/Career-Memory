# T-007A prototype image. Verified locally in RUNTIME-05 (native) and RUNTIME-06 (linux/amd64)
# for both T007A_ROLE=api and T007A_ROLE=worker from a single image. Do NOT push to any registry.
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
