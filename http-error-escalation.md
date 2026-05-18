# SearXNG 跨引擎查询 HTTP 错误升级与归并策略

## 1. 错误分级体系

SearXNG 的错误处理机制围绕 **异常类继承层次** 和 **挂起时长配置** 实现分级管理。所有引擎相关错误最终都会影响引擎的可用性和健康度统计。

### 1.1 错误等级定义

| 等级 | 类型 | 典型场景 | 挂起策略 | 严重程度 |
|------|------|----------|----------|----------|
| **L1 - 致命阻塞** | CAPTCHA / 防火墙封禁 | Cloudflare CAPTCHA、Cloudflare 防火墙 1020、ReCAPTCHA | 长时挂起（1天 ~ 15天）| ⭐⭐⭐⭐⭐ |
| **L2 - 访问拒绝** | 权限类错误 | HTTP 402 / 403、主动拒绝访问 | 中时挂起（180秒）| ⭐⭐⭐⭐ |
| **L3 - 限流触发** | 请求频率超限 | HTTP 429 Too Many Requests | 中时挂起（180秒）| ⭐⭐⭐⭐ |
| **L4 - 传输故障** | 网络协议错误 | SSL 证书错误、连接失败、协议错误、HTTP 5xx | 固定短时挂起（默认5秒）| ⭐⭐⭐ |
| **L5 - 响应超时** | 性能类错误 | 连接超时、读取超时、写入超时 | 固定短时挂起（默认5秒）| ⭐⭐⭐ |
| **L6 - 解析异常** | 数据处理错误 | JSON 解析失败、XPATH 不匹配、API 格式变更 | 不挂起（仅统计）| ⭐⭐ |
| **L7 - 次要告警** | 非预期行为 | 重定向次数超限、软限制触发 | 不挂起（仅统计，标记 secondary）| ⭐ |

### 1.2 异常类继承关系

```
SearxEngineException
└── SearxEngineResponseException
    ├── SearxEngineAPIException
    └── SearxEngineAccessDeniedException  (L2, 默认挂起 180s)
        ├── SearxEngineCaptchaException   (L1, 默认挂起 3600s)
        │   ├── Cloudflare CAPTCHA        (L1, 挂起 1296000s / 15天)
        │   └── ReCAPTCHA                 (L1, 挂起 604800s / 7天)
        ├── SearxEngineTooManyRequestsException (L3, 默认挂起 180s)
        └── Cloudflare Firewall           (L1, 挂起 86400s / 1天)
```

[searx/exceptions.py:60-111](file:///d:/fz/0508-2/solo-dogfeeding/code/29-searxng/searx/exceptions.py#L60-L111)

---

## 2. 错误检测与触发机制

### 2.1 HTTP 状态码映射

HTTP 响应错误首先通过 `raise_for_httperror` 模块进行分类识别：

| 状态码 | 检测条件 | 抛出异常 | 等级 |
|--------|----------|----------|------|
| 402 / 403 | 非 Cloudflare 场景 | `SearxEngineAccessDeniedException` | L2 |
| 403 | 响应包含 `cf-error-code">1020` | `SearxEngineAccessDeniedException` (Cloudflare Firewall) | L1 |
| 429 / 503 | 响应包含 Cloudflare 挑战标识 | `SearxEngineCaptchaException` (Cloudflare CAPTCHA) | L1 |
| 429 | 非 Cloudflare 场景 | `SearxEngineTooManyRequestsException` | L3 |
| 503 | 响应包含 Google ReCAPTCHA 标识 | `SearxEngineCaptchaException` (ReCAPTCHA) | L1 |
| >= 400 | 其他未匹配情况 | `httpx.HTTPStatusError` | L4 |

[searx/network/raise_for_httperror.py:16-79](file:///d:/fz/0508-2/solo-dogfeeding/code/29-searxng/searx/network/raise_for_httperror.py#L16-L79)

### 2.2 网络层错误捕获

在线搜索处理器（`OnlineProcessor`）在 `search` 方法中捕获不同类型的异常：

```python
try:
    search_results = self._search_basic(query, params)
    self.extend_container(result_container, start_time, search_results)
except ssl.SSLError as e:
    self.handle_exception(result_container, e, suspend=True)  # L4, 挂起
except (httpx.TimeoutException, asyncio.TimeoutError) as e:
    self.handle_exception(result_container, e, suspend=True)  # L5, 挂起
except (httpx.HTTPError, httpx.StreamError) as e:
    self.handle_exception(result_container, e, suspend=True)  # L4, 挂起
except (SearxEngineCaptchaException, SearxEngineTooManyRequestsException, SearxEngineAccessDeniedException) as e:
    self.handle_exception(result_container, e, suspend=True)  # L1/L2/L3, 挂起
except Exception as e:
    self.handle_exception(result_container, e)  # L6, 不挂起
```

[searx/search/processors/online.py:239-282](file:///d:/fz/0508-2/solo-dogfeeding/code/29-searxng/searx/search/processors/online.py#L239-L282)

---

## 3. 错误挂起策略（按真实实现校准）

### 3.1 挂起时长阶梯配置

不同等级的错误对应不同的挂起时长，在 `settings.yml` 中配置：

```yaml
search:
  ban_time_on_fail: 5           # 普通错误挂起时间（秒）
  max_ban_time_on_fail: 120     # 普通错误最大挂起时间（秒）
  suspended_times:
    SearxEngineAccessDenied: 180          # L2: 访问拒绝
    SearxEngineCaptcha: 3600              # L1: 普通 CAPTCHA（1小时）
    SearxEngineTooManyRequests: 180       # L3: 限流（3分钟）
    cf_SearxEngineCaptcha: 1296000        # L1: Cloudflare CAPTCHA（15天）
    cf_SearxEngineAccessDenied: 86400     # L1: Cloudflare 防火墙（1天）
    recaptcha_SearxEngineCaptcha: 604800  # L1: ReCAPTCHA（7天）
```

[searx/settings.yml:66-81](file:///d:/fz/0508-2/solo-dogfeeding/code/29-searxng/searx/settings.yml#L66-L81)

### 3.2 挂起时间计算逻辑（关键校准）

**重要说明：普通错误的挂起时间是固定的，不随连续错误次数递增。**

```python
def suspend(self, suspended_time: int | None, suspend_reason: str):
    with self.lock:
        # continuous_errors 仅用于计数，不参与挂起时长计算
        self.continuous_errors += 1
        if suspended_time is None:
            # 普通错误（超时、SSL错误、HTTP错误等）使用固定时长
            # 取 ban_time_on_fail 和 max_ban_time_on_fail 中的较小值
            max_ban: int = get_setting("search.max_ban_time_on_fail")  # 默认 120
            ban_fail: int = get_setting("search.ban_time_on_fail")    # 默认 5
            suspended_time = min(max_ban, ban_fail)  # 默认 min(120, 5) = 5 秒

        self.suspend_end_time = default_timer() + suspended_time
        self.suspend_reason = suspend_reason
        logger.debug("Suspend for %i seconds", suspended_time)
```

[searx/search/processors/abstract.py:90-101](file:///d:/fz/0508-2/solo-dogfeeding/code/29-searxng/searx/search/processors/abstract.py#L90-L101)

**真实行为：**
- 默认配置下，所有普通错误（L4/L5）统一挂起 `min(120, 5) = 5` 秒
- 连续错误仅增加 `continuous_errors` 计数，但挂起时长保持不变
- 若修改 `ban_time_on_fail: 10`，`max_ban_time_on_fail: 30`，则挂起 `min(30, 10) = 10` 秒
- L1/L2/L3 级错误使用异常类自带的 `suspended_time`，不受上述两个参数影响

### 3.3 错误降级与恢复

当引擎成功返回结果时，连续错误计数和挂起状态会被重置：

```python
def extend_container(self, result_container, start_time, search_results):
    if getattr(threading.current_thread(), '_timeout', False):
        self.handle_exception(result_container, 'timeout', False)
    else:
        if search_results is not None:
            self._extend_container_basic(result_container, start_time, search_results)
        self.suspended_status.resume()  # 成功响应后立即重置

def resume(self):
    with self.lock:
        self.continuous_errors = 0
        self.suspend_end_time = 0
        self.suspend_reason = ""
```

[searx/search/processors/abstract.py:210-223](file:///d:/fz/0508-2/solo-dogfeeding/code/29-searxng/searx/search/processors/abstract.py#L210-L223)

---

## 4. 用户提示与页面展示（按时序校准）

### 4.1 两种错误展示时机的核心差异

SearXNG 有两条独立的代码路径会产生用户可见的错误提示，二者的展示形式有本质区别：

| 时机 | 触发场景 | 代码路径 | suspended 参数 | 用户提示前缀 |
|------|----------|----------|----------------|-------------|
| **时机A：首次请求报错** | 当前查询执行失败，引擎刚被挂起 | `handle_exception` → `add_unresponsive_engine(name, error_type)` | `False`（默认值）| 无前缀 |
| **时机B：后续请求命中挂起** | 引擎已处于挂起状态，新查询直接跳过 | `extend_container_if_suspended` → `add_unresponsive_engine(name, reason, suspended=True)` | `True`（显式传入） | `Suspended: ` 前缀 |

**关键代码对比：**

```python
# 时机A：首次请求报错 - handle_exception 第179行
# 注意：没有传第三个参数，suspended 默认是 False
result_container.add_unresponsive_engine(self.engine.name, error_message)

# 时机B：后续请求命中挂起 - extend_container_if_suspended 第227-228行
# 注意：显式传入 suspended=True
result_container.add_unresponsive_engine(
    self.engine.name, self.suspended_status.suspend_reason, suspended=True
)
```

[searx/search/processors/abstract.py:179](file:///d:/fz/0508-2/solo-dogfeeding/code/29-searxng/searx/search/processors/abstract.py#L179)
[searx/search/processors/abstract.py:225-231](file:///d:/fz/0508-2/solo-dogfeeding/code/29-searxng/searx/search/processors/abstract.py#L225-L231)

### 4.2 翻译映射与前缀生成

无论哪种时机，`error_type` 都是异常类名（如 `httpx.TimeoutException`），经过 `exception_classname_to_text` 字典翻译后，再根据 `suspended` 参数决定是否加前缀：

```python
def get_translated_errors(unresponsive_engines):
    for unresponsive_engine in unresponsive_engines:
        # 1. 通过字典映射为用户友好文本
        error_user_text = exception_classname_to_text.get(unresponsive_engine.error_type)
        error_msg = gettext(error_user_text)
        # 2. 只有 suspended=True 时才加前缀
        if unresponsive_engine.suspended:
            error_msg = gettext('Suspended') + ': ' + error_msg
        translated_errors.append((unresponsive_engine.engine, error_msg))
```

[searx/webutils.py:70-82](file:///d:/fz/0508-2/solo-dogfeeding/code/29-searxng/searx/webutils.py#L70-L82)

### 4.3 异常类名到用户文本的映射

#### 访问拒绝 / 限流 / CAPTCHA 类

| 异常类名 | 翻译后文本（英文） | 时机A展示（首次报错） | 时机B展示（命中挂起） |
|----------|-------------------|----------------------|----------------------|
| `searx.exceptions.SearxEngineAccessDeniedException` | `access denied` | `access denied` | `Suspended: access denied` |
| `searx.exceptions.SearxEngineTooManyRequestsException` | `too many requests` | `too many requests` | `Suspended: too many requests` |
| `searx.exceptions.SearxEngineCaptchaException` | `CAPTCHA` | `CAPTCHA` | `Suspended: CAPTCHA` |

#### 超时类

| 异常类名 | 翻译后文本（英文） | 时机A展示（首次报错） | 时机B展示（命中挂起） |
|----------|-------------------|----------------------|----------------------|
| `timeout` / `asyncio.TimeoutError` | `timeout` | `timeout` | `Suspended: timeout` |
| `httpx.TimeoutException` / `httpx.ConnectTimeout` | `timeout` | `timeout` | `Suspended: timeout` |
| `httpx.ReadTimeout` / `httpx.WriteTimeout` | `timeout` | `timeout` | `Suspended: timeout` |

#### 协议错误类

| 异常类名 | 翻译后文本（英文） | 时机A展示（首次报错） | 时机B展示（命中挂起） |
|----------|-------------------|----------------------|----------------------|
| `httpx.RemoteProtocolError` | `HTTP protocol error` | `HTTP protocol error` | `Suspended: HTTP protocol error` |
| `httpx.LocalProtocolError` | `HTTP protocol error` | `HTTP protocol error` | `Suspended: HTTP protocol error` |
| `httpx.ProtocolError` | `HTTP protocol error` | `HTTP protocol error` | `Suspended: HTTP protocol error` |
| `httpx.HTTPStatusError` | `HTTP error` | `HTTP error` | `Suspended: HTTP error` |
| `httpx.ConnectError` | `HTTP connection error` | `HTTP connection error` | `Suspended: HTTP connection error` |
| `httpx.ProxyError` | `proxy error` | `proxy error` | `Suspended: proxy error` |
| `httpx.ReadError` / `httpx.WriteError` | `network error` | `network error` | `Suspended: network error` |

#### SSL 错误类

| 异常类名 | 翻译后文本（英文） | 时机A展示（首次报错） | 时机B展示（命中挂起） |
|----------|-------------------|----------------------|----------------------|
| `ssl.SSLCertVerificationError` | `SSL error: certificate validation has failed` | `SSL error: certificate validation has failed` | `Suspended: SSL error: certificate validation has failed` |
| `ssl.CertificateError` | `SSL error: certificate validation has failed` | `SSL error: certificate validation has failed` | `Suspended: SSL error: certificate validation has failed` |
| 其他 `ssl.SSLError` 子类（未匹配） | `unexpected crash`（回落） | `unexpected crash` | 视错误是否触发挂起而定 |

#### 解析错误类

| 异常类名 | 翻译后文本（英文） | 时机A展示（首次报错） | 时机B展示（命中挂起） |
|----------|-------------------|----------------------|----------------------|
| `json.decoder.JSONDecodeError` | `parsing error` | `parsing error` | （不会挂起，无时机B） |
| `KeyError` | `parsing error` | `parsing error` | （不会挂起，无时机B） |
| `lxml.etree.ParserError` | `parsing error` | `parsing error` | （不会挂起，无时机B） |
| `searx.exceptions.SearxEngineXPathException` | `parsing error` | `parsing error` | （不会挂起，无时机B） |

> **回落机制**：`exception_classname_to_text.get(error_type)` 使用字典的 `get` 方法，未匹配到的异常类名会返回 `None`，最终映射为 `unexpected crash`。

[searx/webutils.py:36-67](file:///d:/fz/0508-2/solo-dogfeeding/code/29-searxng/searx/webutils.py#L36-L67)

[searx/webutils.py:36-67](file:///d:/fz/0508-2/solo-dogfeeding/code/29-searxng/searx/webutils.py#L36-L67)

> **重要澄清**：用户界面 **不会** 显示异常 message 中的 `suspended_time=180` 等内部参数，也不会显示 "Engine X is suspended" 格式。

### 4.4 四类典型场景的时序对齐

#### 场景1：访问拒绝（L2 - HTTP 403）

**时序过程：**
1. **请求 N（首次报错）**：引擎返回 403，触发 `SearxEngineAccessDeniedException`
   - 代码路径：`handle_exception` → `add_unresponsive_engine(engine, "searx.exceptions.SearxEngineAccessDeniedException")`
   - `suspended=False`（默认）
   - **用户看到**：`access denied`
   - 同时引擎被挂起 180 秒

2. **请求 N+1（5秒后，仍在挂起期内）**：新查询到达
   - 代码路径：`extend_container_if_suspended` 检测到挂起 → `add_unresponsive_engine(engine, reason, suspended=True)`
   - **用户看到**：`Suspended: access denied`
   - 不发送实际 HTTP 请求

3. **请求 N+K（180秒后，挂起已结束）**：新查询到达
   - 引擎恢复正常，重新发送请求

#### 场景2：限流触发（L3 - HTTP 429）

**时序过程：**
1. **请求 N（首次报错）**：引擎返回 429，触发 `SearxEngineTooManyRequestsException`
   - 代码路径：`handle_exception` → `add_unresponsive_engine(engine, "searx.exceptions.SearxEngineTooManyRequestsException")`
   - `suspended=False`（默认）
   - **用户看到**：`too many requests`
   - 同时引擎被挂起 180 秒

2. **请求 N+1（挂起期内）**：新查询到达
   - 代码路径：`extend_container_if_suspended` → `add_unresponsive_engine(engine, reason, suspended=True)`
   - **用户看到**：`Suspended: too many requests`
   - 不发送实际 HTTP 请求

#### 场景3：响应超时（L5）

**时序过程：**
1. **请求 N（首次报错）**：HTTP 请求超时，触发 `httpx.TimeoutException`
   - 代码路径：`handle_exception` → `add_unresponsive_engine(engine, "httpx.TimeoutException")`
   - `suspended=False`（默认）
   - **用户看到**：`timeout`
   - 同时引擎被挂起 5 秒

2. **请求 N+1（3秒后，仍在挂起期内）**：新查询到达
   - 代码路径：`extend_container_if_suspended` → `add_unresponsive_engine(engine, reason, suspended=True)`
   - **用户看到**：`Suspended: timeout`
   - 不发送实际 HTTP 请求

3. **请求 N+2（6秒后，挂起已结束）**：新查询到达
   - 引擎恢复正常，重新发送请求

#### 场景4：协议错误（L4 - SSL证书错误）

**时序过程：**
1. **请求 N（首次报错）**：HTTPS 握手失败，触发 `ssl.SSLCertVerificationError`
   - 代码路径：`handle_exception` → `add_unresponsive_engine(engine, "ssl.SSLCertVerificationError")`
   - `suspended=False`（默认）
   - **用户看到**：`SSL error: certificate validation has failed`
   - 同时引擎被挂起 5 秒

2. **请求 N+1（挂起期内）**：新查询到达
   - 代码路径：`extend_container_if_suspended` → `add_unresponsive_engine(engine, reason, suspended=True)`
   - **用户看到**：`Suspended: SSL error: certificate validation has failed`
   - 不发送实际 HTTP 请求

### 4.5 特殊场景：不挂起的错误（L6 解析异常）

**时序过程：**
1. **请求 N（报错）**：JSON 解析失败，触发 `json.decoder.JSONDecodeError`
   - 代码路径：`handle_exception`（`suspend=False`）→ `add_unresponsive_engine(engine, "json.decoder.JSONDecodeError")`
   - `suspended=False`
   - **用户看到**：`parsing error`
   - 引擎不被挂起

2. **请求 N+1（任意时间后）**：新查询到达
   - 不会命中挂起逻辑，直接发送请求
   - 如果再次失败，用户仍然看到 `parsing error`（无前缀）

### 4.6 结果完整性影响

- **单引擎失败**：不影响整体搜索，仅缺少该引擎结果，用户在 "Response time" 区域看到错误提示
- **多引擎同类错误**：如多个引擎同时出现SSL错误，可能是出口网络问题
- **全引擎失败**：页面显示 "Sorry, we didn't find any results"，并在侧边栏列出所有失败引擎
- **挂起期间**：引擎完全不可用，结果中不会包含该引擎的任何内容
- **前缀识别**：用户可通过是否有 "Suspended: " 前缀判断该引擎是本次查询失败，还是之前就已被挂起

---

## 5. 健康度统计与监控（含指标开关前提）

### 5.1 指标开关控制前提

**所有统计功能受 `general.enable_metrics` 配置控制：**

```yaml
general:
  enable_metrics: true   # 设为 false 则完全关闭指标统计
```

**两层控制机制：**

1. **初始化层**：指标系统启动时决定使用真实存储还是空实现
   ```python
   def initialize(engine_names, enabled=True):
       if enabled:
           counter_storage = CounterStorage()          # 真实存储
           histogram_storage = HistogramStorage()
       else:
           counter_storage = VoidCounterStorage()      # 空实现，不存储任何数据
           histogram_storage = HistogramStorage(histogram_class=VoidHistogram)
   ```
   [searx/metrics/__init__.py:70-81](file:///d:/fz/0508-2/solo-dogfeeding/code/29-searxng/searx/metrics/__init__.py#L70-L81)

2. **错误记录层**：详细错误上下文记录前检查开关
   ```python
   def count_exception(engine_name: str, exc: BaseException, secondary: bool = False):
       if not settings['general']['enable_metrics']:
           return  # 直接返回，不记录任何错误上下文
       # ... 后续记录逻辑
   ```
   [searx/metrics/error_recorder.py:174-176](file:///d:/fz/0508-2/solo-dogfeeding/code/29-searxng/searx/metrics/error_recorder.py#L174-L176)

> **注意**：即使 `enable_metrics: false`，`counter_inc` 仍会被调用，但由于使用 `VoidCounterStorage`，实际不会存储任何数据，不产生性能开销。

### 5.2 错误计数体系

当指标开启时，SearXNG 通过多维度统计引擎健康度：

```python
# 指标计数器 - 每次错误调用
counter_inc('engine', engine_name, 'search', 'count', 'error')    # 错误次数计数
counter_inc('engine', engine_name, 'search', 'count', 'successful')  # 成功次数计数

# 响应时间直方图 - 每次成功调用
histogram_observe(engine_time, 'engine', engine_name, 'time', 'total')
histogram_observe(page_load_time, 'engine', engine_name, 'time', 'http')
```

[searx/search/processors/abstract.py:181-208](file:///d:/fz/0508-2/solo-dogfeeding/code/29-searxng/searx/search/processors/abstract.py#L181-L208)

### 5.3 错误上下文记录

每个错误都会记录详细的上下文信息（仅当 `enable_metrics: true` 时）：

```python
class ErrorContext:
    filename: str              # 出错文件
    function: str              # 出错函数
    line_no: int               # 出错行号
    code: str                  # 出错代码行
    exception_classname: str   # 异常类名
    log_message: str           # 日志消息
    log_parameters: tuple      # 日志参数（状态码、原因、主机名等）
    secondary: bool            # 是否为次要错误
```

[searx/metrics/error_recorder.py:25-84](file:///d:/fz/0508-2/solo-dogfeeding/code/29-searxng/searx/metrics/error_recorder.py#L25-L84)

错误按引擎分组统计：
```python
errors_per_engines: dict[str, dict[ErrorContext, int]] = {}
```

### 5.4 与后台监控的关系

1. **Prometheus / OpenMetrics 集成**
   - 当 `open_metrics` 配置启用时，`/metrics` 端点暴露所有引擎指标
   - 关键指标：`searxng_engines_reliability_total`、`searxng_engines_response_time_http_seconds`
   - 可通过配置告警规则监控引擎健康度趋势

2. **健康检查端点**
   - `/healthz` 端点返回实例健康状态（不受 `enable_metrics` 影响）
   - 可用于 Kubernetes livenessProbe 或外部监控服务

3. **日志系统**
   - 所有错误通过 `engine.logger.error()` / `logger.exception()` 记录（不受 `enable_metrics` 影响）
   - 可接入 ELK / Loki 等日志系统进行趋势分析
   - L1 级错误（CAPTCHA/封禁）会记录完整异常栈

4. **管理后台可见性**
   - 管理员统计面板显示各引擎的错误率、平均响应时间、可靠性评分
   - 可靠性计算基于 `errors_per_engines` 统计（需 `enable_metrics: true`）
   - 持续高错误率的引擎建议管理员检查配置或暂时禁用

---

## 6. 错误归并与去重策略

### 6.1 同类错误合并

相同 `ErrorContext` 的错误会被合并计数，避免日志和统计爆炸：

```python
def add_error_context(engine_name: str, error_context: ErrorContext) -> None:
    errors_for_engine = errors_per_engines.setdefault(engine_name, {})
    errors_for_engine[error_context] = errors_for_engine.get(error_context, 0) + 1
```

[searx/metrics/error_recorder.py:87-90](file:///d:/fz/0508-2/solo-dogfeeding/code/29-searxng/searx/metrics/error_recorder.py#L87-L90)

### 6.2 主要/次要错误区分

`secondary=True` 的错误不会触发引擎挂起，仅作为告警统计：

```python
count_error(
    self.engine.name,
    "{} redirects, maximum: {}".format(len(response.history), soft_max_redirects),
    (status_code, reason, hostname),
    secondary=True,  # 标记为次要错误，不影响可靠性评分
)
```

[searx/search/processors/online.py:214-219](file:///d:/fz/0508-2/solo-dogfeeding/code/29-searxng/searx/search/processors/online.py#L214-L219)

可靠性计算时会排除次要错误：
```python
# 仅统计非次要错误的百分比
reliability = 100 - sum([error['percentage'] for error in errors if not error.get('secondary')])
```

[searx/metrics/__init__.py:156](file:///d:/fz/0508-2/solo-dogfeeding/code/29-searxng/searx/metrics/__init__.py#L156)

---

## 7. 配置调优建议

### 7.1 根据实例规模调整

| 实例类型 | 建议配置 | 理由 |
|----------|----------|------|
| 私有实例（单用户） | `ban_time_on_fail: 2`, `max_ban_time_on_fail: 30` | 误封禁概率低，快速恢复 |
| 公共实例（高并发） | `ban_time_on_fail: 10`, `max_ban_time_on_fail: 60` | 避免频繁重试，减少源站压力 |
| 多代理部署 | `ban_time_on_fail: 3`, `max_ban_time_on_fail: 10` | 可通过切换出口快速恢复 |

### 7.2 监控告警阈值建议

| 指标 | 警告阈值 | 严重阈值 | 说明 |
|------|----------|----------|------|
| 单引擎错误率 | > 30% | > 70% | 5分钟滑动窗口 |
| 单引擎超时率 | > 20% | > 50% | 可能是网络或引擎性能问题 |
| 全引擎平均错误率 | > 10% | > 25% | 可能是出口网络故障 |
| L1级错误（CAPTCHA） | 单引擎1小时内 > 5次 | 单引擎1小时内 > 20次 | 需检查IP信誉或更换代理 |

---

## 8. 总结

SearXNG 的错误处理体系采用 **"分级挂起 + 固定时长恢复 + 成功自动重置"** 的策略，既保证了用户体验（避免长时间等待不可用引擎），又保护了源站和出口IP的信誉（避免频繁触发限流）。

**关键校准点：**
1. **普通错误挂起时长固定**：超时、SSL错误等普通错误挂起时长为 `min(max_ban_time_on_fail, ban_time_on_fail)`，默认 5 秒，不随连续错误次数递增
2. **指标统计受开关控制**：`general.enable_metrics` 控制详细错误上下文的记录和可靠性计算
3. **用户提示经过翻译映射**：异常类名通过 `exception_classname_to_text` 映射为用户友好文本，挂起时添加 "Suspended: " 前缀，不显示 `suspended_time` 等内部参数
4. **限流与访问拒绝影响明确**：L2/L3 级错误挂起 180 秒，L1 级错误挂起 1 天以上，期间完全跳过该引擎

**核心设计原则：**
1. **故障隔离**：单个引擎故障不影响全局搜索
2. **自动降级**：根据错误严重程度自动调整挂起时长
3. **快速恢复**：成功响应立即重置错误计数和挂起状态
4. **可观测性**：完整的错误上下文和指标统计（可通过配置开关）
