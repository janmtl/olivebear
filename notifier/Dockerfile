FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bear_notifier.py .
CMD ["python", "bear_notifier.py"]
