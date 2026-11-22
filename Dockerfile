FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /app
COPY selector.py comparison_types.py indexed_coverage.py value_profile.py /app/
COPY tools/index_coverage.py /app/tools/
COPY maps/ /app/maps/
RUN cat /app/maps/coverage.* > /tmp/coverage-map.json.gz \
    && python /app/tools/index_coverage.py /tmp/coverage-map.json.gz /app/coverage-map.bin \
    && rm /tmp/coverage-map.json.gz
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
CMD ["python", "/app/selector.py"]
