# 受信任反向代理判定与真实客户端地址还原技术报告

## 1. 概述

本报告基于 SearXNG 代码库的实际实现，从概念层面阐释如何判定一次请求是否来自受信任的反向代理，并在此基础上还原真实客户端地址与协议。报告覆盖配置项形态、请求头读取顺序，以及与限流和统计模块共享判定结果的机制。

核心实现位于 `searx/botdetection/trusted_proxies.py`，协议处理位于 `searx/flaskfix.py`。

---

## 2. 受信任反向代理判定机制

### 2.1 核心概念

当 Web 应用部署在反向代理（如 Nginx、CDN、负载均衡器）之后时，直接从 TCP 连接获取的 `REMOTE_ADDR` 实际上是代理服务器的地址，而非真实客户端地址。为了获取真实客户端地址，需要：

1. **信任判定**：确认请求确实来自预先配置的受信任代理
2. **头部解析**：从代理添加的 HTTP 头部中提取真实客户端信息

### 2.2 配置项形态

配置定义在 `searx/limiter.toml` 中：

```toml
[botdetection]
# 受信任的反向代理 IP 段列表
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

### 2.3 信任判定算法

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
2. 从右向左遍历，逐个检查 IP 是否在受信任网段内
3. 遇到第一个不在受信任列表中的 IP，即为真实客户端地址
4. 若所有 IP 都受信任（极端情况），则取最左侧地址

---

## 3. 真实客户端地址还原

### 3.1 请求头读取优先级

真实 IP 还原遵循严格的优先级顺序（`trusted_proxies.py:156-173`）：

| 优先级 | 头部名称 | 处理逻辑 | 依赖配置 |
|--------|----------|----------|----------|
| 1 | `X-Forwarded-For` | 从右向左遍历，跳过受信任代理 | 需要 `trusted_proxies` 配置 |
| 2 | `X-Real-IP` | 直接使用头部值 | 不依赖 `trusted_proxies` |
| 3 | `REMOTE_ADDR` | WSGI 环境变量（TCP 连接地址） | 无 |
| 4 | 降级方案 | 使用黑洞地址 `100::`（RFC 6666） | 无 |

### 3.2 完整处理流程

```
请求到达
   ↓
移除原有 REMOTE_ADDR，避免依赖未经处理的值
   ↓
验证并清洗各个头部的 IP 格式
   ├─ REMOTE_ADDR: 验证有效性，处理 IPv4 映射
   ├─ X-Real-IP: 验证有效性，无效则丢弃
   └─ X-Forwarded-For: 逐个验证，任意无效则丢弃整个头部
   ↓
按优先级确定真实 IP
   ├─ 有 X-Forwarded-For 且有 trusted_proxies → 信任判定算法
   ├─ 有 X-Forwarded-For 但无 trusted_proxies → 记录错误，丢弃头部
   ├─ 有 X-Real-IP → 直接使用
   ├─ 有原始 REMOTE_ADDR → 使用
   └─ 全部失败 → 使用 100::
   ↓
将结果写入 environ['REMOTE_ADDR']
```

### 3.3 IP 清洗与规范化

在 `trusted_proxies.py:97-137` 中实现了严格的 IP 验证：

1. **IPv4 映射处理**：IPv6 格式的 IPv4 地址（如 `::ffff:192.168.1.1`）转换为纯 IPv4 格式
2. **格式验证**：使用 `ip_address()` 验证每个 IP 的合法性
3. **错误处理**：无效 IP 会被丢弃并记录错误日志，避免污染后续处理
4. **幂等性**：原始 REMOTE_ADDR 保存在 `environ['botdetection.trusted_proxies.orig']` 中供追溯

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

## 5. 与限流和统计模块的共享机制

### 5.1 架构设计

```
WSGI 请求链
   ↓
ProxyFix 中间件 (IP 还原)
   ├─ 写入 environ['REMOTE_ADDR']
   └─ 写入 environ['botdetection.trusted_proxies.orig']
   ↓
WhiteNoise (静态文件)
   ↓
ReverseProxyPathFix 中间件 (协议还原)
   ├─ 写入 environ['wsgi.url_scheme']
   ├─ 写入 environ['SCRIPT_NAME']
   └─ 写入 environ['HTTP_HOST']
   ↓
Flask 应用
   ├─ flask.request.remote_addr → 读取处理后的 REMOTE_ADDR
   ├─ flask.request.scheme → 读取处理后的 wsgi.url_scheme
   ├─ 限流模块 (ip_limit.py)
   └─ 统计模块 (metrics)
```

### 5.2 限流模块集成

限流逻辑位于 `searx/botdetection/ip_limit.py`，通过以下方式获取真实客户端：

```python
def filter_request(
    network: IPv4Network | IPv6Network,
    request: flask.Request,
    cfg: config.Config,
) -> werkzeug.Response | None:
    # flask.request.remote_addr 已经是 ProxyFix 处理后的真实 IP
    valkey_client = valkeydb.get_valkey_client()
    
    # 使用 IP 网段进行计数（考虑 IPv4/IPv6 前缀配置）
    c = incr_sliding_window(valkey_client, 'ip_limit.BURST_WINDOW:' + network.compressed, BURST_WINDOW)
```

**IP 到网络的转换**（`_helpers.py:56-77`）：
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

### 5.3 共享机制的优势

1. **单点处理**：IP 还原在请求链路最前端完成，下游模块无需重复处理
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

**防护措施**：
- 必须正确配置 `trusted_proxies`，只包含真正的反向代理
- 没有 `trusted_proxies` 时，`X-Forwarded-For` 会被忽略（`trusted_proxies.py:144-148`）
- 反向代理应配置为覆盖而非追加 `X-Forwarded-For`

### 6.2 降级策略的安全性

当无法确定真实 IP 时，使用黑洞地址 `100::`：
- 该地址在 RFC 6666 中被定义为 discard 前缀，不会路由到任何真实主机
- 所有无法识别的客户端会被归到同一地址，可能影响限流准确性
- 但避免了使用无效值导致的程序崩溃

---

## 7. 中间件注册与执行顺序

在 `webapp.py:1394-1404` 中注册：

```python
# 1. 首先注册 ProxyFix（IP 还原）
app.wsgi_app = ProxyFix(app.wsgi_app)

# 2. 然后是 WhiteNoise（静态文件服务）
app.wsgi_app = WhiteNoise(app.wsgi_app, ...)

# 3. 最后注册 ReverseProxyPathFix（协议和路径还原）
patch_application(app)
```

**执行顺序**（从外到内）：
`ReverseProxyPathFix` → `WhiteNoise` → `ProxyFix` → Flask 应用

---

## 8. 关键文件索引

| 文件路径 | 功能 | 关键行号 |
|----------|------|----------|
| `searx/botdetection/trusted_proxies.py` | 受信任代理判定与 IP 还原 | 66-86, 88-176 |
| `searx/botdetection/_helpers.py` | 网络计算与辅助函数 | 56-77 |
| `searx/flaskfix.py` | 协议与路径还原 | 11-69 |
| `searx/limiter.toml` | 受信任代理配置 | 13-20 |
| `searx/botdetection/ip_limit.py` | 限流模块实现 | 92-148 |
| `searx/webapp.py` | 中间件注册 | 1394-1404 |

---

## 9. 总结

SearXNG 的受信任反向代理机制采用分层设计：
1. **配置层**：通过 `trusted_proxies` 明确信任边界
2. **中间件层**：在 WSGI 入口处集中处理 IP 和协议还原
3. **应用层**：下游模块（限流、统计）通过标准接口透明使用

这种设计确保了真实客户端信息的一致性和安全性，同时保持了代码的可维护性。
