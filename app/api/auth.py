"""
API Key 认证与租户身份模块

生产化改造第二批：为所有业务接口增加 API Key 认证，并把每个 Key 映射到
一个租户身份（Principal.user_id）。会话目录、上传目录、LangGraph 状态和
WebSocket 推送都以 user_id 隔离，见 server.py 与 main_agent.py。

配置方式（.env）：

    API_KEYS=alice:sk-alice-0123456789abcdef,bob:sk-bob-0123456789abcdef

- 格式为「用户名:密钥」对，逗号分隔；
- 不配置 API_KEYS 时进入本地开发模式：所有请求归属默认用户 local，
  不校验密钥（方便前端本地联调；部署时必须配置）；
- 密钥建议 32 位以上随机串，例如 `openssl rand -hex 32` 生成。
"""

import os
import re
import secrets
from dataclasses import dataclass

from fastapi import Header, HTTPException

# 本地开发模式下的默认租户身份
DEV_USER_ID = "local"

# 用户名只允许小写字母和数字：保证 "user-thread" 复合键无歧义
# （用户名里如果允许连字符，alice-b 与 thread c 会和 alice 与 b-c 撞键）
_USER_ID_PATTERN = re.compile(r"^[a-z0-9]{1,15}$")

# 密钥最小长度，防止用弱密钥草草上线
_MIN_KEY_LENGTH = 16


@dataclass(frozen=True)
class Principal:
    """一次请求所属的租户身份，贯穿会话目录、存储与推送隔离"""

    user_id: str

    @property
    def is_dev_mode(self) -> bool:
        return self.user_id == DEV_USER_ID


def parse_api_keys(raw: str) -> dict[str, str]:
    """
    解析 API_KEYS 环境变量为 {密钥: 用户名} 映射

    :param raw: 形如 "alice:sk-xxx,bob:sk-yyy" 的原始配置
    :return: 密钥到用户名的映射；未配置时返回空字典
    :raises RuntimeError: 配置格式错误（缺冒号、用户名非法、密钥过短、重复）
    """
    keys: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            raise RuntimeError(f"API_KEYS 配置格式错误（应为 用户名:密钥）: {pair!r}")
        name, key = pair.split(":", 1)
        name, key = name.strip(), key.strip()
        if not _USER_ID_PATTERN.match(name):
            raise RuntimeError(
                f"API_KEYS 用户名非法（仅小写字母/数字，最长 15 位）: {name!r}"
            )
        if len(key) < _MIN_KEY_LENGTH:
            raise RuntimeError(f"API_KEYS 中 {name!r} 的密钥长度不足 {_MIN_KEY_LENGTH} 位")
        if key in keys:
            raise RuntimeError(f"API_KEYS 存在重复密钥")
        keys[key] = name
    return keys


def load_api_keys() -> dict[str, str]:
    """每次请求重新读取环境变量，便于测试注入；规模下解析开销可忽略"""
    raw = os.getenv("API_KEYS", "")
    try:
        return parse_api_keys(raw)
    except RuntimeError as e:
        # 配置错误属于服务端问题，统一转 500 并保留原因，避免带病运行
        raise HTTPException(status_code=500, detail=f"API_KEYS 配置错误: {e}")


def authenticate_api_key(api_key: str | None) -> Principal:
    """
    校验 API Key 并返回所属租户身份

    :param api_key: 请求携带的密钥（HTTP 头 X-API-Key 或 WS 查询参数 api_key）
    :return: 匹配的 Principal
    :raises HTTPException: 已配置密钥但请求未携带或密钥无效时返回 401
    """
    keys = load_api_keys()
    if not keys:
        # 未配置 API_KEYS：本地开发模式，不校验
        return Principal(user_id=DEV_USER_ID)

    if api_key:
        # 逐个常数时间比较，避免时序侧信道
        for candidate, user_id in keys.items():
            if secrets.compare_digest(candidate, api_key):
                return Principal(user_id=user_id)

    raise HTTPException(status_code=401, detail="无效或缺失的 API Key")


async def require_principal(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> Principal:
    """FastAPI 依赖：从请求头解析租户身份，供所有业务接口使用"""
    return authenticate_api_key(x_api_key)
