# 主流商业搜索引擎适配层差异分析报告

## 1. 概述

本报告基于 SearXNG 元搜索引擎的实现，深入分析 Google、Bing、DuckDuckGo、Yahoo 四大主流商业搜索引擎在适配层上的差异。报告覆盖请求拼装、响应解析、错误识别三个核心层面，探讨适配框架如何抽象统一这些差异，并分析其对结果归并、限流、机器人检测的影响。

## 2. 适配层架构总览

SearXNG 采用"处理器-引擎"双层适配架构：

```
┌─────────────────────────────────────────────────────────┐
│              EngineProcessor (抽象层)                   │
├─────────────────────────────────────────────────────────┤
│  OnlineProcessor / OfflineProcessor                     │
│  - 参数标准化                                           │
│  - 异常统一处理                                         │
│  - 限流与熔断机制                                       │
│  - 指标收集                                             │
└─────────────────────┬───────────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────────┐
│              引擎适配层 (每个引擎独立实现)               │
├─────────────────────────────────────────────────────────┤
│  request(query, params)  → 拼装请求                     │
│  response(resp)         → 解析响应                     │
│  fetch_traits()         → 获取引擎特性                 │
└─────────────────────────────────────────────────────────┘
```

## 3. 请求拼装层面差异分析

### 3.1 HTTP 方法差异

| 搜索引擎 | 方法 | 实现位置 | 说明 |
|---------|------|---------|------|
| Google | GET | [google.py:304-346](file:///d:/fz/0508-2/solo-dogfeeding/code/26-searxng/searx/engines/google.py#L304-L346) | 所有请求使用 GET |
| Bing | GET | [bing.py:97-119](file:///d:/fz/0508-2/solo-dogfeeding/code/26-searxng/searx/engines/bing.py#L97-L119) | 所有请求使用 GET |
| DuckDuckGo | POST | [duckduckgo.py:361-456](file:///d:/fz/0508-2/solo-dogfeeding/code/26-searxng/searx/engines/duckduckgo.py#L361-L456) | 使用 POST 提交表单数据 |
| Yahoo | GET | [yahoo.py:144-194](file:///d:/fz/0508-2/solo-dogfeeding/code/26-searxng/searx/engines/yahoo.py#L144-L194) | 所有请求使用 GET |

**差异点：**
- DuckDuckGo 采用 POST 方法，通过表单提交搜索参数，其他引擎均使用 GET
- DuckDuckGo 需要特殊的 Content-Type 头：`application/x-www-form-urlencoded`

### 3.2 语言/区域参数映射

#### Google 的多维度本地化策略

Google 使用最复杂的本地化参数体系，包含 5 个核心参数：

```python
# google.py:199-256
ret_val["params"]["hl"] = f"{lang_code}-{country}"  # 界面语言
ret_val["params"]["lr"] = eng_lang                    # 结果语言限制
ret_val["params"]["cr"] = "country" + country         # 结果国家限制
ret_val["params"]["ie"] = "utf8"                       # 输入编码
ret_val["params"]["oe"] = "utf8"                       # 输出编码
```

此外，Google 还会根据地区选择不同的子域名：
```python
ret_val["subdomain"] = eng_traits.custom["supported_domains"].get(country.upper(), "www.google.com")
```

#### Bing 的简化策略

Bing 仅使用单一 `mkt` 参数：
```python
# bing.py:55-73
def get_locale_params(engine_region):
    if not engine_region or engine_region == "clear":
        return None
    return {"mkt": engine_region}
```

同时通过 `Accept-Language` 头辅助本地化：
```python
# bing.py:75-94
params["headers"]["Accept-Language"] = f"{engine_region},{lang};q=0.9"
```

#### DuckDuckGo 的表单参数

DuckDuckGo 通过表单字段 `kl` 和 Cookie 传递语言信息：
```python
# duckduckgo.py:437-448
data["kl"] = eng_region
params["cookies"]["kl"] = eng_region
params["cookies"]["df"] = t_range  # 时间范围也通过 Cookie 传递
```

#### Yahoo 的域名映射策略

Yahoo 通过不同域名实现本地化：
```python
# yahoo.py:40-120
region2domain = {
    "DE": "de.search.yahoo.com",
    "FR": "fr.search.yahoo.com",
    # ...
}
lang2domain = {
    'zh_chs': 'hk.search.yahoo.com',
    'zh_cht': 'tw.search.yahoo.com',
    # ...
}
```

### 3.3 分页参数差异

| 搜索引擎 | 参数名 | 计算方式 | 实现位置 |
|---------|--------|---------|---------|
| Google | start | `(pageno - 1) * 10` | [google.py:307](file:///d:/fz/0508-2/solo-dogfeeding/code/26-searxng/searx/engines/google.py#L307) |
| Bing | 不支持 | - | - |
| DuckDuckGo | s, dc | 首页: `b=""`，后续页: `s=offset` | [duckduckgo.py:405-435](file:///d:/fz/0508-2/solo-dogfeeding/code/26-searxng/searx/engines/duckduckgo.py#L405-L435) |
| Yahoo | b | `pageno * 7 + 1` | [yahoo.py:165](file:///d:/fz/0508-2/solo-dogfeeding/code/26-searxng/searx/engines/yahoo.py#L165) |

**特殊说明：**
- DuckDuckGo 分页需要 `vqd` 令牌（Validation Query Digest），首次请求时从响应中提取，后续请求必须携带
- Bing 不支持分页，因为其分页依赖 JavaScript
- DuckDuckGo 第二页起的偏移量计算：`10 + (pageno - 2) * 15`

### 3.4 安全搜索参数映射

| 搜索引擎 | 0 (关闭) | 1 (中等) | 2 (严格) | 实现位置 |
|---------|---------|---------|---------|---------|
| Google | off | medium | high | [google.py:65](file:///d:/fz/0508-2/solo-dogfeeding/code/26-searxng/searx/engines/google.py#L65) |
| Bing | off | moderate | strict | [bing.py:44-48](file:///d:/fz/0508-2/solo-dogfeeding/code/26-searxng/searx/engines/bing.py#L44-L48) |
| DuckDuckGo | - | - | - | 无单独参数，由后端控制 |
| Yahoo | p | i | r | [yahoo.py:38](file:///d:/fz/0508-2/solo-dogfeeding/code/26-searxng/searx/engines/yahoo.py#L38) |

### 3.5 时间范围参数映射

| 搜索引擎 | day | week | month | year | 实现位置 |
|---------|-----|------|-------|------|---------|
| Google | d | w | m | y | [google.py:62](file:///d:/fz/0508-2/solo-dogfeeding/code/26-searxng/searx/engines/google.py#L62) |
| Bing | 不支持 | 不支持 | 不支持 | 不支持 | - |
| DuckDuckGo | d | w | m | y | [duckduckgo.py:213](file:///d:/fz/0508-2/solo-dogfeeding/code/26-searxng/searx/engines/duckduckgo.py#L213) |
| Yahoo | d | w | m | 不支持 | [yahoo.py:37](file:///d:/fz/0508-2/solo-dogfeeding/code/26-searxng/searx/engines/yahoo.py#L37) |

### 3.6 特殊请求头与 Cookie

#### Google 的特殊处理
```python
# google.py:270-276
ret_val["headers"]["User-Agent"] = gen_gsa_useragent()  # 专用 GSA UA
ret_val["cookies"]["CONSENT"] = "YES+"                   # 绕过欧盟同意提示
```

#### DuckDuckGo 的反爬防护头
```python
# duckduckgo.py:383-388
headers["Sec-Fetch-Dest"] = "document"
headers["Sec-Fetch-Mode"] = "navigate"
headers["Sec-Fetch-Site"] = "same-origin"
headers["Sec-Fetch-User"] = "?1"
headers["Referer"] = "https://html.duckduckgo.com/"
```

## 4. 响应解析层面差异分析

### 4.1 响应格式差异

| 搜索引擎 | 格式 | 解析方式 | 实现位置 |
|---------|------|---------|---------|
| Google | HTML | lxml + XPath | [google.py:362-428](file:///d:/fz/0508-2/solo-dogfeeding/code/26-searxng/searx/engines/google.py#L362-L428) |
| Bing | HTML | lxml + XPath | [bing.py:122-168](file:///d:/fz/0508-2/solo-dogfeeding/code/26-searxng/searx/engines/bing.py#L122-L168) |
| DuckDuckGo | HTML | lxml + XPath | [duckduckgo.py:465-518](file:///d:/fz/0508-2/solo-dogfeeding/code/26-searxng/searx/engines/duckduckgo.py#L465-L518) |
| Yahoo | HTML | lxml + XPath | [yahoo.py:215-256](file:///d:/fz/0508-2/solo-dogfeeding/code/26-searxng/searx/engines/yahoo.py#L215-L256) |

虽然所有引擎都返回 HTML，但 DOM 结构差异巨大。

### 4.2 结果项 XPath 差异

#### Google 的结果提取
```python
# google.py:374
for result in eval_xpath_list(dom, '//a[@data-ved and not(@class)]'):
    title_tag = eval_xpath_getindex(result, './/div[@style]', 0, default=None)
    raw_url = result.get("href")
    content_nodes = eval_xpath(result, '../..//div[contains(@class, "ilUpNd H66NU aSRlid")]')
```

#### Bing 的结果提取
```python
# bing.py:129-160
for item in eval_xpath_list(dom, '//ol[@id="b_results"]/li[contains(@class, "b_algo")]'):
    link = eval_xpath_getindex(item, ".//h2/a", 0, None)
    content_els = eval_xpath(item, ".//p")
```

#### DuckDuckGo 的结果提取
```python
# duckduckgo.py:493
for div_result in eval_xpath(doc, '//div[@id="links"]/div[contains(@class, "web-result")]'):
    _title = eval_xpath(div_result, ".//h2/a")
    _content = eval_xpath_getindex(div_result, './/a[contains(@class, "result__snippet")]', 0, [])
```

#### Yahoo 的结果提取
```python
# yahoo.py:230-250
for result in eval_xpath_list(dom, '//div[contains(@class,"algo-sr")]'):
    url = eval_xpath_getindex(result, url_xpath, 0, default=None)
    title = eval_xpath_getindex(result, title_xpath, 0, default='')
    content = eval_xpath_getindex(result, './/div[contains(@class, "compText")]', 0, default='')
```

### 4.3 URL 解码差异

#### Google 的重定向 URL 解码
```python
# google.py:393-396
if raw_url.startswith('/url?q='):
    url = unquote(raw_url[7:].split("&sa=U")[0])  # 移除 Google 跳转器
else:
    url = raw_url
```

#### Bing 的 Base64 编码 URL
```python
# bing.py:141-150
if href.startswith("https://www.bing.com/ck/a?"):
    qs = parse_qs(urlparse(href).query)
    u_values = qs.get("u")
    if u_values:
        u_val = u_values[0]
        if u_val.startswith("a1"):
            encoded = u_val[2:]
            encoded += "=" * (-len(encoded) % 4)
            href = base64.urlsafe_b64decode(encoded).decode("utf-8", errors="replace")
```

#### Yahoo 的追踪 URL 解码
```python
# yahoo.py:196-212
def parse_url(url_string):
    start = url_string.find('http', url_string.find('/RU=') + 1)
    # ... 查找 /RS 或 /RK 结束标记
    return unquote(url_string[start:end])
```

### 4.4 特殊内容提取

#### Google 的内联图片提取
```python
# google.py:348-359
RE_DATA_IMAGE = re.compile(r"(data:image[^']*?)'[^']*?'((?:dimg|pimg|tsuid)[^']*)")

def parse_url_images(text: str):
    data_image_map = {}
    for image_url, img_id in RE_DATA_IMAGE.findall(text):
        data_image_map[img_id] = image_url.encode('utf-8').decode("unicode-escape")
    return data_image_map
```

#### DuckDuckGo 的 Zero-Click 信息提取
```python
# duckduckgo.py:504-517
zero_click = extract_text(eval_xpath(doc, '//div[@id="zero_click_abstract"]')).strip()
if zero_click and ...:
    res.add(
        res.types.Answer(
            answer=zero_click,
            url=eval_xpath_getindex(doc, '//div[@id="zero_click_abstract"]/a/@href', 0),
        )
    )
```

## 5. 错误识别层面差异分析

### 5.1 异常类型体系

SearXNG 定义了统一的异常继承体系：

```
SearxEngineException
└── SearxEngineResponseException
    ├── SearxEngineAPIException
    └── SearxEngineAccessDeniedException
        ├── SearxEngineCaptchaException
        └── SearxEngineTooManyRequestsException
```

### 5.2 错误检测层级架构

SearXNG 采用双层错误检测架构：

```
┌─────────────────────────────────────────────────┐
│          网络层通用错误检测 (所有引擎共享)        │
├─────────────────────────────────────────────────┤
│ raise_for_httperror()                            │
│ ├─ Cloudflare CAPTCHA 检测                       │
│ ├─ ReCAPTCHA 检测                                │
│ ├─ HTTP 402/403 → AccessDenied                   │
│ └─ HTTP 429 → TooManyRequests                    │
└─────────────────────┬───────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────┐
│          引擎层自定义错误检测 (可选实现)         │
├─────────────────────────────────────────────────┤
│ Google: detect_google_sorry()  ✓                │
│ DuckDuckGo: is_ddg_captcha()  ✓                 │
│ Bing: (无自定义检测) ✗                           │
│ Yahoo: (无自定义检测) ✗                          │
└─────────────────────────────────────────────────┘
```

### 5.3 各引擎错误检测实现对比

#### Google 的多模式 CAPTCHA 检测
```python
# google.py:281-301
def detect_google_sorry(resp):
    # 1. 重定向到 sorry.google.com
    if resp.url.host == "sorry.google.com" or resp.url.path.startswith("/sorry"):
        raise SearxEngineCaptchaException()
    # 2. HTTP 302 重定向到 /sorry
    if resp.status_code == 302:
        raise SearxEngineCaptchaException()
    # 3. 短响应包含 /sorry/
    if len(resp.text) < 2000 and "/sorry/" in resp.text:
        raise SearxEngineCaptchaException()
```

**检测时机**：在 `response()` 函数开头调用，优先于结果解析
**检测粒度**：3 种模式全覆盖，误判率低

#### DuckDuckGo 的表单式 CAPTCHA 检测
```python
# duckduckgo.py:458-462
def is_ddg_captcha(dom):
    return bool(eval_xpath(dom, "//form[@id='challenge-form']"))

# 响应中检测
if is_ddg_captcha(doc):
    raise SearxEngineCaptchaException(suspended_time=0, message=f"CAPTCHA ({params['data'].get('kl')})")
```

**检测时机**：DOM 解析后、结果提取前
**特殊处理**：设置 `suspended_time=0`，不触发 IP 封禁

#### Bing 的依赖式错误检测

Bing **没有**在引擎层面实现自定义错误检测逻辑，完全依赖网络层的通用错误检测：

1. **通用状态码检测**（网络层自动处理）：
   - HTTP 402/403 → `SearxEngineAccessDeniedException`
   - HTTP 429 → `SearxEngineTooManyRequestsException`
   - Cloudflare / ReCAPTCHA → `SearxEngineCaptchaException`

2. **区域重定向处理**：
```python
# bing.py:115-117
# 某些地区（如中国）存在地理封锁，www.bing.com 会重定向到区域版本
params["allow_redirects"] = True
```

**检测特点**：
- 无引擎特定错误模式
- 依赖 HTTP 状态码和通用 CDN 检测
- 错误粒度较粗，无法识别 Bing 特有的软封禁

#### Yahoo 的极简错误检测

Yahoo **同样没有**实现自定义错误检测，完全依赖网络层通用检测：

1. **通用状态码检测**（网络层自动处理）
2. **无特殊处理逻辑**：既无 CAPTCHA 检测，也无状态码映射
3. **静默失败风险**：当 Yahoo 返回空结果或登录页面时，无法识别为错误

**检测特点**：
- 最简化的错误处理策略
- 可能出现"伪成功"（返回异常页面但状态码为 200）
- 错误识别率最低

### 5.4 四引擎错误检测能力矩阵

| 检测维度 | Google | DuckDuckGo | Bing | Yahoo |
|---------|--------|-----------|------|-------|
| 自定义 CAPTCHA 检测 | ✓ 多模式 | ✓ 表单检测 | ✗ | ✗ |
| HTTP 状态码检测 | ✓ | ✓ | ✓ | ✓ |
| Cloudflare 检测 | ✓ | ✓ | ✓ | ✓ |
| ReCAPTCHA 检测 | ✓ | ✓ | ✓ | ✓ |
| 软封禁识别 | ✓ | ✓ | ✗ | ✗ |
| 响应内容检测 | ✓ | ✓ | ✗ | ✗ |
| 错误粒度 | 细 | 中 | 粗 | 最粗 |
| 误报率 | 低 | 低 | 中 | 高 |
| 漏报率 | 低 | 低 | 中 | 高 |

### 5.5 网络层通用错误检测详解

```python
# network/raise_for_httperror.py:16-78

# 1. Cloudflare 挑战检测
def is_cloudflare_challenge(resp):
    if resp.status_code in [429, 503]:
        if ('__cf_chl_jschl_tk__=' in resp.text) or \
           ('/cdn-cgi/challenge-platform/' in resp.text ...):
            return True
    if resp.status_code == 403 and '__cf_chl_captcha_tk__=' in resp.text:
        return True
    return False

# 2. Cloudflare 防火墙检测
def is_cloudflare_firewall(resp):
    return resp.status_code == 403 and \
           '<span class="cf-error-code">1020</span>' in resp.text

# 3. 通用状态码映射
if resp.status_code in (402, 403):
    raise SearxEngineAccessDeniedException(message='HTTP error ' + str(resp.status_code))
if resp.status_code == 429:
    raise SearxEngineTooManyRequestsException()
```

**Cloudflare 特殊暂停时间**：
- Cloudflare CAPTCHA：默认暂停 2 周（`search.suspended_times.cf_SearxEngineCaptcha`）
- Cloudflare 防火墙：暂停时间更长（`search.suspended_times.cf_SearxEngineAccessDenied`）

### 5.6 错误处理策略差异

| 异常类型 | 默认暂停时间 | 配置项 |
|---------|-------------|--------|
| SearxEngineAccessDeniedException | 86400 秒 (1 天) | search.suspended_times.SearxEngineAccessDenied |
| SearxEngineCaptchaException | 86400 秒 (1 天) | search.suspended_times.SearxEngineCaptcha |
| SearxEngineTooManyRequestsException | 3660 秒 (约 1 小时) | search.suspended_times.SearxEngineTooManyRequests |
| Cloudflare CAPTCHA | 1209600 秒 (2 周) | search.suspended_times.cf_SearxEngineCaptcha |
| Cloudflare Firewall | 更长 | search.suspended_times.cf_SearxEngineAccessDenied |

**引擎特定策略**：
- DuckDuckGo：CAPTCHA 异常设置 `suspended_time=0`，不会导致 IP 被封锁
- Google：多层检测，一旦触发即严格封禁
- Bing/Yahoo：无自定义策略，完全遵循通用规则

**通用递增策略**：
- 连续错误会导致暂停时间递增（`search.ban_time_on_fail` → `search.max_ban_time_on_fail`）
- 成功请求后重置错误计数

## 6. 适配框架的抽象统一机制

### 6.1 参数标准化层

`OnlineProcessor.get_params()` 负责将通用搜索参数转换为引擎特定参数：

```python
# online.py:132-162
def get_params(self, search_query, engine_category):
    base_params = super().get_params(search_query, engine_category)
    params = {**default_request_params(), **base_params}
    
    # 标准化请求头
    headers["Accept-Encoding"] = "gzip, deflate"
    headers["Cache-Control"] = "no-cache"
    headers["DNT"] = "1"
    headers["User-Agent"] = gen_useragent()
    
    # 标准化 Accept-Language
    if self.engine.send_accept_language_header and search_query.locale:
        headers["Accept-Language"] = f"{_l},{_l}-{_t};q=0.7,en;q=0.3"
    
    return params
```

### 6.2 引擎特性（Traits）抽象

每个引擎通过 `fetch_traits()` 获取其特性配置，统一存储在 `EngineTraits` 对象中：

```python
# enginelib/traits.py (概念模型)
class EngineTraits:
    languages: Dict[str, str]      # sxng_lang → engine_lang
    regions: Dict[str, str]        # sxng_region → engine_region
    all_locale: str                # "全部"对应的引擎值
    custom: Dict[str, Any]         # 引擎自定义数据
```

**统一调用流程：**
```python
# 抽象层调用
eng_lang = traits.get_language(sxng_locale, default)
eng_region = traits.get_region(sxng_locale, default)
```

### 6.3 异常统一捕获与处理

```python
# online.py:253-282
try:
    search_results = self._search_basic(query, params)
    self.extend_container(result_container, start_time, search_results)
except (SearxEngineCaptchaException, SearxEngineTooManyRequestsException, SearxEngineAccessDeniedException) as e:
    self.handle_exception(result_container, e, suspend=True)
except Exception as e:
    self.handle_exception(result_container, e)
```

### 6.4 结果格式统一

各引擎的 `response()` 方法返回统一格式的结果列表：

```python
# 统一结果格式
{
    "url": str,           # 标准化后的 URL
    "title": str,         # 提取并清理的标题
    "content": str,       # 提取并清理的内容
    "thumbnail": str,     # 缩略图 URL (可选)
    "suggestion": str,    # 搜索建议 (可选)
    "answer": str,        # 直接答案 (可选)
}
```

## 7. 对结果归并的影响

### 7.1 结果质量差异

| 搜索引擎 | 结果数/页 | 内容完整性 | 广告干扰 |
|---------|----------|-----------|---------|
| Google | ~10 | 高，包含缩略图、结构化数据 | 中等 |
| Bing | ~10 | 中 | 高 |
| DuckDuckGo | ~10-15 | 中 | 低 |
| Yahoo | ~7 | 低 | 高 |

### 7.2 去重挑战

由于各引擎的 URL 编码方式不同，结果归并时需要：
1. 统一解码所有跟踪/跳转 URL
2. 规范化 URL（去除跟踪参数、标准化协议等）
3. 基于标题+内容的模糊去重

### 7.3 排序影响因素

SearXNG 的结果排序综合考虑：
- 引擎权重（`weight` 配置项）
- 结果在原引擎中的位置
- 结果内容的丰富度（是否有缩略图、结构化数据）
- 响应时间

## 8. 对限流策略的影响

### 8.1 各引擎限流敏感度

| 搜索引擎 | 限流阈值 (估计) | 封禁策略 | 恢复时间 |
|---------|----------------|---------|---------|
| Google | 低 (严格) | 显示 CAPTCHA，IP 临时封禁 | 几小时到几天 |
| Bing | 中 | HTTP 429，响应延迟 | 几小时 |
| DuckDuckGo | 高 | 返回空结果或 CAPTCHA | 约 1 小时（滑动窗口） |
| Yahoo | 中 | HTTP 403 | 不确定 |

### 8.2 框架级限流措施

1. **引擎暂停机制**：捕获到限流异常后暂停引擎一段时间
2. **连续错误递增封禁**：`ban_time_on_fail` → `max_ban_time_on_fail`
3. **请求超时控制**：每个引擎有独立的 `timeout` 配置
4. **并发控制**：通过线程池间接控制并发请求数

### 8.3 引擎特有规避策略

- **Google**：使用 `gen_gsa_useragent()` 生成专用 UA，轮换子域名
- **DuckDuckGo**：静态 UA + 完整 Sec-Fetch 头，缓存 vqd 令牌
- **Bing**：设置 `allow_redirects=True` 应对地区重定向

## 9. 对机器人检测的影响

### 9.1 各引擎反爬技术栈

| 搜索引擎 | 检测技术 | 规避难度 |
|---------|---------|---------|
| Google | 行为分析、CAPTCHA、指纹识别、IP 信誉 | 极高 |
| Bing | IP 限流、UA 检测 | 中等 |
| DuckDuckGo | vqd 令牌、Sec-Fetch 头验证、行为分析 | 高 |
| Yahoo | Cookie 验证、IP 限流 | 中等 |

### 9.2 框架级反检测措施

1. **UA 轮换**：`gen_useragent()` 生成随机真实浏览器 UA
2. **请求头标准化**：模拟真实浏览器的请求头顺序和值
3. **Cookie 管理**：支持引擎特定的 Cookie 策略
4. **代理支持**：每个引擎可独立配置代理

### 9.3 引擎特有反检测措施

#### Google
```python
# google.py:79-98
def ui_async(start: int) -> str:
    # 生成随机 arc_id，每小时轮换
    if not _arcid_random or (int(time.time()) - _arcid_random[1]) > 3600:
        _arcid_random = ("".join(random.choices(_arcid_range, k=23)), int(time.time()))
    arc_id = f"arc_id:srp_{_arcid_random[0]}_1{start:02}"
    return ",".join([arc_id, use_ac, _fmt])
```

#### DuckDuckGo
```python
# duckduckgo.py:229-250
def set_vqd(query, value, params):
    # 缓存 vqd 令牌，避免重复获取
    key = cache.secret_hash(f"{query}//{params['headers']['User-Agent']}")
    cache.set(key=key, value=value, expire=3600)
```

## 10. 总结与建议

### 10.1 差异总览

| 维度 | 差异程度 | 主要差异点 | 统一难度 |
|-----|---------|-----------|---------|
| 请求拼装 | 高 | HTTP 方法、参数命名、分页机制、本地化策略 | 中 |
| 响应解析 | 极高 | DOM 结构、URL 编码、内容提取方式 | 高 |
| 错误识别 | 中 | CAPTCHA 形式、状态码使用、封禁策略 | 中 |

### 10.2 适配框架的设计价值

1. **关注点分离**：引擎适配只需关注业务逻辑，通用逻辑由框架处理
2. **可扩展性**：新增引擎只需实现 `request()`、`response()`、`fetch_traits()`
3. **容错性**：统一的异常处理和熔断机制
4. **可观测性**：统一的指标收集和错误记录

### 10.3 优化建议

1. **增加结果中间表示**：在引擎 response() 之上增加一层语义标准化，降低归并复杂度
2. **增强反检测能力**：引入请求指纹随机化、行为模拟等高级反检测技术
3. **自适应限流**：基于历史成功率动态调整各引擎的请求频率
4. **统一测试框架**：为所有引擎提供标准化的集成测试套件

---

*报告基于 SearXNG commit 版本生成，分析覆盖 Google、Bing、DuckDuckGo、Yahoo 四大主流搜索引擎的适配实现。*
