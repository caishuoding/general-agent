FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 HOME=/tmp/skill-home PLAYWRIGHT_BROWSERS_PATH=/opt/browsers
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-impress libreoffice-writer fonts-noto-cjk fonts-liberation \
    ffmpeg pandoc chromium libcairo2 curl \
    && rm -rf /var/lib/apt/lists/*
COPY skills /opt/skills
COPY deploy/skills/prepare_browser.py /opt/prepare_browser.py
COPY deploy/skills/requirements.lock /opt/runtime-requirements.lock
RUN pip install --no-cache-dir -r /opt/runtime-requirements.lock && pip check
RUN python /opt/prepare_browser.py \
    && python /opt/skills/ppt-master/scripts/attribution_guard.py
WORKDIR /workspace
CMD ["sleep", "infinity"]
