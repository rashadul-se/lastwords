FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY ui ui
# Fail the BUILD (not the deploy) if the repo is missing files, e.g. an uncommitted ui/ folder.
RUN test -f ui/streamlit_app.py && test -f app/api/main.py
RUN useradd -m care && chown -R care /srv
USER care
EXPOSE 8501
# Default process = Streamlit UI. The API service overrides this command (see render.yaml / docker-compose.yml).
CMD ["sh","-c","streamlit run ui/streamlit_app.py --server.port=${PORT:-8501} --server.address=0.0.0.0 --server.headless=true --server.maxUploadSize=200 --server.enableXsrfProtection=true"]
