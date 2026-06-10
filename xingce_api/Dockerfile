# 构建上下文 = 仓库根目录(因为要带上 ../extract_*.py)
# 构建:  docker build -f xingce_api/Dockerfile -t xingce .
# 运行:  docker run -p 8000:8000 --env-file xingce_api/.env -v $PWD/xcdata:/app/xingce_api/data xingce
FROM python:3.11-slim

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 fonts-noto-cjk && rm -rf /var/lib/apt/lists/*

COPY xingce_api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY xingce_api /app/xingce_api
COPY extract_graphics.py extract_material.py /app/

WORKDIR /app/xingce_api
EXPOSE 8000
# 首启会下载 bge 向量模型(约400MB)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
