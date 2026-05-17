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

## 4. 用户提示与页面展示（按真实实现校准）

### 4.1 错误信息展示流程

错误信息从后端到前端的完整流程：

1. **错误捕获**：`handle_exception` 调用 `result_container.add_unresponsive_engine(engine_name, error_type, suspended)`
   - `error_type` 是异常类名（如 `httpx.TimeoutException`）或特殊字符串（如 `timeout`）
   - `suspended` 标记该错误是否触发了引擎挂起

2. **翻译映射**：`webutils.get_translated_errors()` 通过 `exception_classname_to_text` 字典将异常类名映射为用户可见文本

3. **前缀添加**：如果 `suspended=True`，在翻译后的文本前添加 "Suspended: " 前缀

4. **页面渲染**：在搜索结果页侧边栏的 "Response time" 区域展示

**关键说明：**
- 用户界面 **不会** 显示异常 message 中的 `suspended_time=180` 等内部参数
- 所有挂起的错误统一添加 "Suspended: " 前缀，而非 "Engine X is suspended" 格式
- 未挂起的错误直接显示翻译后的文本，无前缀

[searx/webutils.py:70-82](file:///d:/fz/0508-2/solo-dogfeeding/code/29-searxng/searx/webutils.py#L70-L82)

### 4.2 异常类名到用户文本的映射

| 异常类名 | 用户可见文本（英文） | 中文翻译示例 | 挂起时展示 |
|----------|----------------------|--------------|------------|
| `timeout` | `timeout` | 超时 | `Suspended: timeout` |
| `httpx.TimeoutException` | `timeout` | 超时 | `Suspended: timeout` |
| `httpx.ConnectTimeout` | `timeout` | 超时 | `Suspended: timeout` |
| `httpx.ReadTimeout` | `timeout` | 超时 | `Suspended: timeout` |
| `ssl.SSLCertVerificationError` | `SSL error: certificate validation has failed` | SSL错误：证书验证失败 | `Suspended: SSL error: certificate validation has failed` |
| `httpx.ConnectError` | `HTTP connection error` | HTTP连接错误 | `Suspended: HTTP connection error` |
| `httpx.HTTPStatusError` | `HTTP error` | HTTP错误 | `Suspended: HTTP error` |
| `httpx.ProxyError` | `proxy error` | 代理错误 | `Suspended: proxy error` |
| `searx.exceptions.SearxEngineCaptchaException` | `CAPTCHA` | 人机验证 | `Suspended: CAPTCHA` |
| `searx.exceptions.SearxEngineTooManyRequestsException` | `too many requests` | 请求过多 | `Suspended: too many requests` |
| `searx.exceptions.SearxEngineAccessDeniedException` | `access denied` | 访问被拒绝 | `Suspended: access denied` |
| `json.decoder.JSONDecodeError` | `parsing error` | 解析错误 | `parsing error`（不挂起） |
| `KeyError` | `parsing error` | 解析错误 | `parsing error`（不挂起） |
| 其他未匹配 | `unexpected crash` | 意外崩溃 | 视 suspended 参数而定 |

[searx/webutils.py:36-67](file:///d:/fz/0508-2/solo-dogfeeding/code/29-searxng/searx/webutils.py#L36-L67)

### 4.3 各错误等级的实际用户提示

#### L1 - CAPTCHA / 防火墙封禁

- **挂起时长**：1天 ~ 15天（视具体类型）
- **用户提示**：
  - Cloudflare CAPTCHA：`Suspended: CAPTCHA`
  - ReCAPTCHA：`Suspended: CAPTCHA`
  - Cloudflare 防火墙 1020：`Suspended: access denied`
- **展示位置**：搜索结果页侧边栏 "Response time" 表格中
- **后续请求**：挂起期间直接跳过该引擎，不再发送请求
- **健康度影响**：严重影响引擎可用性，需要管理员介入

#### L2 - 访问拒绝（HTTP 402/403）

- **挂起时长**：180秒（3分钟）
- **用户提示**：`Suspended: access denied`
- **后续请求**：挂起期间直接跳过该引擎
- **健康度影响**：错误计数 +1，成功率下降，可靠性评分降低

#### L3 - 限流触发（HTTP 429）

- **挂起时长**：180秒（3分钟）
- **用户提示**：`Suspended: too many requests`
- **后续请求**：挂起期间直接跳过该引擎，避免进一步触发源站限流
- **健康度影响**：错误计数 +1，成功率下降，管理员可能需要考虑降低请求频率或更换出口IP

#### L4 - 传输故障

| 具体错误类型 | 用户提示（挂起时） |
|--------------|-------------------|
| SSL证书验证失败 | `Suspended: SSL error: certificate validation has failed` |
| HTTP连接错误 | `Suspended: HTTP connection error` |
| 代理错误 | `Suspended: proxy error` |
| 其他HTTP错误 | `Suspended: HTTP error` |

- **挂起时长**：固定 5 秒（默认配置）
- **后续请求**：5秒后自动恢复，可继续接受请求
- **健康度影响**：错误计数 +1，短时波动不影响长期评分，频繁发生需检查网络

#### L5 - 响应超时

- **挂起时长**：固定 5 秒（默认配置）
- **用户提示**：`Suspended: timeout`
- **注意**：所有类型的超时（连接超时、读取超时、写入超时、asyncio超时）统一展示为 "timeout"
- **后续请求**：5秒后自动恢复
- **健康度影响**：错误计数 +1，若持续超时需检查引擎响应速度或增大 timeout 配置

#### L6 - 解析异常

- **挂起时长**：不挂起
- **用户提示**：`parsing error`（无前缀）
- **后续请求**：不影响，下次请求继续尝试
- **健康度影响**：错误计数 +1，通常表示引擎API格式变更，需维护引擎解析规则

### 4.4 结果完整性影响

- **单引擎失败**：不影响整体搜索，仅缺少该引擎结果，用户在 "Response time" 区域看到错误提示
- **多引擎同类错误**：如多个引擎同时出现SSL错误，可能是出口网络问题
- **全引擎失败**：页面显示 "Sorry, we didn't find any results"，并在侧边栏列出所有失败引擎
- **挂起期间**：引擎完全不可用，结果中不会包含该引擎的任何内容

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
