# CAPEX & WBS Control Hub — demo container
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    CAPEX_DB_PATH=/data/capex.db

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
# THE MIGRATIONS SHIP IN THE IMAGE, and they did not before. The application
# deliberately refuses to migrate itself on boot (DEF-01: an app that does
# cannot be rolled back, races when scaled, and turns a schema error into an
# outage), so the schema is a DEPLOY STEP -- and a step whose files are absent
# from the image cannot be performed. Without this line the container starts,
# finds no schema, and exits.
COPY migrations ./migrations
# The connector reads the verified Zoho endpoint inventory at runtime,
# so this is application data, not documentation.
COPY research/20_verified/zoho_endpoint_inventory.json research/20_verified/
COPY research/20_verified/openapi_findings.json research/20_verified/

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/api/health').read()"

# THE DEPLOY STEP IS NOT IN THIS CMD, ON PURPOSE. `run.py` does not migrate
# (DEF-01), so a fresh volume needs the schema created before the server is
# useful:
#
#     docker run --rm -v capexdata:/data <image> python -m app.backend.migrate --upgrade
#     docker run -p 8000:8000 -v capexdata:/data <image>
#
# Folding the migration into CMD would re-introduce exactly what DEF-01
# removed: every replica racing to migrate on every restart.
CMD ["python", "app/run.py"]
