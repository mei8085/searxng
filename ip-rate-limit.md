# IP 限流完整链路分析

## 1. 触发入口的请求路径

### 1.1 初始化流程

```
应用启动 → webapp.py:1382
  ↓
limiter.initialize(app, settings)
  ↓
注册 Flask before_request 钩子 → limiter.py:251
  ↓
每次请求触发 pre_request() → limiter.py:212-214
  ↓
filter_request(sxng_request) → limiter.py:147-209
```

### 1.2 关键代码位置

- **初始化入口**: `searx/webapp.py:1382` - `limiter.initialize(app, settings)`
- **请求钩子注册**: `searx/limiter.py:251` - `app.before_request(pre_request)`
- **预处理函数**: `searx/limiter.py:212-214` - `pre_request()`
- **核心过滤**: `searx/limiter.py:147-209` - `filter_request()`
- **IP 限流实现**: `searx/botdetection/ip_limit.py:92-148` - `filter_request()`

---

## 2. 计数与窗口的共享存储实现

### 2.1 存储后端

使用 **Valkey（Redis 兼容）作为共享存储，通过 Lua 脚本实现原子性操作。

### 2.2 滑动窗口实现原理

**Lua 脚本：`searx/valkeylib.py:169-179`

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

**实现细节**：
- 使用 Valkey **有序集合（Sorted Set）存储请求时间戳
- `ZREMRANGEBYSCORE` 移除窗口外的旧请求
- `ZADD` 添加当前请求时间戳
- `ZCOUNT` 统计当前窗口内的请求数
- `EXPIRE` 设置键的过期时间

### 2.3 键名设计

所有计数器键名格式：
```
SearXNG_counter_<secret_hash(name)>
```

其中 `name` 的前缀包括：
- `ip_limit.API_WINDOW:<network>`
- `ip_limit.SUSPICIOUS_IP_WINDOW<network>`
- `ip_limit.BURST_WINDOW<network>`
- `ip_limit.LONG_WINDOW<network>`

**隐私保护**：IP 地址通过 HMAC-SHA256 哈希存储，结合 `server.secret_key` 加盐，避免直接存储原始 IP。

### 2.4 窗口类型与配置

| 窗口类型 | 时间窗口 | 正常阈值 | 可疑阈值 | 用途 |
|-----------|---------|---------|---------|-----|
| BURST_WINDOW | 20秒 | 15次 | 2次 | 防止突发请求 |
| LONG_WINDOW | 600秒（10分钟） | 150次 | 10次 | 防止持续请求 |
| API_WINDOW | 3600秒（1小时） | 4次 | - | API 请求限制 |
| SUSPICIOUS_IP_WINDOW | 2592000秒（30天） | 3次 | - | 可疑 IP 长期限制 |

---

## 3. 阈值配置

### 3.1 硬编码阈值（`searx/botdetection/ip_limit.py:61-89）

```python
BURST_WINDOW = 20          # 突发窗口时间（秒）
BURST_MAX = 15            # 突发窗口最大请求数
BURST_MAX_SUSPICIOUS = 2      # 可疑IP突发窗口最大请求数

LONG_WINDOW = 600            # 长窗口时间（秒）
LONG_MAX = 150               # 长窗口最大请求数
LONG_MAX_SUSPICIOUS = 10     # 可疑IP长窗口最大请求数

API_WINDOW = 3600           # API窗口时间（秒）
API_MAX = 4                   # API窗口最大请求数

SUSPICIOUS_IP_WINDOW = 3600 * 24 * 30  # 可疑IP窗口时间（秒）
SUSPICIOUS_IP_MAX = 3                      # 可疑IP窗口最大请求数
```

### 3.2 配置文件（`searx/limiter.toml）

```toml
[botdetection.ip_limit]
filter_link_local = false    # 是否过滤 link-local 地址
link_token = false        # 是否启用 link_token 可疑检测
```

### 3.3 网络前缀配置

```toml
[botdetection]
ipv4_prefix = 32        # IPv4 网络前缀长度
ipv6_prefix = 48        # IPv6 网络前缀长度
```

---

## 4. 豁免配置

### 4.1 IP 白名单（Pass List）

**配置位置**: `searx/limiter.toml`

```toml
[botdetection.ip_lists]
pass_ip = [
  # '192.168.0.0/16',  # IPv4 私有网络
  # 'fe80::/10'            # IPv6 链路本地
]
pass_searxng_org = true         # 是否启用 SearXNG 组织白名单
```

**内置白名单**（`searx/botdetection/ip_lists.py:41-45）：
- `167.235.158.251` - check.searx.space
- `2a01:04f8:1c1c:8fc2::/64` - check.searx.space

### 4.2 IP 黑名单（Block List）

```toml
[botdetection.ip_lists]
block_ip = [
  # '93.184.216.34',  # example.org
]
```

### 4.3 链路本地豁免

- 默认情况下，link-local 网络地址不进行限流监控：
  ```python
  if network.is_link_local and not cfg['botdetection.ip_limit.filter_link_local:
      return None  # 直接放行
  ```

### 4.4 健康检查豁免

`/healthz` 路径完全豁免，用于健康检查。

---

## 5. 与正常用户请求的分流顺序

### 5.1 完整请求处理流程（`searx/limiter.py:147-209）

```
请求到达
  ↓
1. 获取真实 IP 和网络（`get_network`）
  ↓
2. 路径检查：是否为 /healthz → 直接放行
  ↓
3. 网络检查：是否为 link-local → 直接放行
  ↓
4. 白名单检查（`ip_lists.pass_ip）
   ├─ 匹配 → 记录日志 → 直接放行
   └─ 不匹配 → 继续
  ↓
5. 黑名单检查（`ip_lists.block_ip）
   ├─ 匹配 → 返回 429 → 终止
   └─ 不匹配 → 继续
  ↓
6. 通用检测（所有请求）
   └─ http_user_agent.filter_request()
   └─ 触发 → 返回 429 → 终止
   └─ 不触发 → 继续
  ↓
7. /search 路径专属检测（仅 /search 路径）
   ├─ http_accept.filter_request()
   ├─ http_accept_encoding.filter_request()
   ├─ http_accept_language.filter_request()
   ├─ http_user_agent.filter_request()
   ├─ http_sec_fetch.filter_request()
   └─ ip_limit.filter_request()  ← IP 限流核心
   └─ 任意触发 → 返回 429 → 终止
   └─ 全部通过 → 正常处理请求
  ↓
正常处理请求
```

### 5.2 IP 限流内部流程（`searx/botdetection/ip_limit.py:92-148）

```
ip_limit.filter_request()
  ↓
1. link-local 检查（根据配置）
  ↓
2. API 请求检测（format != html）
   └─ API_WINDOW 计数 → 超过 API_MAX → 429
  ↓
3. link_token 可疑检测（如果启用）
   ├─ 不可疑 → 清除可疑计数 → 放行
   └─ 可疑 → 继续
  ↓
4. 可疑 IP 处理
   ├─ SUSPICIOUS_IP_WINDOW 计数
   ├─ 超过 SUSPICIOUS_IP_MAX → 302 重定向到首页
   ├─ BURST_WINDOW 计数 → 超过 BURST_MAX_SUSPICIOUS → 429
   └─ LONG_WINDOW 计数 → 超过 LONG_MAX_SUSPICIOUS → 429
  ↓
5. 普通限流（未启用 link_token 时）
   ├─ BURST_WINDOW 计数 → 超过 BURST_MAX → 429
   └─ LONG_WINDOW 计数 → 超过 LONG_MAX → 429
  ↓
放行
```

### 5.3 Link Token 可疑检测机制

**工作原理**（`searx/botdetection/link_token.py）：

1. 服务器生成随机 token 并注入到 HTML 中的 CSS 链接
2. 正常浏览器会请求 `/client<token>.css
3. 服务器收到请求后存储该 IP 网络的 ping 记录（有效期 3600秒
4. 后续请求检查是否存在有效 ping 记录
5. 无有效 ping 记录的请求被标记为可疑

**配置启用后限流阈值降低：
- 普通：BURST_MAX=15 → 可疑：BURST_MAX_SUSPICIOUS=2
- 普通：LONG_MAX=150 → 可疑：LONG_MAX_SUSPICIOUS=10

---

## 6. 关键文件索引

| 功能 | 文件路径 | 关键函数 |
|-----|---------|---------|
| 限流初始化 | `searx/limiter.py | `initialize()`, `filter_request()` |
| IP 限流核心 | `searx/botdetection/ip_limit.py` | `filter_request()` |
| 滑动窗口实现 | `searx/valkeylib.py` | `incr_sliding_window()` |
| 黑白名单 | `searx/botdetection/ip_lists.py` | `pass_ip()`, `block_ip()` |
| 可疑检测 | `searx/botdetection/link_token.py` | `is_suspicious()`, `ping()` |
| 配置管理 | `searx/botdetection/config.py` | `Config` 类 |
| Valkey 连接 | `searx/botdetection/valkeydb.py` | `get_valkey_client()` |
| 默认配置 | `searx/limiter.toml` | - |
