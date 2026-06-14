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

## 三、内容安全策略（CSP）的限定机制

### 3.1 CSP 不是默认配置，但支持自定义

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

### 3.2 脚本加载来源的限定

SearXNG 的脚本加载架构决定了 CSP `script-src` 指令的限定范围：

**脚本加载方式**：
1. **主入口脚本**：通过 `<script type="module" src=".../sxng-core.min.js">` 静态加载（来自 [base.html#L13](file:///d:/fz/0601-1/solo-dogfeeding/code/77-searxng/searx/templates/simple/base.html#L13)）
2. **动态模块加载**：通过 ES Module `import()` 动态按需加载功能模块（如地图、计算器、无限滚动等）
3. **模块预加载**：Vite 的预加载机制通过动态创建 `<link rel="modulepreload">` 标签预加载依赖

**如果启用 CSP，`script-src` 需要至少允许**：
- `'self'`：同域静态资源目录下的所有 JS 文件
- 如果使用 nonce 策略：`'nonce-<随机值>'`（需配合模板层注入 nonce 到 meta 标签）

### 3.3 资源加载来源的限定

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
- 动态加载的 CSS 也通过 nonce 机制支持 CSP

---

## 四、其他安全相关的头部与机制

### 4.1 `Server-Timing` 性能头（非安全类）

**位置**：[webapp.py#L531-L550](file:///d:/fz/0601-1/solo-dogfeeding/code/77-searxng/searx/webapp.py#L531-L550)

另一个 `after_request` 钩子 `post_request` 负责添加 `Server-Timing` 头，用于性能监控，不直接影响安全。

### 4.2 Cookie 安全属性

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

### 4.3 密钥安全检查

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
- `secret_key` 用于 HMAC 签名（如图片代理的 URL 签名）和会话管理

---

## 五、完整的安全头部装配链路

```
                     +-----------------------+
                     |  settings.yml 配置    |
                     |  default_http_headers |
                     +-----------+-----------+
                                 |
                                 | 配置读取
                                 v
        +------------------------+------------------------+
        |                                                 |
        v                                                 v
+-------+-------+         +--------------+       +-------+-------+
|  Flask 层     |         |  WSGI 中间件层 |       |  WhiteNoise  |
|  after_request|         |  ProxyFix    |       |  静态资源     |
|  钩子         |         +--------------+       +-------+-------+
+-------+-------+                ^                        |
        |                        |                        |
        |  所有动态响应           |  反向代理路径修正      | static_headers
        v                        |                        v
+-------+-------+       +--------+---------+      +-------+-------+
|  HTML 页面    |       |   app.wsgi_app   |      |  JS / CSS /   |
|  JSON API     |       +------------------+      |  图片等静态   |
|  RSS / CSV    |                                 +---------------+
|  图片代理     |
+---------------+
```

**关键设计原则**：

1. **配置驱动**：所有安全头集中在 `default_http_headers` 配置项，管理员可以自由增减。
2. **双路径覆盖**：动态响应走 Flask `after_request`，静态资源走 WhiteNoise 中间件，两者互不干扰但策略一致。
3. **不覆盖原则**：动态响应中已存在的头不会被全局配置覆盖，允许路由级定制。
4. **可扩展性**：CSP 等高级安全策略可通过配置启用，前端已预留 nonce 支持。

---

## 六、管理员自定义 CSP 的建议配置

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
