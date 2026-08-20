# ============================================================
# NexusCompanion — Production Dockerfile
# ============================================================
# 使用 slim 基础镜像，国内用户可替换 registry 镜像加速：
#   docker build --build-arg BASE_IMAGE=python:3.12-slim .
# ============================================================

ARG BASE_IMAGE=python:3.12-slim

# ---- Frontend build stage ----
FROM node:20-slim AS frontend-builder

WORKDIR /build

# 先复制依赖文件，利用 Docker 缓存层
COPY package.json package-lock.json ./
RUN npm ci

# 复制前端源码
COPY frontend/dashboard/ frontend/dashboard/

# 构建 Dashboard 前端
RUN npm run build:dashboard


# ---- Runtime stage ----
FROM ${BASE_IMAGE}

LABEL description="NexusCompanion — 主动 AI 伙伴"
LABEL maintainer="1L7V5101V"

WORKDIR /app

# 从 builder 复制构建好的前端资源
COPY --from=frontend-builder /build/static/dashboard/ /app/static/dashboard/

# 安装 uv（Python 包管理加速工具）
# 使用清华 PyPI 镜像加速国内下载
RUN pip install --no-cache-dir uv -i https://pypi.tuna.tsinghua.edu.cn/simple

# 先复制依赖文件，利用 Docker 缓存层
COPY pyproject.toml requirements.txt ./
RUN uv pip install --system -r requirements.txt \
    -i https://pypi.tuna.tsinghua.edu.cn/simple

# 复制项目其余代码
COPY . .

EXPOSE 2236

CMD ["python", "main.py"]
