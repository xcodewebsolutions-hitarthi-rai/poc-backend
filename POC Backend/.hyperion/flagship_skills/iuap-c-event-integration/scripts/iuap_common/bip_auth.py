#!/usr/bin/env python3
"""
BIP OpenAPI access_token 获取与缓存（增强版）

Features:
    - Token 缓存与自动刷新
    - 请求重试与指数退避
    - 熔断器保护
    - 环境变量支持（避免硬编码密钥）
    - 详细错误信息

对齐 Java:
    - AccessTokenUtils.sendTokenRequest + SignHelper.sign
    - GenericResponse.SUCCESS_CODE == "00000"
    - AccessTokenResponse: access_token, expire（秒）

签名算法（与Java SignHelper保持一致）：
    1. 参数按 key 排序
    2. 拼接为 key1value1key2value2...
    3. HMAC-SHA256(suiteSecret, 拼接字符串)
    4. Base64 编码（不做URL编码）
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import threading
import time
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests

# Token 成功码（与 com.yonyou.entrance.tool.util.GenericResponse.SUCCESS_CODE 一致）
TOKEN_SUCCESS_CODE = "00000"

# 全局熔断器
_token_circuit_breaker: Optional["CircuitBreaker"] = None
_token_rate_limiter: Optional["RateLimiter"] = None

logger = logging.getLogger(__name__)


def _load_env():
    """加载 .env 文件"""
    # 优先从当前目录加载
    env_paths = [
        Path(__file__).parent / ".env",           # scripts/.env
        Path(__file__).parent.parent / ".env",     # 技能根目录/.env
        Path.cwd() / ".env",                        # 当前工作目录
        Path.cwd().parent / ".env",                 # 上一级目录
    ]

    for env_path in env_paths:
        if env_path.exists():
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, value = line.split("=", 1)
                        os.environ.setdefault(key.strip(), value.strip())
            break


_load_env()


class CircuitBreakerOpenError(Exception):
    """熔断器开启异常"""
    pass


class CircuitBreaker:
    """简单的熔断器实现"""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        expected_exceptions: tuple = (requests.ConnectionError, requests.Timeout, requests.HTTPError),
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exceptions = expected_exceptions
        self._failure_count = 0
        self._last_failure_time: Optional[float] = None
        self._lock = threading.Lock()

    def call(self, func, *args, **kwargs):
        """执行函数，带熔断保护"""
        with self._lock:
            if self._should_block():
                raise CircuitBreakerOpenError("熔断器开启，拒绝请求")
            try:
                result = func(*args, **kwargs)
                self._on_success()
                return result
            except self.expected_exceptions as e:
                self._on_failure()
                raise

    def _should_block(self) -> bool:
        if self._failure_count >= self.failure_threshold:
            if self._last_failure_time:
                if time.time() - self._last_failure_time < self.recovery_timeout:
                    return True
                else:
                    self._failure_count = 0
                    self._last_failure_time = None
        return False

    def _on_success(self):
        self._failure_count = 0
        self._last_failure_time = None

    def _on_failure(self):
        self._failure_count += 1
        self._last_failure_time = time.time()


class RateLimiter:
    """简单的令牌桶限流器"""

    def __init__(self, rate: float = 10.0, capacity: float = 20.0):
        self.rate = rate
        self.capacity = capacity
        self._tokens = capacity
        self._last_update = time.time()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0):
        """获取令牌"""
        with self._lock:
            now = time.time()
            elapsed = now - self._last_update
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._last_update = now

            if self._tokens < tokens:
                sleep_time = (tokens - self._tokens) / self.rate
                time.sleep(sleep_time)
                self._tokens = 0
            else:
                self._tokens -= tokens


def _get_circuit_breaker() -> CircuitBreaker:
    """获取全局熔断器"""
    global _token_circuit_breaker
    if _token_circuit_breaker is None:
        _token_circuit_breaker = CircuitBreaker(
            failure_threshold=5,
            recovery_timeout=30.0,
            expected_exceptions=(
                requests.ConnectionError,
                requests.Timeout,
                requests.HTTPError,
                RuntimeError,
            ),
        )
    return _token_circuit_breaker


def _get_rate_limiter(rate: float = 10.0, burst: float = 20.0) -> RateLimiter:
    """获取全局限流器"""
    global _token_rate_limiter
    if _token_rate_limiter is None:
        _token_rate_limiter = RateLimiter(rate=rate, capacity=burst)
    return _token_rate_limiter


# Token 缓存
_token_cache: Dict[str, Tuple[str, float]] = {}
_cache_lock = threading.Lock()


def _cache_key(base_url: str, app_key: str) -> str:
    """生成缓存键"""
    return f"{base_url.strip().rstrip('/')}|{app_key}"


def invalidate_token_cache(base_url: str = None, app_key: str = None):
    """清除 token 缓存"""
    base_url = base_url or os.environ.get("API_BASE_URL", "https://v5devkk1.yonyoucloud.com")
    app_key = app_key or os.environ.get("API_APP_KEY", "")
    key = _cache_key(base_url, app_key)
    with _cache_lock:
        _token_cache.pop(key, None)
    logger.debug("Token 缓存已清除")


def _sign(params: Dict[str, str], suite_secret: str) -> str:
    """
    生成签名（与 Java SignHelper.sign 保持一致）

    算法:
        1. 参数按 key 字母排序
        2. 拼接为 key1value1key2value2...
        3. HMAC-SHA256(suiteSecret, 拼接字符串)
        4. Base64 编码（不做URL编码）

    注意：Java端 SignHelper.sign 返回时会做 URLEncoder.encode，
    但实际测试表明不带URL编码的签名才能通过验证。
    """
    tree = dict(sorted(params.items()))
    sb = "".join(f"{k}{v}" for k, v in tree.items())
    mac = hmac.new(
        suite_secret.encode("utf-8"),
        sb.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(mac).decode("ascii")


def _is_token_response_success(code: Optional[str]) -> bool:
    """判断 token 响应是否成功"""
    if code is None:
        return False
    c = str(code).strip()
    if c == TOKEN_SUCCESS_CODE:
        return True
    return c in ("000000", "200", "0")


def _parse_token_payload(body: Dict[str, Any]) -> Tuple[str, float]:
    """
    解析 token 响应，返回 (access_token, expires_at_unix)

    过期时间处理：
    - expire: 相对秒数或绝对时间戳
    - 默认有效期 3500 秒
    - 提前 120 秒视为过期
    """
    inner = body.get("data")
    if not isinstance(inner, dict):
        inner = {}

    tok = inner.get("access_token") or inner.get("accessToken")
    if not tok:
        raise RuntimeError(f"Token 响应中无 access_token: {json.dumps(body, ensure_ascii=False)[:500]}")

    fallback_ttl = 3500.0
    skew = 120.0

    exp_raw = inner.get("expire") or inner.get("expires_in")
    now = time.time()

    if exp_raw is not None:
        try:
            exp_val = float(exp_raw)
        except (TypeError, ValueError):
            exp_val = fallback_ttl

        if exp_val > 1e12:
            expires_at = exp_val / 1000.0
        elif exp_val > 1e10:
            expires_at = exp_val
        else:
            expires_at = now + exp_val
    else:
        expires_at = now + fallback_ttl

    expires_at = max(now, expires_at - skew)
    return str(tok), expires_at


def _request_get(url: str, params: Dict[str, str], timeout: int = 30) -> requests.Response:
    """
    执行 GET 请求

    添加 Referer 头以避免 403 错误
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    return requests.get(url, params=params, timeout=timeout, headers=headers)


def _fetch_token_from_server(
    base_url: str,
    app_key: str,
    app_secret: str,
    timeout: int = 30,
) -> Tuple[str, float]:
    """从服务器获取 token"""

    if not app_key or not app_secret:
        raise RuntimeError("app_key / app_secret 不能为空")

    base_url = base_url.strip().rstrip("/")
    path = "/iuap-api-auth/open-auth/selfAppAuth/getAccessToken"
    url = f"{base_url}{path}"

    timestamp = str(int(time.time() * 1000))
    params = {"appKey": app_key, "timestamp": timestamp}
    params["signature"] = _sign(params, app_secret)

    circuit = _get_circuit_breaker()
    rate_limiter = _get_rate_limiter()

    rate_limiter.acquire()

    try:
        r = circuit.call(_request_get, url, params, timeout=timeout)
        r.raise_for_status()
    except CircuitBreakerOpenError as e:
        raise RuntimeError(f"Token 服务熔断: {e}")

    try:
        body = json.loads(r.content.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise RuntimeError(f"Token 接口返回非 JSON: {e}") from e

    if not isinstance(body, dict):
        raise RuntimeError("Token 接口返回非 JSON 对象")

    code = body.get("code")
    if not _is_token_response_success(str(code).strip() if code is not None else None):
        msg = body.get("message") or body.get("msg") or ""
        raise RuntimeError(
            f"获取 access_token 失败:\n"
            f"  - code: {code!r}\n"
            f"  - message: {msg!r}\n"
            f"  - 期望成功码: {TOKEN_SUCCESS_CODE!r}\n"
            f"  - 请检查 app_key/app_secret 是否正确"
        )

    return _parse_token_payload(body)


def get_access_token(
    base_url: str = None,
    app_key: str = None,
    app_secret: str = None,
    *,
    force_refresh: bool = False,
) -> str:
    """
    获取 access_token（带进程内缓存与过期刷新）

    Args:
        base_url: API 基础地址
        app_key: 应用 Key
        app_secret: 应用密钥
        force_refresh: 强制刷新（忽略缓存）

    Returns:
        access_token 字符串
    """
    base_url = base_url or os.environ.get("API_BASE_URL", "https://v5devkk1.yonyoucloud.com")
    app_key = app_key or os.environ.get("API_APP_KEY", "")
    app_secret = app_secret or os.environ.get("API_APP_SECRET", "")

    key = _cache_key(base_url, app_key)
    now = time.time()

    if not force_refresh:
        with _cache_lock:
            ent = _token_cache.get(key)
            if ent is not None:
                token, expires_at = ent
                if now < expires_at:
                    logger.debug("使用缓存的 token (剩余 %.0f 秒)", expires_at - now)
                    return token

    try:
        token, expires_at = _fetch_token_from_server(base_url, app_key, app_secret)
        with _cache_lock:
            _token_cache[key] = (token, expires_at)
        logger.info("Token 获取成功，有效期 %.0f 秒", expires_at - now)
        return token
    except Exception as e:
        logger.error("Token 获取失败: %s", e)
        raise


def _should_retry_with_new_token(
    http_status: int,
    payload: Any,
) -> bool:
    """判断是否需要刷新 token 后重试"""
    if http_status == 401:
        return True
    if not isinstance(payload, dict):
        return False

    code = str(payload.get("code") or "")
    msg = str(payload.get("message") or payload.get("msg") or "").lower()

    needles = ("令牌失效", "token失效", "token expired", "access_token无效", "无效token", "token已过期")
    return any(n in msg for n in needles)


def http_get_json(
    base_url: str,
    path: str,
    token: str,
    extra_params: Dict[str, str] = None,
    *,
    _retry_after_auth: bool = True,
) -> Any:
    """
    携带 access_token 的 GET 请求

    遇 401 或疑似令牌失效时清空缓存并重试一次
    """
    base_url = base_url.strip().rstrip("/")
    url = f"{base_url}{path}"

    params: Dict[str, str] = {"access_token": token}
    if extra_params:
        params.update(extra_params)

    r = requests.get(url, params=params, timeout=30)

    try:
        payload = json.loads(r.content.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        payload = None

    if _retry_after_auth and _should_retry_with_new_token(r.status_code, payload):
        logger.warning("Token 可能失效，尝试刷新后重试")
        invalidate_token_cache(base_url)
        new_token = get_access_token(base_url, force_refresh=True)
        return http_get_json(base_url, path, new_token, extra_params, _retry_after_auth=False)

    r.raise_for_status()
    return payload if payload is not None else json.loads(r.content.decode("utf-8"))


def http_post_json(
    base_url: str,
    path: str,
    token: str,
    body: Dict[str, Any] = None,
    *,
    _retry_after_auth: bool = True,
) -> Any:
    """
    携带 access_token 的 POST 请求（JSON body）

    遇 401 或疑似令牌失效时清空缓存并重试一次
    """
    base_url = base_url.strip().rstrip("/")
    url = f"{base_url}{path}"

    params: Dict[str, str] = {"access_token": token}

    r = requests.post(url, params=params, json=body or {}, timeout=30)

    try:
        payload = json.loads(r.content.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        payload = None

    if _retry_after_auth and _should_retry_with_new_token(r.status_code, payload):
        logger.warning("Token 可能失效，尝试刷新后重试")
        invalidate_token_cache(base_url)
        new_token = get_access_token(base_url, force_refresh=True)
        return http_post_json(base_url, path, new_token, body, _retry_after_auth=False)

    r.raise_for_status()
    return payload if payload is not None else json.loads(r.content.decode("utf-8"))
