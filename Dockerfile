FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 先装依赖，利用层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝应用代码
COPY app ./app

RUN mkdir -p /data

EXPOSE 8000

# 容器启动时（FastAPI lifespan）自动完成建表与初始地址池写入，
# 无需额外的初始化命令；故 `docker compose up --build` 即开箱可用。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
