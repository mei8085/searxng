# IP 限流完整链路分析

## 1. Limiter 生效条件与初始化流程

### 1.1 初始化入口

**代码位置**: `searx/limiter.py:222-251`

```python
def initialize(app: flask.Flask, settings):
    cfg = get_cfg()
    valkey_client = valkeydb.client()
    botdetection.init(cfg, valkey_client)  # 无论 limiter 是否启用，botdetection 始终初始化

    # 条件1: server.limiter 或 server.public_instance 必须为 true
    if not (settings['server']['limiter'] or settings['server']['public_instance']):
        return  # 直接返回，不安装 limiter

    # 条件2: Valkey 客户端必须可用
    if not valkey_client:
        logger.error("The limiter requires Valkey...")
        if settings['server']['public_instance']:
            sys.exit(1)  # public_instance 模式下 Valkey 不可用则程序退出
        return  # 普通 limiter 模式下 Valkey 不可用则不安装

    _INSTALLED = True

    # public_instance 模式强制启用 link_token
    if settings['server']['public_instance']:
        cfg.set('botdetection.ip_limit.link_token', True)

    # 注册请求钩子
    app.before_request(pre_request)
```

### 1.2 生效条件分支表

| server.limiter | server.public_instance | Valkey 可用 | Limiter 状态 |
|---------------|----------------------|------------|-------------|
| false | false | 任意 | **不生效** - 直接返回 |
| true | false | false | **不生效** - 记录错误日志后返回 |
| false | true | false | **程序退出** - sys.exit(1) |
| true | false | true | **生效** - 普通模式 |
| false/true | true | true | **生效** - 强制启用 link_token |

### 1.3 请求钩子注册链路

```
应用启动 → webapp.py:1382
  ↓
limiter.initialize(app, settings) → limiter.py:222
  ↓
条件满足 → app.before_request(pre_request) → limiter.py:251
  ↓
每次请求触发 pre_request() → limiter.py:212-214
  ↓
filter_request(sxng_request) → limiter.py:147-209
```

### 1.4 关键代码位置索引

| 功能 | 文件路径 | 行号 |
|-----|---------|-----|
| 初始化入口 | `searx/webapp.py` | 1382 |
| Limiter 初始化 | `searx/limiter.py` | 222-251 |
| 请求钩子注册 | `searx/limiter.py` | 251 |
| 预处理函数 | `searx/limiter.py` | 212-214 |
| 核心过滤函数 | `searx/limiter.py` | 147-209 |
| IP 限流实现 | `searx/botdetection/ip_limit.py` | 92-148 |

---

## 2. 计数与窗口的共享存储实现

### 2.1 存储后端

使用 **Valkey（Redis 兼容）** 作为共享存储，通过 Lua 脚本保证原子性操作。

### 2.2 滑动窗口 Lua 脚本实现

**代码位置**: `searx/valkeylib.py:169-179`

```lua
local expire = tonumber(ARGV[1])
local name = KEYS[1]
local current_time = redis.call('TIME')

redis.call('ZREMRANGEBYSCORE', name, 0, current_time[1] - expire)
redis.call('ZADD', name, current_time[1], current_time[1] .. current_time[2])
local result = redis.call('ZCOUNT', name, 0, current_time[1] + 1)
redis.call('EXPIRE', name, expire)
return result
```

**实现原理**：
1. `ZREMRANGEBYSCORE` - 移除滑动窗口外的过期时间戳
2. `ZADD` - 添加当前请求时间戳（秒 + 微秒保证唯一性）
3. `ZCOUNT` - 统计当前窗口内的请求数
4. `EXPIRE` - 刷新键的过期时间

### 2.3 四类计数键的命名差异

> **重要差异**: API_WINDOW 使用冒号分隔，其他三类直接拼接网络地址。

| 窗口类型 | 原始键名格式 | 代码位置 |
|---------|-------------|---------|
| API_WINDOW | `ip_limit.API_WINDOW:<network>` | `ip_limit.py:106` |
| SUSPICIOUS_IP_WINDOW | `ip_limit.SUSPICIOUS_IP_WINDOW<network>` | `ip_limit.py:116, 121` |
| BURST_WINDOW | `ip_limit.BURST_WINDOW<network>` | `ip_limit.py:129, 140` |
| LONG_WINDOW | `ip_limit.LONG_WINDOW<network>` | `ip_limit.py:133, 144` |

**最终存储键名**（经过 `secret_hash` 处理）：
```
SearXNG_counter_<HMAC-SHA256(原始键名, server.secret_key)>
```

### 2.4 隐私保护机制

IP 地址不直接存储，通过 HMAC-SHA256 哈希处理：

**代码位置**: `searx/valkeylib.py:75-87`

```python
def secret_hash(name: str):
    m = hmac.new(bytes(name, encoding='utf-8'), digestmod='sha256')
    m.update(bytes(get_setting('server.secret_key'), encoding='utf-8'))
    return m.hexdigest()
```

### 2.5 窗口配置总览

| 窗口类型 | 时间窗口 | 正常阈值 | 可疑阈值 | 适用范围 | 用途 |
|---------|---------|---------|---------|---------|-----|
| BURST_WINDOW | 20秒 | 15次 | 2次 | /search 路径 | 防止突发请求 |
| LONG_WINDOW | 600秒（10分钟） | 150次 | 10次 | /search 路径 | 防止持续请求 |
| API_WINDOW | 3600秒（1小时） | 4次 | - | /search 路径且 format != html | API 请求限制 |
| SUSPICIOUS_IP_WINDOW | 2592000秒（30天） | 3次 | - | /search 路径且 link_token 启用 | 可疑 IP 长期限制 |

> **重要说明**：所有限流窗口仅在 `/search` 路径下生效。非 `/search` 路径（如首页、静态资源等）不会进入 `ip_limit.filter_request`，因此不会触发任何计数。

**阈值硬编码位置**: `searx/botdetection/ip_limit.py:61-89`

```python
BURST_WINDOW = 20
BURST_MAX = 15
BURST_MAX_SUSPICIOUS = 2

LONG_WINDOW = 600
LONG_MAX = 150
LONG_MAX_SUSPICIOUS = 10

API_WINDOW = 3600
API_MAX = 4

SUSPICIOUS_IP_WINDOW = 3600 * 24 * 30  # 30天
SUSPICIOUS_IP_MAX = 3
```

---

## 3. 配置与豁免机制

### 3.1 限流配置（limiter.toml）

**默认配置位置**: `searx/limiter.toml`

```toml
[botdetection]
ipv4_prefix = 32        # IPv4 网络前缀长度
ipv6_prefix = 48        # IPv6 网络前缀长度
trusted_proxies = [     # 受信任的代理服务器
  '127.0.0.0/8',
  '::1',
]

[botdetection.ip_limit]
filter_link_local = false  # 是否过滤 link-local 地址
link_token = false         # 是否启用 link_token 可疑检测

[botdetection.ip_lists]
block_ip = []              # IP 黑名单
pass_ip = []               # IP 白名单
pass_searxng_org = true    # 是否启用 SearXNG 组织内置白名单
```

### 3.2 IP 白名单豁免

**代码位置**: `searx/botdetection/ip_lists.py:49-59`

```python
def pass_ip(real_ip, cfg):
    # 1. 检查内置 SearXNG 组织白名单
    if cfg.get('botdetection.ip_lists.pass_searxng_org', default=True):
        for net in SEARXNG_ORG:  # ['167.235.158.251', '2a01:04f8:1c1c:8fc2::/64']
            if real_ip in net:
                return True, "IP matches SEARXNG_ORG list."
    # 2. 检查用户配置的 pass_ip 列表
    return ip_is_subnet_of_member_in_list(real_ip, 'botdetection.ip_lists.pass_ip', cfg)
```

### 3.3 IP 黑名单拦截

**代码位置**: `searx/botdetection/ip_lists.py:62-70`

```python
def block_ip(real_ip, cfg):
    block, msg = ip_is_subnet_of_member_in_list(real_ip, 'botdetection.ip_lists.block_ip', cfg)
    if block:
        msg += " To remove IP from list, please contact the maintainer."
    return block, msg
```

### 3.4 其他豁免机制

1. **链路本地地址豁免**（`searx/botdetection/ip_limit.py:101-103`）：
   ```python
   if network.is_link_local and not cfg['botdetection.ip_limit.filter_link_local']:
       return None  # 直接放行
   ```

2. **健康检查豁免**（`searx/limiter.py:154-155`）：
   ```python
   if request.path == '/healthz':
       return None  # 直接放行
   ```

3. **link-local 网络预豁免**（`searx/limiter.py:159-160`）：
   ```python
   if network.is_link_local:
       return None  # 直接放行
   ```

---

## 4. 请求分流完整流程

### 4.1 外层过滤流程（limiter.filter_request）

**代码位置**: `searx/limiter.py:147-209`

```
请求到达
  ↓
1. 解析真实 IP 和网络（get_network）
  ↓
2. 路径为 /healthz ──→ [放行]
  ↓
3. 网络为 link-local ──→ [放行]
  ↓
4. 白名单检查（pass_ip）
   ├─ 匹配 → 记录日志 → [放行]
   └─ 不匹配 → 继续
  ↓
5. 黑名单检查（block_ip）
   ├─ 匹配 → 返回 429 → [拦截]
   └─ 不匹配 → 继续
  ↓
6. 通用检测（所有请求）
   └─ http_user_agent.filter_request()
      ├─ 触发 → 返回 429 → [拦截]
      └─ 不触发 → 继续
  ↓
7. 路径为 /search ？
   ├─ 否 → [放行]
   └─ 是 → 进入 /search 专属检测
         ↓
         7.1 http_accept.filter_request()
             ├─ 触发 → 返回 429 → [拦截]
             └─ 不触发 → 继续
         ↓
         7.2 http_accept_encoding.filter_request()
             ├─ 触发 → 返回 429 → [拦截]
             └─ 不触发 → 继续
         ↓
         7.3 http_accept_language.filter_request()
             ├─ 触发 → 返回 429 → [拦截]
             └─ 不触发 → 继续
         ↓
         7.4 http_user_agent.filter_request()
             ├─ 触发 → 返回 429 → [拦截]
             └─ 不触发 → 继续
         ↓
         7.5 http_sec_fetch.filter_request()
             ├─ 触发 → 返回 429 → [拦截]
             └─ 不触发 → 继续
         ↓
         7.6 ip_limit.filter_request() ← IP 限流核心
             ├─ 触发 → 返回 429/302 → [拦截]
             └─ 不触发 → [放行]
  ↓
正常处理请求
```

### 4.2 IP 限流内部流程（ip_limit.filter_request）

**代码位置**: `searx/botdetection/ip_limit.py:92-148`

```
ip_limit.filter_request(network, request, cfg)
  ↓
1. link-local 检查
   ├─ 是且 filter_link_local=false → [放行]
   └─ 否 → 继续
  ↓
2. API 请求检测（format != html）
   ├─ 否 → 继续
   └─ 是 → 计数 API_WINDOW
         ├─ > API_MAX → 返回 429 → [拦截]
         └─ ≤ API_MAX → 继续后续检测
  ↓
3. link_token 已启用？
   ├─ 否 → 进入【普通限流模式】
   └─ 是 → 进入【可疑检测模式】
```

#### 普通限流模式（link_token 未启用）

```
  ↓
4. 计数 BURST_WINDOW
   ├─ > BURST_MAX(15) → 返回 429 → [拦截]
   └─ ≤ BURST_MAX → 继续
  ↓
5. 计数 LONG_WINDOW
   ├─ > LONG_MAX(150) → 返回 429 → [拦截]
   └─ ≤ LONG_MAX → [放行]
```

#### 可疑检测模式（link_token 已启用）

```
  ↓
4. 调用 link_token.is_suspicious()
   ├─ 不可疑 → drop SUSPICIOUS_IP_WINDOW 计数 → [放行]
   └─ 可疑 → 继续
  ↓
5. 计数 SUSPICIOUS_IP_WINDOW
   ├─ > SUSPICIOUS_IP_MAX(3) → 302 重定向到 / → [拦截]
   └─ ≤ SUSPICIOUS_IP_MAX → 继续
  ↓
6. 计数 BURST_WINDOW
   ├─ > BURST_MAX_SUSPICIOUS(2) → 返回 429 → [拦截]
   └─ ≤ BURST_MAX_SUSPICIOUS → 继续
  ↓
7. 计数 LONG_WINDOW
   ├─ > LONG_MAX_SUSPICIOUS(10) → 返回 429 → [拦截]
   └─ ≤ LONG_MAX_SUSPICIOUS → [放行]
```

### 4.3 各节点返回值汇总

| 检查节点 | 放行条件 | 拦截条件 | 拦截返回 |
|---------|---------|---------|---------|
| /healthz 路径 | 路径匹配 | - | - |
| link-local 网络 | 网络类型匹配 | - | - |
| pass_ip 白名单 | IP 匹配 | - | - |
| block_ip 黑名单 | IP 不匹配 | IP 匹配 | 429 |
| http_user_agent（通用） | 通过 | 不通过 | 429 |
| 非 /search 路径 | 路径不匹配 | - | - |
| http_accept | 通过 | 不通过 | 429 |
| http_accept_encoding | 通过 | 不通过 | 429 |
| http_accept_language | 通过 | 不通过 | 429 |
| http_user_agent（search） | 通过 | 不通过 | 429 |
| http_sec_fetch | 通过 | 不通过 | 429 |
| API_WINDOW | ≤ 4（继续后续检测） | > 4 | 429 |
| SUSPICIOUS_IP_WINDOW | ≤ 3 | > 3 | 302 重定向 / |
| BURST_WINDOW（普通） | ≤ 15 | > 15 | 429 |
| BURST_WINDOW（可疑） | ≤ 2 | > 2 | 429 |
| LONG_WINDOW（普通） | ≤ 150 | > 150 | 429 |
| LONG_WINDOW（可疑） | ≤ 10 | > 10 | 429 |

---

## 5. Link Token 可疑检测机制

### 5.1 工作原理

**代码位置**: `searx/botdetection/link_token.py`

```
服务器端                            客户端（浏览器）
  │                                   │
  │ 1. 生成随机 token（16位）         │
  │    存储到 Valkey，TTL=600秒       │
  │                                   │
  │ 2. HTML 中注入 CSS 链接           │
  │    <link href="/client<token>.css">│
  │                                   │
  │    ───────────────────────────────>
  │                                   │ 3. 浏览器请求 CSS 文件
  │                                   │
  │ 4. 收到 /client<token>.css 请求   │
  │    - 验证 token 有效性            │
  │    - 存储 ping 记录               │
  │      key: SearXNG_limiter.ping[hash]│
  │      TTL: 3600秒                  │
  │                                   │
  │ 5. 后续搜索请求                   │
  │    - 检查是否存在有效 ping 记录   │
  │    - 不存在 → 标记为可疑          │
  │    - 存在 → 刷新 ping TTL         │
```

### 5.2 关键函数

**is_suspicious()** - `searx/botdetection/link_token.py:73-90`
- 检查是否存在有效 ping 记录
- 存在则刷新 TTL，不存在返回 `True`（可疑）

**ping()** - `searx/botdetection/link_token.py:93-112`
- 验证 token 有效性
- 存储 ping 记录，TTL = 3600秒

**get_ping_key()** - `searx/botdetection/link_token.py:115-125`
- 生成 ping 键名：`SearXNG_limiter.ping[<hash(network + Accept-Language + User-Agent)>]`
- 按网络 + 浏览器特征组合区分会话

---

## 6. 关键文件索引

| 功能模块 | 文件路径 | 核心函数/配置 |
|---------|---------|-------------|
| Limiter 初始化与过滤 | `searx/limiter.py` | `initialize()`, `filter_request()`, `pre_request()` |
| IP 限流核心逻辑 | `searx/botdetection/ip_limit.py` | `filter_request()` |
| IP 黑白名单 | `searx/botdetection/ip_lists.py` | `pass_ip()`, `block_ip()` |
| Link Token 可疑检测 | `searx/botdetection/link_token.py` | `is_suspicious()`, `ping()`, `get_token()` |
| Valkey 滑动窗口 | `searx/valkeylib.py` | `incr_sliding_window()`, `drop_counter()`, `secret_hash()` |
| Valkey 连接管理 | `searx/botdetection/valkeydb.py` | `get_valkey_client()`, `set_valkey_client()` |
| 配置管理 | `searx/botdetection/config.py` | `Config` 类 |
| 辅助工具 | `searx/botdetection/_helpers.py` | `get_network()`, `too_many_requests()` |
| 默认配置 | `searx/limiter.toml` | 配置 schema |
| Web 应用集成 | `searx/webapp.py` | `limiter.initialize()` 调用 |
