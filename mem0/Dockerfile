FROM python:3.12

WORKDIR /opt/mem0-source

# 通过 Docker 可访问的 codeload 通道取得官方 main 分支源码。
ADD https://codeload.github.com/mem0ai/mem0/tar.gz/refs/heads/main /tmp/mem0.tar.gz
RUN tar -xzf /tmp/mem0.tar.gz --strip-components=1 -C /opt/mem0-source

# 以下安装顺序与官方 server/dev.Dockerfile 保持一致。
RUN curl -sSL https://install.python-poetry.org | python3 -
WORKDIR /app
RUN cp /opt/mem0-source/server/requirements.txt . && pip install -r requirements.txt
WORKDIR /app/packages
RUN cp /opt/mem0-source/pyproject.toml /opt/mem0-source/poetry.lock /opt/mem0-source/README.md . \
    && cp -r /opt/mem0-source/mem0 ./mem0 \
    && pip install -e .[graph]
WORKDIR /app
RUN cp -r /opt/mem0-source/server/. .

CMD ["sh", "-c", "alembic upgrade head && uvicorn main:app --host 0.0.0.0 --port 8000"]
