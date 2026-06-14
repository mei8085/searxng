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

### 3.3 图片代理端点：`/image_proxy`

**代码位置**：[webapp.py#L1002-L1071](file:///d:/fz/0601-1/solo-dogfeeding/code/77-searxng/searx/webapp.py#L1002-L1071)

```python
@app.route('/image_proxy', methods=['GET'])
def image_proxy():
    # ... HMAC 校验、请求上游 ...
    
    try:
        headers = dict_subset(resp.headers, {'Content-Type', 'Content-Encoding', 'Content-Length', 'Length'})
        response = Response(stream, mimetype=resp.headers['Content-Type'], headers=headers, direct_passthrough=True)
        response.call_on_close(close_stream)
        return response
    except httpx.HTTPError:
        close_stream()
        return '', 400
```

**逐段解析**：

- **旁路性质**：图片代理将第三方图片通过 SearXNG 中转，保护用户隐私。
- **透传上游头部**：通过 `dict_subset(resp.headers, {'Content-Type', 'Content-Encoding', 'Content-Length', 'Length'})` 从上游响应中提取指定头部并透传给客户端。
- **不设置 `Cache-Control`**：图片代理本身不设置缓存控制头，完全依赖上游返回的头部信息。
- **与统一注入的共存**：由于透传的头部仅限于 `Content-Type`、`Content-Encoding`、`Content-Length`、`Length`，与 `default_http_headers` 中的安全头（`X-Content-Type-Options`、`Referrer-Policy` 等）不重叠，因此所有安全头都会被 `after_request` 钩子正常注入。

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
|  图片/fav 代理|
|  限流429/302  |
+---------------+

旁路（视图层直接设头）：
  /client<token>.css → Cache-Control: no-store
  /favicon_proxy    → Cache-Control: max-age=N
  /image_proxy      → 透传 Content-Type 等
  限流 302 重定向   → Cache-Control: no-store
  （以上旁路的安全头仍由 after_request 注入）
```

**关键设计原则**：

1. **配置驱动**：所有安全头集中在 `default_http_headers` 配置项，管理员可以自由增减。
2. **双路径覆盖**：动态响应走 Flask `after_request`，静态资源走 WhiteNoise 中间件，两者互不干扰但策略一致。
3. **不覆盖原则**：动态响应中已存在的头不会被全局配置覆盖，允许路由级定制。
4. **before_request 不绕过**：限流等 `before_request` 拦截返回的响应仍然经过 `after_request`，安全头不丢失。
5. **可扩展性**：CSP 等高级安全策略可通过配置启用，前端已预留 nonce 支持（但后端链路不完整）。

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
