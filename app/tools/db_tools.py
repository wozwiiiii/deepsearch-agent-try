"""
MySQL 数据库查询工具模块

封装数据库查询助手使用的三个 LangChain 工具：
list_sql_tables 用于发现真实表名，get_table_data 用于预览字段和样例数据，
execute_sql_query 用于在确认结构后执行自定义查询。
"""

import os
import re

import sqlglot
from dotenv import load_dotenv
from langchain_core.tools import tool
from mysql.connector import Error, connect

from app.api.monitor import monitor

load_dotenv()

# ---------------------------------------------------------------------------
# SQL 只读防护（生产化改造）
#
# 原版 execute_sql_query 依赖提示词约束模型只生成 SELECT，工具层不做校验，
# 且连接开启 autocommit，一旦模型生成 DML/DDL 会真实生效。
# 防护分两层：
# 1. 工具层：assert_readonly_sql 只放行 SELECT/SHOW/DESCRIBE/EXPLAIN 单条语句；
# 2. 账号层：部署时为 Agent 单独创建仅 SELECT 权限的 MySQL 账号（见
#    docs/PRODUCTION_NOTES.md），工具层被绕过时仍有数据库权限兜底。
# ---------------------------------------------------------------------------

# 去掉 -- 注释、# 注释和 /* */ 块注释后再做语句校验，防止用注释伪装语句开头
_SQL_COMMENT_PATTERN = re.compile(r"--[^\n]*|#[^\n]*|/\*.*?\*/", re.S)

# 语句必须以这些只读关键字开头（大小写不敏感）
_READONLY_SQL_START_PATTERN = re.compile(
    r"^\s*(SELECT|SHOW|DESCRIBE|DESC|EXPLAIN)\b", re.IGNORECASE
)

# 语句中不允许出现的写操作/危险关键字；保守起见全语句匹配，
# 若业务表字段名恰好命中这些词，需要先改名再接入本工具
_FORBIDDEN_SQL_KEYWORD_PATTERN = re.compile(
    r"\b("
    r"INSERT|UPDATE|DELETE|REPLACE|DROP|ALTER|CREATE|TRUNCATE|RENAME|"
    r"GRANT|REVOKE|LOCK|UNLOCK|CALL|SET|KILL|SHUTDOWN|HANDLER|LOAD|"
    r"LOAD_FILE|LOAD_DATA|PREPARE|EXECUTE"
    r")\b|INTO\s+(OUTFILE|DUMPFILE)",
    re.IGNORECASE,
)

# 合法表名只允许字母、数字和下划线，配合反引号包裹彻底杜绝表名注入
_TABLE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,64}$")

# 无 LIMIT 的 SELECT 自动追加的行数上限（第三批改造），可用 SQL_MAX_LIMIT 调整
DEFAULT_SELECT_LIMIT = int(os.getenv("SQL_MAX_LIMIT", "1000"))

# 仅 SELECT 走 sqlglot 改写；SHOW/DESCRIBE/EXPLAIN 语法特殊，原样透传
_SELECT_START_PATTERN = re.compile(r"^\s*SELECT\b", re.IGNORECASE)


class SQLSafetyError(ValueError):
    """SQL 未通过只读校验时抛出，由工具捕获后转为模型可读的错误提示"""


def assert_readonly_sql(query: str) -> str:
    """
    校验 SQL 语句只读，返回去除注释和结尾分号后的语句

    :param query: 模型生成的 SQL
    :return: 清理后的 SQL 文本
    :raises SQLSafetyError: 空语句、多语句、非只读语句或包含危险关键字
    """
    if not query or not query.strip():
        raise SQLSafetyError("SQL 语句不能为空")

    cleaned = _SQL_COMMENT_PATTERN.sub(" ", query).strip().rstrip(";").strip()

    if not cleaned:
        raise SQLSafetyError("SQL 去除注释后为空")

    # 多条语句（stacked queries）一律拒绝，防止 SELECT 后面藏一条写操作
    if ";" in cleaned:
        raise SQLSafetyError("禁止一次执行多条 SQL 语句")

    if not _READONLY_SQL_START_PATTERN.match(cleaned):
        raise SQLSafetyError(
            f"仅允许只读查询（SELECT/SHOW/DESCRIBE/EXPLAIN），当前语句被拒绝"
        )

    if _FORBIDDEN_SQL_KEYWORD_PATTERN.search(cleaned):
        raise SQLSafetyError("SQL 中包含写操作或危险关键字，已拒绝执行")

    return cleaned


def validate_table_name(table_name: str) -> str:
    """
    校验并返回安全的表名

    :param table_name: 模型传入的表名
    :return: 通过白名单校验的表名
    :raises SQLSafetyError: 表名含非法字符或超长
    """
    if not table_name or not _TABLE_NAME_PATTERN.match(str(table_name)):
        raise SQLSafetyError(
            f"非法表名: {table_name!r}，仅允许字母、数字和下划线"
        )
    return str(table_name)


def enforce_select_limit(sql: str, default_limit: int = None) -> str:
    """
    给不带 LIMIT 的 SELECT 追加行数上限（第三批改造）

    背景：只读校验放行的 `SELECT *` 在真实业务库（百万行表）上会拖垮数据库，
    也会把海量行拼进模型上下文。用 sqlglot 按 MySQL 方言解析后检查 LIMIT 子句，
    缺失则补默认值；已有 LIMIT 的语句保持原样。

    :param sql: 已通过 assert_readonly_sql 校验的 SQL
    :param default_limit: 追加的行数上限，默认取 SQL_MAX_LIMIT
    :return: 改写后的 SQL
    :raises SQLSafetyError: SELECT 无法解析时拒绝执行（fail-closed，
        模型收到错误文本后可改写重试）
    """
    limit = default_limit if default_limit is not None else DEFAULT_SELECT_LIMIT

    # SHOW/DESCRIBE/EXPLAIN 语法特殊且本身返回量小，原样透传
    if not _SELECT_START_PATTERN.match(sql):
        return sql

    try:
        expression = sqlglot.parse_one(sql, read="mysql")
    except sqlglot.errors.ParseError as e:
        raise SQLSafetyError(
            f"SQL 解析失败，已拒绝执行（请检查语法后重试）: {e}"
        )

    # 顶层 SELECT 与集合操作（UNION/INTERSECT/EXCEPT，sqlglot 统一为 SetOperation）
    # 都补 LIMIT；带 LIMIT 的语句 sqlglot 会记录在 args['limit']，原样保留。
    # 子查询内的无 LIMIT 不在此处理：外层 LIMIT 已限制最终返回模型的行数。
    # （审查修复：原实现只判 Select，UNION 等集合操作会漏过 LIMIT 注入）
    if (
        isinstance(expression, (sqlglot.exp.Select, sqlglot.exp.SetOperation))
        and expression.args.get("limit") is None
    ):
        expression = expression.limit(limit)

    return expression.sql(dialect="mysql")


# 集中读取数据库配置，后续三个工具都复用这份连接参数
def get_db_config():
    """
    从环境变量读取 MySQL 连接配置

    所有数据库工具都通过此函数拿到同一份连接参数，避免每个工具重复读取环境变量
    :return: mysql.connector.connect 可直接使用的连接参数
    """
    config = {
        "host": os.getenv("MYSQL_HOST", "localhost"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER"),
        "password": os.getenv("MYSQL_PASSWORD"),
        "database": os.getenv("MYSQL_DATABASE"),
        "charset": os.getenv("MYSQL_CHARSET", "utf8mb4"),
        "collation": os.getenv("MYSQL_COLLATION", "utf8mb4_unicode_ci"),
        "autocommit": True,
        "sql_mode": os.getenv("MYSQL_SQL_MODE", "TRADITIONAL"),
        # connection_timeout=10：TCP 连接阶段的超时（秒），网络半开时避免 connect()
        # 无限期阻塞而永久占用 LangChain 线程池工作线程。仅约束建连，不影响查询阶段；
        # mysql-connector-python 对查询阶段缺少可靠的超时控制，属已知边界
        "connection_timeout": 10,
    }

    # 去掉未配置的可选项，避免把 None 传给 mysql.connector 造成连接参数异常
    config = {k: v for k, v in config.items() if v is not None}

    # user/password/database 是本教程工具能正常查询业务库的最小必要配置
    required_keys = ["user", "password", "database"]
    missing_keys = [k for k in required_keys if k not in config]
    if missing_keys:
        raise ValueError(f"缺失数据库核心配置：{', '.join(missing_keys)}")

    return config


@tool
def list_sql_tables() -> str:
    """
    查询当前数据库中所有可用表

    作用：让模型先识别真实可用的表名，方便后续预览表结构和编写自定义 SQL。
    :return: 有表：可用的表有：表1,表2,表3...
             没有表：没有可用的表
             出现异常：查询出现异常：异常信息
    """

    # 埋点：工具一被调用，前端可以展示当前正在查询数据库表名
    monitor.report_tool(tool_name="数据库表名查询工具：list_sql_tables", args={})

    # 加载数据库连接信息
    config = get_db_config()

    # MySQL 查询的固定步骤：
    # 1. 创建连接
    # 2. 创建 cursor
    # 3. 执行 SQL
    # 4. 获取返回结果
    # 5. 释放连接和 cursor 资源
    # 这里捕获异常并返回中文提示，避免工具报错直接中断 Agent 执行链路
    try:
        # 使用 with 管理连接和游标，查询结束后自动释放数据库资源
        with connect(**config) as conn:
            with conn.cursor() as cursor:
                sql = "SHOW TABLES"
                cursor.execute(sql)

                # SHOW TABLES 返回形如：[("drugs",), ("inventory",), ("sales_records",)]
                tables = cursor.fetchall()
                if not tables:
                    return "没有可用的表"

                # 取每个元组的第一个元素，拼成模型容易阅读的表名列表
                table_names = [table[0] for table in tables]
                return f"可用的表有：{', '.join(table_names)}"
    except Error as e:
        return f"查询出现异常：{str(e)}"


@tool
def get_table_data(table_name) -> str:
    """
    查询指定表的前 100 行数据

    当前工具调用之前，应先调用 list_sql_tables 完成表名校验。
    此工具的作用：
    1. 完成单表样例数据查询
    2. 为多表查询提供表结构信息和数据格式参考
    :param table_name: 表名
    :return: CSV 格式数据
             1. 第一行是列信息，列之间使用英文逗号分隔
             2. 第二行开始是表数据，值之间也使用英文逗号分隔
             3. 行和行之间使用 \n 分隔
             4. 至多查询 100 条表数据
             例如：
                id,name,age\n -> 列头
                1,张三,18\n
                1,张三,18\n
                1,张三,18\n -> 至多查询 100 条
    """
    # 埋点：工具二被调用，前端可以展示当前正在预览哪张表
    monitor.report_tool(
        tool_name="数据库表数据查询工具：get_table_data",
        args={"table_name": table_name},
    )

    # 获取数据库参数
    config = get_db_config()

    # 表名先过白名单校验，再配合反引号包裹，杜绝通过表名注入 SQL
    try:
        safe_table = validate_table_name(table_name)
    except SQLSafetyError as e:
        return str(e)

    # 查询流程同样是：连接 -> cursor -> 执行 SQL -> 获取列信息和数据 -> 自动释放资源
    try:
        with connect(**config) as conn:
            with conn.cursor() as cursor:
                sql = f"SELECT * FROM `{safe_table}` LIMIT 100"
                cursor.execute(sql)

                # cursor.description 保存查询结果的列元信息
                # 例如：[("id", ...), ("name", ...), ("age", ...)]
                # 如果 SQL 没有结果集，description 可能为 None
                description = cursor.description
                if not description:
                    return f"数据表 {table_name} 暂无数据。"

                # 只取每个列信息元组的第一个元素，也就是列名
                # 例如：["id", "name", "age"]
                columns = [desc[0] for desc in description]

                # fetchall 返回表数据，形如：[(1, "张三", 18), (2, "李四", 20)]
                rows = cursor.fetchall()

                # 把每一行数据从元组转成 CSV 行文本
                # 例如：(1, "张三", 18) -> "1,张三,18"
                results = [",".join(map(str, row)) for row in rows]

                # columns 组成 CSV 头部，rows 组成 CSV 数据体
                # 最终返回：
                # id,name,age
                # 1,张三,18
                header_str = ",".join(columns)
                data_str = "\n".join(results)
                return f"{header_str}\n{data_str}"
    except Error as e:
        return f"查询出现异常：{str(e)}"


@tool
def execute_sql_query(query) -> str:
    """
    执行自定义 SQL 查询

    切记：执行之前，需要通过 list_sql_tables 明确真实表名，
    再通过 get_table_data 明确表结构和数据格式。
    适合多表关联、筛选、聚合、排序等复杂查询。
    :param query: 要执行的自定义 SQL 语句
    :return: CSV 格式数据
             1. 第一行是列信息，列之间使用英文逗号分隔
             2. 第二行开始是表数据，值之间也使用英文逗号分隔
             3. 行和行之间使用 \n 分隔
             例如：
                id,name,age\n -> 列头
                1,张三,18\n
                1,张三,18\n
    """
    # 埋点：记录模型最终生成的 SQL，便于教学时观察是否真的落到了正确表字段上
    monitor.report_tool(
        tool_name="数据库表数据查询工具：execute_sql_query", args={"query": query}
    )

    # 获取数据库参数
    config = get_db_config()

    # 只读防护：剥离注释后校验语句形式，不通过则直接拒绝，不建立数据库连接
    try:
        safe_query = enforce_select_limit(assert_readonly_sql(query))
    except SQLSafetyError as e:
        return str(e)

    # 自定义查询和 get_table_data 的结果处理逻辑一致：
    # 执行 SQL -> 读取 description 得到列名 -> fetchall 得到数据 -> 拼成 CSV 返回
    try:
        with connect(**config) as conn:
            with conn.cursor() as cursor:
                cursor.execute(safe_query)

                # 非查询类 SQL 没有结果集描述，这里统一返回提示，避免工具调用直接抛错给模型
                description = cursor.description
                if not description:
                    return f"执行自定义 SQL 语句没有查询结果，SQL 为：{query}"
                # description => [("列1", ...), ("列2", ...)]
                columns = [desc[0] for desc in description]

                # rows => [(值1, 值2), (值1, 值2)]
                rows = cursor.fetchall()

                # 每行元组统一转为逗号分隔文本，便于模型读取和后续整理
                results = [",".join(map(str, row)) for row in rows]

                # 第一行是列名，后续是查询数据
                header_str = ",".join(columns)
                data_str = "\n".join(results)
                return f"{header_str}\n{data_str}"
    except Error as e:
        return f"查询出现异常：{str(e)}"


if __name__ == "__main__":
    # 本地调试入口：直接运行本文件可验证 .env 中的 MySQL 连接配置是否可用
    print(
        execute_sql_query.invoke(
            {
                "query": "SELECT * FROM `drugs` dgs join sales_records srd on dgs.drug_id = srd.drug_id"
            }
        )
    )
