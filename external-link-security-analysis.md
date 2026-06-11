# SearXNG 外链规范化、重定向及中转保护机制分析报告

## 1. 概述

SearXNG 作为一款隐私优先的元搜索引擎，对外链（搜索结果中的 URL）实施了一套多层次的安全处理流水线，涵盖 **规范化 → 去追踪 → 主机重写 → 代理中转 → 渲染输出** 全链路。本报告基于源码逐层分析各环节的实现机制、触发条件与安全考量。

---

## 2. 外链点击前的六条分流路径总览

从用户提交搜索请求，到外链最终呈现在浏览器中被点击，SearXNG 内部共有六条不同的分流路径，它们在不同条件下触发：

```
用户提交查询 q
   │
   ├─→ 查询解析阶段 ──────────────────────────────────────────────┐
   │    ├─ 命中外部 bang (!!xxx)  → 路径A: Bang 服务端 302 重定向  │
   │    └─ 命中 !! 前缀          → 路径B: 首结果 302 重定向        │
   │                                                               │
   ▼                                                               │
搜索引擎返回原始 URL                                               │
   │                                                               │
   ▼                                                               │
结果规范化 + 插件管道                                               │
   │                                                               │
   ├─ 含 img_src / thumbnail_src / thumbnail → 路径C: 图片代理中转 │
   │                                                               │
   ├─ 含 iframe_src / audio_src  → 路径D: iframe 延迟加载           │
   │                                                               │
   └─ 其他所有结果 URL          → 路径E: 普通搜索结果直出            │
                                                                   │
   ▼                                                               │
每个结果还附带 favicon → 路径F: Favicon 代理中转                    │
                                                                   │
   └───────────────────────────────────────────────────────────────┘
```

---

## 3. 六条分流路径详解（含触发条件）

### 路径 A：Bang 服务端 302 重定向

**触发条件**：用户查询包含外部 bang 前缀（如 `!!g hello`）

完整调用链：

1. **查询解析** — [query.py:163-168](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/query.py#L163-L168)

   `ExternalBangParser._parse()` 在查询词中匹配 `!!<bang>`：
   ```python
   def _parse(self, value):
       bang_definition, bang_ac_list = get_bang_definition_and_autocomplete(value)
       if bang_definition is not None:
           self.raw_text_query.external_bang = value  # 标记为外部 bang
           found = True
       return found, bang_ac_list
   ```

2. **Web 适配** — [webadapter.py:259](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/webadapter.py#L259)

   ```python
   external_bang = raw_text_query.external_bang  # 传递给 SearchQuery
   ```

3. **搜索阶段触发** — [search/__init__.py:59-69](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/search/__init__.py#L59-L69)

   `Search.search_external_bang()` 在普通搜索之前抢先执行：
   ```python
   def search_external_bang(self) -> bool:
       if self.search_query.external_bang:
           self.result_container.redirect_url = get_bang_url(self.search_query)
           if isinstance(self.result_container.redirect_url, str):
               return True  # 跳过重定向之外的所有搜索
       return False
   ```

   在 [Search.search()](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/search/__init__.py#L174-L179) 中，它享有最高优先级：
   ```python
   def search(self) -> ResultContainer:
       self.start_time = default_timer()
       if not self.search_external_bang():     # 1. 先试 bang 重定向
           if not self.search_answerers():     # 2. 再试 answerer
               self.search_standard()          # 3. 最后才标准搜索
       return self.result_container
   ```

4. **Bang URL 构造** — [external_bang.py:93-109](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/external_bang.py#L93-L109)

   ```python
   def get_bang_url(search_query, external_bangs_db=None):
       if search_query.external_bang:
           bang_definition, _ = get_bang_definition_and_ac(
               external_bangs_db, search_query.external_bang)
           if bang_definition and isinstance(bang_definition, str):
               ret_val = resolve_bang_definition(
                   bang_definition, search_query.query)[0]
       return ret_val
   ```

5. **Bang URL 规范化** — [external_bang.py:49-61](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/external_bang.py#L49-L61)

   ```python
   def resolve_bang_definition(bang_definition: str, query: str):
       url, rank = bang_definition.split(chr(1))
       if url.startswith('//'):
           url = 'https:' + url               # //xxx → https://xxx
       if query:
           url = url.replace(chr(2), quote_plus(query))  # 编码查询
       else:
           o = urlparse(url)
           url = o.scheme + '://' + o.netloc   # 无查询时只留主页
       return (url, int(rank))
   ```

6. **服务端 302** — [webapp.py:665-667](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/webapp.py#L665-L667)

   在搜索路由的最开始检查：
   ```python
   # 1. check if the result is a redirect for an external bang
   if result_container.redirect_url:
       return redirect(result_container.redirect_url)  # Flask redirect → 302
   ```

**路径 A 关键特征**：
- 用户浏览器 **从未收到 HTML 结果页**，直接 302 跳转到目标引擎
- 触发优先级最高（先于 answerer 和标准搜索）
- `//` 协议相对 URL 强制升级为 `https:`
- 查询内容经 `quote_plus()` URL 编码

---

### 路径 B：首结果 302 重定向（`!!`）

**触发条件**：用户查询包含独立的 `!!` 前缀（如 `!! python docs`）

1. **查询解析** — [query.py:241-247](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/query.py#L241-L247)

   `RedirectFirstResultParser` 将标记设为 `True`：
   ```python
   class RedirectFirstResultParser:
       @staticmethod
       def check(raw_value):
           return raw_value == '!!'

       def __call__(self, raw_value):
           self.raw_text_query.redirect_to_first_result = True
           return True
   ```

   在 [RawTextQuery.__init__](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/query.py#L277) 中默认值为 `False`。

2. **服务端 302** — [webapp.py:698-699](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/webapp.py#L698-L699)

   在渲染 HTML 之前检查：
   ```python
   if search_query.redirect_to_first_result and results:
       return redirect(results[0]['url'], 302)
   ```

**路径 B 关键特征**：
- 仅对 `output_format='html'` 生效（JSON/RSS/CSV 不走这个分支）
- 必须有搜索结果才重定向（`and results`）
- 目标 URL 是 `results[0]['url']`，即经过完整规范化 + 插件管道处理后的首个结果 URL
- 在 bang 重定向之后、HTML 渲染之前执行

---

### 路径 C：图片代理中转

**触发条件**：结果中含有 `img_src`、`thumbnail_src` 或 `thumbnail` 字段，且用户偏好 `image_proxy=True`

#### C.1 调用位置

图片代理在两个层面被调用：

**① 模板层显式调用** — 所有结果模板在渲染时将图片 URL 传给 `image_proxify()`：

| 模板 | 字段 | 代码位置 |
|---|---|---|
| [images.html:3](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/templates/simple/result_templates/images.html#L3) | thumbnail_src / img_src | `<img src="{{ image_proxify(result.thumbnail_src or result.img_src) }}"` |
| [images.html:13](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/templates/simple/result_templates/images.html#L13) | img_src | `<img data-src="{{ image_proxify(result.img_src) }}">` |
| [macros.html:33](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/templates/simple/macros.html#L33) | thumbnail | `<img src="{{ image_proxify(result.thumbnail) }}">` |

**② 插件管道隐式处理**：Tracker URL Remover 和 Hostnames 插件的 `filter_urls()` 会对 `img_src`、`thumbnail_src`、`thumbnail` 字段先执行去追踪/主机重写（见 [_base.py:272](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/result_types/_base.py#L272)）。

#### C.2 代理 URL 生成决策树

**源码位置**：[webapp.py:298-321](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/webapp.py#L298-L321)

```python
def image_proxify(url: str):
    if not url:                    # ① 空值 → 原样返回
        return url
    if url.startswith('//'):
        url = 'https:' + url       # ② //xxx → https://xxx
    if not sxng_request.preferences.get_value('image_proxy'):
        return url                 # ③ 用户关闭代理 → 直连返回
    if url.startswith('data:image/'):
        # 白名单格式校验
        if (合法 base64 图片格式):
            return url             # ④ data: 合法图片 → 原样放行
        return None                # ⑤ data: 非法 → 阻止（返回 None）
    # ⑥ 其他 → 生成代理 URL
    h = new_hmac(settings['server']['secret_key'], url.encode())
    return '{0}?{1}'.format(
        url_for('image_proxy'),
        urlencode(dict(url=url.encode(), h=h)))
```

**六种分支**：

| # | 条件 | 输出 | 举例 |
|---|---|---|---|
| ① | `url` 为空/None | 原样返回空值 | `""` |
| ② | `url` 以 `//` 开头 | 先补 `https:`，然后继续判断 | `//x.com/a.png` → `https://x.com/a.png` |
| ③ | 用户偏好 `image_proxy=False` | 直连（不代理） | `https://x.com/a.png` 原样输出 |
| ④ | `data:image/{合法格式};base64,` | 原样放行 data URL | `data:image/png;base64,iVBOR...` |
| ⑤ | `data:` 但不符合白名单 | 返回 `None`（阻止） | `data:text/html,<script>...` |
| ⑥ | 其他所有情况（http/https URL） | `/image_proxy?url=<URL>&h=<HMAC>` | 带签名的代理 URL |

**④ 中合法 data: 格式白名单**（精确匹配）：
```
['gif', 'png', 'jpeg', 'pjpeg', 'webp', 'tiff', 'bmp']
```
且必须包含 `;base64,` 标记。非图片 `data:`（如 `data:text/html`、`data:application/javascript`）或非 base64 编码的图片均被阻止。

#### C.3 服务端代理校验

**源码位置**：[webapp.py:1002-L1071](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/webapp.py#L1002-L1071)

```python
@app.route('/image_proxy', methods=['GET'])
def image_proxy():
    url = sxng_request.args.get('url')
    if not url:                                   # 1. 参数存在性
        return '', 400
    if not is_hmac_of(secret_key, url.encode(), args.get('h', '')):
        return '', 400                            # 2. HMAC 签名
    maximum_size = 5 * 1024 * 1024                # 3. 5MB 上限
    resp, stream = http_stream('GET', url, ..., allow_redirects=True)
    if content_length and int(content_length) > maximum_size:
        return 'Max size', 400                    # 4. 大小限制
    if resp.status_code != 200:
        return '', 400                            # 5. 状态码必须 200
    ct = resp.headers.get('Content-Type', '')
    if not ct.startswith('image/') and not ct.startswith('binary/octet-stream'):
        return '', 400                            # 6. Content-Type 白名单
    # 通过所有校验 → 流式转发响应
```

**注意**：图片点击跳转的 `<a href="{{ result.img_src }}">`（见 [images.html:2](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/templates/simple/result_templates/images.html#L2) 和 [images.html:12](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/templates/simple/result_templates/images.html#L12)）**不经过图片代理**，直接直连原图 URL，仅 `<img>` 的 `src` 走代理。

---

### 路径 D：iframe 媒体延迟加载

**触发条件**：结果含有 `iframe_src` 字段（常见于视频结果）

#### D.1 模板层渲染

**源码位置**：[macros.html:72-79](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/templates/simple/macros.html#L72-L79)

```html
{% macro iframe(iframe_src) %}
  <iframe data-src="{{iframe_src}}" frameborder="0" allowfullscreen
    {% if result.parsed_url.hostname in ("www.youtube.com",) %}
    allow="picture-in-picture" referrerpolicy="origin"
    {% endif %}>
  </iframe>
{% endmacro %}
```

关键属性：
- **`data-src` 而非 `src`**：浏览器初始不会加载 iframe，需要 JS 端将 `data-src` 复制到 `src`
- **YouTube 特殊处理**：`allow="picture-in-picture"` 限制 iframe 权限，`referrerpolicy="origin"` 仅发送源站 Referer

#### D.2 触发加载 — 用户点击展开

在 [default.html:5-6](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/templates/simple/result_templates/default.html#L5-L6) 和 [videos.html:5-7](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/templates/simple/result_templates/videos.html#L5-L7) 中，iframe 被折叠在 `btn-collapse` 按钮之后：

```html
{% if result.iframe_src %}
<p class="altlink">
  <a class="btn-collapse collapsed media-loader disabled_if_nojs"
     data-target="#result-media-{{ index }}"
     data-btn-text-collapsed="{{ _('show media') }}"
     data-btn-text-not-collapsed="{{ _('hide media') }}">
     {{ icon_small('play') }} {{ _('show media') }}
  </a>
</p>
```

按钮带 `media-loader` class，由前端 JS 监听点击事件，点击后将对应 iframe 的 `data-src` → `src`，触发加载。

#### D.3 插件管道预处理

在 [_base.py:272](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/result_types/_base.py#L272) 的 URL 字段列表中，`iframe_src` 和 `audio_src` 与 `url`、`img_src` 同列，因此：
- Tracker URL Remover 会清理 iframe URL 中的追踪参数
- Hostnames 插件会对 iframe 主机名执行 replace/remove

---

### 路径 E：普通搜索结果直出

**触发条件**：所有未被路径 A/B 拦截的常规搜索结果

#### E.1 完整流程

```
ResultContainer.extend()
   │
   ├─ result.normalize_result_fields()
   │    └─ _normalize_url_fields(result)         # URL 规范化
   │
   ├─ self.on_result(result)                      # 插件 on_result 钩子
   │    ├─ tracker_url_remover.on_result()
   │    │    └─ result.filter_urls(clean_url)     # 清追踪参数（所有 URL 字段）
   │    ├─ hostnames.on_result()
   │    │    ├─ 主机名匹配 → return False (删除整个结果)
   │    │    ├─ result.filter_urls(replace/remove_host)
   │    │    └─ 设置 priority
   │    └─ oa_doi_rewrite.on_result()
   │         └─ result.filter_urls(rewrite_doi)   # 仅重写 url 字段
   │
   └─ 加入结果列表
```

调用顺序见 [results.py:83-133](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/results.py#L83-L133)：

```python
def extend(self, engine_name, results):
    for result in list(results):
        result.normalize_result_fields()            # 先规范化
        if not self.on_result(result):              # 再跑插件管道
            continue                                # 插件返回 False → 丢弃
        # 加入对应结果列表...
```

插件管道由 [SearchWithPlugins._on_result()](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/search/__init__.py#L198-L199) 触发：
```python
def _on_result(self, result):
    return searx.plugins.STORAGE.on_result(self.request, self, result)
```

#### E.2 filter_urls() 覆盖字段

**源码位置**：[_base.py:261-286](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/result_types/_base.py#L261-L286)

```python
def filter_urls(self, filter_func):
    # 覆盖字段列表：
    ["url", "iframe_src", "audio_src", "img_src", "thumbnail_src", "thumbnail"]
    # + Infobox 的 urls 列表
    # + Infobox attributes 中的 image.src
```

`filter_func` 返回值约定：
- `True` → 字段保持不变
- `False` → 删除该 URL（整个字段清空或结果被移除）
- `str` → 用返回值替换原 URL

#### E.3 HTML 渲染

**① 结果标题链接** — [macros.html:8-10](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/templates/simple/macros.html#L8-L10)

```html
{% macro result_open_link(url, classes='') %}
    <a href="{{ url }}"
       {% if classes %}class="{{ classes }}"{% endif %}
       {% if results_on_new_tab %}
           target="_blank" rel="noopener noreferrer"
       {% else %}
           rel="noreferrer"
       {% endif %}>
{% endmacro %}
```

`results_on_new_tab` 来自 [settings.yml:145](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/settings.yml#L145)（默认 `false`）。

**② URL 面包屑展示** — [macros.html:27-30](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/templates/simple/macros.html#L27-L30)

```html
<div class="url_wrapper">
  {% for part in get_pretty_url(result.parsed_url) %}
  <span class="url_o{{loop.index}}"><span class="url_i{{loop.index}}">{{ part }}</span></span>
  {% endfor %}
</div>
```

`get_pretty_url()` 格式由偏好 `url_formatting` 控制（[preferences.py:487-490](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/preferences.py#L487-L490)）：

| 值 | 输出（以 `https://example.com/foo/bar?q=1` 为例） |
|---|---|
| `full` | `["https://example.com/foo/bar?q=1"]` |
| `host` | `["example.com"]` |
| `pretty`（默认） | `["example.com", " › foo", " › bar"]` |

**③ 缓存链接** — [macros.html:48-54](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/templates/simple/macros.html#L48-L54)

```html
{{ icon_small('ellipsis-vertical')
   + result_link(cache_url + result.url, _('cached'), "cache_link") }}
```

`cache_url` 来自 [settings.yml:139-140](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/settings.yml#L139-L140)（默认空字符串，可配置为如 `"https://webcache.googleusercontent.com/search?q=cache:"`）。当为空时，`cache_url + result.url` 就是 `result.url` 本身。

**④ 磁力/种子链接** — [torrent.html:6-9](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/templates/simple/result_templates/torrent.html#L6-L9)

```html
<p class="altlink">
  {% if result.magnetlink %}
    {{ result_link(result.magnetlink, ...) }}
  {% endif %}
  {% if result.torrentfile %}
    {{ result_link(result.torrentfile, ...) }}
  {% endif %}
</p>
```

`magnet:` 协议链接不经过任何代理，直接通过 `result_link()` 渲染，带 `rel="noreferrer"` 保护。

---

### 路径 F：Favicon 代理中转

**触发条件**：偏好 `favicon_resolver != ""`

#### F.1 调用位置

**源码位置**：[macros.html:21-35](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/templates/simple/macros.html#L21-L35)

```html
{% macro result_header(result, favicons, image_proxify) %}
<article ...>
  {{ result_open_link(result.url, "url_header") }}
  {% if favicon_resolver != "" %}
  <div class="favicon">
    <img loading="lazy" src="{{ favicon_url(result.parsed_url.netloc) }}">
  </div>
  {% endif %}
  ...
```

传入参数是 `result.parsed_url.netloc`（主机名+端口，不含路径），而非完整 URL。

#### F.2 favicon_url() 决策树

**源码位置**：[favicons/proxy.py:195-237](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/favicons/proxy.py#L195-L237)

```python
def favicon_url(authority: str) -> str:
    resolver = sxng_request.preferences.get_value('favicon_resolver')
    if not resolver or resolver not in CFG.resolver_map.keys():
        return ""                            # ① resolver 未配置 → 空字符串

    data_mime = cache.CACHE(resolver, authority)  # 调用 FaviconCache.__call__()

    if data_mime == (None, None):
        # 缓存命中：已确认为 FALLBACK_ICON（无 favicon）
        return CFG.favicon_data_url(theme=theme)  # ② → data:image/svg+xml;utf8,...

    if data_mime is not None:
        # 缓存命中：有实际 favicon 数据
        data, mime = data_mime
        return f"data:{mime};base64,{base64}"    # ③ → data:image/xxx;base64,...

    # 缓存未命中 → 生成代理 URL
    h = new_hmac(CFG.secret_key, authority.encode())
    proxy_url = flask.url_for('favicon_proxy')
    query = urllib.parse.urlencode({"authority": authority, "h": h})
    return f"{proxy_url}?{query}"                  # ④ → 代理 URL
```

**缓存查询**：`cache.CACHE(resolver, authority)` 调用的是 [FaviconCacheSQLite.__call__()](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/favicons/cache.py#L320-L336)，其返回值语义：

| 返回值 | 含义 |
|---|---|
| `None` | 缓存未命中（无此 resolver+authority 记录） |
| `(None, None)` | 缓存命中，但标记为 `FALLBACK_ICON`（已确认无 favicon） |
| `(bytes_data, mime_str)` | 缓存命中，有实际 favicon 数据 |

`FALLBACK_ICON` 是 [cache.py](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/favicons/cache.py) 中的常量，表示"已查询但该域名无 favicon"。`cache.set()` 在 `data is None` 时将 `sha256` 设为 `FALLBACK_ICON`，查询时匹配到 `FALLBACK_ICON` 则返回 `(None, None)`。

**四种分支**：

| # | 条件 | 输出 | 举例 |
|---|---|---|---|
| ① | `resolver` 未配置或不在白名单 | `""`（空字符串，不渲染 favicon） | 偏好 `favicon_resolver=""` 时 |
| ② | 缓存命中 `(None, None)`（FALLBACK_ICON） | `data:image/svg+xml;utf8,...`（默认空 favicon SVG） | 主题对应的 `empty_favicon.svg` |
| ③ | 缓存命中 `(data, mime)` | `data:{mime};base64,{base64}` | `data:image/x-icon;base64,AAABAA...` |
| ④ | 缓存未命中 `None` | `/favicon_proxy?authority={host}&h={HMAC}` | 带签名的代理 URL |

**注意**：分支 ② 中 `CFG.favicon_data_url()` 返回的是 `data:image/svg+xml;utf8,{url_encoded_svg}` 格式（[proxy.py:107](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/favicons/proxy.py#L107)），编码方式为 `utf8` 而非 `base64`。分支 ③ 中缓存命中的 favicon 数据则使用 `base64` 编码。

这是一个**缓存优先策略**：首次加载走代理（④），代理成功后将 favicon 存入 SQLite 缓存（[cache.py:338-373](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/favicons/cache.py#L338-L373)），后续请求直接从缓存返回 data URL（③），不再产生额外网络请求。

#### F.3 服务端代理校验

**源码位置**：[favicons/proxy.py:112-157](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/favicons/proxy.py#L112-L157)

```python
def favicon_proxy():
    authority = sxng_request.args.get('authority')
    if not authority or "/" in authority:      # 1. authority 格式校验（不允许含 /）
        return '', 400
    if not is_hmac_of(CFG.secret_key, authority.encode(), args.get('h', '')):
        return '', 400                          # 2. HMAC 签名校验
    resolver = sxng_request.preferences.get_value('favicon_resolver')
    if not resolver or resolver not in CFG.resolver_map:
        return "", 400                          # 3. 解析器必须在白名单

    data, mime = search_favicon(resolver, authority)  # 查询 + 缓存

    if data is not None and mime is not None:
        resp = flask.Response(data, mimetype=mime)    # 4. 成功 → 直接返回图片数据
        resp.headers['Cache-Control'] = f"max-age={CFG.max_age}"
        return resp

    # 5. 降级：返回默认 SVG favicon 文件
    theme = sxng_request.preferences.get_value("theme")
    fav, mimetype = CFG.favicon(theme=theme)  # → (pathlib.Path, "image/svg+xml")
    return flask.send_from_directory(fav.parent, fav.name, mimetype=mimetype)
```

**降级路径说明**：`favicon_proxy()` 的降级是通过 `CFG.favicon()` 返回 `(pathlib.Path, str)` 元组（[proxy.py:86-91](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/favicons/proxy.py#L86-L91)），指向主题目录下的 `empty_favicon.svg` 文件，然后由 `send_from_directory()` 以 `image/svg+xml` 类型发送文件响应。**注意**：这是文件响应（`Content-Type: image/svg+xml`），不是 data URL——与 `favicon_url()` 中 `CFG.favicon_data_url()` 返回的 `data:image/svg+xml;utf8,...` 格式不同。

`favicon_url()` 在模板层有自己的降级链：
- resolver 未配置 → `""`（空字符串，不渲染 `<img>`）
- 缓存命中 FALLBACK_ICON → `CFG.favicon_data_url()` → `data:image/svg+xml;utf8,{svg}` （data URL 内嵌）
- 缓存未命中 → 代理 URL（由 `favicon_proxy()` 处理后续降级）

---

## 4. 无 scheme 地址的最终形态与降级路径

### 4.1 降级入口：_normalize_url_fields()

**源码位置**：[result_types/_base.py:38-L83](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/result_types/_base.py#L38-L83)

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
            scheme=result.parsed_url.scheme or "http",  # ← 降级核心
            path=result.parsed_url.path,
        )
        result.url = result.parsed_url.geturl()
```

**核心语句**：`scheme=result.parsed_url.scheme or "http"`

对于以下三处 URL，规则完全一致：
1. 主结果 `result.url`（第 52-54 行）
2. Infobox 的 `urls[]` 列表（第 68-70 行）
3. Infobox 的 `id` 字段（第 77-79 行）

### 4.2 urllib.parse.urlunparse() 的拼接逻辑（关键）

`_normalize_url_fields()` 最终通过 `parsed_url.geturl()` 回写 `result.url`，而 `geturl()` 内部调用的是 `urllib.parse.urlunparse()`。其拼接逻辑如下（CPython 源码）：

```python
def urlunparse(components):
    scheme, netloc, url, params, query, fragment = components
    if params:    url = "%s;%s" % (url, params)
    if query:     url = "%s?%s" % (url, query)
    if fragment:  url = "%s#%s" % (url, fragment)
    if netloc:    url = '//' + netloc + url     # ← 仅当 netloc 非空时加 //
    if scheme:    url = scheme + ':' + url
    return url
```

**关键**：`//` 前缀仅在 `netloc` 非空时才添加。当 `urlparse()` 因输入无 `//` 前缀而将域名放入 `path` 时，`netloc` 为空，`urlunparse()` 不会加 `//`。

`urllib.parse.urlparse("example.com/path?q=1")` 的解析结果：
```
ParseResult(
    scheme='',          # 空字符串 → 或运算后变成 "http"
    netloc='',          # 空！没有 // 前缀时，urlparse 把整段当作 path
    path='example.com/path',
    params='',
    query='q=1',
    fragment=''
)
```

### 4.3 七类典型输入的最终形态（已通过 CPython urlunparse 源码验证）

| 输入（原始 URL） | `urlparse()` 关键字段 | `_replace(scheme or "http")` 后 | `geturl()` 最终输出 | 是否正常 |
|---|---|---|---|---|
| `"example.com"` | `scheme=""`, `netloc=""`, `path="example.com"` | `scheme="http"`, **`netloc=""`** | **`http:example.com`** | ❌ 畸形 URL |
| `"example.com/path?q=1"` | `scheme=""`, `netloc=""`, `path="example.com/path"`, `query="q=1"` | `scheme="http"`, **`netloc=""`** | **`http:example.com/path?q=1`** | ❌ 畸形 URL |
| `"//example.com/path"` | `scheme=""`, **`netloc="example.com"`**, `path="/path"` | `scheme="http"` | `http://example.com/path` | ✅ |
| `"ftp://example.com/file"` | `scheme="ftp"`, `netloc="example.com"` | `scheme="ftp"`（不变） | `ftp://example.com/file` | ✅ |
| `"magnet:?xt=urn:btih:abc"` | `scheme="magnet"`, `netloc=""`, `path=""`, `query="xt=urn:btih:abc"` | `scheme="magnet"`（不变） | `magnet:?xt=urn:btih:abc` | ✅ |
| `"javascript:alert(1)"` | `scheme="javascript"`, `netloc=""`, `path="alert(1)"` | `scheme="javascript"`（不变） | `javascript:alert(1)` | ⚠️ 危险协议保留 |
| `"data:image/png;base64,abc"` | `scheme="data"`, `netloc=""`, `path="image/png;base64,abc"` | `scheme="data"`（不变） | `data:image/png;base64,abc` | ✅ |

**关键纠正**：
- `example.com` 经过 `_normalize_url_fields()` 后变为 **`http:example.com`** 而非 `http://example.com`。因为 `urlparse("example.com")` 将其放入 `path` 字段，`netloc` 为空，`urlunparse()` 不加 `//`。结果是 **畸形 URL**。
- 同理，`example.com/path?q=1` 变为 **`http:example.com/path?q=1`**。
- 只有带 `//` 前缀的输入（如 `//example.com`）才能使 `netloc` 非空，从而生成正确的 `http://example.com`。
- `magnet:` 和 `javascript:` 等 scheme 非空的 URL 不受 `scheme or "http"` 影响，原样保留。

### 4.4 其他位置的无 scheme 处理

| 位置 | 处理方式 | 最终 scheme |
|---|---|---|
| `_normalize_url_fields()` 主/infobox URL | `scheme or "http"` | `http` |
| `utils.normalize_url()` 中 `//xxx` | `base_url.scheme or 'http'` | 与 base_url 一致或 `http` |
| `utils.normalize_url()` 中 `/path` | `urljoin(base_url, ...)` | 继承 base_url 的 scheme |
| `webapp.image_proxify()` 中 `//xxx` | 硬编码 `'https:'` | `https` |
| `external_bang.resolve_bang_definition()` 中 `//xxx` | 硬编码 `'https:'` | `https` |

**不一致性**：`_normalize_url_fields()` 对 `//xxx` 补 `http`，但 `image_proxify()` 和 `resolve_bang_definition()` 对 `//xxx` 补 `https`。三处实现不统一。

### 4.5 合并阶段的 HTTPS 升级

**源码位置**：[results.py:377-L381](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/results.py#L377-L381)

```python
# merge_two_main_results() 中
if origin.parsed_url and not origin.parsed_url.scheme.endswith("s"):
    # 当前 scheme 不以 s 结尾（http, ftp）
    if other.parsed_url and other.parsed_url.scheme.endswith("s"):
        # 另一来源有安全 scheme（https, ftps）
        origin.parsed_url = origin.parsed_url._replace(scheme=other.parsed_url.scheme)
        origin.url = origin.parsed_url.geturl()
```

这意味着：**即使在规范化阶段降级为 `http`，如果同一结果的另一引擎返回了 `https` URL，最终结果仍会被升级为 `https`**。

最终的降级-升级路径（以 `example.com` 为例）：
```
example.com
   │ urlparse("example.com")
   ▼
scheme="", netloc="", path="example.com"
   │ _replace(scheme=scheme or "http")
   ▼
scheme="http", netloc="", path="example.com"
   │ geturl() → urlunparse
   ▼
http:example.com                  ← 畸形 URL！（缺少 //）
   │ （如有另一引擎返回 https://example.com）
   ▼ merge_two_main_results: endswith("s") 升级
https:example.com                 ← 仍然畸形！
```

**注意**：`merge_two_main_results()` 只替换 `scheme`，不修复 `netloc` 为空的畸形结构。因此即使升级到 `https`，URL 仍然是 `https:example.com` 而非 `https://example.com`。

---

## 5. 特殊协议处理

### 5.1 `//` 协议相对 URL

| 位置 | 处理方式 | 代码 |
|---|---|---|
| `_normalize_url_fields()` | `or "http"` → `http://` | [_base.py:54](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/result_types/_base.py#L54) |
| `utils.normalize_url()` | 继承 base_url scheme，兜底 `http` | [utils.py:284-286](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/utils.py#L284-L286) |
| `webapp.image_proxify()` | 硬编码 `https:` | [webapp.py:293-294](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/webapp.py#L293-L294) |
| `external_bang.resolve_bang_definition()` | 硬编码 `https:` | [external_bang.py:51-52](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/external_bang.py#L51-L52) |

### 5.2 `data:` 协议

仅在 `image_proxify()` 中有显式处理（见路径 C 第 ④⑤ 分支）：
- `data:image/{gif,png,jpeg,pjpeg,webp,tiff,bmp};base64,...` → 放行
- 其他所有 `data:`（含非图片类型、非 base64 编码）→ 返回 `None` 阻止

其他代码路径中 `data:` URL 没有特殊处理，按普通字符串传递。

### 5.3 `magnet:` 协议

**关键事实**：`magnetlink` 和 `torrentfile` **不经过任何规范化或 URL 过滤**。

理由如下：

1. **`_normalize_url_fields()`** 仅处理 `result.url` 字段（以及 infobox 的 `urls` 和 `id`）。`magnetlink` 和 `torrentfile` 是与 `url` 平行的独立字段，不在处理范围内。

2. **`_filter_urls()`** 仅处理以下字段列表（[result_types/_base.py:119](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/result_types/_base.py#L119)）：
   ```python
   url_fields = ["url", "iframe_src", "audio_src", "img_src", "thumbnail_src", "thumbnail"]
   ```
   `magnetlink` 和 `torrentfile` **不在该列表中**，因此 Tracker URL Remover 和 Hostnames 插件 **不会** 对其执行去追踪或主机重写。

3. 在模板中（[torrent.html:6-9](file:///d:/fz/0601-1/solo-dogfeeding/code/15-searxng/searx/templates/simple/result_templates/torrent.html#L6-L9)），它们通过 `result_link()` 宏直接渲染：
   ```html
   {% if result.magnetlink %}
     {{ result_link(result.magnetlink, ...) }}
   {% endif %}
   {% if result.torrentfile %}
     {{ result_link(result.torrentfile, ...) }}
   {% endif %}
   ```
   带有 `rel="noreferrer"` 保护，但 URL 本身是原始值。

4. `magnet:` scheme 在 `_normalize_url_fields()` 中不受影响，因为 `urlparse("magnet:?xt=...")` 的 `scheme="magnet"` 非空，`scheme or "http"` 不会改写它。

**结论**：magnet 和 torrent 链接从引擎返回到用户点击，中间 **不经过任何 URL 规范化、去追踪或主机重写处理**，是"直通"渲染。

### 5.4 `javascript:` / `vbscript:` / `data:`（非图片）等危险协议

代码中 **没有显式 scheme 黑名单**。但以下机制提供间接防护：

| 机制 | 防护原理 |
|---|---|
| 引擎代码约束 | 绝大多数搜索引擎只返回 http/https URL，恶意 scheme 很难进入结果集 |
| `urlparse()` 行为 | `javascript:alert(1)` 被解析为 `scheme="javascript"`, `path="alert(1)"` |
| Hostnames 插件 | 对 netloc 为空的 URL（如 `javascript:` 的 netloc 为空字符串），正则通常不匹配，不执行 replace，但也不阻止 |
| Tracker URL Remover | 同理，正则通常只匹配正常互联网域名，不匹配 `javascript:` |
| `normalize_url()` | 含 `'://' not in url` 检查，但 `javascript:alert(1)` 不含 `://`，会走 `urljoin(base_url, ...)` → 变为 `base_url + javascript:alert(1)`，实际上仍然异常 |

---

## 6. 完整安全机制矩阵

| 分流路径 | Referrer 保护 | 代理中转 | HMAC 签名 | 延迟加载 | 参数清洗 | 主机重写 |
|---|---|---|---|---|---|---|
| **A: Bang 302** | —（浏览器自身行为） | ✗ | ✗ | ✗ | ✓（quote_plus） | ✗ |
| **B: 首结果 302** | — | ✗ | ✗ | ✗ | ✓（插件管道已执行） | ✓（插件管道已执行） |
| **C: 图片 `<img>`** | — | ✓ `/image_proxy` | ✓ | ✓ `loading="lazy"` | ✓（filter_urls） | ✓（filter_urls） |
| **C: 图片 `<a>` 跳转** | ✓ `rel="noreferrer"` | ✗（直连） | ✗ | ✗ | ✓ | ✓ |
| **D: iframe 媒体** | YouTube 特供 `referrerpolicy="origin"` | ✗ | ✗ | ✓ `data-src` + 点击触发 | ✓（filter_urls） | ✓（filter_urls） |
| **E: 普通结果** | ✓ `rel="noreferrer"` / `rel="noopener noreferrer"` | ✗ | ✗ | ✗ | ✓ | ✓ |
| **F: Favicon** | — | ✓ `/favicon_proxy` | ✓ | ✓ `loading="lazy"` | ✗ | ✗（仅传 authority） |

---

## 7. 潜在改进建议

1. **修复无 scheme URL 生成畸形 URL 的问题**：`_normalize_url_fields()` 对 `example.com` 这类无 `//` 前缀的 URL 生成 `http:example.com`（缺少 `//`），这是一个 bug。应在 `scheme or "http"` 补全后检查 `netloc` 是否为空，若为空则尝试用 `urllib.parse.urlparse("http://" + original_url)` 重新解析，确保生成合法的 `http://example.com`。

2. **scheme 显式白名单**：在 `_normalize_url_fields()` 中增加 scheme 白名单校验（仅允许 `http`、`https`、`ftp`、`ftps`、`magnet`），其他 scheme（`javascript:`、`vbscript:`、`data:` 等）直接清空 URL，杜绝间接防护的盲区。

3. **magnet/torrent 链接纳入 filter_urls**：当前 `magnetlink` 和 `torrentfile` 不在 `_filter_urls()` 的 `url_fields` 列表中，完全绕过了去追踪和主机重写。建议将它们加入 `url_fields` 或单独处理。

4. **统一 `//` 协议处理**：当前三处 `//` 处理逻辑不一致（两处补 `https`，一处补 `http`）。建议统一为 `https`，与现代 Web 实际情况一致。

5. **规范化阶段默认 HTTPS**：无 scheme URL 当前降级为 `http`，可考虑改为 `https` 或通过 `settings.yml` 配置项控制。

6. **图片代理重定向限制**：`image_proxy()` 使用 `allow_redirects=True` 但未限制重定向次数和内网目标，建议增加最大重定向次数（如 3 次）并限制目标为公网 IP。

7. **代理 HMAC 有效期**：当前 HMAC 无时间戳，签名永久有效。建议在 HMAC 输入中加入时间戳，服务端校验时限制有效期（如 24 小时）。

8. **Content-Security-Policy 头**：建议在渲染结果页时设置严格的 CSP（如 `default-src 'self'`、`img-src 'self' data:`、`frame-src *`），从浏览器层面进一步约束外链行为。
