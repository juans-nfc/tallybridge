# TallyBridge — SIMS packing-line files to Paycom timecard imports
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir openpyxl flask waitress

COPY timecard_converter.py timecard_web.py share_fetch.py Timecard_Import_template_CLEAN.xlsx /app/

EXPOSE 8080

# docker-compose sets the real command per service; this is a sane default.
CMD ["python3", "timecard_web.py", "--host", "0.0.0.0", "--port", "8080", \
     "--watch-dir", "/data/incoming", "--output-dir", "/data/converted", \
     "--staging-dir", "/data/staging"]
