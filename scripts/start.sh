#!/bin/sh
# Container entrypoint. Picks the process from $SERVICE so no quoted command is ever needed in a dashboard.
#   SERVICE=ui  (default)  Streamlit staff UI
#   SERVICE=api            FastAPI server
set -eu
PORT="${PORT:-}"
case "${SERVICE:-ui}" in
  api)
    exec uvicorn app.api.main:app --host 0.0.0.0 --port "${PORT:-8000}" \
      --workers "${WEB_CONCURRENCY:-2}" --proxy-headers --forwarded-allow-ips='*'
    ;;
  ui)
    exec streamlit run ui/streamlit_app.py --server.port="${PORT:-8501}" --server.address=0.0.0.0 \
      --server.headless=true --server.maxUploadSize=200 --server.enableXsrfProtection=true
    ;;
  *)
    echo "start.sh: unknown SERVICE='${SERVICE}' (use ui or api)" >&2
    exit 2
    ;;
esac
