# SearXNG 图片代理严格代码核实报告

本报告针对图片代理与图标代理的六个细节问题进行源码级严格核实，所有结论均有明确代码引用支撑。

---

## 一、流式响应阶段是否对累计字节做主动拦截？

**结论：不做主动拦截。**

### 1.1 5MB 限制的唯一防线：Content-Length 预检

[webapp.py L1013-L1027](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/webapp.py#L1013-L1027)

```python
maximum_size = 5 * 1024 * 1024
# ...
resp, stream = http_stream(method='GET', url=url, headers=request_headers, allow_redirects=True)
content_length = resp.headers.get('Content-Length')
if content_length and content_length.isdigit() and int(content_length) > maximum_size:
    return 'Max size', 400
```

**拦截机制：** 仅当响应头包含 `Content-Length` 时触发。

### 1.2 流式传输阶段：无累计字节计数器

[\_\_init\_\_.py L204-L227](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/network/__init__.py#L204-L227)

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

**关键观察：**
- 逐块读取 64KB（65536 字节）写入队列
- 只有 `len(chunk) > 0` 的单块判断，**没有累计字节数的累加逻辑**
- 没有 `total_bytes += len(chunk)` 或类似计数器

### 1.3 分块编码响应的实际约束

对于使用 **Chunked Transfer Encoding**（无 `Content-Length` 头）的响应：
- Content-Length 预检失效
- 流式传输阶段无字节计数
- 唯一的间接约束：**全局请求超时 120 秒**

[\_\_init\_\_.py L86](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/network/__init__.py#L86)

```python
timeout = timeout or 120
```

**风险评估：** 攻击者可构造无 Content-Length 的无限分块响应，在超时前消耗服务器带宽。但由于 `direct_passthrough=True` 直接写入 socket，内存不会增长。

---

## 二、5MB 上限按 1024 还是 1000 进制换算？

**结论：1024 进制（二进制 MiB），非 1000 进制。**

[webapp.py L1013](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/webapp.py#L1013)

```python
maximum_size = 5 * 1024 * 1024
```

**精确值计算：**

```
5 * 1024 * 1024 = 5,242,880 字节
= 5 MiB（Mebibyte，二进制单位）
≈ 5.24 MB（Megabyte，十进制单位）
```

**对比 1000 进制的差异：**

| 进制 | 表达式 | 字节数 |
|------|--------|--------|
| 1024（实际） | `5 * 1024 * 1024` | 5,242,880 |
| 1000（假设） | `5 * 1000 * 1000` | 5,000,000 |
| 差值 | — | +242,880 字节（约 237 KB） |

---

## 三、默认重定向次数与其在网络模块的定义位置

**结论：默认 30 次重定向，定义在两处。**

### 3.1 定义位置 1：settings_defaults.py（用户可配置入口）

[settings_defaults.py L260](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/settings_defaults.py#L260)

```python
'max_redirects': SettingsValue(int, 30),
```

这是 `settings.yml` 中 `outgoing.max_redirects` 的默认值，来自 requests 库的默认值。

### 3.2 定义位置 2：network.py Network 类（运行时默认值）

[network.py L82](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/network/network.py#L82)

```python
max_redirects: int = 30,
```

这是 Network 类构造函数的参数默认值。

### 3.3 实际传递链路

1. 初始化时从 settings 读取 → [network.py L361](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/network/network.py#L361)
   ```python
   'max_redirects': settings_outgoing['max_redirects'],
   ```

2. 创建 Network 实例时传入 → [network.py L373](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/network/network.py#L373)
   ```python
   return Network(**result)
   ```

3. Network 保存到 `self.max_redirects` → [network.py L97](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/network/network.py#L97)
   ```python
   self.max_redirects = max_redirects
   ```

4. 创建 httpx client 时传入 → [client.py L204](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/network/client.py#L204)
   ```python
   max_redirects=max_redirects,
   ```

### 3.4 图片代理端是否显式覆盖？

[webapp.py L1024](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/webapp.py#L1024)

```python
resp, stream = http_stream(method='GET', url=url, headers=request_headers, allow_redirects=True)
```

**没有显式传 `max_redirects`**，使用 network 实例的默认值 30。

---

## 四、data URI 切片只取 50 字符是否覆盖所有合法 MIME 边界？

**结论：覆盖所有白名单 MIME，但 50 是一个超量预留值。**

### 4.1 切片逻辑分析

[webapp.py L308-L310](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/webapp.py#L308-L310)

```python
if url.startswith('data:image/'):
    # 50 is an arbitrary number to get only the beginning of the image.
    partial_base64 = url[len('data:image/') : 50].split(';')
```

**切片范围：**
- `len('data:image/')` = 11
- `url[11:50]` → 从第 11 位到第 50 位，共 **39 个字符**

### 4.2 白名单 MIME 长度分析

白名单定义：

[webapp.py L311-L314](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/webapp.py#L311-L314)

```python
and partial_base64[0] in ['gif', 'png', 'jpeg', 'pjpeg', 'webp', 'tiff', 'bmp']
and partial_base64[1].startswith('base64,')
```

**各 MIME 的 header 长度：**

| MIME 类型 | header 示例 | 实际长度 |
|----------|-------------|----------|
| gif | `data:image/gif;base64,` | 22 |
| png | `data:image/png;base64,` | 22 |
| jpeg | `data:image/jpeg;base64,` | 23 |
| **pjpeg** | `data:image/pjpeg;base64,` | **24** |
| webp | `data:image/webp;base64,` | 23 |
| tiff | `data:image/tiff;base64,` | 23 |
| bmp | `data:image/bmp;base64,` | 22 |

**最长的 MIME header 是 `pjpeg` 共 24 字符。**

切片取到第 50 位（39 字符可用），而最长只需 24 字符（`;` 分隔符在 11+5=16 位，剩余 34 字符完全覆盖 `base64,` 前缀）。

### 4.3 为何取 50 这个"任意"数字？

1. **保守的超量预留**：即使未来添加更长的 MIME 类型（如 `svg+xml`，但 svg 不在白名单中），也有足够余量
2. **避免超长 data URI 性能问题**：有些 data URI 可能几 MB 大，只取前 50 字符分析避免浪费
3. **注释已说明是"arbitrary number"**：代码注释 `50 is an arbitrary number` 明确说明这是一个经验性的安全余量

### 4.4 潜在边缘案例

**被拒绝的合法 data URI 案例**：
- `data:image/svg+xml;base64,...` → `svg+xml` 不在白名单 → 被拒（故意为之，SVG 可执行脚本）
- `data:image/png;charset=utf-8;base64,...` → 多参数的 data URI，`split(';')` 后第 2 段是 `charset=utf-8`，不满足 `startswith('base64,')` → 被拒

**白名单设计的取舍**：为了安全，牺牲了少量理论合法的 data URI 格式。

---

## 五、TLS 密码套件打乱是否为图片代理独占？

**结论：不是图片代理独占，是所有 HTTPS 请求的共享行为。**

### 5.1 密码套件打乱的代码位置

[client.py L28-L48](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/network/client.py#L28-L48)

```python
def shuffle_ciphers(ssl_context: SSLContext):
    c_list = [cipher["name"] for cipher in ssl_context.get_ciphers()]
    sc_list, c_list = c_list[:3], c_list[3:]
    random.shuffle(c_list)
    ssl_context.set_ciphers(":".join(sc_list + c_list))
```

### 5.2 调用链：所有 SSLContext 创建都会触发

[client.py L51-L58](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/network/client.py#L51-L58)

```python
def get_sslcontexts(proxy_url, cert, verify, trust_env):
    key = (proxy_url, cert, verify, trust_env)
    if key not in SSLCONTEXTS:
        SSLCONTEXTS[key] = httpx.create_ssl_context(verify, cert, trust_env)
    shuffle_ciphers(SSLCONTEXTS[key])  # 每次获取都重新打乱
    return SSLCONTEXTS[key]
```

### 5.3 哪些网络传输会触发？

`get_sslcontexts` 被以下函数调用：

**1. SOCKS 代理传输** — [client.py L128](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/network/client.py#L128)
```python
_verify = get_sslcontexts(proxy_url, None, verify, True) if verify is True else verify
```

**2. 普通 HTTP/HTTPS 传输** — [client.py L148](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/network/client.py#L148)
```python
_verify = get_sslcontexts(None, None, verify, True) if verify is True else verify
```

### 5.4 所有会经过 TLS 指纹随机化的请求

| 请求类型 | 是否经过打乱 | 代码证据 |
|---------|-------------|----------|
| 图片代理请求 | ✅ 是 | 使用 `image_proxy` network，调用 `get_sslcontexts` |
| Favicon Resolver 请求 | ✅ 是 | `network.get()` → `get_context_network()` → 走默认 network |
| 搜索引擎抓取请求 | ✅ 是 | 各 engine 的 network 实例 |
| 插件发起的 HTTP 请求 | ✅ 是 | 使用默认 network |
| **纯 HTTP（非 HTTPS）请求** | ❌ 否 | 不需要 SSLContext |

### 5.5 图片代理的网络专属差异

图片代理有专属 network 实例，但差异仅在 **HTTP/2 禁用**，不在 TLS 打乱：

[network.py L414-L420](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/network/network.py#L414-L420)

```python
if 'image_proxy' not in NETWORKS:
    image_proxy_params = default_params.copy()
    image_proxy_params['enable_http2'] = False  # 唯一差异
    NETWORKS['image_proxy'] = new_network(image_proxy_params, logger_name='image_proxy')
```

---

## 六、图标代理在 Resolver 调度与缓存键生成上的真实链路

### 6.1 完整调用链路图

```
模板层：favicon_url(authority)
    │
    ├─► 检查 resolver 偏好是否配置
    │
    ├─► 查服务端缓存 cache.CACHE(resolver, authority)
    │   ├─► 缓存命中且非 fallback → 返回 data URL（base64 内嵌）
    │   ├─► 缓存命中且是 fallback → 返回默认 SVG data URL
    │   └─► 缓存未命中 → 生成带 HMAC 的代理 URL
    │
    ▼
浏览器请求 /favicon_proxy?authority=...&h=...
    │
    ▼
favicon_proxy() 端点
    │
    ├─► authority 格式校验（不含 /）
    ├─► HMAC 校验
    ├─► resolver 有效性校验
    │
    ▼
search_favicon(resolver, authority)
    │
    ├─► 再查缓存 cache.CACHE(resolver, authority)
    │   └─► 缓存命中 → 直接返回
    │
    ├─► CFG.get_resolver(resolver) → 获取函数对象
    │   ├─► 从 resolver_map 拿 fqn
    │   └─► importlib.import_module + getattr
    │
    ├─► 调用 resolver 函数（如 duckduckgo(authority, timeout)）
    │   └─► network.get(url) → HTTP 请求到第三方服务
    │
    └─► cache.CACHE.set(resolver, authority, mime, data)
        └─► 计算 SHA-256，写入 SQLite
```

### 6.2 Resolver 调度细节

#### 6.2.1 resolver 函数获取

[proxy.py L68-L81](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/favicons/proxy.py#L68-L81)

```python
def get_resolver(self, name: str) -> Callable | None:
    fqn = self.resolver_map.get(name)
    if fqn is None:
        return None
    mod_name, _, func_name = fqn.rpartition('.')
    mod = importlib.import_module(mod_name)  # 动态 import
    func = getattr(mod, func_name)
    return func
```

**resolver_map 初始化：**

[proxy.py L34-L41](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/favicons/proxy.py#L34-L41)

```python
def _initial_resolver_map():
    d = {}
    name = get_setting("search.favicon_resolver", None)
    if name:
        func = DEFAULT_RESOLVER_MAP.get(name)
        if func:
            d = {name: f"searx.favicons.resolvers.{func.__name__}"}
    return d
```

**默认 resolver 映射表：**

[resolvers.py L94-L99](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/favicons/resolvers.py#L94-L99)

```python
DEFAULT_RESOLVER_MAP = {
    "allesedv": allesedv,
    "duckduckgo": duckduckgo,
    "google": google,
    "yandex": yandex,
}
```

#### 6.2.2 Resolver 实际 HTTP 调用

[resolvers.py L43-L55](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/favicons/resolvers.py#L43-L55)

```python
def duckduckgo(domain: str, timeout: int) -> tuple[None | bytes, None | str]:
    url = f"https://icons.duckduckgo.com/ip2/{domain}.ico"
    response = network.get(url, **_req_args(timeout=timeout))
    if response and response.status_code == 200:
        mime = response.headers['Content-Type']
        data = response.content  # 完整加载到内存
    return data, mime
```

**关键点：**
- 使用 `network.get()` → `get_context_network()` → 默认 network 实例
- **没有设置专用 context network**，与普通搜索请求共享网络
- `_req_args` 加了 `raise_for_httperror=False`，避免异常中断

### 6.3 缓存键生成细节

#### 6.3.1 双层表结构

[cache.py L259-L277](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/favicons/cache.py#L259-L277)

```sql
-- BLOB 存储表：去重相同的图标二进制
CREATE TABLE blobs (
  sha256     TEXT PRIMARY KEY,  -- 二进制内容的 SHA-256 哈希
  bytes_c    INTEGER,           -- 字节数
  mime       TEXT NOT NULL,
  data       BLOB NOT NULL
)

-- 映射表：(resolver, authority) → sha256
CREATE TABLE blob_map (
  m_time     INTEGER DEFAULT (strftime('%s', 'now')),
  sha256     TEXT,
  resolver   TEXT,
  authority  TEXT,
  PRIMARY KEY (resolver, authority)  -- 复合主键
)
```

#### 6.3.2 缓存写入流程

[cache.py L338-L373](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/favicons/cache.py#L338-L373)

```python
def set(self, resolver: str, authority: str, mime: str | None, data: bytes | None) -> bool:
    # 大小限制
    bytes_c = len(data or b"")
    if bytes_c > self.cfg.BLOB_MAX_BYTES:  # 20KB
        return False

    # 计算 SHA-256
    if data is None:
        sha256 = FALLBACK_ICON  # 特殊标记："FALLBACK_ICON"
    else:
        sha256 = hashlib.sha256(data).hexdigest()  # 键1：内容哈希

    # 写入 blobs 表（去重）
    if sha256 != FALLBACK_ICON:
        conn.execute(self.SQL_INSERT_BLOBS, (sha256, bytes_c, mime, data))

    # 写入 blob_map 表（复合键）
    conn.execute(self.SQL_INSERT_BLOB_MAP, (sha256, resolver, authority))  # 键2：resolver+authority
```

#### 6.3.3 缓存查询流程

[cache.py L320-L336](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/favicons/cache.py#L320-L336)

```python
def __call__(self, resolver: str, authority: str):
    # 用复合键查询映射表
    sql = "SELECT sha256 FROM blob_map WHERE resolver = ? AND authority = ?"
    res = self.DB.execute(sql, (resolver, authority)).fetchone()

    if res is None:
        return None  # 缓存未命中

    sha256 = res[0]
    if sha256 == FALLBACK_ICON:
        return (None, None)  # 已确认无 favicon

    # 用哈希查实际数据
    sql = "SELECT data, mime FROM blobs WHERE sha256 = ?"
    res = self.DB.execute(sql, (sha256,)).fetchone()
    return res  # (data, mime)
```

### 6.4 内存缓存（FaviconCacheMEM）的键结构

[cache.py L463-L490](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/favicons/cache.py#L463-L490)

```python
def __call__(self, resolver: str, authority: str):
    sha, mime = self._sha_mime.get(f"{resolver}:{authority}", (None, None))
    # ↑ 键格式："resolver_name:domain.com"
    data = self._data.get(sha)
    return data, mime

def set(self, resolver: str, authority: str, mime, data):
    digest = hashlib.sha256(data).hexdigest()
    self._data[digest] = data
    self._sha_mime[f"{resolver}:{authority}"] = (digest, mime)
```

### 6.5 图片代理 vs 图标代理：缓存键对照

| 维度 | 图片代理 | 图标代理 |
|------|---------|----------|
| **是否缓存** | ❌ 无服务端缓存 | ✅ SQLite/内存双层缓存 |
| **缓存查询时机** | — | 2 次：URL 生成时 + 代理请求时 |
| **一级键** | — | 复合键 `(resolver, authority)` |
| **二级键** | — | `sha256(data)` 内容哈希去重 |
| **键的持久化** | — | SQLite 磁盘持久化 |
| **命中后响应** | — | 直接返回 base64 data URL（节省 HTTP 请求） |
| **失效策略** | — | HOLD_TIME 30 天 + LIMIT_TOTAL_BYTES 50MB LRU |

---

## 七、六项问题核实结论总表

| 问题 | 结论 | 代码引用 |
|------|------|----------|
| **流式累计字节拦截** | ❌ 无主动拦截，仅 Content-Length 预检 + 120s 超时 | [webapp.py L1013](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/webapp.py#L1013) / [\_\_init\_\_.py L86](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/network/__init__.py#L86) |
| **5MB 进制** | ✅ 1024 进制（5,242,880 字节） | [webapp.py L1013](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/webapp.py#L1013) |
| **默认重定向次数** | ✅ 30 次，定义在 settings_defaults.py 和 network.py | [settings_defaults.py L260](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/settings_defaults.py#L260) / [network.py L82](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/network/network.py#L82) |
| **data URI 50 字符边界** | ✅ 完全覆盖，最长白名单 MIME `pjpeg` header 仅 24 字符 | [webapp.py L308-L314](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/webapp.py#L308-L314) |
| **TLS 指纹打乱独占性** | ❌ 非独占，所有 HTTPS 请求共享 | [client.py L51-L58](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/network/client.py#L51-L58) / [client.py L128](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/network/client.py#L128) / [client.py L148](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/network/client.py#L148) |
| **图标代理缓存键** | ✅ 复合主键 `(resolver, authority)` + 内容哈希 `sha256(data)` 双层 | [cache.py L259-L277](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/favicons/cache.py#L259-L277) / [cache.py L338-L373](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/favicons/cache.py#L338-L373) |
