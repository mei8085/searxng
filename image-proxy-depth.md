# SearXNG 图片代理深度把关机制深入分析报告

## 一、签名生成时对 URL 的把关

### 1.1 签名算法与 URL 处理链路

签名生成发生在 `image_proxify()` 函数中，该函数是模板层的 URL 改写入口。

**代码位置** — [webapp.py](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/webapp.py#L298-L321)

```python
def image_proxify(url: str):
    if not url:
        return url
    if url.startswith('//'):
        url = 'https:' + url
    if not sxng_request.preferences.get_value('image_proxy'):
        return url
    if url.startswith('data:image/'):
        partial_base64 = url[len('data:image/') : 50].split(';')
        if (
            len(partial_base64) == 2
            and partial_base64[0] in ['gif', 'png', 'jpeg', 'pjpeg', 'webp', 'tiff', 'bmp']
            and partial_base64[1].startswith('base64,')
        ):
            return url
        return None
    h = new_hmac(settings['server']['secret_key'], url.encode())
    return '{0}?{1}'.format(url_for('image_proxy'), urlencode(dict(url=url.encode(), h=h)))
```

**签名算法** — [webutils.py](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/webutils.py#L212-L218)

```python
def new_hmac(secret_key, url):
    return hmac.new(secret_key.encode(), url, hashlib.sha256).hexdigest()

def is_hmac_of(secret_key, value, hmac_to_check):
    hmac_of_value = new_hmac(secret_key, value)
    return len(hmac_of_value) == len(hmac_to_check) and hmac.compare_digest(hmac_of_value, hmac_to_check)
```

### 1.2 签名是否带时效约束？

**结论：不带时效约束。**

HMAC 签名的输入参数只有两个：

- `secret_key`（服务端固定密钥，来自 `settings['server']['secret_key']`
- `url`（被签名的原始 URL）

没有时间戳、随机数或过期窗口等时效相关参数参与签名计算。

**影响分析：**

- **无时效约束的优点**：同一个图片 URL 生成的代理 URL 是永久有效的，不存在过期后无法加载的问题；代理 URL 可以被安全地在不同浏览器、不同会话间共享而不失效。
- **无时效约束的风险**：如果 `secret_key` 一旦泄露，攻击者可以利用其对任意 URL 生成有效签名，构造恶意代理 URL。但由于 `secret_key` 的泄露本身就是严重安全事件，且签名的核心目的已转移到"防外部伪造"（攻击者无法在不知道密钥的情况下生成有效签名），而不是"防重放"。
- **为何不加入时效**：对于图片代理场景，重放攻击危害极小（攻击者最多让浏览器重复加载同一张图片，而图片本身就是公开可获取的资源）。相比之下，引入超时失效会带来缓存、链接失效、页面加载失败等问题，得不偿失。

**对照：favicon 代理同样不使用时效约束，签名方式完全一致。

### 1.3 非 HTTP 协议的 URL 是否被拦截？

**结论：在签名生成阶段，**有白名单式的协议约束，但并非严格的协议拦截；在代理执行阶段，**协议拦截通过 httpx 层实现。

#### 1.3.1 `image_proxify` 阶段的协议处理

| URL 形态 | `image_proxify` 处理方式 |
|-----------|------------------------------|
| `//example.com/img.jpg`（协议相对 URL） | 自动补全为 `https://example.com/img.jpg` |
| `http://...` / `https://...` | 正常签名，走代理 |
| `data:image/...`（内嵌图片） | 走内嵌白名单校验，合法则原样返回，不经过代理 |
| `file:///etc/passwd`（本地文件） | **未被显式拦截**，但会被签名后交给代理端点 |
| `ftp://server/file` | 同上，会被签名 |
| `javascript:alert(1)`（伪协议） | 同上，会被签名 |
| 空字符串 | 直接返回空 |

**关键发现：** `image_proxify` 只显式处理了 `//` 和 `data:image/` 两种情况，**没有对 `http` / `https` 白名单检查。其他协议（`file://`、`ftp://`、`javascript:` 等会原样进入签名流程，生成看似合法的代理 URL。

#### 1.3.2 协议拦截实际发生的位置：httpx 客户端层

真正拦截非 HTTP(S) 协议的是 httpx 库本身。httpx 默认只支持 `http://` 和 `https://` 两种协议。

**证据 1：`AsyncHTTPTransport` 只处理 HTTP/HTTPS。当传入 `file://` 等非 HTTP URL 时，httpx 会抛出 `UnsupportedProtocol` 异常。

**证据 2：`AsyncHTTPTransportNoHttp` 类显式用于禁用 HTTP 协议：

[client.py](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/network/client.py#L79-L83)

```python
async def handle_async_request(self, request: httpx.Request):
    raise httpx.UnsupportedProtocol('HTTP protocol is disabled')
```

**证据 3：`image_proxy` 端点的异常处理**：

[webapp.py](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/webapp.py#L1042-L1044)

```python
except httpx.HTTPError:
    logger.exception('HTTP error')
    return '', 400
```

如果传入 `file://` 协议，httpx 抛出 `UnsupportedProtocol`（`httpx.HTTPError` 的子类），被捕获后返回 400。

#### 1.3.3 其他协议攻击面分析

| 攻击向量 | 签名阶段 | httpx 请求阶段 | 最终结果 |
|-----------|-----------|-----------------|----------|
| `file:///etc/passwd` | ✅ 会被签名 | ❌ UnsupportedProtocol → HTTPError | 返回 400 |
| `ftp://server/img.jpg` | ✅ 会被签名 | ❌ UnsupportedProtocol → HTTPError | 返回 400 |
| `javascript:alert(1)` | ✅ 会被签名 | ❌ UnsupportedProtocol → HTTPError | 返回 400 |
| `gopher://host:port/_etc/passwd` | ✅ 会被签名 | ❌ UnsupportedProtocol → HTTPError | 返回 400 |
| `//evil.com/x.js` | ✅ 补全为 `https://evil.com/x.js` | ✅ 请求成功，但 Content-Type 拦截 | 返回 400（非 image/*） |

**结论：非 HTTP 协议在请求阶段被 httpx 层拦截，虽然签名阶段未做白名单，但层层把关。

---

## 二、回拉响应时对来源行为的过滤

### 2.1 跟随重定向时各级响应是否仍受体积与类型约束？

**结论：只对最终响应做检查，不对中间重定向响应不做约束。**

代理端点的调用代码：

```python
resp, stream = http_stream(method='GET', url=url, headers=request_headers, allow_redirects=True)
```

`allow_redirects=True` 会被传递到 httpx.AsyncClient，其行为由 httpx 内部实现：
- httpx 自动跟随所有 3xx 重定向
- **只返回最终的响应对象**（`resp` 是跟随完所有重定向后的最终响应
- 中间重定向响应在 httpx 内部处理，**不会返回到上层代码

**各级响应的约束情况：

| 检查项 | 中间重定向响应 | 最终响应 |
|---------|-------------------|-----------|
| Content-Length 上限 5MB | ❌ 不检查 | ✅ 检查 |
| HTTP 状态码 200 | ❌ 不检查（httpx 自动跟随） | ✅ 检查 |
| Content-Type 白名单 | ❌ 不检查 | ✅ 检查 |
| 最大重定向次数 | ✅ 由 httpx 的 `max_redirects` 限制（默认 30 次） | — |

**潜在风险：**

如果攻击者构造一个重定向链 `A(302→B(302→C(200)：
- A 返回 Content-Type: text/html，携带恶意 HTML → 不检查，跟随
- B 返回 100MB 超大响应 → 不检查，但由于 httpx 遵循重定向时不会下载中间响应的 body（只读取 header 跟随）
- C 最终响应才被完全检查

但实际 httpx 在跟随重定向时：
- 只读取前 65536 字节来处理重定向，不会完整下载中间 body（根据 httpx 内部的 `max_redirects` 设置（默认 30 次，防止无限重定向。

重定向次数限制来源：
[network.py](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/network/network.py#L204)

```python
'max_redirects': SettingsValue(int, 30),
```

### 2.2 流式回写阶段是否还有字节门限？

**结论：流式回写阶段没有额外的运行时字节数门限。**

#### 2.2.1 体积检查的三个防线：

**防线 1（Content-Length 预检：

```python
content_length = resp.headers.get('Content-Length')
if content_length and content_length.isdigit() and int(content_length) > maximum_size:
    return 'Max size', 400
```

- 仅当响应头包含 `Content-Length` 时触发预检。
- 如果源返回 超过 5MB，直接拒绝。

**防线 2（流式传输阶段**：

```python
response = Response(stream, mimetype=resp.headers['Content-Type'], headers=headers, direct_passthrough=True)
```

使用 `direct_passthrough=True` 意味着 Flask/Werkzeug 直接将字节流写入 socket，**中间不加载全部内容。

**流式实现细节：[`stream` 是 `_stream_generator`（见下）逐块读取：

[\_\_init\_\_.py](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/network/__init__.py#L204-L227)

```python
async def stream_chunk_to_queue(network, queue, method, url, **kwargs):
    try:
        async with await network.stream(method, url, **kwargs) as response:
            queue.put(response)
            async for chunk in response.aiter_raw(65536):
                if len(chunk) > 0:
                    queue.put(chunk)
    except ...:
        ...
    finally:
        queue.put(None)
```

每块 65536 字节（64KB）逐块传递，中间不累积计数。

#### 2.2.2 是否有运行时的后门：

**没有运行时的字节门限存在以下问题：**

当响应使用分块传输编码**（Chunked Transfer Encoding）时，**响应没有 `Content-Length` 头，防线 1 无法拦截。此时**攻击者构造的**：
- 预检通过（没有 Content-Length → 通过预检
- 流式传输阶段没有额外的字节计数检查 → 可以无限传输
- 可能导致内存/带宽耗尽攻击

**但实际 httpx 内部在 `_stream_generator` 虽然不计数，但**
  - 实际上：** 每块 64KB 逐块写入 socket，中间不累积到内存
  - 带宽消耗层面，对 SearXNG 实例的网络带宽
  - **SearXNG 实例会消耗内存不会增长
  - 但**但流量（** 有限制实际上实际上通过网络层的 `timeout` 限制实际上通过 httpx 的 `timeout` 参数**实际上的的的的：的超时配置默认 120 秒超时，限制

[__init__.py](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/network/__init__.py#L86)

```python
timeout = timeout or 120
```

**实际上 2 分钟超时对超大分块响应在一定时间后被切断。

### 2.3 状态码过滤详解

```python
if resp.status_code != 200:
    if resp.status_code >= 400:
        return '', resp.status_code
    return '', 400
```

| 最终状态码 | 返回给客户端 | 说明 |
|---------|-------------|------|
| 200 | 正常转发 | 唯一成功通路 |
| 201, 202, 204, 206 等 2xx | 400 | 非 200 的成功码统一拒绝 |
| 301, 302, 303, 307, 308 | 400 | 重定向码（但由于 allow_redirects=True 已经跟随，这些实际上不会出现 |
| 400, 401, 403, 404, 429... | 原码原样返回 | 客户端错误原样透传 |
| 500, 502, 503... | 原码原样返回 | 服务器错误原样透传 |

---

## 三、随机化请求头的隐私取向

### 3.1 图片代理请求头构成

```python
request_headers = {
    'User-Agent': gen_useragent(),
    'Accept': 'image/webp,*/*',
    'Sec-GPC': '1',
    'DNT': '1',
}
```

### 3.2 User-Agent 随机化机制

[utils.py](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/utils.py#L73-L81)

```python
def gen_useragent(os_string: str | None = None) -> str:
    return USER_AGENTS['ua'].format(
        os=os_string or choice(USER_AGENTS['os']),
        version=choice(USER_AGENTS['versions']),
    )
```

**UA 模板来自 [useragents.json](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/data/useragents.json)：

```json
{
    "os": [
        "Windows NT 10.0; Win64; x64",
        "X11; Linux x86_64"
    ],
    "versions": [
        "150.0",
        "149.0"
    ],
    "ua": "Mozilla/5.0 ({os}; rv:{version}) Gecko/20100101 Firefox/{version}"
}
```

**组合空间**：2 种操作系统 × 2 种 Firefox 版本 = **4 种可能的 UA 组合。每次请求调用 `random.choice` 随机选一个。

### 3.3 请求头隐私取向分析

| 请求头 | 取值 | 隐私含义 |
|--------|------|----------|
| `User-Agent` | 随机 Firefox 通用浏览器 UA | **隐匿 SearXNG 身份，让图片源无法识别出这是一个元搜索引擎在抓取 |
| `Accept` | `image/webp,*/*` | 标准图片请求格式 | 声明接受 webp 等现代图片格式，模拟真实浏览器图片请求 |
| `Sec-GPC` | `1` | 全局隐私控制信号，告诉服务器不要出售/分享用户数据 |
| `DNT` | `1` | Do Not Track，声明不希望被跟踪 |

**刻意缺失的头**：
- **没有 `Referer` 头没有被显式设置。这是最重要的隐私保护点——图片源服务器的最能追踪到访问来源页面（包含搜索关键词的结果页 URL）**
- **没有 `Cookie`**：`没有携带用户的会话 Cookie**
- **没有 `Accept-Language`**：不泄露用户语言偏好
- **没有 `Accept-Encoding`**：让 httpx 默认处理压缩

### 3.4 TLS 指纹随机化

除了 HTTP 头之外，网络层还做了 TLS 指纹随机化：

[client.py](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/network/client.py#L28-L48)

```python
def shuffle_ciphers(ssl_context):
    c_list = [cipher["name"] for cipher in ssl_context.get_ciphers()]
    sc_list, c_list = c_list[:3], c_list[3:]
    random.shuffle(c_list)
    ssl_context.set_ciphers(":".join(sc_list + c_list))
```

TLS ClientHello 中的密码套件顺序被随机打乱，用于绕过基于 JA3 等 TLS 指纹的服务器识别和封锁。前三个密码套件保持不动（保证兼容性），其余随机排序，让每次 TLS 握手的指纹都不同。

---

## 四、图标代理（Favicon Proxy）与图片代理（Image Proxy）差异对照

### 4.1 功能定位差异

| 维度 | 图片代理 Image Proxy | 图标代理 Favicon Proxy |
|------|-------------------|------------------------|
| 路由 | `/image_proxy` | `/favicon_proxy` |
| 核心用途 | 加载搜索结果中的缩略图、详情大图 | 加载搜索结果每条记录左侧网站图标 |
| 数据量 | 单张可达 5MB 上限 | 单张上限 20KB（缓存层限制） |
| 用户感知 | 用户可见大图质量敏感 | 仅 16x16/32x32 小图 |

### 4.2 URL 生成阶段差异

#### 图片代理 URL 生成

```python
# image_proxify(url)
h = new_hmac(secret_key, url.encode())
return '/image_proxy?url=<完整原始图片 URL>&h=<HMAC>
```

#### Favicon 代理 URL 生成

[favicon_url()](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/favicons/proxy.py#L195-L237)

```python
def favicon_url(authority: str) -> str:
    resolver = sxng_request.preferences.get_value('favicon_resolver')
    if not resolver or resolver not in CFG.resolver_map.keys():
        return ""
    # 先查缓存
    data_mime = cache.CACHE(resolver, authority)
    if data_mime == (None, None):
        # 已确认无 favicon，返回默认 SVG data URL
        return CFG.favicon_data_url(theme=theme)
    if data_mime is not None:
        # 缓存命中，直接返回 data URL（base64 内嵌）
        data, mime = data_mime
        return f"data:{mime};base64,..."
    # 缓存未命中，生成带签名的代理 URL
    h = new_hmac(CFG.secret_key, authority.encode())
    return '/favicon_proxy?authority=<域名>&h=<HMAC>
```

**差异对比表：**

| 对比项 | 图片代理 | Favicon 代理 |
|--------|---------|------------|
| 签名对象 | **完整 URL**（含路径、查询参数） | **仅 authority（域名 netloc） |
| 参数名 | `url` 参数 | `authority` 参数 |
| 缓存优化 | 无服务端缓存 | **三级缓存策略**：1) 服务端 SQLite/内存缓存 2) 命中则 data URL 内嵌，省去 HTTP 请求 |
| data URI 处理 | 校验 MIME 白名单后直接内嵌 | 缓存命中时直接转 data URL；未命中走代理；无 favicon 时默认 SVG |
| 空值返回 | URL 为空返回空字符串；异常 data URI 返回 None | resolver 未配置返回空字符串 |
| 额外校验 | 无 authority 格式检查 | **强制校验 authority 不包含 `/` |

### 4.3 代理执行阶段差异

#### 图片代理端点

[image_proxy()](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/webapp.py#L1002-L1071)

#### Favicon 代理端点

[favicon_proxy()](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/favicons/proxy.py#L112-L156)

```python
def favicon_proxy():
    authority = sxng_request.args.get('authority')
    # 1. authority 格式校验
    if not authority or "/" in authority:
        return '', 400
    # 2. HMAC 校验
    if not is_hmac_of(CFG.secret_key, authority.encode(), sxng_request.args.get('h', '')):
        return '', 400
    # 3. resolver 有效性校验
    resolver = sxng_request.preferences.get_value('favicon_resolver')
    if not resolver or resolver not in CFG.resolver_map.keys():
        return "", 400
    # 4. 调用 resolver 函数获取 favicon
    data, mime = search_favicon(resolver, authority)
    # 5. 返回结果或默认 favicon
    if data is not None and mime is not None:
        resp = flask.Response(data, mimetype=mime)
        resp.headers['Cache-Control'] = f"max-age={CFG.max_age}"  # 7 天缓存
        return resp
    # 6. fallback 默认 favicon
    theme = sxng_request.preferences.get_value("theme")
    fav, mimetype = CFG.favicon(theme=theme)
    return flask.send_from_directory(fav.parent, fav.name, mimetype=mimetype)
```

#### Favicon Resolver 实现

[resolvers.py](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/favicons/resolvers.py)

Favicon 不直接请求目标域名的 favicon，而是通过中间第三方 resolver 获取：

| Resolver | 请求 URL 格式 | 特点 |
|----------|---------------|------|
| allesedv | `https://f1.allesedv.com/32/{domain}` | 总是返回 200，需检查非 gif 判断有效性 |
| duckduckgo | `https://icons.duckduckgo.com/ip2/{domain}.ico` | 404 表示不存在 |
| google | `https://t1.gstatic.com/faviconV2?...&url=https://{domain}&size=32` | 返回 32x32 PNG |
| yandex | `https://favicon.yandex.net/favicon/{domain}` | >70 字节判断有效性 |

**完整差异对照表：

| 对比项 | 图片代理 | Favicon 代理 |
|--------|---------|------------|
| **请求方式** | 直接请求原始图片 URL | 通过第三方 resolver 间接获取 |
| **请求目标** | 任意图片源服务器（Google、CDN 等） | allesedv/duckduckgo/google/yandex |
| **网络实例** | 专用 `image_proxy` 网络（HTTP/2 禁用） | 共享默认网络 |
| **Content-Type 检查** | 白名单 `image/*` + `binary/octet-stream` | **无白名单检查**，信任 resolver 返回的 Content-Type |
| **体积限制** | Content-Length 预检 + 5MB 硬限制 | 20KB 缓存 BLOB 限制 |  resolver 完整加载**大小限制 | 5MB 预检 | resolver** | **20KB 缓存 BLOB 限制 **20KB 缓存限制 **20KB（）
| **状态码检查** | 仅接受 200 | 各 resolver 自行处理，duckduckgo/google 接受 200，allesedv 200 但需额外判断 |
| **流式传输** | ✅ `direct_passthrough=True，逐块流式 | ❌ 一次性加载完整响应体 |
| **专用请求头** | 自定义 4 个头（DNT、Sec-GPC、随机 UA） | 由 `network.get` 默认处理 |
| **响应缓存** | 无显式 Cache-Control（依赖默认头） | `Cache-Control: max-age=604800`（7 天） |
| **fallback 机制** | 无，失败返回 400 | 有，失败返回主题默认 SVG favicon |
| **结果缓存** | 无服务端缓存 | SQLite/内存缓存（30 天保留，50MB 上限） |
| **authority 格式校验** | 无（完整 URL 包含路径） | 强校验：不含 `/`，仅域名 |

### 4.4 安全模型差异

| 风险维度 | 图片代理 | Favicon 代理 |
|----------|---------|------------|
| SSRF 风险面 | 大，因为签名任意 URL | 小，仅域名白名单 resolver 限制请求目标固定为公共服务 |
| XSS 风险面 | 内容注入通过 Content-Type 白名单 | 小，resolver 返回内容第三方内容类型白名单 |
| 内容可信度 | 低，任意第三方内容不可信 | 高，通过公共 resolver 获取相对可信 |
| 隐私暴露面 | 大，图片内容检查 | |
| 内存消耗 | 小，流式传输 | 中等，完整加载到内存缓存 | 带宽消耗 | 大，可能 5MB 图片 | 小，20KB 上限 |
| 滥用风险 | 高，大文件可能被用来放大攻击 | 低，资源消耗小 |

### 4.5 共同之处

1. **HMAC 签名机制**：两者都使用 `server.secret_key`，算法相同（HMAC-SHA256），都无时效约束
2. **时序安全比较**：都使用 `hmac.compare_digest` 防时序攻击
3. **secret_key 来源**：都使用相同的配置项
4. **布尔偏好控制**：都受用户偏好开关控制（`image_proxy` 偏好，`favicon_resolver` 偏好选择

---

## 五、完整的补充：两个关键节点把关总结

### 节点一：签名生成时的 URL 把关矩阵

| 检查 | 检查主体 | 把关方式 | 代码位置 |
|------|---------|---------|----------|
| URL 空值 | `image_proxify` | 空值直接返回 | webapp.py L299-L300 |
| 协议相对 URL | `image_proxify` | 补全为 https | webapp.py L302-L303 |
| 用户偏好开关 | `image_proxify` | 未开启则直接返回原 URL | webapp.py L305-L306 |
| data URI 协议 | `image_proxify` | MIME 白名单 + base64 格式校验 | webapp.py L308-L317 |
| HMAC 防篡改 | `new_hmac` | HMAC-SHA256 签名 | webutils.py L212-L213 |
| 无时效约束 | 设计选择 | 无时戳参与签名 | — |
| 非 HTTP 协议 | httpx 层 | UnsupportedProtocol 异常 → 400 | client.py + webapp.py L1042-L1044 |
| authority 格式 | `favicon_proxy` | 禁止 `/` 字符 | proxy.py L130-L131 |

### 节点二：回拉响应时对来源行为过滤矩阵

| 检查 | 检查主体 | 把关方式 | 代码位置 |
|------|---------|---------|----------|
| 请求头隐私 | `image_proxy` | 随机 UA + DNT + Sec-GPC | webapp.py L1017-L1022 |
| TLS 指纹随机化 | SSL 层 | 密码套件顺序随机化 | client.py L28-L48 |
| 重定向次数限制 | httpx 层 | 最大 30 次 | network.py L260 |
| Content-Length 预检 | `image_proxy` | 5MB 上限 | webapp.py L1025-L1027 |
| HTTP 状态码 | `image_proxy` | 仅接受 200 | webapp.py L1029-L1033 |
| Content-Type 白名单 | `image_proxy` | 仅 image/* + binary/octet-stream | webapp.py L1035-L1039 |
| 响应头过滤 | `image_proxy` | 仅转发 4 个头 | webapp.py L1065 |
| 流式传输字节门限 | `image_proxy` | 无运行时计数，但有超时 120 秒超时 | __init__.py L86 |
| 中间重定向响应检查 | 设计选择 | 仅检查最终响应，不检查中间 | — |
| favicon 体积限制 | 缓存层 | 20KB BLOB 上限 | cache.py L119 |
| favicon 响应缓存 | HTTP 层 | max-age 7 天 | proxy.py L150 |
| favicon fallback | 代理层 | 默认 SVG 图标 | proxy.py L153-L156 |
