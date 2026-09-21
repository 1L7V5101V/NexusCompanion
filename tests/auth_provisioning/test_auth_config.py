"""C5 ``[auth]`` 配置节 timeout 契约单元测试（design ADR-7，PILOT_ROADMAP §5.9.3）。

非 PG、纯配置：断言 §10 PROPOSED DEFAULT 冻结数值（session idle 7d /
absolute 30d；admin idle 30min / absolute 12h；invitation ttl 7d）、秒换算
属性、以及 cookie/admin 网络边界默认值。会话创建时会把这些数值固化到行
（design §1），故此处校验的是配置源本身；validate/expiration 行为归
test_http_contract / test_provisioning_lifecycle（PG write-only）。
"""

from __future__ import annotations

from agent.config_models import AuthConfig

# §5.9.3 / §10 PROPOSED DEFAULT 冻结数值（P-1 复核后按部署记录）。
SESSION_IDLE_S = 7 * 24 * 3600
SESSION_ABSOLUTE_S = 30 * 24 * 3600
ADMIN_IDLE_S = 30 * 60
ADMIN_ABSOLUTE_S = 12 * 3600
INVITATION_TTL_S = 7 * 24 * 3600


def test_timeout_defaults_match_roadmap_freeze() -> None:
    """默认值与 §10 PROPOSED DEFAULT 逐项相等。"""
    cfg = AuthConfig()
    assert cfg.session_idle_hours == 168
    assert cfg.session_absolute_hours == 720
    assert cfg.admin_idle_minutes == 30
    assert cfg.admin_absolute_hours == 12
    assert cfg.invitation_token_ttl_hours == 168


def test_timeout_seconds_properties() -> None:
    """秒换算属性与冻结值一致（service/repo 按秒消费）。"""
    cfg = AuthConfig()
    assert cfg.session_idle_s == SESSION_IDLE_S
    assert cfg.session_absolute_s == SESSION_ABSOLUTE_S
    assert cfg.admin_idle_s == ADMIN_IDLE_S
    assert cfg.admin_absolute_s == ADMIN_ABSOLUTE_S
    assert cfg.invitation_token_ttl_hours * 3600 == INVITATION_TTL_S


def test_cookie_and_admin_boundary_defaults() -> None:
    """§5.9.3/ADR-5 默认：Secure on、admin 仅回环地址。"""
    cfg = AuthConfig()
    assert cfg.cookie_secure is True
    assert cfg.admin_allow_ips == ["127.0.0.1", "::1"]
    assert cfg.origin_allowlist == []