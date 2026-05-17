# Valkey 后端存储运行时状态分析报告

## 一、核心架构概览

### 1.1 模块分层

Valkey 后端存储在 SearXNG 中采用三层架构设计：

| 层级 | 模块文件 | 职责 |
|------|---------|------|
| 连接管理层 | `searx/valkeydb.py` | 全局 Valkey 客户端初始化、连接管理、健康检查 |
| 工具函数层 | `searx/valkeylib.py` | Lua 脚本缓存、计数器原语、滑动窗口实现 |
| 业务逻辑层 | `searx/limiter.py` `searx/botdetection/*.py` | 限流、机器人检测等业务功能 |

### 1.2 初始化流程

初始化入口在 `searx/webapp.py:1376` 的 `init()` 函数中：

```
webapp.init()
└── valkey_initialize()  [valkeydb.py:38]
    ├── 读取配置 valkey.url
    ├── 创建 Valkey 客户端（懒连接）
    ├── 执行 PING 命令验证连接
    └── 成功则设置全局 _CLIENT，失败则置为 None
```

## 二、持久化状态分类与读写时机

### 2.1 状态类型总览

Valkey 中存储的所有状态均为**临时状态**，全部带有过期时间（TTL），**无持久化到磁盘的业务数据**。所有 key 前缀统一为 `SearXNG_`。

### 2.2 限流计数器状态

**Key 模式**: `SearXNG_counter_<secret_hash(name)>`

**存储内容**: 
- 简单计数器：字符串类型，存储整数值
- 滑动窗口计数器：有序集合（ZSET），成员为时间戳，分数也为时间戳

**读写时机**:
- **写入**: 每次请求经过 `ip_limit.filter_request()` 时调用 `incr_sliding_window()` 或 `incr_counter()`
- **读取**: 同写入操作，原子性读写
- **过期**: 根据窗口类型自动设置 TTL

**计数器分类**:

| 计数器名称 | 窗口时长 | 阈值 | 用途 | 位置 |
|-----------|---------|------|------|------|
| BURST_WINDOW | 20秒 | 15次 | 突发请求限流 | `ip_limit.py:61-65` |
| BURST_MAX_SUSPICIOUS | 20秒 | 2次 | 可疑IP突发限流 | `ip_limit.py:67-68` |
| LONG_WINDOW | 600秒 | 150次 | 长窗口限流 | `ip_limit.py:70-74` |
| LONG_MAX_SUSPICIOUS | 600秒 | 10次 | 可疑IP长窗口限流 | `ip_limit.py:76-77` |
| API_WINDOW | 3600秒 | 4次 | API格式请求限流 | `ip_limit.py:79-83` |
| SUSPICIOUS_IP_WINDOW | 30天 | 3次 | 可疑IP长期追踪 | `ip_limit.py:85-89` |

### 2.3 Link Token 机器人检测状态

**Token 状态**:
- **Key**: `SearXNG_limiter.token`
- **类型**: 字符串
- **值**: 16位随机字符串
- **TTL**: 600秒 (`TOKEN_LIVE_TIME`)
- **读写时机**: 
  - 读取：每次渲染 HTML 模板时调用 `link_token.get_token()`
  - 写入：Token 过期后首次访问时自动生成新 token

**Ping 状态**:
- **Key 模式**: `SearXNG_limiter.ping[<hash>]`
- **Hash 构成**: `secret_hash(network + Accept-Language + User-Agent)`
- **类型**: 字符串，值为 `1`
- **TTL**: 3600秒 (`PING_LIVE_TIME`)
- **读写时机**:
  - 写入：客户端请求 `/client<token>.css` 时调用 `link_token.ping()`
  - 读取：每次 `/search` 请求时调用 `link_token.is_suspicious()` 验证

### 2.4 状态隐私保护

所有涉及 IP 的 key 均经过 `secret_hash()` 处理（`valkeylib.py:75-87`）：
- 使用 `server.secret_key` 作为 HMAC-SHA256 密钥
- 存储的是哈希值而非原始 IP
- 即使 Valkey 数据泄露也无法反向追溯真实 IP

## 三、与核心功能的协作逻辑

### 3.1 机器人检测协作流程

```
请求到达
    ↓
limiter.pre_request()  [limiter.py:212]
    ↓
http_* 系列头部检查（UA、Accept 等）
    ↓
ip_limit.filter_request()  [ip_limit.py:92]
    ├─> 检查 link_token.is_suspicious()  [link_token.py:73]
    │   └─> 查询 Valkey: GET SearXNG_limiter.ping[hash]
    │       ├─ 存在 → 非可疑，正常限流阈值
    │       └─ 不存在 → 可疑，使用更严格阈值
    ├─> 滑动窗口计数 incr_sliding_window()
    │   └─> Lua 脚本原子执行 ZREMRANGEBYSCORE / ZADD / ZCOUNT
    └─> 超过阈值 → 返回 429 Too Many Requests
```

**关键协作点**:
- `link_token` 模块通过验证 CSS token 请求来区分真实浏览器与机器人
- 真实浏览器会自动加载 CSS，从而在 Valkey 中留下 ping 记录
- 机器人通常不会执行 JS/CSS 渲染，因此缺少 ping 记录，被标记为可疑

### 3.2 限流计数协作

限流功能完全依赖 Valkey 的原子操作保证分布式环境下的准确性：

```python
# incr_sliding_window Lua 脚本  [valkeylib.py:169-179]
1. ZREMRANGEBYSCORE 移除窗口外的旧记录
2. ZADD 添加当前时间戳
3. ZCOUNT 统计当前窗口内的请求数
4. EXPIRE 刷新键的过期时间
5. 返回计数结果
```

**协作特性**:
- 全部操作在单个 Lua 脚本中原子执行，无竞态条件
- 多进程/多线程部署下计数准确
- 滑动窗口精度为毫秒级（使用 Valkey TIME 命令）

### 3.3 与查询历史的关系

**重要结论**: SearXNG **不使用 Valkey 存储任何查询历史**。

- 查询历史不属于当前系统设计的功能范围
- 用户搜索查询仅在内存中短暂存在于请求处理周期
- 无任何持久化用户搜索记录的代码逻辑
- SQLite 缓存（`searx/cache.py`）目前仅用于可配置的缓存场景，默认不启用

## 四、后端不可用时的降级路径

### 4.1 连接失败降级

**初始化阶段** (`valkeydb.py:61-65`):
- 捕获 `ValkeyError` 异常
- 全局 `_CLIENT` 置为 `None`
- 记录错误日志但不终止应用启动
- 函数返回 `False`

### 4.2 Limiter 降级

**Limiter 初始化** (`limiter.py:236-243`):
```python
if not valkey_client:
    logger.error("The limiter requires Valkey...")
    if settings['server']['public_instance']:
        sys.exit(1)  # 公共实例强制要求 Valkey
    return  # 普通实例静默降级，不启用 limiter
```

**降级行为**:
- `_INSTALLED` 标志保持 `False`
- `app.before_request(pre_request)` 不会被注册
- **所有请求直接通过，无限流和机器人检测**

### 4.3 Link Token 降级

**Token 获取降级** (`link_token.py:143-148`):
```python
try:
    valkey_client = valkeydb.get_valkey_client()
except ValueError:
    return '12345678'  # 返回固定默认值
```

**降级行为**:
- Token 固定为 `'12345678'`，失去随机安全性
- Ping 写入和验证逻辑仍会调用，但因 Valkey 不可用会抛出异常
- 实际效果等同于机器人检测失效

### 4.4 降级后的系统状态

| 功能模块 | Valkey 可用时 | Valkey 不可用时 |
|---------|-------------|----------------|
| 核心搜索功能 | 正常 | 完全正常，无影响 |
| 页面渲染 | 正常 | 完全正常，无影响 |
| 限流功能 | 启用，按阈值限制 | 完全禁用，无任何限制 |
| 机器人检测 | 启用，拦截可疑请求 | 完全禁用，所有请求放行 |
| API 限流 | 启用，每小时4次 | 完全禁用，无限制 |
| 公共实例 | 正常启动 | 启动失败（强制退出） |
| 私有实例 | 正常启动 | 正常启动（静默降级） |

### 4.5 运行时连接中断

当前代码**未实现运行时重连机制**：
- 初始化成功后，若 Valkey 中途宕机，后续请求会抛出 `ValkeyError`
- 异常未被业务代码捕获，将导致 500 错误
- 需重启应用才能重新初始化连接

## 五、关键设计决策分析

### 5.1 全内存无持久化设计

**决策依据**:
- 限流和机器人检测状态本质上都是临时状态
- 丢失这些数据仅影响安全性，不影响核心功能可用性
- 避免了 RDB/AOF 持久化的性能开销和复杂性

**权衡**:
- Valkey 重启后所有计数清零，限流窗口重置
- 攻击者可能通过触发 Valkey 重启来绕过限流

### 5.2 Lua 脚本原子性

**决策依据**:
- 滑动窗口计数需要读-改-写原子操作
- 避免使用 WATCH/MULTI/EXEC 事务的复杂性
- 减少网络往返，提升性能

### 5.3 公共实例强制依赖

**决策依据**:
- 公共实例面临更高的机器人和滥用风险
- 无限流保护可能导致实例被搜索引擎封禁
- 强制 Valkey 依赖是合理的安全权衡

## 六、总结

Valkey 在 SearXNG 中扮演**安全防护层**的角色，而非核心业务存储。其状态管理具有以下特征：

1. **状态临时性**: 所有数据均带 TTL，重启即清零
2. **隐私保护性**: IP 等敏感信息均以哈希形式存储
3. **优雅降级**: 私有实例可在无 Valkey 环境下正常运行
4. **原子性保障**: 计数操作通过 Lua 脚本保证分布式一致性
5. **功能边界清晰**: 仅用于限流和机器人检测，不涉及查询历史等业务数据

当 Valkey 不可用时，系统从"安全防护模式"降级为"裸奔模式"，核心搜索功能不受影响，但失去所有反滥用保护。
