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

### 2.4 哈希匿名机制

**实现细节** (`valkeylib.py:75-87`):

```python
def secret_hash(name: str):
    m = hmac.new(bytes(name, encoding='utf-8'), digestmod='sha256')
    m.update(bytes(get_setting('server.secret_key'), encoding='utf-8'))
    return m.hexdigest()
```

**密钥与消息的关系**：
- 此实现对 HMAC 的使用**非标准**：`name` 作为 HMAC 的密钥，`secret_key` 作为 HMAC 的消息
- 标准 HMAC 应为 `HMAC(key, message)`，此处实际计算的是 `HMAC(name, secret_key)`
- 即：待匿名化的标识（如 IP）是密钥，而配置中的 `secret_key` 是被哈希的消息

**可逆风险边界**：
- **单向性**: HMAC-SHA256 本身是密码学单向函数，无法从哈希输出直接反推输入
- **暴力破解风险**: 如果 `name` 的取值空间有限（如 IPv4 仅 2^32 种可能），攻击者在**已知 `secret_key`** 的前提下，可以预计算所有可能 `name` 的哈希值，建立彩虹表进行反向匹配
- **安全前提**: 匿名化的有效性完全依赖 `server.secret_key` 的保密性；若密钥泄露，攻击者可对所有存储的哈希值进行批量脱匿名
- **隐私保护等级**: 属于"弱匿名化"，而非强加密；可抵御低频偶发查询，但无法对抗有资源的定向攻击

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

Valkey 不可用分为两种典型场景，各场景下不同请求路径的异常行为存在显著差异。

### 4.1 场景 A：初始化阶段连接失败

**触发条件**: 应用启动时 `valkey_initialize()` 因连接超时而失败（`valkeydb.py:61-65`）。

**全局状态**:
- `searx.valkeydb._CLIENT = None`
- `botdetection.valkeydb.CLIENT = None`（因 `botdetection.init()` 仅在 client 为真值时才设置）
- `limiter._INSTALLED = False`（私有实例）或进程退出（公共实例）

#### 4.1.1 页面主流程异常路径

**首页与搜索页 (`/`, `/search`)**:
```
请求到达
    ↓
limiter.pre_request 未注册（因 _INSTALLED=False）
    ↓
正常执行业务逻辑
    ↓
模板渲染调用 link_token.get_token()  [link_token.py:134]
    ├─> try: get_valkey_client()
    │   └─> CLIENT is None → raise ValueError
    └─> except ValueError → return '12345678'
    ↓
页面正常渲染，token 固定为 '12345678'
```

**结果**: 核心搜索功能**完全正常**，仅失去限流和机器人检测保护。

#### 4.1.2 探针请求异常路径

**CSS Token 探针 (`/client<token>.css`)**:
```
请求到达
    ↓
执行 link_token.ping(request, token)  [link_token.py:93]
    ├─> get_valkey_client()
    │   └─> CLIENT is None → raise ValueError
    └─> 异常未被捕获 → 向上抛出
    ↓
Flask 返回 500 Internal Server Error
```

**结果**: CSS 探针请求返回 500，但浏览器通常忽略 CSS 加载失败，主页面仍可正常显示。

**健康检查 (`/healthz`)**:
- 不经过 limiter 过滤（`limiter.py:154` 特殊处理）
- 始终返回 200 OK，**无法反映 Valkey 可用性**

#### 4.1.3 降级汇总表

| 请求类型 | 路径 | HTTP 状态 | 用户感知 | 影响程度 |
|---------|------|----------|---------|---------|
| 首页 | `/` | 200 | 正常显示 | 无 |
| 搜索页 | `/search` | 200 | 正常搜索 | 无 |
| CSS 探针 | `/client<token>.css` | 500 | 无感知（浏览器忽略） | 轻微 |
| 健康检查 | `/healthz` | 200 | 无 | 无（监控盲点） |
| API 请求 | `/search?format=json` | 200 | 正常返回 | 无（但无限流） |

### 4.2 场景 B：运行时连接中断

**触发条件**: 初始化成功后 Valkey 服务中途宕机或网络分区。

**全局状态**:
- `searx.valkeydb._CLIENT` 非空（对象仍存在）
- `botdetection.valkeydb.CLIENT` 非空
- `limiter._INSTALLED = True`
- 但所有 Valkey 命令执行时会抛出 `ValkeyError`

#### 4.2.1 页面主流程异常路径

**所有经过 limiter 的请求**:
```
请求到达
    ↓
limiter.pre_request() 被调用
    ↓
ip_limit.filter_request()  [ip_limit.py:92]
    ├─> get_valkey_client() → 正常返回客户端对象
    └─> incr_sliding_window() → 执行 Lua 脚本
        └─> 连接中断 → raise ValkeyError
    ↓
异常未被捕获 → 向上抛出
    ↓
Flask 返回 500 Internal Server Error
```

**结果**: **全站不可用**，所有经过 limiter 的请求均返回 500。

#### 4.2.2 探针请求异常路径

**CSS Token 探针 (`/client<token>.css`)**:
```
link_token.ping()
    ├─> get_valkey_client() → 正常返回
    └─> valkey_client.set(ping_key, 1, ex=...)
        └─> 连接中断 → raise ValkeyError
    ↓
500 Internal Server Error
```

**健康检查 (`/healthz`)**:
- 仍直接返回 200 OK
- **完全无法检测到此故障**，属于严重监控盲点

#### 4.2.3 降级汇总表

| 请求类型 | 路径 | HTTP 状态 | 用户感知 | 影响程度 |
|---------|------|----------|---------|---------|
| 首页 | `/` | 500 | 错误页面 | 严重 |
| 搜索页 | `/search` | 500 | 错误页面 | 严重 |
| CSS 探针 | `/client<token>.css` | 500 | 无感知 | 轻微 |
| 健康检查 | `/healthz` | 200 | 无 | 严重（监控失效） |
| API 请求 | `/search?format=json` | 500 | 错误响应 | 严重 |

### 4.3 公共实例 vs 私有实例的降级差异

| 场景 | 公共实例 (`public_instance=true`) | 私有实例 (`public_instance=false`) |
|------|-----------------------------------|-----------------------------------|
| 初始化失败 | `sys.exit(1)` 强制退出，拒绝启动 | 静默降级，应用正常启动但 limiter 不启用 |
| 运行时中断 | 全站 500 错误 | 全站 500 错误 |
| 设计意图 | 安全优先，宁停勿滥 | 可用性优先，降级运行 |

### 4.4 现有降级机制的缺陷

1. **运行时中断无容错**: 初始化成功后的连接中断没有任何重试或降级逻辑，直接全站 500
2. **健康检查盲点**: `/healthz` 不检查 Valkey 可用性，无法通过常规监控发现故障
3. **部分失败未隔离**: CSS 探针失败不会影响主流程，但搜索主流程中的 Valkey 失败会导致整体失败
4. **无熔断机制**: 连续失败后不会自动切断 Valkey 依赖，也不会进入 limp mode

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

### 5.4 非标准 HMAC 使用

**决策依据**:
- 代码可能存在历史原因或笔误导致的参数顺序颠倒
- 实际效果仍能实现单向映射，但安全假设与标准 HMAC 不同
- 只要 `secret_key` 不泄露，仍能提供合理的匿名化效果

## 六、总结

Valkey 在 SearXNG 中扮演**安全防护层**的角色，而非核心业务存储。其状态管理具有以下特征：

1. **状态临时性**: 所有数据均带 TTL，重启即清零
2. **隐私保护条件性**: IP 等敏感信息以 HMAC 哈希形式存储，匿名化有效性依赖 `secret_key` 保密性，在密钥泄露或取值空间有限时存在脱匿名风险
3. **降级不一致性**: 初始化失败时私有实例可优雅降级，但运行时中断会导致全站不可用
4. **原子性保障**: 计数操作通过 Lua 脚本保证分布式一致性
5. **功能边界清晰**: 仅用于限流和机器人检测，不涉及查询历史等业务数据
6. **监控盲点**: 健康检查不覆盖 Valkey 状态，运行时故障难以被及时发现

当 Valkey 不可用时，系统的表现取决于故障发生时机：初始化阶段失败仅失去安全防护，核心功能仍可用；运行时中断则会导致全站 500 错误。
