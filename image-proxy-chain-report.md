# SearXNG 图片代理链路分析报告

## 一、为什么外部图片要经代理转发而非直接加载

SearXNG 搜索结果中的外部图片经代理转发，核心目的是 **保护用户隐私**：

1. **防止图片源追踪用户**：若浏览器直接加载外部图片（如 `https://example.com/img.jpg`），图片服务器可通过 HTTP 请求获取用户的 IP 地址、浏览器 User-Agent、Referer（包含搜索关键词）等敏感信息。通过 SearXNG 代理转发后，图片源服务器只能看到 SearXNG 实例的 IP，无法触达用户真实身份。

2. **避免搜索意图泄露**：直接加载图片时，`Referer` 头会暴露用户当前所在的搜索结果页，进而泄露搜索意图。代理在请求外部图片时使用独立的请求头，完全剥离原始搜索上下文。

3. **防止混合内容与安全风险**：代理对响应内容进行严格的类型校验（仅允许 `image/*` 和 `binary/octet-stream`），防止恶意图片源返回 HTML/JavaScript 等危险内容，在用户浏览器中执行 XSS 攻击。

4. **统一流量出口**：SearXNG 实例可配置 Tor 代理或出站代理，所有图片请求都通过同一网络出口，进一步匿名化流量来源。

---

## 二、图片代理完整链路

### 2.1 配置层：开关控制

图片代理功能通过 `image_proxy` 配置项控制，默认值为 `False`（关闭）。

**默认配置定义** — [settings_defaults.py](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/settings_defaults.py#L217)

```python
'image_proxy': SettingsValue(bool, False, 'SEARXNG_IMAGE_PROXY'),
```

可通过环境变量 `SEARXNG_IMAGE_PROXY` 或 `settings.yml` 中的 `server.image_proxy` 覆盖。

**用户偏好绑定** — [preferences.py](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/preferences.py#L430-L433)

```python
'image_proxy': BooleanSetting(
    settings['server']['image_proxy'],
    locked=is_locked('image_proxy')
),
```

`image_proxy` 是一个 `BooleanSetting`，用户可在偏好设置页面切换，也可被管理员锁定。偏好值通过 Cookie 持久化。

### 2.2 模板层：URL 生成

SearXNG 将 `image_proxify` 函数注入到所有 Jinja2 模板中，模板通过调用该函数将外部图片 URL 转换为代理 URL。

**注入点** — [webapp.py](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/webapp.py#L436)

```python
kwargs['image_proxify'] = image_proxify
```

**`image_proxify` 函数核心逻辑** — [webapp.py](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/webapp.py#L298-L321)

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

该函数执行以下处理：

| 步骤 | 检查内容 | 处理方式 |
|------|----------|----------|
| 1 | URL 为空 | 直接返回空 |
| 2 | `//` 开头的协议相对 URL | 补全为 `https:` |
| 3 | 用户未开启 image_proxy | 直接返回原始 URL（不代理） |
| 4 | `data:image/` 内嵌图片 | 校验 MIME 类型和 base64 编码，合法则原样返回，否则返回 `None`（阻断异常 data URI） |
| 5 | 外部 HTTP(S) URL | 计算 HMAC 签名，生成代理 URL |

生成的代理 URL 格式为：

```
/image_proxy?url=<编码后的原始URL>&h=<HMAC-SHA256签名>
```

### 2.3 HMAC 签名机制

**签名生成** — [webutils.py](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/webutils.py#L212-L213)

```python
def new_hmac(secret_key, url):
    return hmac.new(secret_key.encode(), url, hashlib.sha256).hexdigest()
```

**签名校验** — [webutils.py](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/webutils.py#L216-L218)

```python
def is_hmac_of(secret_key, value, hmac_to_check):
    hmac_of_value = new_hmac(secret_key, value)
    return len(hmac_of_value) == len(hmac_to_check) and hmac.compare_digest(hmac_of_value, hmac_to_check)
```

HMAC 机制的作用：
- **防篡改**：攻击者无法伪造有效的 `(url, h)` 参数对，因为不知道 `secret_key`
- **防 SSRF 滥用**：外部用户无法让图片代理访问任意 URL，只能访问 SearXNG 模板签名过的 URL
- **使用 `hmac.compare_digest`**：时序安全比较，防止时序侧信道攻击

### 2.4 模板调用点

图片代理在以下模板位置被调用：

**图片搜索结果** — [images.html](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/templates/simple/result_templates/images.html#L3)

- 缩略图：`image_proxify(result.thumbnail_src)` 或 `image_proxify(result.img_src)`
- 详情大图（懒加载）：`image_proxify(result.img_src)`（放在 `data-src` 属性中）

**通用结果缩略图** — [macros.html](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/templates/simple/macros.html#L33)

- 所有带 `result.thumbnail` 的结果模板（videos、map、torrent 等）均通过 `result_header` 宏调用 `image_proxify(result.thumbnail)`

**信息框** — [infobox.html](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/templates/simple/elements/infobox.html#L3)

- 信息框主图和属性中的图片

### 2.5 代理端点：`/image_proxy`

**路由定义** — [webapp.py](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/webapp.py#L1002-L1071)

```python
@app.route('/image_proxy', methods=['GET'])
def image_proxy():
```

这是代理链路的核心处理函数，完整处理流程如下：

#### 步骤 1：参数提取与 HMAC 校验

```python
url = sxng_request.args.get('url')
if not url:
    return '', 400

if not is_hmac_of(settings['server']['secret_key'], url.encode(), sxng_request.args.get('h', '')):
    return '', 400
```

- 缺少 `url` 参数 → 400
- HMAC 签名不匹配 → 400（防篡改/防滥用）

#### 步骤 2：构建安全请求头

```python
request_headers = {
    'User-Agent': gen_useragent(),
    'Accept': 'image/webp,*/*',
    'Sec-GPC': '1',
    'DNT': '1',
}
```

- `User-Agent`：使用通用 UA，不暴露 SearXNG 身份
- `Accept`：声明接受 webp 和其他格式
- `Sec-GPC: 1`：Global Privacy Control 信号
- `DNT: 1`：Do Not Track 信号

#### 步骤 3：通过专用网络发起流式请求

```python
set_context_network_name('image_proxy')
resp, stream = http_stream(method='GET', url=url, headers=request_headers, allow_redirects=True)
```

使用专用的 `image_proxy` 网络实例 — [network.py](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/network/network.py#L414-L420)

```python
if 'image_proxy' not in NETWORKS:
    image_proxy_params = default_params.copy()
    image_proxy_params['enable_http2'] = False
    NETWORKS['image_proxy'] = new_network(image_proxy_params, logger_name='image_proxy')
```

专用网络的特殊配置：
- **禁用 HTTP/2**：降低 CPU 负载，总耗时基本不变
- 与默认网络共享其他参数（代理、验证、重试等）

#### 步骤 4：响应大小检查

```python
maximum_size = 5 * 1024 * 1024  # 5MB
content_length = resp.headers.get('Content-Length')
if content_length and content_length.isdigit() and int(content_length) > maximum_size:
    return 'Max size', 400
```

- 图片大小上限 5MB
- 通过 `Content-Length` 头提前拦截超大文件

#### 步骤 5：HTTP 状态码检查

```python
if resp.status_code != 200:
    if resp.status_code >= 400:
        return '', resp.status_code
    return '', 400
```

- 非 200 状态码一律拒绝
- 4xx/5xx 错误原样转发状态码
- 3xx（重定向）和其他非 200 返回 400

#### 步骤 6：Content-Type 类型检查（核心安全关卡）

```python
if not resp.headers.get('Content-Type', '').startswith('image/') and not resp.headers.get(
    'Content-Type', ''
).startswith('binary/octet-stream'):
    return '', 400
```

- **仅允许 `image/*` MIME 类型**：如 `image/jpeg`、`image/png`、`image/webp` 等
- **额外允许 `binary/octet-stream`**：部分图片源返回此通用类型
- 拒绝 `text/html`、`application/javascript` 等危险类型，**从根本上阻断 XSS 攻击向量**

#### 步骤 7：流式转发响应

```python
headers = dict_subset(resp.headers, {'Content-Type', 'Content-Encoding', 'Content-Length', 'Length'})
response = Response(stream, mimetype=resp.headers['Content-Type'], headers=headers, direct_passthrough=True)
response.call_on_close(close_stream)
return response
```

- 仅转发 4 个安全头：`Content-Type`、`Content-Encoding`、`Content-Length`、`Length`
- 使用 `direct_passthrough=True` 流式传输，避免将整个图片加载到内存
- 注册 `close_stream` 回调，确保连接在响应关闭时正确释放

#### 步骤 8：异常处理与资源清理

```python
except httpx.HTTPError:
    return '', 400
finally:
    if resp and not forward_resp:
        try:
            resp.close()
        except httpx.HTTPError:
            pass
```

- 任何 HTTP 异常返回 400
- 在 `finally` 中确保未转发的响应被关闭，防止连接泄漏

### 2.6 robots.txt 禁止爬取

[webapp.py](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/webapp.py#L1193)

```
Disallow: /image_proxy
```

搜索引擎爬虫被禁止索引代理端点。

---

## 三、保护检查汇总

| 检查环节 | 保护目标 | 具体措施 | 失败返回 |
|----------|----------|----------|----------|
| URL 为空 | 防止空请求 | `if not url` | 400 |
| HMAC 签名校验 | 防篡改、防 SSRF | `is_hmac_of(secret_key, url, h)` | 400 |
| data URI 白名单 | 仅允许合法内嵌图片 | 校验 MIME（gif/png/jpeg/webp/tiff/bmp）和 base64 格式 | 返回 `None`（不渲染） |
| Content-Length 上限 | 防止大文件 DoS | 5MB 限制 | 400 |
| HTTP 状态码 | 确保成功响应 | 仅接受 200 | 400 或原状态码 |
| Content-Type 白名单 | 防止 XSS/内容注入 | 仅允许 `image/*` 和 `binary/octet-stream` | 400 |
| 响应头过滤 | 防止头注入 | 仅转发 Content-Type/Encoding/Length | — |
| 专用网络实例 | 隔离与性能 | 禁用 HTTP/2、独立代理配置 | — |
| 安全请求头 | 保护用户隐私 | DNT、Sec-GPC、通用 UA | — |
| 时序安全比较 | 防止时序攻击 | `hmac.compare_digest` | — |
| robots.txt | 防止索引 | Disallow /image_proxy | — |

---

## 四、完整调用链路图

```
搜索引擎返回结果 (img_src / thumbnail_src)
    │
    ▼
Jinja2 模板调用 image_proxify(url)
    │
    ├── URL 为空 → 返回空
    ├── 协议相对 URL → 补全 https:
    ├── image_proxy 未开启 → 返回原始 URL
    ├── data:image/ URI → 白名单校验 MIME + base64
    │   ├── 合法 → 原样返回
    │   └── 非法 → 返回 None
    └── 外部 URL → 计算 HMAC，生成代理 URL
        │
        ▼
浏览器请求 /image_proxy?url=...&h=...
    │
    ▼
Flask 路由 image_proxy()
    │
    ├── 缺少 url 参数 → 400
    ├── HMAC 校验失败 → 400
    │
    ▼
使用 image_proxy 专用网络实例发起流式 HTTP 请求
    │
    ├── Content-Length > 5MB → 400
    ├── 状态码 ≠ 200 → 400 / 原状态码
    ├── Content-Type 非 image/* 且非 binary/octet-stream → 400
    │
    ▼
过滤响应头，流式转发图片数据给浏览器
    │
    ▼
浏览器渲染图片
```
