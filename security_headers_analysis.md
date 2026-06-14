# SearXNG 安全响应头装配机制分析

## 一、安全响应头统一注入的两个环节

SearXNG 的安全响应头通过**两条独立路径**分别注入到动态响应和静态资源响应中，两者共享同一份配置来源，但挂载在不同的中间件层。

### 1.1 动态响应：Flask `after_request` 钩子

**注入点**：[webapp.py#L521-L528](file:///d:/fz/0601-1/solo-dogfeeding/code/77-searxng/searx/webapp.py#L521-L528)

```python
@app.after_request
def add_default_headers(response: flask.Response):
    # set default http headers
    for header, value in settings['server']['default_http_headers'].items():
        if header in response.headers:
            continue
        response.headers[header] = value
    return response
```

**逐段解析**：

- **`@app.after_request` 装饰器**：将函数注册为 Flask 的请求后处理器。每个请求处理完成后、响应返回客户端前，Flask 会按注册顺序依次调用所有 `after_request` 钩子。
- **遍历配置字典**：从 `settings['server']['default_http_headers']` 读取管理员配置的所有 HTTP 头，这是安全头部的唯一数据源。
- **"不覆盖已有头"原则**：`if header in response.headers: continue` 意味着如果视图函数已经显式设置了同名头部，`after_request` 钩子不会覆盖它。这保证了特定路由可以按需定制安全策略而不被全局配置强制覆盖。
- **写入响应头**：通过 `response.headers[header] = value` 将安全头添加到响应对象。

**适用范围**：所有经 Flask 路由处理的动态响应，包括 HTML 页面、JSON API、RSS 输出、图片代理响应等。

### 1.2 静态资源：WhiteNoise 中间件

**注入点**：[webapp.py#L1386-L1402](file:///d:/fz/0601-1/solo-dogfeeding/code/77-searxng/searx/webapp.py#L1386-L1402)

```python
def static_headers(headers: Headers, _path: str, _url: str) -> None:
    headers['Cache-Control'] = 'public, max-age=30, stale-while-revalidate=60'

    for header, value in settings['server']['default_http_headers'].items():
        # cast value to string, as WhiteNoise requires header values to be strings
        headers[header] = str(value)


app.wsgi_app = ProxyFix(app.wsgi_app)
app.wsgi_app = WhiteNoise(
    app.wsgi_app,
    root=settings['ui']['static_path'],
    prefix="static",
    max_age=None,
    allow_all_origins=False,
    add_headers_function=static_headers,
)
```

**逐段解析**：

- **WhiteNoise 中间件**：通过 `app.wsgi_app = WhiteNoise(...)` 包装 Flask 的 WSGI 应用，使静态文件直接由 WhiteNoise 高效提供，不经过 Flask 路由层。
- **`add_headers_function=static_headers`**：WhiteNoise 在返回每个静态文件响应前，会调用此函数来附加自定义头部。这是静态资源安全头的注入入口。
- **缓存控制头**：首先设置 `Cache-Control`，控制静态资源的浏览器缓存策略。
- **复用同一配置**：与动态响应共享 `settings['server']['default_http_headers']` 配置，保证静态资源和动态页面的安全策略一致。
- **强制字符串转换**：`str(value)` 是因为 WhiteNoise 的 `Headers` 对象要求值必须为字符串类型，而配置中可能包含非字符串值。
- **直接覆盖**：与 `add_default_headers` 不同，`static_headers` 直接赋值（`headers[header] = str(value)`），不检查是否已存在，说明 WhiteNoise 层默认不会预先设置这些安全头。

**适用范围**：所有 `/static/` 前缀下的静态资源，包括 JS、CSS、图片、字体文件等。

### 1.3 WSGI 前置层：自实现 `ProxyFix` —— 真实客户端 IP 决策

**中间件位置**：[trusted_proxies.py#L23-L176](file:///d:/fz/0601-1/solo-dogfeeding/code/77-searxng/searx/botdetection/trusted_proxies.py#L23-L176)

**挂载点**：[webapp.py#L1394](file:///d:/fz/0601-1/solo-dogfeeding/code/77-searxng/searx/webapp.py#L1394)

```python
# 中间件嵌套顺序（洋葱模型，从外到内执行）
app.wsgi_app = ProxyFix(app.wsgi_app)     # 最外层：先于 WhiteNoise 执行
app.wsgi_app = WhiteNoise(app.wsgi_app, ...)
```

这不是 Werkzeug 自带的简单 ProxyFix（按固定 hop 数挑 IP），而是 **SearXNG 自实现的可信代理网段感知版**。整个限流、IP 黑白名单、link_token 的安全决策全部建立在它输出的 `REMOTE_ADDR` 之上，是所有请求安全决策的根基。

#### 1.3.1 核心算法：从右向左反向扫描 X-Forwarded-For

**关键方法**：`trusted_remote_addr` — [trusted_proxies.py#L66-L86](file:///d:/fz/0601-1/solo-dogfeeding/code/77-searxng/searx/botdetection/trusted_proxies.py#L66-L86)

```python
def trusted_remote_addr(
    self,
    x_forwarded_for: list[IPv4Address | IPv6Address],
    trusted_proxies: list[IPv4Network | IPv6Network],
) -> str:
    # always rtl
    for addr in reversed(x_forwarded_for):
        trust: bool = False
        for net in trusted_proxies:
            if addr.version == net.version and addr in net:
                trust = True
                break
        # client address
        if not trust:
            return addr.compressed
    # fallback to first address
    return x_forwarded_for[0].compressed
```

**逐段解析**：

- **`# always rtl`（从右向左扫描）**：`X-Forwarded-For` 的格式是 `client_ip, proxy1_ip, proxy2_ip, ...`，每个代理追加自己的前一跳 IP 到右侧。最右侧是**直接连接 SearXNG 的那一跳**（即离服务器最近的代理）。从右向左扫描可以按信任边界逐级剥离。
- **双层循环验证归属**：外层遍历 X-Forwarded-For 列表（从右向左），内层遍历 `botdetection.trusted_proxies` 配置的所有可信网段，判断当前 IP 是否属于任一可信代理网络。
- **第一个不可信 IP 即真实客户端**：找到第一个不在可信网段内的 IP，就认定它是真实客户端地址并返回。这是反 IP 伪造的关键 —— 攻击者可以随意在 X-Forwarded-For 左侧追加伪造 IP，但伪造 IP 后面一定会跟着某个可信代理的 IP（因为流量确实经过了反向代理），所以从右向左扫时，伪造 IP 永远出现在第一个不可信 IP 之后（左侧），不会被选中。
- **fallback 兜底**：如果所有 IP 都在可信网段内（极端情况），回退到列表最左侧（理论上的最原始客户端）。

#### 1.3.2 IP 验证与回退优先级链

**`__call__` 方法完整处理流程**：[trusted_proxies.py#L88-L176](file:///d:/fz/0601-1/solo-dogfeeding/code/77-searxng/searx/botdetection/trusted_proxies.py#L88-L176)

```
步骤 1：environ.pop("REMOTE_ADDR")  → 先清空 WSGI 原始 REMOTE_ADDR，不信任上游
步骤 2：验证原始 REMOTE_ADDR         → 用 ip_address() 校验合法性，非合法值丢弃
步骤 3：验证 X-Real-IP                → 同上，非法值从 environ 中彻底删除
步骤 4：验证 X-Forwarded-For 列表    → 每个 IP 逐一校验，一个非法则整条头丢弃
步骤 5：完整性检查
   ├─ 无 X-Forwarded-For 且无 X-Real-IP → 打告警（但不拦截，继续用 REMOTE_ADDR）
   └─ 有 X-Forwarded-For 但 trusted_proxies 为空 → 打告警 + 丢弃 X-Forwarded-For
步骤 6：按优先级确定最终 REMOTE_ADDR
   ├─ 有 X-Forwarded-For + 有 trusted_proxies → 调用 trusted_remote_addr() 反向扫描
   ├─ 否则有 X-Real-IP                      → 直接用 X-Real-IP
   ├─ 否则原始 REMOTE_ADDR 合法              → 用原始 REMOTE_ADDR
   └─ 否则                                   → 用黑洞地址 "100::" (RFC6666)
步骤 7：最终再做一次 ip_address() 校验，失败则再回退到 100::
```

**关键安全语义**：

- **"不信任"原则**：第一步直接 `environ.pop("REMOTE_ADDR")`，意味着完全不信任 WSGI 层传入的原始连接 IP（可能来自负载均衡或容器网络的 SNAT），必须通过可信代理链路重新推导。
- **IP 格式硬校验**：所有输入（`REMOTE_ADDR`、`X-Real-IP`、`X-Forwarded-For` 中的每个值）都经过 `ip_address()` 解析，IPv4 映射的 IPv6 地址（如 `::ffff:192.168.1.1`）会被归一化到 IPv4。非法值被就地丢弃，**避免 IP 格式伪造绕过黑名单**。
- **X-Forwarded-For 依赖 trusted_proxies**：如果管理员忘了配 `botdetection.trusted_proxies`，即使收到 X-Forwarded-For 也直接清空（`x_forwarded_for = []`），防止攻击者直接伪造 `X-Forwarded-For: <合法白名单IP>` 绕过限流。
- **黑洞地址兜底**：`100::` 是 RFC 6666 规定的 Discard-Only 地址段，保证即使所有推导路径失败，`request.remote_addr` 也不会是 `None` 或空字符串，避免下游 `ip_address(None)` 崩溃。

#### 1.3.3 所有安全决策都消费 ProxyFix 输出的 `request.remote_addr`

ProxyFix 作为最外层 WSGI 中间件最先执行，重写后的 `REMOTE_ADDR` 对整个 Flask 应用透明。以下模块的安全决策全部基于它：

| 消费模块 | 代码位置 | 如何使用 `request.remote_addr` |
|----------|----------|-------------------------------|
| **限流核心 filter_request** | [limiter.py#L147-L209](file:///d:/fz/0601-1/solo-dogfeeding/code/77-searxng/searx/limiter.py#L147-L209) | `real_ip = ip_address(request.remote_addr)` → 计算 network → 过 pass-list/block-list → 传参给 http_user_agent、ip_limit 等过滤器 |
| **IP 白名单** | [ip_lists.py#L49-L59](file:///d:/fz/0601-1/solo-dogfeeding/code/77-searxng/searx/botdetection/ip_lists.py#L49-L59) | `pass_ip(real_ip, cfg)` → 逐一比对 `botdetection.ip_lists.pass_ip` 中的网段 |
| **IP 黑名单** | [ip_lists.py#L62-L69](file:///d:/fz/0601-1/solo-dogfeeding/code/77-searxng/searx/botdetection/ip_lists.py#L62-L69) | `block_ip(real_ip, cfg)` → 逐一比对 `botdetection.ip_lists.block_ip` 中的网段，命中直接 429 |
| **IP 请求速率限制** | [ip_limit.py#L124-L127](file:///d:/fz/0601-1/solo-dogfeeding/code/77-searxng/searx/botdetection/ip_limit.py#L124-L127) | 基于 `network`（由 real_ip 换算）统计 SUSPICIOUS_IP_WINDOW 内的请求数，超阈值则 302 跳转 |
| **客户端 link_token** | [link_token.py#L105-L110](file:///d:/fz/0601-1/solo-dogfeeding/code/77-searxng/searx/botdetection/link_token.py#L105-L110) | `real_ip = ip_address(request.remote_addr)` → 计算 network → 以此为维度记录和校验客户端 token |
| **Tor 出口节点检查** | [tor_check.py#L68-L76](file:///d:/fz/0601-1/solo-dogfeeding/code/77-searxng/searx/plugins/tor_check.py#L68-L76) | 用 remote_addr 比对 Tor 出口节点列表，在结果页提示用户 |

**因果链总结**：

```
botdetection.trusted_proxies 配置
        │
        ▼
ProxyFix.__call__()  ─── 验证 + 清洗 + 反向扫描 X-Forwarded-For
        │
        ▼
environ["REMOTE_ADDR"] (重写)
        │
        ├─► flask.request.remote_addr (全局透明)
        │
        ├─► limiter.filter_request() ─┐
        ├─► ip_lists.pass_ip()        │
        ├─► ip_lists.block_ip()       ├── 所有限流/黑白名单决策
        ├─► ip_limit (速率计数)       │
        └─► link_token (浏览器验证)   ┘
```

> **风险提示**：如果 `botdetection.trusted_proxies` 配置错误（例如漏了外层 CDN 的 IP 段），ProxyFix 会把 CDN 节点 IP 误认为客户端 IP。后果是：所有来自该 CDN 的用户共享同一个限流计数，且 CDN 节点的一次误封会阻断所有真实用户的访问。

---

## 二、配置来源：`default_http_headers`

**默认配置**：[settings.yml#L115-L119](file:///d:/fz/0601-1/solo-dogfeeding/code/77-searxng/searx/settings.yml#L115-L119)

```yaml
server:
  default_http_headers:
    X-Content-Type-Options: nosniff
    X-Download-Options: noopen
    X-Robots-Tag: noindex, nofollow
    Referrer-Policy: no-referrer
```

### 各安全头作用解析

| 响应头 | 值 | 安全作用 |
|--------|----|----------|
| **X-Content-Type-Options** | `nosniff` | 禁止浏览器进行 MIME 类型嗅探，强制浏览器严格按照 `Content-Type` 字段解析资源，防止基于 MIME 混淆的攻击（如将文本文件当作脚本执行）。 |
| **X-Download-Options** | `noopen` | IE8+ 专用，禁止用户直接打开下载的文件，必须先保存，减少恶意文件通过浏览器自动打开执行的风险。 |
| **X-Robots-Tag** | `noindex, nofollow` | 阻止搜索引擎索引页面内容和跟踪链接，保护搜索实例不被搜索引擎收录。 |
| **Referrer-Policy** | `no-referrer` | 不发送 `Referer` 头，最大程度保护用户隐私，防止目标网站通过 Referer 获知用户的搜索关键词和来源页面。 |

> **注意**：默认配置中**不包含** `Content-Security-Policy`、`Strict-Transport-Security`、`X-Frame-Options`、`Permissions-Policy` 等常见安全头。这些需要管理员根据自身部署需求手动添加到 `default_http_headers` 配置中。

---

## 三、视图层旁路端点与统一注入的共存机制

虽然 `add_default_headers` 的 `after_request` 钩子理论上覆盖所有动态响应，但代码中存在多个**视图层直接设置响应头**的旁路端点。这些端点与统一注入路径的共存依赖于"不覆盖已有头"的原则。

### 3.1 限流探针端点：`/client<token>.css`

**代码位置**：[webapp.py#L605-L608](file:///d:/fz/0601-1/solo-dogfeeding/code/77-searxng/searx/webapp.py#L605-L608)

```python
@app.route('/client<token>.css', methods=['GET', 'POST'])
def client_token(token=None):
    link_token.ping(sxng_request, token)
    return Response('', mimetype='text/css', headers={"Cache-Control": "no-store, max-age=0"})
```

**逐段解析**：

- **旁路性质**：这是 botdetection 限流模块的客户端探针端点。通过返回一个空的 CSS 文件，配合 `link_token` 机制验证真实浏览器访问。
- **直接设置 `Cache-Control`**：在构造 `Response` 对象时通过 `headers` 参数直接传入 `Cache-Control: no-store, max-age=0`。
- **与统一注入的共存**：由于响应在构造时已经包含了 `Cache-Control` 头，`add_default_headers` 钩子会跳过它（不覆盖已有头原则）。而 `default_http_headers` 中其他安全头（如 `X-Content-Type-Options`、`Referrer-Policy` 等）因为不在响应头中，仍然会被正常注入。
- **为什么需要 `no-store`**：客户端 token 是动态生成的，每个请求的 token 都不同，必须禁止浏览器缓存，否则探针机制失效。

### 3.2 Favicon 代理端点：`/favicon_proxy`

**代码位置**：[proxy.py#L112-L156](file:///d:/fz/0601-1/solo-dogfeeding/code/77-searxng/searx/favicons/proxy.py#L112-L156)

```python
def favicon_proxy():
    # ... HMAC 校验、resolver 选择 ...
    
    data, mime = search_favicon(resolver, authority)

    if data is not None and mime is not None:
        resp = flask.Response(data, mimetype=mime)
        resp.headers['Cache-Control'] = f"max-age={CFG.max_age}"
        return resp

    # return default favicon from static path
    theme = sxng_request.preferences.get_value("theme")
    fav, mimetype = CFG.favicon(theme=theme)
    return flask.send_from_directory(fav.parent, fav.name, mimetype=mimetype)
```

**逐段解析**：

- **旁路性质**：favicon 代理端点负责根据域名获取网站 favicon，支持多种 resolver（allesedv、duckduckgo、google、yandex 等）。
- **成功分支直接设置 `Cache-Control`**：当从 resolver 成功获取到 favicon 时，通过 `resp.headers['Cache-Control'] = f"max-age={CFG.max_age}"` 显式设置缓存策略。
- **失败分支使用 `send_from_directory`**：当获取失败时，返回默认的空 favicon（`empty_favicon.svg`），使用 Flask 内置的 `send_from_directory`，该函数不会预先设置安全头。
- **与统一注入的共存**：成功分支中 `Cache-Control` 已被设置，`add_default_headers` 不会覆盖；其他安全头正常注入。失败分支的所有安全头都由 `after_request` 钩子注入。

### 3.3 图片代理端点：`/image_proxy` — 双向白名单防开放代理元数据泄漏

**代码位置**：[webapp.py#L1002-L1071](file:///d:/fz/0601-1/solo-dogfeeding/code/77-searxng/searx/webapp.py#L1002-L1071)

```python
@app.route('/image_proxy', methods=['GET'])
def image_proxy():
    # pylint: disable=too-many-return-statements, too-many-branches

    url = sxng_request.args.get('url')
    if not url:
        return '', 400

    if not is_hmac_of(settings['server']['secret_key'], url.encode(), sxng_request.args.get('h', '')):
        return '', 400

    maximum_size = 5 * 1024 * 1024
    forward_resp = False
    resp = None
    try:
        # ============================================================
        # 【上游请求侧白名单】仅发出 4 个硬编码头，剥离一切客户端来源头
        # ============================================================
        request_headers = {
            'User-Agent': gen_useragent(),
            'Accept': 'image/webp,*/*',
            'Sec-GPC': '1',
            'DNT': '1',
        }
        set_context_network_name('image_proxy')
        resp, stream = http_stream(method='GET', url=url, headers=request_headers, allow_redirects=True)

        # ... 中间省略 Content-Length / status_code / Content-Type 校验 ...

        forward_resp = True
    except httpx.HTTPError:
        ...

    try:
        # ============================================================
        # 【上游响应侧白名单】只回传 4 个内容类头，剥光一切上游元数据头
        # ============================================================
        headers = dict_subset(resp.headers, {'Content-Type', 'Content-Encoding', 'Content-Length', 'Length'})
        response = Response(stream, mimetype=resp.headers['Content-Type'], headers=headers, direct_passthrough=True)
        response.call_on_close(close_stream)
        return response
    except httpx.HTTPError:
        close_stream()
        return '', 400
```

**`dict_subset` 白名单工具实现**：[utils.py#L343-L352](file:///d:/fz/0601-1/solo-dogfeeding/code/77-searxng/searx/utils.py#L343-L352)

```python
def dict_subset(dictionary, properties):
    return {k: dictionary[k] for k in properties if k in dictionary}
```

#### 3.3.1 设计背景：开放代理的元数据泄漏威胁模型

图片代理本质上是一个**受控的开放 HTTP GET 代理**。用户浏览器无法直接访问上游图片服务器（为了隐私保护或绕过跨域限制），于是把 URL 交给 SearXNG，由 SearXNG 代发请求、再把响应转回浏览器。

如果不加任何头过滤，一个**开放代理**会天然成为双向的元数据泄漏管道：

```
【客户端 → SearXNG → 上游】方向上：
  浏览器正常请求会附带 Cookie、Authorization、Referer、Origin 等头。
  如果代理原样转发这些头，攻击者可以构造 URL 指向受害者账号下的敏感资源，
  代理就会替他"带着凭证访问"，造成 CSRF / 带凭证 SSRF / 用户跟踪。

【上游 → SearXNG → 客户端】方向上：
  上游服务器可以在响应里塞 Set-Cookie、Server、ETag、CSP、Location 等头。
  如果代理原样回传这些头，浏览器会把这些头当作 SearXNG 自己发出的，
  造成会话污染、指纹识别、跨站跟踪、安全策略覆盖等一系列问题。
```

SearXNG 的双向白名单就是为了**在双向通道上同时切断这两条泄漏路径**。

---

#### 3.3.2 请求侧白名单：为什么只保留 User-Agent / Accept / Sec-GPC / DNT

`http_stream` 的调用中，`headers=request_headers` 参数会**完全替换** httpx 默认请求头，不会与 httpx 的默认头或客户端头合并。这意味着客户端发来的任何头、httpx 的默认头都被丢弃了。

逐一分析这 4 个被保留的头以及关键被剥离的头：

| 请求头 | 处理方式 | 安全语义 |
|--------|---------|---------|
| **`User-Agent: gen_useragent()`** | ✅ **保留，且用 SearXNG 自生成值** | 上游服务器需要 UA 才能正确判断返回什么格式的图片（例如移动端和桌面端图片尺寸不同）。但**不用客户端的真实 UA**（避免通过 UA 指纹关联 SearXNG 用户与上游站点），而是调用 `gen_useragent()` 生成一个通用的、带随机版本号的 UA，所有用户共享同一份 UA 池。 |
| **`Accept: image/webp,*/*`** | ✅ **保留，硬编码** | 告知上游 SearXNG 接受 WebP 和其他任意图片格式，便于上游返回最优压缩格式。硬编码而不用客户端传来的值：客户端的 Accept 里通常携带 `text/html,application/xhtml+xml,...` 等一大串，会泄漏客户端浏览器类型（Chrome/Safari/Firefox 对 Accept 格式和 q 值排序各有差异，是 UA 指纹的重要维度），所以统一用图片专用值。 |
| **`Sec-GPC: 1`** | ✅ **保留，主动添加** | Global Privacy Control 标准头。主动声明"用户不希望被跨站跟踪"，是 SearXNG 作为隐私搜索引擎的立场表达 —— 即使是代用户请求图片，也要向上游传递不被跟踪的意愿。 |
| **`DNT: 1`** | ✅ **保留，主动添加** | Do Not Track 头。作用与 Sec-GPC 类似，兼容旧系统的跟踪退出标准。 |
| **`Cookie` / `Cookie2`** | ❌ **强制剥离** | 最关键的剥离项。如果转发客户端 Cookie，攻击者可以构造这样的攻击：把图片 URL 指向 `https://victim-site.com/account/avatar`，让 SearXNG 代理用**当前登录用户的 Cookie**去拉取头像，虽然拿不到响应体（浏览器只渲染成图片），但可以通过**头像是否存在 / 尺寸**判断用户是否在 victim-site.com 上拥有特定角色或权限（是一种基于图片尺寸的身份侧信道）。同时也避免上游站点通过 SearXNG 代理路径向客户端 Cookie 写入跟踪标记。 |
| **`Authorization`** | ❌ **强制剥离** | 如果客户端是登录态且页面上有资源请求携带 Basic/Bearer Token，原样转发会直接把 SearXNG 用户的认证凭据暴露给任意第三方图片源。特别危险的是 `Authorization: Bearer <SearXNG-session-JWT>`，如果转发出去等同于把 SearXNG 自己的会话令牌送给上游。 |
| **`Referer` / `Origin`** | ❌ **强制剥离** | Referer 会携带搜索关键词（SearXNG 的搜索结果页 URL 的 query 参数就包含 `q=<keyword>`），直接转发等于把用户搜索内容告诉上游图片站。Origin 也是同理。这和 `default_http_headers` 里的 `Referrer-Policy: no-referrer` 形成**双重保障**：即使浏览器因为降级忽略了 Referrer-Policy 仍然发了 Referer 头，代理这侧也会再剥一次。 |
| **`Accept-Language` / `Accept-Encoding`** | ❌ **强制剥离** | `Accept-Language: zh-CN,zh;q=0.9,en;q=0.8` 这类取值是极强的用户指纹（语言偏好列表和 q 值排序的组合熵很高）。`Accept-Encoding` 也是同理（不同浏览器支持的压缩算法顺序不同）。统一剥离后，上游看到所有用户的语言偏好完全一致，无法按语言分群跟踪。 |
| **`If-None-Match` / `If-Modified-Since`** | ❌ **强制剥离** | 这是条件请求头。如果转发，上游可以通过 ETag/Last-Modified 的回显配合响应头构建**"代理级缓存跟踪器"**——本质是让每个用户的浏览器缓存一个唯一的 ETag 值，此后每次通过代理请求图片时，浏览器会自动带上 `If-None-Match: <唯一ID>`，上游就能识别这是哪个用户（即使 IP 变化）。剥离条件请求头从根源上杜绝了这种"回环式跟踪"。 |
| **`Range` / `If-Range`** | ❌ **强制剥离** | 范围请求头。剥离后避免上游通过 206 Partial Content 的分片响应，用分片边界和长度作为隐形指纹识别并跟踪用户。 |
| **`X-Forwarded-For` / `X-Real-IP`** | ❌ **（httpx 默认不会发出，无需显式处理）** | 这层依赖 httpx 的默认行为（不自动添加 XFF）。如果未来 httpx 版本或某个中间件开始加 XFF，就等于把 SearXNG 服务器的内网 IP 或客户端真实 IP 泄漏给上游。目前链路中 ProxyFix 只是重写了 Flask 侧的 environ，对 httpx 发出的请求无影响，所以这里是隐含假设，需要注意版本升级风险。 |

> **设计要点总结（请求侧）**：白名单的四选三原则是——"图片渲染必须"（UA、Accept）加"隐私立场声明"（Sec-GPC、DNT）。凡是能**识别用户**（Cookie、Auth、语言）、**暴露搜索内容**（Referer）、**用于跨站回环跟踪**（条件请求头）的，一律剥离。UA 虽然是标识符，但把它换成共享池的通用值后，就从"识别个人"降级为"识别设备大类"。

---

#### 3.3.3 响应侧白名单：`dict_subset` 只放 4 个头，其余全部剥离

`dict_subset(resp.headers, {'Content-Type', 'Content-Encoding', 'Content-Length', 'Length'})` 的语义是**只挑出白名单中的键，其余全部静默丢弃**。

这比黑名单安全得多——即使上游塞了 SearXNG 开发者没想到的新头部（例如未来浏览器新增 `X-New-Standard-Tracking-Header`），白名单机制也会自动挡掉，不需要代码更新。

#### 3.3.3.1 保留的 4 个头：图片渲染的最小必要集

| 响应头 | 保留原因 | 不可替代性 |
|--------|---------|-----------|
| **`Content-Type`** | 浏览器必须知道 MIME 类型才能渲染图片。 | 缺了它，浏览器要么触发下载，要么尝试 MIME 嗅探（后面 X-Content-Type-Options: nosniff 会兜底禁止）。**双重保险**：同时在 `Response(mimetype=...)` 构造参数里再显式传一次。 |
| **`Content-Encoding`** | 如果图片被 gzip/br 压缩过（部分 CDN 对 SVG/WebP 再做 HTTP 层压缩），浏览器需要此字段解码。 | 不保留会导致压缩图片解压失败显示为乱码。 |
| **`Content-Length` / `Length`** | 浏览器下载进度显示、缓冲区预分配。保留非标准的 `Length` 是兼容极少数老服务器。 | 不保留不影响正确性，但用户体验差（进度条永远 0% 直到下载完成才跳 100%）。 |

#### 3.3.3.2 被强制剥离的头：逐类对应攻击链

以下每一类被剥离的头，都对应一个**明确可行的攻击链**。剥离它们不是"过度谨慎"，而是"必要的深度防御"。

##### 第一类：会话 / Cookie 污染

**被剥离的头**：`Set-Cookie`、`Set-Cookie2`

**攻击链（如果不透传的反面）**：

```
1. 攻击者在 evil.com 上部署一张图片，响应带 Set-Cookie: tracker_id=<唯一UUID>; Domain=.searxng-instance.com
2. SearXNG 用户在搜索时触发图片代理，访问 evil.com 的图片
3. 若代理透传 Set-Cookie → 浏览器为 searxng-instance.com 设置了 tracker_id Cookie
4. 该用户此后对 searxng-instance.com 的**任何请求**（包括真实的搜索请求），
   浏览器都会自动带上这个 tracker_id
5. evil.com 可以在其他合作站点的图片里读取同一个 tracker_id → 跨站关联该用户
```

更严重的变种是 `Set-Cookie: session=<伪造值>; HttpOnly; Secure`，攻击者可以通过代理**向 SearXNG 域名注入伪造的会话 Cookie**，如果 SearXNG 的会话机制有缺陷（例如不校验 session 完整性签名），可能直接导致会话固定或会话劫持。

**剥离的必要性**：Cookie 是浏览器的"按域名隔离"机制。代理把不同域名的响应转给浏览器时，**上游的域名身份被屏蔽**，浏览器把一切都归于 searxng-instance.com。所以**任何 Set-Cookie 都必须被代理拦截**，这是开放代理的基本安全规则。

##### 第二类：服务器指纹与链路追踪

**被剥离的头**：`Server`、`X-Powered-By`、`Via`、`X-Cache`、`X-Cache-Hits`、`X-Amz-Cf-Id`、`CF-Ray`、`X-Fastly-Request-ID`、`X-Request-ID` 等 CDN / 负载均衡标记头

**攻击链**：

```
攻击者视角的信息收集：

1. 批量检查 searxng 实例代理的多个常见图片源的响应头
2. 如果看到 Server: nginx/1.18.0 + Via: 1.1 google → 推断该实例上游走 Google CDN
3. 如果看到 CF-Ray: <node-id> → 推断该实例走 Cloudflare，且具体出节点位置
4. 如果看到 X-Powered-By: PHP/7.4.33 → 知道某图片源存在已知 CVE 的 PHP 版本

→ 这些信息帮助攻击者：
   a) 做"链路特征指纹"：把某个公开 SearXNG 实例和它的管理员身份关联
      （如果该管理员的个人博客也用了同一个 CF-Ray 出节点）
   b) 做针对性的上游攻击：知道上游用特定版本 nginx，就用对应 CVE 打上游
      （上游被攻陷后，可通过图片响应投毒反向攻击 SearXNG 用户）
```

**剥离的必要性**：SearXNG 的隐私承诺不仅是**用户之间互不关联**，也包括**SearXNG 实例自身的部署结构不应被外部轻易探明**。任何能帮助描绘"实例→上游 CDN→软件栈"链路图的头都要剥光。

##### 第三类：基于缓存的跨站跟踪（ETag / Last-Modified "超级 Cookie"）

**被剥离的头**：`Cache-Control`、`Last-Modified`、`ETag`、`Date`、`Expires`、`Age`

**攻击链（ETag 作为跟踪器，最经典的开放代理滥用方式）**：

```
1. 浏览器第一次通过代理请求 tracker.com/pixel.gif
2. tracker.com 返回:
     ETag: "user-<UUID-12345>"
     Cache-Control: max-age=31536000, immutable
3. 代理如果透传这两个头 → 浏览器缓存 pixel.gif，ETag 存为 UUID-12345
4. 浏览器第二次请求同一张图（换了 URL 参数，或换了页面）
5. 浏览器自动带上 If-None-Match: "user-<UUID-12345>"
6. → 问题来了：请求侧已经剥离了 If-None-Match，这一步攻击者收不到？
   ↓ 但还有变种 ↓
7. 更聪明的变种：tracker.com 返回 ETag 时，同一个用户的 ETag 值是固定的
8. 攻击者在 site-A.com 和 site-B.com 上都嵌了指向 tracker.com 的 1x1 像素图
9. 浏览器对 A、B 站点的像素图分别发两个独立请求到 SearXNG 代理
10. tracker.com 对这两个请求返回的 ETag 值**完全一致**（基于代理出口 IP 推断关联）
11. 这两个 ETag 被存入浏览器各自域名（不，是存入 searxng-instance.com）的缓存
12. site-A.com 和 site-B.com 用 JS 读 searxng-instance.com 缓存中的图片响应头时受限
    （同源策略）→ 但可以通过"缓存命中时序"区分：
    → site-A 页面里的像素图加载耗时 2ms（命中缓存）
    → site-B 页面里的像素图加载耗时 150ms（未命中，因为 ETag 不同）
    → 可以判断这两个用户不是同一个
```

更简单的版本：**即使请求侧剥了 If-None-Match**，只要响应侧透传了 `Cache-Control: max-age=...`，浏览器就会把图片缓存起来。攻击者可以通过"第一次加载慢、第二次加载快"的时序测量，判断某个追踪像素图是否已被访问过，从而判断用户最近是否访问过某个嵌入了该像素的站点。

**剥离的必要性**：把所有缓存相关头全部剥离后，浏览器无法获知该图片应如何缓存（只能退回到默认启发式缓存，通常很短或不缓存），**缓存侧信道被切断**。同时 `after_request` 钩子也不会向这个响应注入 Cache-Control（因为白名单里没设置过它，"不覆盖原则"只对已存在的头生效），因此整个响应没有明确的缓存指令，这正是我们想要的。

##### 第四类：安全策略反向污染

**被剥离的头**：`Content-Security-Policy`、`Content-Security-Policy-Report-Only`、`Strict-Transport-Security`、`X-Frame-Options`、`X-Content-Type-Options`、`Permissions-Policy`、`Cross-Origin-Resource-Policy`、`Cross-Origin-Embedder-Policy`、`Cross-Origin-Opener-Policy`、`Report-To`、`NEL`

**攻击链（CSP 反向污染，最精巧的攻击）**：

```
假设 searxng-instance.com 自己的 CSP 是 script-src 'self'（默认没有配置，但假设有）。
上游 evil.com 的图片响应返回：
  Content-Security-Policy: script-src 'none'

浏览器行为：
  收到 /image_proxy?url=evil.com/x.png 时，这个响应来自 searxng-instance.com 域名，
  所以浏览器会把这个 CSP 应用到**当前整个 searxng-instance.com 的浏览上下文**。

  不同浏览器对"子资源响应的 CSP 是否覆盖文档 CSP"处理不一致。
  但在某些旧浏览器或特定组合下（例如 iframe 内嵌图片），
  子资源的 CSP 可能与文档 CSP 合并甚至覆盖，导致：
    - 页面脚本被拦截（script-src 'none' 生效）
    - SearXNG 页面变白板，功能全部失效
  
  同理，evil.com 可以返回 X-Frame-Options: DENY 试图阻止 SearXNG 被嵌入，
  或者返回过于严格的 Permissions-Policy 禁用摄像头/麦克风等 API，
  影响 SearXNG 页面上的其他功能。
```

**HSTS 注入攻击链**：

```
上游返回 Strict-Transport-Security: max-age=31536000; includeSubDomains

→ 浏览器把 SearXNG 实例加入 HSTS 列表，一年之内只能 HTTPS 访问。
→ 如果管理员是 HTTP 部署（内网、本地开发），用户访问会被强制跳 HTTPS，
  实例直接无法使用。这是一种"拒绝服务型"的响应头污染攻击。
```

**剥离的必要性**：所有浏览器安全策略类头部**都只应该由 SearXNG 自己决定**，不能让上游资源"越俎代庖"。这些头被剥离后，SearXNG 自己在 `default_http_headers` 中配置的安全策略会在 `after_request` 阶段被注入，保证安全上下文的**来源唯一**和**一致性**。

##### 第五类：跨域策略放宽

**被剥离的头**：`Access-Control-Allow-Origin`、`Access-Control-Allow-Credentials`、`Access-Control-Expose-Headers`、`Timing-Allow-Origin`

**攻击链**：

```
正常情况下：
  浏览器对 searxng-instance.com 发跨域图片请求受 CORS 限制。
  如果脚本尝试 fetch('/image_proxy?url=evil.com/x.png') 并读取响应，
  需要 searxng-instance.com 返回 Access-Control-Allow-Origin。

攻击路径：
1. evil.com 返回 Access-Control-Allow-Origin: *
2. SearXNG 代理把这个头透传给浏览器
3. evil.com 页面的脚本现在可以 fetch searxng-instance.com/image_proxy?...
   且不受 CORS 限制读取响应
4. 虽然只是图片，脚本可以：
   a) 用 canvas.getImageData() 读取像素 → 判断图片内容是否存在
      （例如用户是否有某个特定平台的头像 → 身份侧信道）
   b) 测量精确加载时间 → Timing Attack，根据响应长度/时间推断内容
5. Timing-Allow-Origin 同理，会暴露更精细的时序信息，用于侧信道分析。
```

**剥离的必要性**：开放代理的 CORS 头必须由 SearXNG 自己定义。第三方资源无权"授权"浏览器对 SearXNG 域名发起跨域访问。

##### 第六类：跳转与重定向滥用

**被剥离的头**：`Location`、`Refresh`

**攻击链**：

```
情形 1（直接跳转型）：
  上游返回 HTTP 302 + Location: javascript:alert(document.domain)
  → httpx 跟随 302（allow_redirects=True），但 Location 的协议如果是
     javascript: / data: 等危险 scheme，httpx 会拒绝。

情形 2（绕过 httpx 跟随）：
  上游返回 HTTP 200 + body = <img content...> + 额外头 Location: https://evil.com
  → HTTP 规范里 200 响应带 Location 是非法的，但某些浏览器会"宽容处理"。
  → 虽然不是标准重定向，但如果 SearXNG 透传这个 Location，
     部分浏览器插件或特定场景下可能触发非预期跳转。

更实际的风险：Refresh: 5; url=https://evil.com
  → 这个头不是 HTTP 规范，是老 IE/Netscape 的遗留扩展，少数浏览器仍支持。
  → 透传后浏览器会在 N 秒后跳转到 evil.com，把用户带离 SearXNG。
```

**剥离的必要性**：代理本身已经用 `allow_redirects=True` 在 httpx 内部处理了规范的 HTTP 重定向（301/302/303/307/308），任何浏览器侧再次看到的 Location 都是"多余的、非标准的"，可能被用于绕过 httpx 跳转安全检查的 trick。剥离后，浏览器只能接收到"最终内容响应"，中间跳转过程完全不可见。

##### 第七类：其他元数据侧信道

**被剥离的头**（没有穷尽，白名单机制自动屏蔽所有未列出的头）：

- `Accept-Ranges`、`Content-Disposition`、`Content-Language`、`Link`（WebSub/Pingback 等）
- `X-RateLimit-Limit`、`X-RateLimit-Remaining`、`Retry-After`、`X-Robots-Tag`
- `Digest`、`Signature`、`Want-Digest` 等 HTTP 签名头
- 以及未来任何被上游加入的新头部

**侧信道示例**：`X-RateLimit-Remaining: 47`

```
攻击者可以：
1. 构建一组不同关键词的搜索，每个关键词触发的上游图片源不同
2. 对比各个图片代理响应里的 X-RateLimit-Remaining 值
3. 反推：哪些关键词走了同一家上游 → 推断 SearXNG 的引擎配置
4. 如果某个上游的速率限制很严（例如 100req/h），攻击者可以故意消耗掉配额，
   再观察哪些搜索的图片开始失效 → 精确识别 SearXNG 使用了哪些图片引擎。
```

这是**元数据侧信道攻击**的典型例子：即使头本身"看起来无害"，但只要它携带了状态信息，就可以被攻击者用于信息收集。

**白名单策略的本质优势**：与其维护一个永远追不上的黑名单（每出现一个新头就要补代码），不如用一个永远正确的白名单（只放行图片渲染必须的那几个）。`dict_subset` 的实现确保了**任何未被明确允许的头都会被自动剥离**，即使是代码写完后才出现的新头部标准。

---

#### 3.3.4 与统一注入路径的纵深防御

响应侧白名单只放行 `Content-Type`、`Content-Encoding`、`Content-Length`、`Length`，这 4 个**纯内容传输类**头与 `default_http_headers` 中的**安全策略类**头（`X-Content-Type-Options`、`Referrer-Policy` 等）**完全不重叠**，因此：

1. **不会触发"不覆盖原则"**：白名单里没设的头，`after_request` 钩子会正常逐个注入。
2. **Content-Type 校验的实际强度与 nosniff 的配合**：

   图片代理对上游响应的 Content-Type 做了两条分支放行：

   ```python
   # webapp.py#L1035-L1039
   if not resp.headers.get('Content-Type', '').startswith('image/') and not resp.headers.get(
       'Content-Type', ''
   ).startswith('binary/octet-stream'):
       return '', 400
   ```

   - **`image/*` 分支**：这是主通道，覆盖绝大多数正常图片响应。
   - **`binary/octet-stream` 分支**：这是一个**弱放行口**。部分上游服务器（尤其是老旧系统或 CDN 的默认回退行为）对图片资源返回 `Content-Type: binary/octet-stream` 而非具体的 `image/png`。SearXNG 为了兼容这些上游而放行了此类型。

   **`binary/octet-stream` 放行口带来的风险**：`binary/octet-stream` 是一个"万能二进制"类型，任何二进制数据都可以声明为此类型——包括可执行文件、PDF、甚至是精心构造的 HTML/JS。如果上游被攻陷，返回 `Content-Type: binary/octet-stream` + 恶意载荷，图片代理不会拦截。此时唯一的防线就是 `X-Content-Type-Options: nosniff`：浏览器在 nosniff 约束下，遇到 `binary/octet-stream` 不会尝试嗅探为可执行脚本，只会按二进制下载或交给注册的处理程序。**但要注意**：nosniff 对 `binary/octet-stream` 的保护力度不如对 `text/html` 的保护——部分浏览器对 `binary/octet-stream` 仍可能触发"另存为"对话框，而不会静默丢弃。

   因此，纵深防御链路的实际强度是：

   ```
   上游返回恶意 Content-Type →
     ├─ image/* → 图片代理校验通过 → nosniff 禁止嗅探 → 安全
     ├─ binary/octet-stream → 图片代理校验通过 → nosniff 限制执行但非绝对 → 有残余风险
     └─ 其他类型 → 图片代理直接 400 拦截 → 安全
   ```

   换句话说，**纵深防御不是"双保险"，而是"image/* 侧双保险 + binary/octet-stream 侧单保险"**。`binary/octet-stream` 分支是安全边界上的一个已知的弱口，是兼容性对安全性的让步。

3. **CSP 一致性**：上游可能携带的污染 CSP 被白名单剥离后，SearXNG 自己配置的 CSP（如果管理员在 `default_http_headers` 里加了）会被正常注入。不会出现"两个 CSP 冲突"的情况。

---

#### 3.3.5 双向白名单威胁模型总览

```
┌─────────────────────────────────────────────────────────────────────┐
│                        双向白名单威胁模型                              │
├──────────────────────┬──────────────────────────────────────────────┤
│                      │  客户端 → SearXNG → 上游                       │
│   请求侧白名单        │                                              │
│   (4 个硬编码头)      │  防 CSRF / 带凭证 SSRF                        │
│                      │  防搜索关键词泄漏（Referer）                    │
│                      │  防 UA / 语言 / 编码指纹                        │
│                      │  防 ETag 缓存回环跟踪（If-None-Match）          │
├──────────────────────┼──────────────────────────────────────────────┤
│                      │  上游 → SearXNG → 客户端                       │
│   响应侧白名单        │                                              │
│   (dict_subset       │  防 Cookie / 会话注入污染（Set-Cookie）        │
│    4 个内容类头)      │  防 ETag / Last-Modified 缓存超级 Cookie       │
│                      │  防 CSP / HSTS / XFO 反向安全策略污染           │
│                      │  防 Server / Via / CF-Ray 部署指纹泄漏          │
│                      │  防 CORS 放宽导致跨域像素读取侧信道               │
│                      │  防 Location / Refresh 隐蔽跳转滥用             │
│                      │  防 X-RateLimit 等元数据推断引擎配置             │
│                      │  自动屏蔽未来新增的头部（白名单天生优势）         │
└──────────────────────┴──────────────────────────────────────────────┘
```

双向白名单是 SearXNG 图片代理作为"受控开放代理"的**核心安全边界**。没有它，图片代理就会变成一个"双向元数据泄漏管道"，不仅起不到隐私保护作用，反而会把用户置于比直接访问第三方更危险的境地。

### 3.4 限流拦截响应：`before_request` 阶段直接返回

**代码位置1**：[ip_limit.py#L124-L127](file:///d:/fz/0601-1/solo-dogfeeding/code/77-searxng/searx/botdetection/ip_limit.py#L124-L127)

```python
logger.error("BLOCK: too many request from %s in SUSPICIOUS_IP_WINDOW (redirect to /)", network)
response = flask.redirect(flask.url_for('index'), code=302)
response.headers["Cache-Control"] = "no-store, max-age=0"
return response
```

**代码位置2**：[_helpers.py#L45-L53](file:///d:/fz/0601-1/solo-dogfeeding/code/77-searxng/searx/botdetection/_helpers.py#L45-L53)

```python
def too_many_requests(network, log_msg):
    logger.debug("BLOCK %s: %s", network.compressed, log_msg)
    return flask.make_response(('Too Many Requests', 429))
```

**逐段解析**：

- **旁路性质**：限流模块通过 `app.before_request(pre_request)` 在请求处理前就进行拦截，如果触发限流规则，直接返回响应，不会进入视图函数。
- **302 重定向响应**：当 IP 被判定为"可疑"且请求数超过阈值时，返回 302 重定向到首页，并设置 `Cache-Control: no-store, max-age=0` 禁止缓存。
- **429 限流响应**：通过 `too_many_requests` 函数返回标准的 429 "Too Many Requests" 响应，不设置任何自定义头。
- **与统一注入的共存**：`before_request` 钩子返回非 `None` 响应时，Flask 会直接使用该响应，**不会经过视图函数**，但**仍然会经过 `after_request` 钩子**。因此：
  - 302 重定向响应中已有的 `Cache-Control` 不会被覆盖，但其他安全头会正常注入。
  - 429 响应中没有预先设置安全头，所有安全头都由 `after_request` 钩子注入。
- **关键知识点**：Flask 的 `before_request` 返回响应对象时，会跳过路由处理，但 `after_request` 钩子链仍然会执行。这是 SearXNG 安全头覆盖率高的重要原因。

### 3.5 其他旁路端点

**健康检查端点**：[webapp.py#L600-L602](file:///d:/fz/0601-1/solo-dogfeeding/code/77-searxng/searx/webapp.py#L600-L602)
- `/healthz` 返回 `Response('OK', mimetype='text/plain')`，不设置自定义头，安全头全部由 `after_request` 注入。

**Favicon.ico 端点**：[webapp.py#L1241-L1248](file:///d:/fz/0601-1/solo-dogfeeding/code/77-searxng/searx/webapp.py#L1241-L1248)
- `/favicon.ico` 使用 `send_from_directory`，不设置自定义头，安全头全部由 `after_request` 注入。

### 3.6 共存机制总结

| 端点/场景 | 直接设置的头 | 安全头注入方式 | 不覆盖原则是否生效 |
|-----------|-------------|---------------|-------------------|
| 普通 HTML 页面 | 无 | 全部由 `after_request` 注入 | 不适用 |
| `/client<token>.css` | `Cache-Control` | 除 `Cache-Control` 外全部注入 | 是 |
| `/favicon_proxy`（成功） | `Cache-Control` | 除 `Cache-Control` 外全部注入 | 是 |
| `/favicon_proxy`（失败） | 无 | 全部由 `after_request` 注入 | 不适用 |
| `/image_proxy` | `Content-Type` 等 | 全部安全头正常注入 | 不重叠 |
| 限流 302 响应 | `Cache-Control` | 除 `Cache-Control` 外全部注入 | 是 |
| 限流 429 响应 | 无 | 全部由 `after_request` 注入 | 不适用 |
| 静态资源（WhiteNoise） | `Cache-Control` | 全部注入（直接赋值，会覆盖） | 否（WhiteNoise 路径独立） |

**核心结论**：

1. **动态响应侧**：`after_request` 钩子的"不覆盖"原则，使得视图层定制的 `Cache-Control` 等头部得以保留，同时安全头（X-Content-Type-Options、Referrer-Policy 等）因不与自定义头冲突而总能注入。
2. **静态资源侧**：WhiteNoise 路径完全独立，`static_headers` 函数中 `Cache-Control` 和安全头都是直接赋值，不存在"不覆盖"原则。
3. **限流拦截不绕开安全头**：即使在 `before_request` 阶段就返回响应，`after_request` 钩子仍然执行，保证了限流响应也带有安全头。

---

## 四、内容安全策略（CSP）的限定机制

### 4.1 CSP 不是默认配置，但支持自定义

SearXNG 默认配置中**没有**启用 `Content-Security-Policy` 头。但系统架构完全支持 CSP：

1. **通过 `default_http_headers` 配置注入**：管理员可以在 `settings.yml` 的 `server.default_http_headers` 中添加任意 CSP 策略，系统会通过上述两条注入路径自动应用到所有响应。

2. **前端代码的 nonce 支持**：前端构建产物（Vite 打包）中内置了对 CSP nonce 的读取逻辑。

**前端 nonce 读取代码**（来自 [sxng-core.min.js](file:///d:/fz/0601-1/solo-dogfeeding/code/77-searxng/searx/static/themes/simple/sxng-core.min.js)）：

```javascript
// 从 meta 标签读取 CSP nonce
let nonceMeta = document.querySelector(`meta[property=csp-nonce]`);
let nonce = nonceMeta?.nonce || nonceMeta?.getAttribute(`nonce`);

// 动态创建 link 元素时设置 nonce
let link = document.createElement(`link`);
if (nonce) {
    link.setAttribute(`nonce`, nonce);
}
document.head.appendChild(link);
```

**解读**：
- 前端在动态注入 `<link>` 标签（用于 JS 模块预加载和 CSS 懒加载）时，会自动携带 CSP nonce。
- nonce 来源于 `<meta property="csp-nonce" nonce="xxx">` 标签。
- 这意味着如果管理员启用了带 nonce 的 CSP 策略，前端动态资源加载仍然可以正常工作。

### 4.2 CSP nonce 链路是否畅通？—— 模板层缺失，链路半断

**关键发现**：虽然前端 JS 代码预留了 nonce 读取逻辑，但**后端模板层并未注入 `csp-nonce` meta 标签**，导致 nonce 链路不完整。

**模板验证**：在 [base.html](file:///d:/fz/0601-1/solo-dogfeeding/code/77-searxng/searx/templates/simple/base.html) 和所有模板文件中搜索 `nonce` 或 `csp`，结果为 0 匹配。

**完整的 nonce 链路需要四个环节**：

```
   生成 nonce      →  注入 CSP 响应头   →  注入 meta 标签   →  前端读取并使用
  (服务端随机)     (after_request)    (模板渲染)        (JS 动态加载)
```

**SearXNG 的现状**：

| 环节 | 状态 | 代码位置 |
|------|------|----------|
| 1. 生成 nonce | ❌ 缺失 | 无对应代码 |
| 2. 注入 CSP 响应头 | ⚠️ 可配置但默认无 | `default_http_headers` 配置项 |
| 3. 注入 meta 标签 | ❌ 缺失 | 模板中无 `<meta property="csp-nonce">` |
| 4. 前端读取并使用 | ✅ 已实现 | [sxng-core.min.js](file:///d:/fz/0601-1/solo-dogfeeding/code/77-searxng/searx/static/themes/simple/sxng-core.min.js) 中的 `document.querySelector('meta[property=csp-nonce]')` |

**逐段分析**：

- **环节 1（缺失）**：服务端没有生成随机 nonce 的代码。要启用 nonce 策略，需要在 `before_request` 或渲染时生成一个加密安全的随机值，存储到请求上下文中。
- **环节 2（可配置）**：管理员可以在 `default_http_headers` 中添加 `Content-Security-Policy`，但如果使用 nonce 策略，就不能写死在配置中，必须动态生成。
- **环节 3（缺失）**：`base.html` 模板中没有 `<meta property="csp-nonce" nonce="{{ csp_nonce }}">` 标签。前端 JS 代码执行时 `document.querySelector('meta[property=csp-nonce]')` 会返回 `null`，nonce 变量为 `undefined`。
- **环节 4（已实现）**：前端的 Vite 预加载逻辑会在动态创建 `<link>` 元素时尝试设置 `nonce` 属性，但由于 nonce 为 `undefined`，`setAttribute('nonce', undefined)` 会设置为字符串 `"undefined"`，或被条件判断跳过（`a && o.setAttribute('nonce', a)` 中 `a` 为假值时不执行）。

**前端代码的非空保护**：从压缩代码 `a && o.setAttribute('nonce', a)` 可以看出，当 `a`（即 nonce）为假值时，`setAttribute` 不会被调用。因此**即使没有 nonce，前端也不会报错**，只是动态加载的 link 标签不带 nonce 属性。

### 4.3 脚本加载来源的限定

SearXNG 的脚本加载架构决定了 CSP `script-src` 指令的限定范围：

**脚本加载方式**：
1. **主入口脚本**：通过 `<script type="module" src=".../sxng-core.min.js">` 静态加载（来自 [base.html#L13](file:///d:/fz/0601-1/solo-dogfeeding/code/77-searxng/searx/templates/simple/base.html#L13)）
2. **动态模块加载**：通过 ES Module `import()` 动态按需加载功能模块（如地图、计算器、无限滚动等）
3. **模块预加载**：Vite 的预加载机制通过动态创建 `<link rel="modulepreload">` 标签预加载依赖

**如果启用 CSP，`script-src` 需要至少允许**：
- `'self'`：同域静态资源目录下的所有 JS 文件
- 如果使用 nonce 策略：`'nonce-<随机值>'`（需配合模板层注入 nonce 到 meta 标签 —— 当前缺失）

**如果不使用 nonce 策略**，`script-src 'self'` 就足够了，因为：
- 所有脚本都来自同域 `/static/` 路径
- 没有内联脚本（`base.html` 中没有 `<script>...</script>` 内联代码块）
- 动态 `import()` 加载的模块也属于同域源

### 4.4 资源加载来源的限定

**图片资源**：

默认情况下，搜索结果中的图片直接指向第三方源。如果启用了图片代理（`image_proxy: true`），所有图片都会经过 SearXNG 的 `/image_proxy` 端点代理。

**图片代理端点**：[webapp.py#L1002-L1049](file:///d:/fz/0601-1/solo-dogfeeding/code/77-searxng/searx/webapp.py#L1002-L1049)

Google Videos 引擎的文档特别说明了 CSP 配置需求：

**来源**：[google_videos.py#L4-L9](file:///d:/fz/0601-1/solo-dogfeeding/code/77-searxng/searx/engines/google_videos.py#L4-L9)

```python
.. admonition:: Content-Security-Policy (CSP)

   This engine needs to allow images from the `data URLs`_ (prefixed with the
   ``data:`` scheme)::

     Header set Content-Security-Policy "img-src 'self' data: ;"
```

**解读**：
- Google Videos 引擎使用 `data:` URL 内嵌图片缩略图
- 如果启用严格的 CSP `img-src`，必须显式包含 `data:` 源，否则这些缩略图无法显示
- 推荐的 `img-src` 策略：`'self' data:`（启用图片代理时）或更宽松的策略

**样式资源**：
- 主样式表：`sxng-ltr.min.css` / `sxng-rtl.min.css`（同域静态资源）
- 客户端 token 样式：`/client_token?token=...`（动态生成，用于速率限制）
- 动态加载的 CSS 也通过 nonce 机制支持 CSP（但 nonce 链路当前不完整）

**favicon 资源**：
- favicon 代理返回的图片也受 `img-src` 约束
- 启用 favicon 代理后，favicon 均通过 `/favicon_proxy` 端点返回，属于同域源

---

## 五、其他安全相关的头部与机制

### 5.1 `Server-Timing` 性能头（非安全类）

**位置**：[webapp.py#L531-L550](file:///d:/fz/0601-1/solo-dogfeeding/code/77-searxng/searx/webapp.py#L531-L550)

另一个 `after_request` 钩子 `post_request` 负责添加 `Server-Timing` 头，用于性能监控，不直接影响安全。

### 5.2 Cookie 安全属性

**Cookie 设置**：[preferences.py#L28](file:///d:/fz/0601-1/solo-dogfeeding/code/77-searxng/searx/preferences.py#L28) 和 [preferences.py#L74](file:///d:/fz/0601-1/solo-dogfeeding/code/77-searxng/searx/preferences.py#L74)

```python
COOKIE_MAX_AGE = 60 * 60 * 24 * 365 * 5  # 5 years

# 各 Setting 子类的 save 方法
resp.set_cookie(name, self.value, max_age=COOKIE_MAX_AGE)
```

**逐段解析**：

- **有效期**：Cookie 有效期为 5 年，属于长效持久化 Cookie。
- **未设置 Secure 属性**：代码中未显式设置 `secure=True`，依赖 Flask 默认配置。如果通过 HTTPS 部署，建议在 CSP 或 Flask 配置中强制 Secure。
- **未设置 HttpOnly 属性**：Cookie 可被 JavaScript 读取。这是设计使然，因为客户端需要读取偏好设置。
- **未设置 SameSite 属性**：使用 Flask 默认值。

### 5.3 密钥安全检查

**位置**：[webapp.py#L1369-L1373](file:///d:/fz/0601-1/solo-dogfeeding/code/77-searxng/searx/webapp.py#L1369-L1373)

```python
if not app.debug and get_setting("server.secret_key") == 'ultrasecretkey':
    logger.error("server.secret_key is not changed. Please use something else instead of ultrasecretkey.")
    sys.exit(1)
```

**逐段解析**：

- 在 `init()` 初始化函数中执行安全检查
- 非调试模式下，如果 `secret_key` 仍为默认值 `ultrasecretkey`，程序直接退出
- 防止管理员使用默认密钥部署，避免会话安全风险
- `secret_key` 用于 HMAC 签名（如图片代理的 URL 签名、favicon 代理的 URL 签名）和会话管理

---

## 六、完整的安全头部装配链路

```
                +------------------------------+
                |  botdetection.trusted_proxies |
                |  (可信代理网段 CIDR 列表)     |
                +--------------+---------------+
                               │
                               ▼
          ┌──────────────────────────────────────────┐
          │  ProxyFix (WSGI 最外层，先于一切执行)       │
          │  1. environ.pop("REMOTE_ADDR")            │
          │  2. 验证 X-Forwarded-For / X-Real-IP      │
          │  3. trusted_remote_addr(): RTL 反向扫描   │
          │     → 剥离可信代理，锁定第一个不可信 IP     │
          │  4. 重写 environ["REMOTE_ADDR"]           │
          │     (fallback 黑洞 100::)                 │
          └─────────────────────┬────────────────────┘
                                │
                                ▼
          ┌──────────────────────────────────────────┐
          │  WhiteNoise 中间件 (静态资源)               │
          │  static_headers()                           │
          │    Cache-Control: public, max-age=30...    │
          │    + default_http_headers 全部安全头        │
          └─────────────────────┬────────────────────┘
                                │
                                ▼
                +-------------------------------+
                |  settings.yml 配置             |
                |  default_http_headers          |
                +-----------+-------------------+
                            │
                            │ 配置读取
                            ▼
          +-----------------+------------------+
          │                                    │
          ▼                                    ▼
 +--------+---------+               +----------+---------+
 |  Flask 层         |               |  limiter / botdetection   |
 |  after_request    |               |  before_request 钩子       |
 |  add_default_headers              |  filter_request()          |
 |  (不覆盖已有头原则)               |     ↓                        |
 |  post_request                     |  基于 ProxyFix 重写的       |
 |  (Server-Timing)                  |  REMOTE_ADDR 做决策:        |
 +--------+---------+               |    pass_ip / block_ip       |
          |                         |    ip_limit (速率计数)      |
          |  所有动态响应            |    link_token (浏览器验证) |
          ▼                         |    http_user_agent 等       |
 +--------+---------+               +-----------+----------------+
 |  HTML / JSON /   |                           │
 |  RSS / CSV 等    |                           ▼
 |                  |               +-----------+----------------+
 |  ┌──────────────┐|               | before_request 返回响应?    |
 |  │/image_proxy  │|               │  ├─ 是：跳过视图函数          |
 |  │  请求头硬编码 │|               │  │   但仍走 after_request    |
 |  │  (剥离 Cookie │|               │  │   安全头不丢失            |
 |  │   / Referer) │|               │  └─ 否：正常进入视图函数     │
 |  │                                              │
 |  │  响应头白名单 │|                              ▼
 |  │  dict_subset  │|               +--------------+--------------+
 |  │  {Content-    │|               | 视图函数（旁路直接设头示例）：  |
 |  │   Type, Enc.  │|               │  • /client<token>.css         |
 |  │   Length}     │|               │     Cache-Control: no-store   |
 |  │  剥离：        │|               │  • /favicon_proxy (成功)      |
 |  │   Set-Cookie  │|               │     Cache-Control: max-age=N  |
 |  │   Server/ETag │|               │  • /image_proxy               │
 |  │   Location    │|               │     dict_subset 白名单 4 项    |
 |  │   CSP/CORS 等 │|               │  • 限流 302 重定向             |
 |  └──────────────┘|               │     Cache-Control: no-store   |
 +------------------+               +--------------+--------------+
                                                                │
                                                                ▼
                                                    Flask 按注册顺序
                                                    执行 after_request
                                                    add_default_headers:
                                                    跳过已存在的头
                                                    注入其余安全头
```

**关键设计原则**：

1. **配置驱动**：所有安全头集中在 `default_http_headers` 配置项，管理员可以自由增减。
2. **双路径覆盖**：动态响应走 Flask `after_request`，静态资源走 WhiteNoise 中间件，两者互不干扰但策略一致。
3. **不覆盖原则**：动态响应中已存在的头不会被全局配置覆盖，允许路由级定制（如探针端点的 `Cache-Control: no-store`）。
4. **before_request 不绕过安全头**：限流等 `before_request` 拦截返回的响应仍然经过 `after_request`，安全头不丢失。
5. **IP 决策根基独立**：自实现 ProxyFix 在 WSGI 最外层重写 `REMOTE_ADDR`，所有限流/黑白名单共享同一可信 IP 来源，反向扫描算法防御 XFF 左侧伪造。
6. **开放代理白名单剥离**：图片代理对请求头和响应头都采用显式白名单策略，双向剥离 Cookie、Server、ETag、CSP、CORS 等元数据，防止代理被滥用为"带凭证攻击跳板"或"跟踪 Cookie 注入器"。
7. **可扩展性**：CSP 等高级安全策略可通过配置启用，前端已预留 nonce 支持（但后端链路不完整）。

---

## 七、管理员自定义 CSP 的建议配置

### 7.1 基础 CSP 配置（不使用 nonce）

如果管理员希望启用 CSP，可以在 `settings.yml` 中添加如下配置：

```yaml
server:
  default_http_headers:
    X-Content-Type-Options: nosniff
    X-Download-Options: noopen
    X-Robots-Tag: noindex, nofollow
    Referrer-Policy: no-referrer
    # 新增 CSP
    Content-Security-Policy: >-
      default-src 'self';
      script-src 'self';
      style-src 'self' 'unsafe-inline';
      img-src 'self' data:;
      font-src 'self';
      connect-src 'self';
      frame-ancestors 'none';
      base-uri 'self';
      form-action 'self';
```

**注意事项**：
- `style-src` 可能需要 `'unsafe-inline'`，因为某些 UI 组件可能使用内联样式
- `img-src` 必须包含 `data:` 以支持 Google Videos 等引擎的 data URI 图片
- 如果启用图片代理，`img-src` 只需要 `'self' data:` 即可
- 如果不使用图片代理且允许直接加载第三方图片，`img-src` 需要更宽松的策略（如 `*` 或具体域名白名单）

### 7.2 启用 nonce 策略需要的改造

如果希望使用更严格的 nonce 策略，需要对代码进行以下补充：

1. **生成 nonce**：在 `before_request` 中生成加密安全的随机 nonce，存入请求上下文
2. **动态设置 CSP 头**：在 `after_request` 中动态构建 CSP 头，将 nonce 嵌入
3. **注入模板变量**：在 `render()` 函数中将 nonce 传递给模板
4. **添加 meta 标签**：在 `base.html` 中添加 `<meta property="csp-nonce" nonce="{{ csp_nonce }}">`

完成以上改造后，前端的 nonce 读取逻辑才能正常工作，实现完整的 CSP nonce 链路。
