FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY static static
ENV DATA_DIR=/data
VOLUME /data
EXPOSE 5000
# One worker on purpose: state lives in a JSON file guarded by an in-process lock.
CMD ["gunicorn", "-b", "0.0.0.0:5000", "-w", "1", "--threads", "4", "app:app"]
