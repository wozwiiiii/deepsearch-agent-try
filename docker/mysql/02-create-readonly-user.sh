#!/bin/bash
# 创建应用只读账号（生产化改造第三批）
#
# MySQL 官方镜像首次初始化数据目录时，会自动执行 /docker-entrypoint-initdb.d/
# 下的 .sh 脚本（docker-compose.yaml 已挂载本文件为 02 号脚本）。
# 容器环境变量在本脚本中可用，因此密码来自 MYSQL_READONLY_PASSWORD。
#
# 权限设计：Agent 后端的 .env 应将 MYSQL_USER 配置为 deepsearch_ro。
# 工具层的 assert_readonly_sql 是第一道防线；本账号仅有 SELECT 权限，
# 即使工具层校验被绕过，数据库引擎也会拒绝一切写操作（纵深防御第二层）。
#
# 注意：initdb 脚本只在数据卷为空的首次启动执行；已有卷的环境需要手动执行
# 相同 SQL，或执行 `docker compose down -v` 重建卷。

set -euo pipefail

mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" <<-EOSQL
    CREATE USER IF NOT EXISTS 'deepsearch_ro'@'%' IDENTIFIED BY '${MYSQL_READONLY_PASSWORD:-deepsearch_ro_dev}';
    GRANT SELECT ON \`${MYSQL_DATABASE:-deepsearch_db}\`.* TO 'deepsearch_ro'@'%';
    FLUSH PRIVILEGES;
    SELECT 'readonly user deepsearch_ro created' AS status;
EOSQL
