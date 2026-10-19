FROM python:3.12

ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.aliyun.com

WORKDIR /opt/mem0-source

# 通过 Docker 可访问的 codeload 通道取得官方 main 分支源码。
ADD https://codeload.github.com/mem0ai/mem0/tar.gz/refs/heads/main /tmp/mem0.tar.gz
RUN tar -xzf /tmp/mem0.tar.gz --strip-components=1 -C /opt/mem0-source

WORKDIR /app
COPY constraints.txt /tmp/constraints.txt
RUN cp /opt/mem0-source/server/requirements.txt . && pip install -r requirements.txt -c /tmp/constraints.txt
WORKDIR /app/packages
RUN cp /opt/mem0-source/pyproject.toml /opt/mem0-source/poetry.lock /opt/mem0-source/README.md . \
    && cp -r /opt/mem0-source/mem0 ./mem0 \
    && pip install -e .[graph]
WORKDIR /app
RUN cp -r /opt/mem0-source/server/. .

CMD ["sh", "-c", "alembic upgrade head && uvicorn main:app --host 0.0.0.0 --port 8000"]
