# 通用 FastAPI 服务镜像，通过 APP_DIR 构建参数区分 proxy / backend / web
FROM python:3.11-slim

ARG APP_DIR=app
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

COPY requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir -r /srv/requirements.txt

COPY ${APP_DIR} /srv/app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
