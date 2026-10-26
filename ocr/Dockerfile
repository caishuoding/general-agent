FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PADDLE_PDX_CACHE_HOME=/models/paddlex \
    HF_HOME=/models/huggingface \
    MODELSCOPE_CACHE=/models/modelscope \
    OCR_DEVICE=cpu
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 libglib2.0-0 libgl1 \
    && rm -rf /var/lib/apt/lists/*
COPY ocr/requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt
COPY ocr ./ocr
EXPOSE 8090
CMD ["uvicorn", "ocr.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8090", "--limit-concurrency", "4"]
