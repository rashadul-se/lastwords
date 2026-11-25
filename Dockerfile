FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY ui ui
COPY scripts/start.sh start.sh
# Fail the BUILD (not the deploy) if the repo is missing files, e.g. an uncommitted ui/ folder.
RUN test -f ui/streamlit_app.py && test -f app/api/main.py && test -f start.sh
RUN useradd -m care && chown -R care /srv
USER care
EXPOSE 8501
# Process chosen by $SERVICE (ui by default, api for the API) in start.sh.
CMD ["sh","/srv/start.sh"]
