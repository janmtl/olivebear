FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY tapo_bridge.py .
# Unhealthy if the poll loop hasn't heartbeated in 60s (wedged loop, stuck await).
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
  CMD python -c "import os,sys,time; sys.exit(0 if time.time()-os.path.getmtime('/tmp/heartbeat')<60 else 1)"
CMD ["python", "tapo_bridge.py"]
