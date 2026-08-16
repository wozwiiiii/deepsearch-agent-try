"""
SQL 只读防护测试

纯函数测试，不依赖 MySQL：覆盖语句形式校验、注释伪装、多语句注入、
危险关键字和表名白名单。
"""

import pytest

from app.tools.db_tools import (
    SQLSafetyError,
    assert_readonly_sql,
    validate_table_name,
)


class TestAssertReadonlySql:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM drugs",
            "select count(*) from sales_records where amount > 100",
            "SHOW TABLES",
            "DESCRIBE drugs",
            "DESC drugs",
            "EXPLAIN SELECT 1",
            "  SELECT 1;  ",  # 结尾分号允许，会被 strip 掉
        ],
    )
    def test_readonly_statements_pass(self, sql):
        assert assert_readonly_sql(sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "DROP TABLE drugs",
            "DELETE FROM drugs",
            "UPDATE drugs SET price = 0",
            "INSERT INTO drugs VALUES (1, 'x')",
            "TRUNCATE TABLE drugs",
            "CREATE TABLE evil (id INT)",
            "ALTER TABLE drugs ADD COLUMN x INT",
            # SELECT 语法但带写副作用
            "SELECT * FROM drugs INTO OUTFILE '/tmp/x'",
            "SELECT load_file('/etc/passwd')",
            # 注释伪装：真正语句藏在注释后面
            "/* harmless */ DROP TABLE drugs",
            "-- comment\nDELETE FROM drugs",
            # 多语句注入
            "SELECT 1; DROP TABLE drugs",
        ],
    )
    def test_write_statements_rejected(self, sql):
        with pytest.raises(SQLSafetyError):
            assert_readonly_sql(sql)

    def test_empty_statement_rejected(self):
        with pytest.raises(SQLSafetyError):
            assert_readonly_sql("")
        with pytest.raises(SQLSafetyError):
            assert_readonly_sql("   ")

    def test_comment_only_statement_rejected(self):
        with pytest.raises(SQLSafetyError):
            assert_readonly_sql("-- only a comment")

    def test_stacked_queries_without_semicolon_tail_rejected(self):
        # rstrip(';') 只去结尾分号，中间分号必须仍然触发多语句拒绝
        with pytest.raises(SQLSafetyError):
            assert_readonly_sql("SELECT 1; DROP TABLE drugs;")


class TestValidateTableName:
    @pytest.mark.parametrize(
        "table",
        ["drugs", "sales_records", "T1", "_tmp", "a" * 64],
    )
    def test_valid_table_names_pass(self, table):
        assert validate_table_name(table) == table

    @pytest.mark.parametrize(
        "table",
        [
            "",
            "drug; DROP TABLE x",  # SQL 片段
            "drugs` WHERE 1=1 -- ",  # 反引号逃逸
            "表名",  # 非 ASCII
            "a" * 65,  # 超长
            "drug-name",  # 连字符会被 SQL 当减号
            None,
        ],
    )
    def test_invalid_table_names_rejected(self, table):
        with pytest.raises(SQLSafetyError):
            validate_table_name(table)
