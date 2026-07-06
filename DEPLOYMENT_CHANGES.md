# 部署修改说明

## 问题总结

将 NexusCompanion 部署到阿里云 ECS（国内服务器）时遇到以下问题：

### 1. `.dockerignore` 配置错误

**原配置：**
```
**
!requirements.txt
!requirements-dev.txt
!docker/debug/entrypoint.sh
```
`**` 排除了**所有文件**，只放行了 3 个 debug 用的文件。导致 `docker build` 时 `COPY . .` 几乎没复制任何代码进去：
- `pyproject.toml` 找不到 → 构建失败
- `plugins/qqbot/` 等插件目录被排除 → agent 启动后 QQ Bot 连不上

**修改：** 改为只排除非必要文件（git、pycache、venv、IDE 配置、node_modules 等），保留全部源代码。

### 2. 缺少生产用 Dockerfile

项目原有的是 `docker/debug/Dockerfile`，基于 `archlinux:latest`，专门用于本地调试沙盒。生产部署需要一个轻量、通用的 Docker 镜像。

**修改：** 创建根目录 `Dockerfile`：
- 基于 `python:3.12-slim`
- 使用 `uv` 加速 Python 依赖安装
- 内置清华 PyPI 镜像（`https://pypi.tuna.tsinghua.edu.cn/simple`）
- 暴露 Dashboard 端口 2236

### 3. 缺少 docker-compose.yml

原有 compose 只在 `docker/debug/docker-compose.yml`，用于调试。

**修改：** 创建根目录 `docker-compose.yml`：
- 端口映射 `2236:2236`（Dashboard）
- 数据卷持久化 `~/.nexus:/root/.nexus`
- 配置文件挂载 `config.toml`
- `restart: unless-stopped` 保证崩溃后自动重启
- 时区设为 `Asia/Shanghai`

### 4. 国内网络限制

| 问题 | 原因 | 解决 |
|------|------|------|
| Docker Hub 访问超时 | 国内服务器连 registry-1.docker.io 被墙 | Docker 配置 registry mirror |
| PyPI 下载极慢 | 默认走国外源 | Dockerfile 硬编码清华镜像源 |

### 5. QQ Bot 插件缺失

首次 `git clone` 中途中断导致部分目录没拉全，`plugins/qqbot/` 和 `plugins/feishu/` 缺失，agent 启动后找不到 QQ Bot 通道。

**解决：** 重新完整 clone 一次。

---

## 新增 / 修改的文件

| 文件 | 操作 | 说明 |
|------|------|------|
| `.dockerignore` | 🔄 重写 | 从"排除全部"改为"只排除非必要" |
| `Dockerfile` | ✨ 新增 | 生产环境构建，含 uv + 清华 PyPI 镜像 |
| `docker-compose.yml` | ✨ 新增 | 一键部署，持久化存储，自动重启 |
| `DEPLOYMENT_CHANGES.md` | ✨ 新增 | 本文件 |

---

## 下次部署步骤

```bash
# 1. 服务器上装 Docker
curl -fsSL https://get.docker.com | bash

# 2. 配置 Docker 镜像加速（可选，国内必配）
cat > /etc/docker/daemon.json << 'EOF'
{
  "registry-mirrors": ["https://docker.1ms.run"]
}
EOF
systemctl restart docker

# 3. Clone 代码
git clone git@github.com:1L7V5101V/NexusCompanion.git /opt/NexusCompanion

# 4. 配置 config.toml
cp /opt/NexusCompanion/config.example.toml /opt/NexusCompanion/config.toml
nano /opt/NexusCompanion/config.toml

# 5. 启动
cd /opt/NexusCompanion
docker compose up -d

# 6. 查看日志
docker compose logs -f
```
