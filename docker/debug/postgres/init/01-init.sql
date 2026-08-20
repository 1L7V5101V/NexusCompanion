-- 本地开发库初始化：容器首次启动建库时执行一次。
-- 镜像自带 pgvector，但扩展需要显式创建。
CREATE EXTENSION IF NOT EXISTS vector;
