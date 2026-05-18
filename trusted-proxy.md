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

### 4.4 协议信息的消费边界

**代码证据核查结论**：

1. **直接写入**：`ReverseProxyPathFix.__call__()` 在 `flaskfix.py:64` 直接写入 `environ['wsgi.url_scheme']`
2. **框架层间接读取**：Flask 框架内部通过 `request.scheme` 和 `request.is_secure` 属性读取该值
3. **无业务代码直接读取**：在整个 SearXNG 代码库中，没有任何业务模块直接读取 `environ['wsgi.url_scheme']`

**使用场景（全部通过 Flask 框架间接完成）**：
- `url_for(_external=True)` 生成绝对 URL（如 `webapp.py:872`, `webapp.py:877`, `webapp.py:1253`）
- `flask.redirect()` 处理重定向（如 `webapp.py:589`, `webapp.py:667`, `webapp.py:699`, `webapp.py:792`）
- `request.is_secure` 判断协议安全性（`http_sec_fetch.py:83`）

---

## 5. 中间件执行顺序（唯一统一口径）

### 5.1 注册顺序

在 `webapp.py:1394-1404` 中注册：

```python
# 1. 首先：ProxyFix 包装原始 Flask app.wsgi_app（最内层）
app.wsgi_app = ProxyFix(app.wsgi_app)

# 2. 然后：WhiteNoise 包装 ProxyFix（中间层）
app.wsgi_app = WhiteNoise(app.wsgi_app, ...)

# 3. 最后：ReverseProxyPathFix 包装 WhiteNoise（最外层）
patch_application(app)
```

### 5.2 WSGI 洋葱模型执行顺序

WSGI 中间件遵循**洋葱模型**，请求从外到内传递，响应从内到外返回：

```
                    ┌──────────────────────────┐
                    │   HTTP 请求进入 WSGI    │
                    └─────────────┬────────────┘
                                  │
                                  ▼
                    ┌──────────────────────────┐
                    │ ReverseProxyPathFix      │
                    │ （最外层，最先处理请求） │
                    │ - 设置 wsgi.url_scheme   │
                    │ - 设置 SCRIPT_NAME      │
                    │ - 设置 HTTP_HOST        │
                    └─────────────┬────────────┘
                                  │
                                  ▼
                    ┌──────────────────────────┐
                    │ WhiteNoise               │
                    │ - 静态文件服务           │
                    │ - 命中静态文件则直接返回 │
                    └─────────────┬────────────┘
                                  │
                                  ▼
                    ┌──────────────────────────┐
                    │ ProxyFix                 │
                    │ （最内层，最后处理请求） │
                    │ - 解析 X-Forwarded-For   │
                    │ - 剥离受信任代理         │
                    │ - 设置 REMOTE_ADDR       │
                    └─────────────┬────────────┘
                                  │
                                  ▼
                    ┌──────────────────────────┐
                    │ Flask 应用逻辑           │
                    │ - before_request 钩子    │
                    │ - 视图函数               │
                    │ - 模板渲染               │
                    └─────────────┬────────────┘
                                  │
                                  ▼
                    ┌──────────────────────────┐
                    │   响应按相反路径返回     │
                    └──────────────────────────┘
```

**统一口径**：
- 请求处理顺序（从外到内）：`ReverseProxyPathFix` → `WhiteNoise` → `ProxyFix` → Flask 应用
- **ProxyFix 是最内层中间件，最后一个处理请求后才到达 Flask 应用**
- 因此 Flask 应用内通过 `request.remote_addr` 读取到的已经是 ProxyFix 处理后的真实客户端 IP

---

## 6. 与限流、统计及其他模块的共享机制

### 6.1 整体数据流

```
WSGI 请求
   ↓
ReverseProxyPathFix: 设置 wsgi.url_scheme, SCRIPT_NAME, HTTP_HOST
   ↓
WhiteNoise: 处理静态文件
   ↓
ProxyFix: 设置 REMOTE_ADDR（真实客户端 IP）
   ↓
Flask 应用
   ├─ before_request 钩子（limiter.pre_request）
   │   └─ filter_request(sxng_request)  [limiter.py:147]
   │       ├─ real_ip = ip_address(request.remote_addr)  [直接消费]
   │       ├─ network = get_network(real_ip, cfg)
   │       ├─ ip_lists.pass_ip/block_ip(real_ip, cfg)
   │       └─ ip_limit.filter_request(network, request, cfg)
   │           └─ [如果 botdetection.ip_limit.link_token = true]
   │               └─ link_token.is_suspicious(network, request, True)
   ├─ 视图函数
   │   ├─ /client<token>.css → client_token() [webapp.py:605]
   │   │   └─ link_token.ping(sxng_request, token)  [直接读取 remote_addr]
   │   └─ 插件系统（post_search 钩子）
   │       ├─ tor_check.py: request.remote_addr  [直接消费]
   │       └─ self_info.py: request.remote_addr  [直接消费]
   └─ 模板渲染
       └─ url_for() → 通过 Flask 框架间接使用 wsgi.url_scheme
```

### 6.2 直接消费 REMOTE_ADDR 的模块（完整代码证据链）

#### 6.2.1 限流模块（核心消费者）

**入口**：`searx/limiter.py:147-209`
```python
def filter_request(request: SXNG_Request) -> werkzeug.Response | None:
    cfg = get_cfg()
    real_ip = ip_address(request.remote_addr)  # [1] 直接读取处理后的 IP
    network = get_network(real_ip, cfg)       # [2] 转换为网络段
    
    # ... 传递给 botdetection 子模块
    match, msg = ip_lists.pass_ip(real_ip, cfg)    # 直接传递 real_ip
    match, msg = ip_lists.block_ip(real_ip, cfg)   # 直接传递 real_ip
    val = ip_limit.filter_request(network, request, cfg)  # 传递 network
```

**重要修正**：limiter 主流程**不会直接调用** `link_token.is_suspicious`，而是通过 `ip_limit.filter_request` 分支触发。

**ip_limit 内部调用 link_token**（`ip_limit.py:110-112`）：
```python
if cfg['botdetection.ip_limit.link_token']:
    suspicious = link_token.is_suspicious(network, request, True)
```

**子模块消费证据**：
- `searx/botdetection/ip_limit.py:92-148`：基于 `network` 参数进行滑动窗口限流计数
- `searx/botdetection/ip_lists.py`：接收 `real_ip` 参数进行黑白名单匹配
- `searx/botdetection/link_token.py:73-90`：`is_suspicious()` 接收 `network` 参数进行判定
- 所有 header probe 模块：接收 `network` 参数，基于准确的客户端识别进行请求计数

#### 6.2.2 client_token 到 link_token.ping 链路

**路由入口**：`webapp.py:605-608`
```python
@app.route('/client<token>.css', methods=['GET', 'POST'])
def client_token(token=None):
    link_token.ping(sxng_request, token)
    return Response('', mimetype='text/css', headers={"Cache-Control": "no-store, max-age=0"})
```

**ping 函数直接读取 remote_addr**：`link_token.py:93-112`
```python
def ping(request: flask.Request, token: str):
    valkey_client = valkeydb.get_valkey_client()
    cfg = config.get_global_cfg()

    if not token_is_valid(token):
        return

    real_ip = ip_address(request.remote_addr)  # [直接读取]
    network = get_network(real_ip, cfg)

    ping_key = get_ping_key(network, request)
    logger.debug(
        "store ping_key for (client) network %s (IP %s) -> %s", network.compressed, real_ip.compressed, ping_key
    )
    valkey_client.set(ping_key, 1, ex=PING_LIVE_TIME)
```

**链路说明**：
- 浏览器加载页面时，会请求 `/client<token>.css` 这个样式表
- 该请求触发 `client_token` 视图函数
- 函数调用 `link_token.ping()`，后者直接读取 `request.remote_addr`
- 将该客户端网络标记为"已验证"，降低其限流阈值

#### 6.2.3 插件系统

**Tor 检查插件**：`searx/plugins/tor_check.py:68`
```python
real_ip = ip_address(address=str(request.remote_addr)).compressed
# 检查 real_ip 是否在 Tor 出口节点列表中
```

**自信息插件**：`searx/plugins/self_info.py:51-54`
```python
if self.ip_regex.search(search.search_query.query) and request.remote_addr:
    results.add(
        results.types.Answer(answer=gettext("Your IP is: ") + ip_address(request.remote_addr).compressed)
    )
```

#### 6.2.4 网络转换辅助函数

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

### 6.3 协议信息的消费边界

| 消费方式 | 代码证据 | 说明 |
|----------|----------|------|
| 直接写入 | `flaskfix.py:64` | `environ['wsgi.url_scheme'] = scheme` |
| 框架间接读取 | Flask 内部 | `request.scheme` 和 `request.is_secure` 属性读取 `environ['wsgi.url_scheme']` |
| 业务代码直接读取 | **无** | 代码库中未发现业务模块直接读取 `environ['wsgi.url_scheme']` |
| 业务代码间接使用 | `webapp.py:872`、`webapp.py:877`、`webapp.py:1253` | 通过 `url_for(_external=True)` 生成绝对 URL |
| 业务代码间接使用 | `webapp.py:589`、`webapp.py:667`、`webapp.py:699`、`webapp.py:792` | 通过 `flask.redirect()` 处理重定向 |
| 业务代码间接使用 | `http_sec_fetch.py:83` | 通过 `request.is_secure` 判断协议安全性 |

### 6.4 统计模块的受益边界（可核对依据）

**代码核查结论**：统计模块（metrics）**不直接消费** `REMOTE_ADDR` 或协议信息。

**证据依据**：
1. 统计模块核心代码位于 `searx/metrics/`，统计维度包括：
   - 引擎响应时间分布（histogram）：`searx/metrics/__init__.py`
   - 搜索成功/失败计数（counter）：`searx/search/processors/abstract.py:181, 205`
   - 引擎错误统计：`searx/metrics/error_recorder.py`
2. 上述代码中均未出现 `remote_addr`、`REMOTE_ADDR`、`wsgi.url_scheme` 或 `scheme` 的读取
3. `/stats` 和 `/metrics` 接口（`webapp.py:1100-1184`）仅聚合引擎级别的统计数据，不涉及客户端 IP

**间接受益的边界说明**：
- 统计模块本身不依赖 IP 还原结果
- 只有在以下二阶效应下才可能间接受益：
  ```
  IP 还原错误 → 限流模块错误封禁/放行 → 搜索引擎请求模式改变 → 统计数据分布变化
  ```
- 这是逻辑推断，**没有直接代码链路**可以证明统计模块的准确性依赖于 IP 还原
- 因此，统计模块与反向代理处理机制的关联是松散的、非直接的

### 6.5 间接受益的模块汇总

| 模块 | 受益方式 | 证据强度 | 边界说明 |
|------|----------|----------|----------|
| 模板渲染 | 通过 `url_for` 生成正确的绝对 URL | 强 | 不直接读取 remote_addr 或 scheme，依赖 Flask 框架 |
| 搜索引擎反爬 | 真实客户端 IP 用于检测和绕过搜索引擎的反爬机制 | 弱 | 通过限流模块间接保证，无直接读取链路 |
| 统计模块 (metrics) | 限流的准确执行确保统计数据反映真实客户端行为 | 极弱 | 二阶效应，无直接代码证据 |

---

## 7. 安全边界与风险分析

### 7.1 客户端 IP 伪造风险的前提链（可复核）

**"攻击者可伪造客户端 IP"这一结论仅在以下条件**全部同时成立**时才成立：**

| 序号 | 前提条件 | 代码/配置位置 | 验证方式 |
|------|----------|--------------|----------|
| 1 | `trusted_proxies` 配置**非空** | `searx/limiter.toml` 中 `[botdetection] trusted_proxies` 列表 | 检查配置文件，空列表则此前提不成立 |
| 2 | `trusted_proxies` 配置**包含攻击者的 IP 段** | `trusted_proxies.py:91` 读取配置，`trusted_proxies.py:61-67` 进行网段匹配 | 审计 trusted_proxies 列表，确保只包含真正的反向代理 |
| 3 | 攻击者能够**绕过反向代理直接连接应用端口** | 网络层面配置，代码层无检测逻辑 | 检查防火墙/安全组规则，确认应用端口是否只对反向代理开放 |
| 4 | 攻击者能够在请求中**设置 `X-Forwarded-For` 头部** | `trusted_proxies.py:125-137` 解析该头部 | 验证反向代理是否配置为**覆盖**（而非追加）X-Forwarded-For |

**攻击成立的完整链路**：
```
攻击者 IP: 192.168.1.100（假设在 trusted_proxies 中）
   ↓
直接连接应用端口（绕过反向代理）
   ↓
伪造请求头: X-Forwarded-For: 1.2.3.4, 5.6.7.8
   ↓
ProxyFix 处理:
  1. 从右向左遍历: 5.6.7.8 → 检查是否在 trusted_proxies
     → 假设不在 → 返回 5.6.7.8 作为客户端 IP
  2. 攻击者成功伪造客户端 IP 为 5.6.7.8
```

---

### 7.2 风险不成立的反例边界

以下场景中，**即使攻击者能够发送请求，也无法伪造客户端 IP**：

#### 反例 1：trusted_proxies 为空或未配置
- **代码证据**：`trusted_proxies.py:144-148`
  ```python
  if x_forwarded_for and not trusted_proxies:
      log_error_only_once("missing botdetection.trusted_proxies config")
      x_forwarded_for = []  # 丢弃整个头部
  ```
- **结果**：X-Forwarded-For 被完全忽略，回退到 X-Real-IP 或 REMOTE_ADDR（攻击者真实 IP）

#### 反例 2：攻击者 IP 不在 trusted_proxies 中
- **代码证据**：`trusted_proxies.py:66-86` 信任剥离算法
- **场景**：
  ```
  攻击者 IP: 192.168.1.100（不在 trusted_proxies）
  X-Forwarded-For: 1.2.3.4, 192.168.1.100
  ```
- **处理过程**：
  1. 从右向左遍历：192.168.1.100 → 不在 trusted_proxies
  2. 返回 192.168.1.100 作为客户端 IP
- **结果**：攻击者无法伪造，客户端 IP 就是攻击者真实 IP

#### 反例 3：请求经过反向代理且代理配置为覆盖 X-Forwarded-For
- **场景**：Nginx 配置 `proxy_set_header X-Forwarded-For $remote_addr;`（覆盖而非追加）
- **结果**：攻击者发送的伪造头部被反向代理覆盖，应用只能看到代理设置的真实客户端 IP

#### 反例 4：网络层面阻止直接连接
- **场景**：防火墙/安全组只允许反向代理 IP 访问应用端口
- **结果**：攻击者根本无法建立 TCP 连接，攻击无从谈起

#### 反例 5：使用 X-Real-IP 而非 X-Forwarded-For
- **代码证据**：`trusted_proxies.py:159-160`
  ```python
  elif x_real_ip:
      environ["REMOTE_ADDR"] = x_real_ip
  ```
- **说明**：X-Real-IP 直接使用头部值，不经过 trusted_proxies 剥离逻辑
- **注意**：这并不意味着更安全，只是攻击面不同——攻击者需要伪造 X-Real-IP 头部

---

### 7.3 降级策略的安全性

当无法确定真实 IP 时，使用黑洞地址 `100::`（`trusted_proxies.py:166-167`）：
- 该地址在 RFC 6666 中被定义为 discard 前缀，不会路由到任何真实主机
- 所有无法识别的客户端会被归到同一地址，可能影响限流准确性
- 但避免了使用无效值导致的程序崩溃
- **边界**：这是最终降级手段，仅在所有头部都无效或缺失时触发

---

### 7.4 协议安全性

代码证据（`http_sec_fetch.py:83-87`）：
```python
if not request.is_secure:
    logger.warning(
        "Sec-Fetch cannot be verified for non-secure requests (HTTP headers are not set/sent by the client)."
    )
    return None
```

**说明**：
- `request.is_secure` 依赖于 `wsgi.url_scheme` 的正确设置（由 `ReverseProxyPathFix` 中间件写入）
- 非 HTTPS 环境下，Sec-Fetch 头部验证会被跳过
- 这是合理的安全降级，因为 HTTP 环境下这些头部本身就不可靠
- **边界**：仅影响 Sec-Fetch 头部验证，不影响 IP 还原逻辑

---

### 7.5 安全配置最佳实践

基于上述边界分析，推荐的安全配置：

1. **正确配置 trusted_proxies**：
   - 只包含真正的反向代理 IP 段
   - 定期审计，避免遗留无效条目
   - 配置入口：`searx/limiter.toml`

2. **网络层隔离**：
   - 防火墙只允许反向代理访问应用端口
   - 应用不直接暴露给公网

3. **反向代理配置**：
   - 使用 `proxy_set_header X-Forwarded-For $remote_addr;` 覆盖而非追加
   - 确保代理不会转发客户端伪造的 X-Forwarded-For

4. **监控告警**：
   - 关注 `missing botdetection.trusted_proxies config` 错误日志
   - 异常 IP 模式变化时及时排查

---

## 8. 关键文件索引

| 文件路径 | 功能 | 关键行号 |
|----------|------|----------|
| `searx/botdetection/trusted_proxies.py` | 受信任代理剥离与 IP 还原 | 66-86, 88-176 |
| `searx/botdetection/_helpers.py` | 网络计算与辅助函数 | 56-77 |
| `searx/flaskfix.py` | 协议与路径还原 | 11-69 |
| `searx/limiter.toml` | 受信任代理配置 | 13-20 |
| `searx/limiter.py` | 限流模块入口，核心消费者 | 147-209 |
| `searx/botdetection/ip_limit.py` | IP 限流实现，内部调用 link_token | 92-148 |
| `searx/botdetection/link_token.py` | link_token ping 和 is_suspicious 方法 | 73-112 |
| `searx/webapp.py` | client_token 路由入口 | 605-608 |
| `searx/plugins/tor_check.py` | Tor 检查插件 | 68 |
| `searx/plugins/self_info.py` | 自信息插件 | 51-54 |
| `searx/botdetection/http_sec_fetch.py` | Sec-Fetch 头部验证，使用 request.is_secure | 83-87 |
| `searx/webapp.py` | 中间件注册 | 1394-1404 |

---

## 9. 总结

SearXNG 的受信任反向代理机制采用分层设计：

1. **配置层**：通过 `trusted_proxies` 明确信任边界，未配置则忽略 `X-Forwarded-For`
2. **中间件层**：在 WSGI 入口处集中处理 IP 和协议还原，采用"剥离而非前置验证"的算法
3. **应用层**：下游模块通过标准接口透明使用，限流、插件等模块直接消费处理后的结果

**关键设计决策**：
- 不做 TCP 层的前置信任验证，而是对 `X-Forwarded-For` 从右向左剥离受信任代理
- 严格的头部优先级和降级策略，确保在各种配置下都能正常工作
- 单点处理、多处受益的架构，保证一致性和可维护性
- WSGI 中间件按 `ReverseProxyPathFix` → `WhiteNoise` → `ProxyFix` 的顺序执行，ProxyFix 作为最内层中间件直接为 Flask 应用提供处理后的 REMOTE_ADDR

**调用链修正**：
- limiter 主流程不会直接调用 `link_token.is_suspicious`，而是通过 `ip_limit.filter_request` 内部分支触发
- 新增 `client_token` → `link_token.ping` 链路，这是直接读取 `remote_addr` 的重要消费者

**边界澄清**：
- 统计模块（metrics）不直接消费 IP 还原结果，间接受益的说法缺乏直接代码证据
- 协议信息全部通过 Flask 框架间接使用，无业务代码直接读取 `wsgi.url_scheme`
- 直接消费者包括：限流模块（limiter + ip_limit + ip_lists + link_token）、client_token 路由、tor_check 插件、self_info 插件
- Cookie Secure 相关表述无代码证据支持，已移除；协议安全性的实际使用体现在 `request.is_secure` 判断

这种设计在安全性和易用性之间取得了平衡，既防止了未配置时的头部伪造风险，又在正确配置后能准确还原真实客户端信息。
