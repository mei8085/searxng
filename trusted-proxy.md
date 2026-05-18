# 受信任反向代理判定与真实客户端地址还原技术报告

## 1. 概述

本报告基于 SearXNG 代码库的实际实现，从概念层面阐释反向代理环境下真实客户端地址与协议的还原机制。报告重点澄清：**实现不是先验证请求一定来自受信任代理，而是在配置 `trusted_proxies` 时按 `X-Forwarded-For` 从右向左剥离受信任代理，并按优先级回退到 `X-Real-IP`、`REMOTE_ADDR`**。

核心实现位于：
- IP 还原：`searx/botdetection/trusted_proxies.py`
- 协议还原：`searx/flaskfix.py`
- 限流入口：`searx/limiter.py`

---

## 2. 受信任反向代理判定机制

### 2.1 核心设计原则

SearXNG 的实现遵循**"尽力而为、安全降级"**的设计原则：

1. **不做前置信任验证**：不先验证 TCP 连接的 `REMOTE_ADDR` 是否在受信任列表中
2. **按头部优先级解析**：始终按 `X-Forwarded-For` → `X-Real-IP` → `REMOTE_ADDR` 的顺序尝试
3. **配置驱动的剥离逻辑**：仅当配置了 `trusted_proxies` 时，才对 `X-Forwarded-For` 执行从右向左的受信任代理剥离
4. **安全降级**：任何环节失败都有明确的降级策略

### 2.2 配置项形态

配置定义在 `searx/limiter.toml` 中：

```toml
[botdetection]
# 受信任的反向代理 IP 段列表
# 只有配置了此项，X-Forwarded-For 头部才会被用于客户端 IP 还原
trusted_proxies = [
  '127.0.0.0/8',    # IPv4 回环地址
  '::1',            # IPv6 回环地址
  # '192.168.0.0/16',  # 私有网络示例
  # '172.16.0.0/12',
  # '10.0.0.0/8',
  # 'fd00::/8',
]
```

**配置说明**：
- 类型：字符串数组，每个元素为 CIDR 格式的 IP 网段
- 支持 IPv4 和 IPv6
- 运行时通过 `ip_network(net, strict=False)` 解析为 `IPv4Network` 或 `IPv6Network` 对象
- 默认包含本地回环地址，适用于单机部署场景
- **空列表或未配置时，`X-Forwarded-For` 会被完全忽略**

### 2.3 信任剥离算法（而非前置验证）

判定逻辑位于 `ProxyFix.trusted_remote_addr()` 方法（`trusted_proxies.py:66-86`）：

```python
def trusted_remote_addr(
    self,
    x_forwarded_for: list[IPv4Address | IPv6Address],
    trusted_proxies: list[IPv4Network | IPv6Network],
) -> str:
    # 从右向左遍历 X-Forwarded-For 列表
    for addr in reversed(x_forwarded_for):
        trust: bool = False
        # 检查该 IP 是否属于任一受信任网段
        for net in trusted_proxies:
            if addr.version == net.version and addr in net:
                trust = True
                break
        # 找到第一个不受信任的 IP，即为真实客户端
        if not trust:
            return addr.compressed
    # 全部受信任时，返回最左侧地址
    return x_forwarded_for[0].compressed
```

**算法原理**：
1. X-Forwarded-For 头部格式为 `client, proxy1, proxy2, ...`，最右侧是最近的代理
2. **从右向左遍历**，逐个检查 IP 是否在受信任网段内
3. 遇到第一个不在受信任列表中的 IP，即为真实客户端地址
4. 若所有 IP 都受信任（极端情况：全部由内部代理转发），则取最左侧地址

**关键澄清**：
- ❌ 不是：先检查 TCP 连接的 REMOTE_ADDR 是否在 trusted_proxies 中，再决定是否信任头部
- ✅ 而是：直接解析 X-Forwarded-For，从右向左剥离受信任的代理 IP，剩余的第一个即为客户端

---

## 3. 真实客户端地址还原流程

### 3.1 请求头读取优先级

真实 IP 还原遵循严格的优先级顺序（`trusted_proxies.py:156-173`）：

| 优先级 | 头部名称 | 处理逻辑 | 依赖配置 |
|--------|----------|----------|----------|
| 1 | `X-Forwarded-For` | 从右向左遍历，剥离受信任代理 | **必须**配置 `trusted_proxies` |
| 2 | `X-Real-IP` | 直接使用头部值 | 不依赖 `trusted_proxies` |
| 3 | `REMOTE_ADDR` | WSGI 环境变量（TCP 连接地址） | 无 |
| 4 | 降级方案 | 使用黑洞地址 `100::`（RFC 6666） | 无 |

### 3.2 完整处理流程

```
请求到达
   ↓
[关键步骤] 移除原有 REMOTE_ADDR，避免依赖未经处理的值
   ↓
验证并清洗各个头部的 IP 格式
   ├─ REMOTE_ADDR: 验证有效性，处理 IPv4 映射（::ffff:x.x.x.x → x.x.x.x）
   ├─ X-Real-IP: 验证有效性，无效则丢弃
   └─ X-Forwarded-For: 逐个验证，任意无效则丢弃整个头部
   ↓
按优先级确定真实 IP
   ├─ 有 X-Forwarded-For 且有 trusted_proxies → 信任剥离算法
   ├─ 有 X-Forwarded-For 但无 trusted_proxies → 记录错误，丢弃头部，继续降级
   ├─ 有 X-Real-IP → 直接使用
   ├─ 有原始 REMOTE_ADDR → 使用
   └─ 全部失败 → 使用 100::
   ↓
将结果写入 environ['REMOTE_ADDR']
   ↓
保存原始值到 environ['botdetection.trusted_proxies.orig']['REMOTE_ADDR'] 供追溯
```

### 3.3 IP 清洗与规范化

在 `trusted_proxies.py:97-137` 中实现了严格的 IP 验证：

1. **IPv4 映射处理**：IPv6 格式的 IPv4 地址（如 `::ffff:192.168.1.1`）转换为纯 IPv4 格式
2. **格式验证**：使用 `ip_address()` 验证每个 IP 的合法性
3. **错误处理**：无效 IP 会被丢弃并记录错误日志，避免污染后续处理
4. **幂等性**：原始 REMOTE_ADDR 保存在 `environ['botdetection.trusted_proxies.orig']` 中供追溯

### 3.4 关键边界行为

代码证据（`trusted_proxies.py:144-148`）：
```python
if x_forwarded_for and not trusted_proxies:
    log_error_only_once("missing botdetection.trusted_proxies config")
    # without trusted_proxies, this variable is useless for determining
    # the real IP
    x_forwarded_for = []
```

**行为说明**：
- 配置 `trusted_proxies` 是使用 `X-Forwarded-For` 的前提条件
- 未配置时，即使请求携带 `X-Forwarded-For`，也会被忽略
- 这是一项安全措施，防止在未正确配置代理的情况下被头部伪造攻击

---

## 4. 客户端协议还原

### 4.1 实现位置

协议还原由 `ReverseProxyPathFix` 中间件实现（`flaskfix.py:11-69`），与 IP 还原是两个独立的中间件。

### 4.2 配置与头部读取

配置项：
```python
# settings.yml 中 server.base_url 优先级最高
if settings['server']['base_url']:
    base_url = urlparse(settings['server']['base_url'])
    self.scheme = base_url.scheme  # 从配置读取协议
```

头部读取优先级（`flaskfix.py:62`）：
```python
scheme = self.scheme or environ.get('HTTP_X_SCHEME') or environ.get('HTTP_X_FORWARDED_PROTO')
```

| 优先级 | 来源 | 说明 |
|--------|------|------|
| 1 | `server.base_url` 配置 | 静态配置，优先级最高 |
| 2 | `X-Scheme` 头部 | 反向代理设置 |
| 3 | `X-Forwarded-Proto` 头部 | 标准代理头部 |

### 4.3 执行结果

将解析出的协议写入 WSGI 环境：
```python
environ['wsgi.url_scheme'] = scheme  # 'http' 或 'https'
```

---

## 5. 与限流、统计及其他模块的共享机制

### 5.1 中间件执行顺序与数据流

在 `webapp.py:1394-1404` 中注册：

```python
# 1. 首先注册 ProxyFix（IP 还原）- 最内层，最先处理请求
app.wsgi_app = ProxyFix(app.wsgi_app)

# 2. 然后是 WhiteNoise（静态文件服务）
app.wsgi_app = WhiteNoise(app.wsgi_app, ...)

# 3. 最后注册 ReverseProxyPathFix（协议和路径还原）- 最外层，最后处理请求
patch_application(app)
```

**执行顺序**（请求从外到内）：
`ReverseProxyPathFix` → `WhiteNoise` → `ProxyFix` → Flask 应用

**数据流**：
```
WSGI 请求
   ↓
ReverseProxyPathFix: 设置 wsgi.url_scheme, SCRIPT_NAME, HTTP_HOST
   ↓
WhiteNoise: 处理静态文件
   ↓
ProxyFix: 设置 REMOTE_ADDR（真实客户端 IP）
   ↓
Flask 应用: request.remote_addr / request.scheme 可直接使用
   ├─ limiter.pre_request (before_request 钩子)
   │   └─ botdetection 各模块
   ├─ 视图函数
   ├─ 插件系统
   └─ 模板渲染
```

### 5.2 直接消费 REMOTE_ADDR 的模块（代码证据链）

#### 5.2.1 限流模块（核心消费者）

**入口**：`searx/limiter.py:147-209`
```python
def filter_request(request: SXNG_Request) -> werkzeug.Response | None:
    cfg = get_cfg()
    real_ip = ip_address(request.remote_addr)  # [1] 直接读取处理后的 IP
    network = get_network(real_ip, cfg)       # [2] 转换为网络段
    
    # ... 传递给所有 botdetection 子模块
    val = ip_limit.filter_request(network, request, cfg)
    val = ip_lists.pass_ip(real_ip, cfg)
    val = link_token.is_suspicious(network, request, True)
```

**子模块消费**：
- `searx/botdetection/ip_limit.py:92-148`：基于 IP 网络的滑动窗口限流
- `searx/botdetection/ip_lists.py`：IP 黑白名单检查
- `searx/botdetection/link_token.py:105`：link_token 验证时获取客户端 IP
- 所有 header probe 模块（`http_user_agent`、`http_accept` 等）：通过 `network` 参数间接受益于准确的客户端识别

#### 5.2.2 插件系统

**Tor 检查插件**：`searx/plugins/tor_check.py:68`
```python
real_ip = ip_address(address=str(request.remote_addr)).compressed
# 检查是否为 Tor 出口节点
```

**自信息插件**：`searx/plugins/self_info.py:51-54`
```python
if self.ip_regex.search(search.search_query.query) and request.remote_addr:
    results.add(
        results.types.Answer(answer=gettext("Your IP is: ") + ip_address(request.remote_addr).compressed)
    )
```

#### 5.2.3 网络转换辅助函数

`searx/botdetection/_helpers.py:56-77`：
```python
def get_network(real_ip: IPv4Address | IPv6Address, cfg: "config.Config") -> IPv4Network | IPv6Network:
    prefix: int = cfg["botdetection.ipv4_prefix"]  # 默认 32
    if real_ip.version == 6:
        prefix = cfg["botdetection.ipv6_prefix"]    # 默认 48
    return ip_network(f"{real_ip}/{prefix}", strict=False)
```

**配置说明**：
- `ipv4_prefix = 32`：每个 IPv4 地址作为独立网络
- `ipv6_prefix = 48`：同一 /48 网段的 IPv6 地址视为同一客户端
- 这种设计可以应对 IPv6 动态分配的场景

### 5.3 直接消费协议信息的模块

协议信息（`wsgi.url_scheme`）主要由 Flask 框架内部使用：
- 生成重定向 URL（`flask.redirect`、`url_for(_external=True)`）
- 构建响应中的绝对 URL
- 模板中的 `url_for` 函数

**没有业务模块直接读取 `wsgi.url_scheme`**，所有使用都通过 Flask 的抽象层（`request.scheme`、`url_for`）间接完成。

### 5.4 间接受益的模块

| 模块 | 受益方式 | 边界说明 |
|------|----------|----------|
| 模板渲染 | 通过 `url_for` 生成正确的绝对 URL | 不直接读取 remote_addr 或 scheme |
| 统计模块 (metrics) | 限流的准确执行确保统计数据反映真实客户端行为 | 不直接使用 remote_addr，依赖限流模块的正确工作 |
| 搜索引擎请求 | 真实客户端 IP 用于检测和绕过搜索引擎的反爬机制 | 通过限流模块间接保证，不直接读取 |
| 会话管理 | 正确的协议确保 Cookie 的 Secure 标志正确设置 | 通过 Flask 框架间接完成 |

### 5.5 共享机制的优势

1. **单点处理**：IP 和协议还原在请求链路最前端完成，下游模块无需重复处理
2. **一致性保证**：所有模块使用相同的 `remote_addr`，避免判定不一致
3. **性能优化**：只进行一次 IP 解析和验证
4. **可追溯性**：原始地址保存在 `botdetection.trusted_proxies.orig` 中
5. **安全边界**：未通过验证的头部会被提前丢弃，防止头部伪造攻击

---

## 6. 安全考虑

### 6.1 头部伪造风险

如果 `trusted_proxies` 配置不正确，攻击者可以通过伪造 `X-Forwarded-For` 头部绕过限流：

```
攻击者请求 → 直接连接应用（不经过代理）
X-Forwarded-For: 1.2.3.4, 5.6.7.8
REMOTE_ADDR: 攻击者真实IP
```

**防护措施**（已在代码中实现）：
- 必须正确配置 `trusted_proxies`，只包含真正的反向代理
- 没有 `trusted_proxies` 时，`X-Forwarded-For` 会被忽略（`trusted_proxies.py:144-148`）
- 反向代理应配置为**覆盖**而非追加 `X-Forwarded-For`

### 6.2 降级策略的安全性

当无法确定真实 IP 时，使用黑洞地址 `100::`：
- 该地址在 RFC 6666 中被定义为 discard 前缀，不会路由到任何真实主机
- 所有无法识别的客户端会被归到同一地址，可能影响限流准确性
- 但避免了使用无效值导致的程序崩溃

### 6.3 信任链的完整性

**重要安全边界**：
- ProxyFix 不验证 TCP 连接的 `REMOTE_ADDR` 是否在受信任列表中
- 这意味着如果请求绕过代理直接到达应用，且携带伪造的 `X-Forwarded-For`，同时 `trusted_proxies` 包含了攻击者的 IP，那么攻击者可以伪造客户端 IP
- 缓解措施：确保网络层面只允许受信任的反向代理访问应用端口

---

## 7. 关键文件索引

| 文件路径 | 功能 | 关键行号 |
|----------|------|----------|
| `searx/botdetection/trusted_proxies.py` | 受信任代理剥离与 IP 还原 | 66-86, 88-176 |
| `searx/botdetection/_helpers.py` | 网络计算与辅助函数 | 56-77 |
| `searx/flaskfix.py` | 协议与路径还原 | 11-69 |
| `searx/limiter.toml` | 受信任代理配置 | 13-20 |
| `searx/limiter.py` | 限流模块入口，核心消费者 | 147-209 |
| `searx/botdetection/ip_limit.py` | IP 限流实现 | 92-148 |
| `searx/botdetection/link_token.py` | link_token 方法 | 73-112 |
| `searx/plugins/tor_check.py` | Tor 检查插件 | 68 |
| `searx/plugins/self_info.py` | 自信息插件 | 51-54 |
| `searx/webapp.py` | 中间件注册 | 1394-1404 |

---

## 8. 总结

SearXNG 的受信任反向代理机制采用分层设计：

1. **配置层**：通过 `trusted_proxies` 明确信任边界，未配置则忽略 `X-Forwarded-For`
2. **中间件层**：在 WSGI 入口处集中处理 IP 和协议还原，采用"剥离而非前置验证"的算法
3. **应用层**：下游模块通过标准接口透明使用，限流、插件等模块直接消费处理后的结果

**关键设计决策**：
- 不做 TCP 层的前置信任验证，而是对 `X-Forwarded-For` 从右向左剥离受信任代理
- 严格的头部优先级和降级策略，确保在各种配置下都能正常工作
- 单点处理、多处受益的架构，保证一致性和可维护性

这种设计在安全性和易用性之间取得了平衡，既防止了未配置时的头部伪造风险，又在正确配置后能准确还原真实客户端信息。
