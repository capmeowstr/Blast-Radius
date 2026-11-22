FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
# Test dependencies exactly as the organizer labelled with (PyPI only).
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock
# sqlglot source at the pinned commit (its own repository, fixed commit), used
# to run the selected tests against each patched tree at run time.
RUN python -c "import io,tarfile,urllib.request; d=urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz',timeout=120).read(); tarfile.open(fileobj=io.BytesIO(d)).extractall('/opt',filter='data')"
RUN mv /opt/sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa /opt/sqlglot \
 && python -m compileall -q /opt/sqlglot/sqlglot /opt/sqlglot/tests \
 && chmod -R a+rX /opt/sqlglot
WORKDIR /app
COPY selector.py verify.py bytecode_safety.py comparison_types.py import_effects.py /app/
COPY maps/ /app/maps/
RUN cat /app/maps/coverage.* > /app/coverage-map.json.gz \
 && python -m compileall -q /app \
 && chmod -R a+rX /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SQLGLOT_BASE=/opt/sqlglot TMPDIR=/tmp
CMD ["python", "/app/selector.py"]
