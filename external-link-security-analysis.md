# SearXNG 外链规范化、重定向及中转保护机制分析报告

## 1. 概述

SearXNG 作为一款隐私优先的元搜索引擎，对外链（搜索结果中的 URL）实施了一套多层次的安全处理流水线，涵盖 **规范化 → 去追踪 → 主机重写 → 代理中转 → 渲染输出** 全链路。本报告基于源码逐层分析各环节的实现机制与安全考量。

---

## 2. 外链处理流水线总览

```
搜索引擎返回原始 URL
       │
       ▼
  ┌──────────────┐
  │ URL 规范化     │  ← _normalize_url_fields() / normalize_url()
  └──────┬───────┘
         │
       插件管道（按注册顺序执行）
  ┌──────▼───────┐
  │ Tracker Remover│  ← TRACKER_PATTERNS.clean_url()
  └──────┬───────┘
  ┌──────▼───────┐
  │ Hostnames     │  ← replace / remove / priority
  └──────┬───────┘
  ┌──────▼───────┐
  │ OA DOI Rewrite│  ← DOI 重写为开放获取链接
  └──────┬───────┘
         │
       渲染层
  ┌──────▼───────┐
  │ Image Proxify │  ← 图片代理中转
  └──────┬───────┘
  ┌──────▼───────┐
  │ Favicon Proxy │  ← 图标代理中转
  └──────┬───────┘
  ┌──────▼───────┐
  │ 模板渲染      │  ← rel="noreferrer" / target="_blank"
  └──────────────┘
```

---

## 3. URL 规范化（Normalization）

### 3.1 结果级规范化 — `_normalize_url_fields()`

**源码位置**: [result_types/_base.py](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/result_types/_base.py#L38-L83)

每个搜索结果在进入 `ResultContainer` 时，都会调用 `normalize_result_fields()`，其核心逻辑在 `_normalize_url_fields()` 中：

```python
def _normalize_url_fields(result):
    if result.url and not result.parsed_url:
        if not isinstance(result.url, str):
            result.url = ""
            result.parsed_url = None
        else:
            result.parsed_url = urllib.parse.urlparse(result.url)

    if result.parsed_url:
        result.parsed_url = result.parsed_url._replace(
            scheme=result.parsed_url.scheme or "http",
            path=result.parsed_url.path,
        )
        result.url = result.parsed_url.geturl()
```

**关键安全行为**：
- **缺失协议降级为 `http`**：如果 URL 没有 scheme（如 `example.com/path`），默认补全为 `http://`。这是一个有意的保守选择——比猜测 `https` 更安全，因为 `http` 不会产生虚假的安全承诺。
- **类型校验**：非字符串类型的 URL 被强制清空为 `""`，防止类型混淆攻击。
- **同步 `url` 与 `parsed_url`**：规范化后用 `parsed_url.geturl()` 回写 `url` 字段，确保两者始终一致。

Infobox 中的 URL 也经过同样的规范化处理（第 59-83 行），包括 `urls` 列表和 `id` 字段中的 URL。

### 3.2 引擎级规范化 — `normalize_url()`

**源码位置**: [utils.py](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/utils.py#L256-L303)

引擎在解析搜索结果时使用的工具函数，处理更复杂的相对 URL 场景：

```python
def normalize_url(url: str, base_url: str) -> str:
    if url.startswith('//'):
        parsed_search_url = urlparse(base_url)
        url = '{0}:{1}'.format(parsed_search_url.scheme or 'http', url)
    elif url.startswith('/'):
        url = urljoin(base_url, url)

    if '://' not in url:
        url = urljoin(base_url, url)

    parsed_url = urlparse(url)
    if not parsed_url.netloc:
        raise ValueError('Cannot parse url')
    if not parsed_url.path:
        url += '/'

    return url
```

**处理场景**：
| 输入 URL | base_url | 输出 |
|---|---|---|
| `//example.com` | `https://engine.com/` | `https://example.com/` |
| `/path?a=1` | `https://engine.com` | `https://engine.com/path?a=1` |
| `relative/path` | `https://engine.com/page/` | `https://engine.com/page/relative/path` |

**协议继承**：`//` 开头的 URL 从 `base_url` 继承 scheme，而非硬编码 `https`。这确保了协议一致性。

### 3.3 HTTPS 升级 — `merge_two_main_results()`

**源码位置**: [results.py](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/results.py#L377-L381)

当相同结果来自多个引擎时，合并逻辑会自动升级到更安全的协议：

```python
if origin.parsed_url and not origin.parsed_url.scheme.endswith("s"):
    if other.parsed_url and other.parsed_url.scheme.endswith("s"):
        origin.parsed_url = origin.parsed_url._replace(scheme=other.parsed_url.scheme)
        origin.url = origin.parsed_url.geturl()
```

这实现了 **自动 HTTPS 优先合并**：如果任一来源提供了 `https`（或 `ftps` 等安全变体），最终结果会使用安全协议。

---

## 4. 追踪器移除（Tracker URL Remover）

### 4.1 插件实现

**源码位置**: [plugins/tracker_url_remover.py](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/plugins/tracker_url_remover.py#L44-L58)

该插件通过 `on_result` 钩子，对每个搜索结果的所有 URL 字段执行追踪参数清理：

```python
def on_result(self, request, search, result):
    result.filter_urls(self.filter_url_field)
    return True

@classmethod
def filter_url_field(cls, result, field_name, url_src):
    if not url_src:
        return True
    return TRACKER_PATTERNS.clean_url(url=url_src)
```

`filter_urls()` 方法会对结果中 **所有 URL 字段** 执行过滤：
- `url`, `iframe_src`, `audio_src`, `img_src`, `thumbnail_src`, `thumbnail`
- Infobox 的 `urls` 列表和 `attributes` 中的 `image.src`

### 4.2 ClearURLs 规则引擎 — `TrackerPatternsDB.clean_url()`

**源码位置**: [data/tracker_patterns.py](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/data/tracker_patterns.py#L111-L176)

这是追踪参数清理的核心引擎，基于开源项目 [ClearURLs](https://clearurls.xyz) 的规则：

**规则数据结构**：
```python
RuleType = tuple[str, list[str], list[str]]
# Fields: (url_regexp, url_ignore, del_args)
```

**清理流程**：

1. **URL 匹配**：用 `url_regexp` 正则匹配完整 URL
2. **忽略检查**：如果 URL 命中 `url_ignore` 中的任一模式，跳过此规则
3. **参数清理**：遍历查询参数，删除匹配 `del_args` 中正则的参数名
4. **非标准查询处理**：对于 `?/foo/bar` 这类非键值对查询，如果匹配则直接清空整个查询字符串

**规则来源**（第 31-36 行）：
```python
CLEAR_LIST_URL = [
    "https://rules1.clearurls.xyz/data.minify.json",
    "https://rules2.clearurls.xyz/data.minify.json",
    "https://raw.githubusercontent.com/ClearURLs/Rules/refs/heads/master/data.min.json",
]
```

采用多源容灾设计，按顺序尝试获取规则列表，首个返回 HTTP 200 的源即为有效源。

---

## 5. 主机名重写与过滤（Hostnames Plugin）

**源码位置**: [plugins/hostnames.py](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/plugins/hostnames.py#L110-L200)

### 5.1 四种操作模式

| 配置项 | 类型 | 行为 |
|---|---|---|
| `hostnames.replace` | 正则→主机名映射 | 将匹配的主机名替换为新主机名 |
| `hostnames.remove` | 正则列表 | 从结果中移除匹配主机的所有结果 |
| `hostnames.high_priority` | 正则列表 | 提升匹配主机结果的排序优先级 |
| `hostnames.low_priority` | 正则列表 | 降低匹配主机结果的排序优先级 |

### 5.2 `filter_url_field()` — URL 级重写与删除

```python
def filter_url_field(result, field_name, url_src):
    url_src_parsed = urlparse(url_src)

    for pattern in REMOVE:
        if pattern.search(url_src_parsed.netloc):
            return False  # 删除此 URL

    for pattern, replacement in REPLACE.items():
        if pattern.search(url_src_parsed.netloc):
            new_url = url_src_parsed._replace(
                netloc=pattern.sub(replacement, url_src_parsed.netloc)
            )
            new_url = urlunparse(new_url)
            return new_url  # 重写主机名

    return True  # 保持不变
```

**安全意义**：
- **隐私保护**：可将 `youtube.com` 重写为 `invidious.example.com` 等前端替代品，避免用户直接访问跟踪型网站
- **内容过滤**：`remove` 规则可完全移除特定主机的结果，如屏蔽 `facebook.com`
- **仅重写 netloc**：使用 `urlparse._replace(netloc=...)` 精确替换，保留路径、查询等部分不变

### 5.3 结果级操作 — `on_result()`

```python
def on_result(self, request, search, result):
    for pattern in REMOVE:
        if result.parsed_url and pattern.search(result.parsed_url.netloc):
            return False  # 从结果列表中移除

    result.filter_urls(filter_url_field)  # URL 级操作

    if isinstance(result, (MainResult, LegacyResult)):
        for pattern in LOW:
            if result.parsed_url and pattern.search(result.parsed_url.netloc):
                result.priority = "low"
        for pattern in HIGH:
            if result.parsed_url and pattern.search(result.parsed_url.netloc):
                result.priority = "high"

    return True
```

### 5.4 外部配置文件支持

主机名规则支持从外部 YAML 文件加载（第 168-169 行），方便大规模部署时独立管理重写规则：
```yaml
hostnames:
  replace: 'rewrite-hosts.yml'
```

---

## 6. DOI 开放获取重写（OA DOI Rewrite）

**源码位置**: [plugins/oa_doi_rewrite.py](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/plugins/oa_doi_rewrite.py#L25-L89)

该插件将学术出版物的 DOI 链接重写为开放获取版本：

```python
def filter_url_field(result, field_name, url_src):
    if field_name != "url":
        return True  # 仅重写主 URL 字段

    doi = extract_doi(result.parsed_url)
    if doi and len(doi) < 50:
        for suffix in ("/", ".pdf", ".xml", "/full", "/meta", "/abstract"):
            doi = doi.removesuffix(suffix)
        new_url = get_doi_resolver() + doi
        return new_url

    return True
```

**安全防护**：
- **DOI 长度限制**（`len(doi) < 50`）：防止过长的 DOI 导致重写后 URL 异常
- **后缀清理**：移除 `.pdf`、`.xml` 等常见后缀，确保解析器获得干净的 DOI
- **仅重写 `url` 字段**：其他字段如 `img_src` 不受影响

---

## 7. 图片代理中转（Image Proxy）

### 7.1 代理 URL 生成 — `image_proxify()`

**源码位置**: [webapp.py](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/webapp.py#L298-L321)

```python
def image_proxify(url: str):
    if not url:
        return url

    if url.startswith('//'):
        url = 'https:' + url

    if not sxng_request.preferences.get_value('image_proxy'):
        return url  # 用户未启用代理，直连

    if url.startswith('data:image/'):
        partial_base64 = url[len('data:image/') : 50].split(';')
        if (
            len(partial_base64) == 2
            and partial_base64[0] in ['gif', 'png', 'jpeg', 'pjpeg', 'webp', 'tiff', 'bmp']
            and partial_base64[1].startswith('base64,')
        ):
            return url  # 合法 data: URL，直接放行
        return None  # 非法 data: URL，阻止

    h = new_hmac(settings['server']['secret_key'], url.encode())
    return '{0}?{1}'.format(url_for('image_proxy'), urlencode(dict(url=url.encode(), h=h)))
```

**防护要点**：

1. **协议补全**：`//` 开头的 URL 强制升级为 `https:`
2. **data: URL 白名单**：仅放行格式合法的 base64 编码图片（白名单格式：gif, png, jpeg 等），阻止可能的恶意 data: URL
3. **HMAC 签名**：对代理 URL 计算 HMAC-SHA256，防止伪造请求
4. **用户可控**：通过 `image_proxy` 偏好设置，用户可自行决定是否启用

### 7.2 图片代理服务端 — `image_proxy()`

**源码位置**: [webapp.py](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/webapp.py#L1002-L1071)

```python
@app.route('/image_proxy', methods=['GET'])
def image_proxy():
    url = sxng_request.args.get('url')
    if not url:
        return '', 400

    if not is_hmac_of(settings['server']['secret_key'], url.encode(), sxng_request.args.get('h', '')):
        return '', 400  # HMAC 校验失败

    maximum_size = 5 * 1024 * 1024  # 5MB 大小限制

    resp, stream = http_stream(method='GET', url=url, headers=request_headers, allow_redirects=True)

    # Content-Length 检查
    if content_length and int(content_length) > maximum_size:
        return 'Max size', 400

    # 状态码检查
    if resp.status_code != 200:
        return '', 400

    # Content-Type 白名单
    if not resp.headers.get('Content-Type', '').startswith('image/') and \
       not resp.headers.get('Content-Type', '').startswith('binary/octet-stream'):
        return '', 400
```

**多层防护**：

| 层级 | 检查项 | 安全意义 |
|---|---|---|
| 1 | HMAC 签名校验 | 防止攻击者伪造代理请求，将 SearXNG 变为开放代理 |
| 2 | 5MB 大小限制 | 防止资源耗尽攻击 |
| 3 | HTTP 状态码校验 | 仅转发成功的响应 |
| 4 | Content-Type 白名单 | 仅转发 `image/*` 和 `binary/octet-stream`，阻止 HTML/JS 等恶意内容 |
| 5 | 随机 User-Agent | 代理请求使用随机浏览器 UA，防止指纹追踪 |

### 7.3 HMAC 实现

**源码位置**: [webutils.py](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/webutils.py#L212-L218)

```python
def new_hmac(secret_key, url):
    return hmac.new(secret_key.encode(), url, hashlib.sha256).hexdigest()

def is_hmac_of(secret_key, value, hmac_to_check):
    hmac_of_value = new_hmac(secret_key, value)
    return len(hmac_of_value) == len(hmac_to_check) and \
           hmac.compare_digest(hmac_of_value, hmac_to_check)
```

使用 `hmac.compare_digest()` 进行**恒定时间比较**，防止时序攻击。长度检查作为前置快速失败条件。

---

## 8. Favicon 代理中转

**源码位置**: [favicons/proxy.py](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/favicons/proxy.py#L112-L157)

Favicon 代理与图片代理采用类似的 HMAC 保护模式：

```python
def favicon_proxy():
    authority = sxng_request.args.get('authority')

    if not authority or "/" in authority:  # 仅允许合法域名
        return '', 400

    if not is_hmac_of(CFG.secret_key, authority.encode(), sxng_request.args.get('h', '')):
        return '', 400  # HMAC 校验失败

    resolver = sxng_request.preferences.get_value('favicon_resolver')
    if not resolver or resolver not in CFG.resolver_map.keys():
        return "", 400  # 解析器未配置
```

**额外防护**：
- **Authority 校验**：参数中不允许包含 `/`，确保只传递域名而非完整 URL
- **解析器白名单**：仅允许使用配置中注册的解析器

### 8.1 降级路径

当 favicon 不可用时，有优雅的降级策略（第 153-156 行）：
```python
# 返回默认空 favicon（SVG data URL）
theme = sxng_request.preferences.get_value("theme")
fav, mimetype = CFG.favicon(theme=theme)
return flask.send_from_directory(fav.parent, fav.name, mimetype=mimetype)
```

`favicon_url()` 函数（第 195-237 行）还实现了**缓存优先策略**：
1. 如果 favicon 已在缓存中 → 直接返回 data URL（避免额外 HTTP 请求）
2. 如果缓存标记为"无 favicon" → 返回默认 SVG data URL
3. 如果未缓存 → 返回代理 URL（带 HMAC 签名）

---

## 9. 特殊协议处理

### 9.1 `//` 协议相对 URL

多处代码对 `//` 开头的协议相对 URL 进行处理：

| 位置 | 处理方式 |
|---|---|
| [webapp.py:302-303](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/webapp.py#L302-L303) `image_proxify()` | 强制 `https:` |
| [utils.py:283-286](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/utils.py#L283-L286) `normalize_url()` | 从 base_url 继承 scheme |
| [external_bang.py:51-52](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/external_bang.py#L51-L52) `resolve_bang_definition()` | 强制 `https:` |

### 9.2 `data:` 协议

`image_proxify()` 对 `data:image/` URL 实施严格的白名单验证：
- 格式必须为 `data:image/{type};base64,...`
- `{type}` 仅允许：`gif`, `png`, `jpeg`, `pjpeg`, `webp`, `tiff`, `bmp`
- 不符合规范的 data URL 返回 `None`（完全阻止）

### 9.3 `magnet:` 协议

**源码位置**: [templates/simple/result_templates/torrent.html](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/templates/simple/result_templates/torrent.html#L7-L8)

磁力链接和 torrent 文件链接在模板中直接渲染，不经过代理：
```html
{%- if result.magnetlink %}{{ result_link(result.magnetlink, ...) }}{%- endif -%}
{%- if result.torrentfile %}{{ result_link(result.torrentfile, ...) }}{%- endif -%}
```

`result_link()` 宏（[macros.html:16-18](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/templates/simple/macros.html#L16-L18)）会为这些链接添加 `rel="noreferrer"` 或 `rel="noopener noreferrer"` 保护。

### 9.4 `javascript:` / `vbscript:` 等危险协议

SearXNG 没有显式的危险协议黑名单，但通过以下机制间接防护：
1. **`_normalize_url_fields()`** 中 `urllib.parse.urlparse()` 会对异常 scheme 进行解析
2. **引擎代码** 通常只返回 `http`/`https` URL
3. **Tracker URL Remover** 和 **Hostnames 插件** 的正则匹配通常不会匹配 `javascript:` 等 scheme
4. **`normalize_url()`** 中 `://` 检查会拒绝无 `://` 的非标准 URL

### 9.5 无 scheme 降级

**默认降级为 `http`**：`_normalize_url_fields()` 中 `scheme=result.parsed_url.scheme or "http"` 意味着所有无 scheme 的 URL 都降级为 HTTP。

**合并时 HTTPS 升级**：在 `merge_two_main_results()` 中，如果任一来源提供了安全 scheme，会自动升级。

---

## 10. 渲染层防护

### 10.1 Referrer 泄露防护

**源码位置**: [macros.html](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/templates/simple/macros.html#L8-L10)

所有外链统一添加 `rel="noreferrer"` 属性：

```html
<a href="{{ url }}" {% if results_on_new_tab %}
   target="_blank" rel="noopener noreferrer"
{% else %}
   rel="noreferrer"
{% endif %}>
```

- **`noreferrer`**：阻止浏览器在跳转时发送 Referer 头，防止目标站点获知用户来源
- **`noopener`**（新标签页时附加）：防止 `window.opener` 漏洞，目标页面无法通过 `opener` 访问来源页

### 10.2 新标签页策略

通过 `results_on_new_tab` 配置项（[settings.yml:145](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/settings.yml#L145)），用户可选择：
- **同标签打开**：`rel="noreferrer"`
- **新标签打开**：`target="_blank" rel="noopener noreferrer"`（双重保护）

### 10.3 iframe 安全

**源码位置**: [macros.html](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/templates/simple/macros.html#L72-L79)

```html
<iframe data-src="{{iframe_src}}" frameborder="0" allowfullscreen
  {% if result.parsed_url.hostname in ("www.youtube.com",) -%}
  allow="picture-in-picture" referrerpolicy="origin"
  {%- endif -%}
>
</iframe>
```

- **延迟加载**：使用 `data-src` 而非 `src`，iframe 在用户点击后才加载（由 JS 端 `media-loader` 控制）
- **YouTube 特殊处理**：添加 `referrerpolicy="origin"` 仅发送源站信息（而非完整路径），限制 `allow` 权限

---

## 11. Bang 重定向保护

**源码位置**: [external_bang.py](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/external_bang.py#L49-L61)

当用户使用 bang 搜索（如 `!g query`）时，系统会重定向到外部搜索引擎：

```python
def resolve_bang_definition(bang_definition, query):
    url, rank = bang_definition.split(chr(1))
    if url.startswith('//'):
        url = 'https:' + url  # 强制 HTTPS
    if query:
        url = url.replace(chr(2), quote_plus(query))  # URL 编码查询
    else:
        o = urlparse(url)
        url = o.scheme + '://' + o.netloc  # 无查询时仅跳转主页
    return (url, int(rank))
```

**安全考量**：
- `//` 开头的 bang URL 强制升级为 `https:`
- 查询参数通过 `quote_plus()` 编码，防止注入
- bang 定义数据来自 `searx/data/external_bangs.json`，由项目维护

在 [webapp.py:665-667](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/webapp.py#L665-L667) 中，重定向直接通过 HTTP 302 实现：
```python
if result_container.redirect_url:
    return redirect(result_container.redirect_url)
```

---

## 12. robots.txt 防爬

**源码位置**: [webapp.py:1187-1198](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/webapp.py#L1187-L1198)

```
User-agent: *
Allow: /info/en/about
Disallow: /stats
Disallow: /image_proxy
Disallow: /preferences
Disallow: /*?*q=*
```

`/image_proxy` 被 `Disallow`，防止搜索引擎索引代理的图片资源。

---

## 13. 安全机制总结

| 防护层级 | 机制 | 防止的威胁 |
|---|---|---|
| URL 规范化 | scheme 补全、类型校验 | 格式异常导致的解析漏洞 |
| HTTPS 优先 | 合并时自动升级 | 中间人攻击 |
| Tracker 移除 | ClearURLs 规则引擎 | 追踪参数泄露用户行为 |
| 主机名重写 | 正则替换/删除 | 隐私泄露（直访追踪站点） |
| DOI 重写 | 开放获取替代 | 付费墙追踪 |
| 图片代理 | HMAC + Content-Type 白名单 | IP 泄露、恶意内容注入 |
| Favicon 代理 | HMAC + authority 校验 | 开放代理滥用 |
| Referrer 保护 | `rel="noreferrer"` | 来源信息泄露 |
| 新标签保护 | `rel="noopener noreferrer"` | `window.opener` 攻击 |
| data: URL 白名单 | 格式+类型严格校验 | XSS via data: URI |
| HMAC 签名 | SHA256 + 恒定时间比较 | 代理伪造、时序攻击 |
| iframe 延迟加载 | `data-src` + 点击触发 | 意外加载追踪资源 |

---

## 14. 潜在改进建议

1. **危险协议显式黑名单**：当前对 `javascript:`、`vbscript:`、`data:`（非图片）等危险协议缺少显式过滤，建议在 `_normalize_url_fields()` 中添加 scheme 白名单（仅允许 `http`、`https`、`ftp`、`ftps`、`magnet`）。

2. **规范化阶段 HTTPS 优先**：当前无 scheme URL 默认降级为 `http`，可考虑默认 `https`（现代 Web 环境下更合理），或通过配置项控制。

3. **图片代理重定向限制**：`image_proxy()` 使用 `allow_redirects=True` 但未限制重定向次数和目标域，可能被利用为 SSRF 向内网探测。

4. **代理 URL 过期机制**：当前 HMAC 签名无时间戳，生成的代理 URL 永久有效。添加时间窗口可限制签名有效期。

5. **Content-Security-Policy**：模板中未设置 CSP 头，建议添加严格的 CSP 策略进一步限制外链加载行为。
