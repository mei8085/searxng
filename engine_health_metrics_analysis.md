# SearXNG 搜索引擎健康度与统计指标系统分析报告

## 一、系统架构总览

SearXNG 的指标系统采用 **"请求驱动采样 + 内存累积存储 + 按需聚合展示"** 的架构模式。核心模块分布如下：

| 模块 | 职责 | 核心文件 |
|------|------|----------|
| 指标数据模型 | 定义直方图(Histogram)和计数器(Counter)的数据结构 | [models.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/metrics/models.py) |
| 指标初始化与接口 | 暴露采样 API，组织指标配置 | [__init__.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/metrics/__init__.py) |
| 错误记录器 | 分类记录引擎异常，计算可靠性指标 | [error_recorder.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/metrics/error_recorder.py) |
| 搜索处理器 | 在搜索流程各阶段埋点采样 | [abstract.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/search/processors/abstract.py) |
| 结果容器 | 结果数量与得分采样 | [results.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/results.py) |
| Web 应用路由 | 指标展示与 API 暴露 | [webapp.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/webapp.py) |

---

## 二、指标初始化与配置

### 2.1 初始化入口

系统启动时通过 [searx/search/__init__.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/search/__init__.py#L33-L44) 的 `initialize()` 函数触发指标初始化：

```python
def initialize(settings_engines=None, check_network=False, enable_metrics=True):
    # ...
    initialize_metrics([engine['name'] for engine in settings_engines], enable_metrics)
    # ...
```

配置开关来自 `settings.yml` 的 `general.enable_metrics`，默认值为 `True`（见 [settings_defaults.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/settings_defaults.py#L186)）。

### 2.2 指标项配置

在 [metrics/__init__.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/metrics/__init__.py#L70-L108) 的 `initialize()` 函数中，为每个引擎配置以下指标：

**计数器类（Counter）指标：**
- `engine.{name}.search.count.sent` - 已发送请求数
- `engine.{name}.search.count.successful` - 成功请求数
- `engine.{name}.search.count.error` - 错误请求数
- `engine.{name}.score` - 结果累计得分

**直方图类（Histogram）指标：**
- `engine.{name}.result.count` - 单次请求返回结果数量（分桶 1-100）
- `engine.{name}.time.http` - HTTP 请求响应时间（分桶宽度 0.1s，上限 1.5×最大超时）
- `engine.{name}.time.total` - 引擎总处理时间（分桶宽度 0.1s，上限 1.5×最大超时）

直方图分桶宽度与数量由所有引擎的最大超时时间动态计算：
```python
histogram_width = 0.1
histogram_size = int(1.5 * max_timeout / histogram_width)
```

---

## 三、周期采样机制（埋点分布）

SearXNG 采用 **事件驱动采样** 而非定时轮询。采样点分布在搜索请求生命周期的关键节点：

### 3.1 搜索请求发出阶段

位置：[searx/search/__init__.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/search/__init__.py#L102)

```python
# 在 _get_requests() 中，为每个即将执行的引擎请求计数
counter_inc('engine', engineref.name, 'search', 'count', 'sent')
```

### 3.2 搜索成功完成阶段

位置：[searx/search/processors/abstract.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/search/processors/abstract.py#L193-L208)

在 `_extend_container_basic()` 方法中完成成功路径的指标采样：

```python
def _extend_container_basic(self, result_container, start_time, search_results):
    result_container.extend(self.engine.name, search_results)
    engine_time = default_timer() - start_time          # 总耗时
    page_load_time = get_time_for_thread()               # HTTP 耗时

    counter_inc('engine', self.engine.name, 'search', 'count', 'successful')
    histogram_observe(engine_time, 'engine', self.engine.name, 'time', 'total')
    if page_load_time is not None:
        histogram_observe(page_load_time, 'engine', self.engine.name, 'time', 'http')
```

### 3.3 搜索异常阶段

位置：[searx/search/processors/abstract.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/search/processors/abstract.py#L165-L191)

`handle_exception()` 方法处理各类异常：

```python
def handle_exception(self, result_container, exception_or_message, suspend=False):
    result_container.add_unresponsive_engine(self.engine.name, error_message)

    counter_inc('engine', self.engine.name, 'search', 'count', 'error')
    if isinstance(exception_or_message, BaseException):
        count_exception(self.engine.name, exception_or_message)  # 记录异常详情
    else:
        count_error(self.engine.name, exception_or_message)      # 记录错误消息
```

异常分类由 [error_recorder.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/metrics/error_recorder.py) 处理，通过 `ErrorContext` 类以（文件名、函数、行号、代码、异常类、日志消息、参数、是否次要）为键进行聚合计数。

### 3.4 结果统计阶段

位置：[searx/results.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/results.py#L154-L195)

```python
# 在 extend() 中记录单引擎返回结果数量
if engine_name in searx.engines.engines:
    histogram_observe(main_count, "engine", eng.name, "result", "count")

# 在 close() 中累计每个引擎的结果得分
for result in self.main_results_map.values():
    result.score = calculate_score(result, result.priority)
    for eng_name in result.engines:
        counter_add(result.score, 'engine', eng_name, 'score')
```

---

## 四、数据存储结构

### 4.1 计数器存储（CounterStorage）

位置：[models.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/metrics/models.py#L130-L158)

```python
class CounterStorage:
    __slots__ = 'counters', 'lock'

    def __init__(self):
        self.lock = threading.Lock()
        self.counters = {}  # key: tuple[str, ...], value: int
```

- 线程安全：使用 `threading.Lock` 保护 `counters` 字典
- 以多级元组 `('engine', name, 'search', 'count', 'sent')` 作为键
- 操作：`configure()` 初始化、`add()` 累加、`get()` 查询

### 4.2 直方图存储（Histogram）

位置：[models.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/metrics/models.py#L17-L101)

```python
class Histogram:
    _slots__ = '_lock', '_size', '_sum', '_quartiles', '_count', '_width'

    def observe(self, value):
        q = int(value / self._width)          # 计算分桶索引
        if q < 0: q = 0
        if q >= self._size: q = self._size - 1  # 越值归入最后一桶
        with self._lock:
            self._quartiles[q] += 1
            self._count += 1
            self._sum += value
```

- 定宽分桶数组 `_quartiles`，宽度由配置决定
- `percentage(p)` 方法计算 P50/P80/P95 等分位数
- 同时维护 `_count`（样本数）和 `_sum`（累计值）用于平均值计算

---

## 五、管理界面展示逻辑

### 5.1 独立统计页面（/stats）

位置：[webapp.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/webapp.py#L1100-L1158)，模板：[stats.html](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/templates/simple/stats.html)

核心流程：
```python
@app.route('/stats', methods=['GET'])
def stats():
    engine_stats = get_engines_stats(filtered_engines)        # 获取性能指标
    engine_reliabilities = get_reliabilities(filtered_engines)  # 获取可靠性指标
    # 排序后渲染
    engine_stats['time'] = sorted(engine_stats['time'], reverse=reverse, key=get_key)
    return render('stats.html', ...)
```

**`get_engines_stats()`** 聚合出每个引擎的：
- 响应时间：median(P50)、P80、P95（区分 total/http/processing）
- 结果数量中位数
- 累计得分与单结果平均得分

**`get_reliabilities()`** 计算可靠性分数：
```python
reliability = 100 - sum([error['percentage'] for error in errors if not error.get('secondary')])
```
可靠性 = 100% 减去所有非次要（primary）错误的百分比之和。

**STATS_SORT_PARAMETERS** 排序配置（[webapp.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/webapp.py#L137-L143)）：
```python
STATS_SORT_PARAMETERS = {
    'name': (False, 'name', ''),
    'score': (True, 'score_per_result', 0),
    'result_count': (True, 'result_count', 0),
    'time': (False, 'total', 0),
    'reliability': (False, 'reliability', 100),
}
```

排序时先按有无可靠性分组（无可靠性数据的排后），再按指定字段排序。

### 5.2 偏好设置页面（/preferences）

位置：[webapp.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/webapp.py#L890-L954)，模板：[preferences.html](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/templates/simple/preferences.html#L89-L145) + [engines.html](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/templates/simple/preferences/engines.html)

在偏好设置的引擎列表中，展示每个引擎的：
- **响应时间**：`engine_time()` 宏渲染堆叠条形图（中位数/P80/P95）
- **可靠性**：`engine_reliability()` 宏按阈值着色显示

### 5.3 OpenMetrics 端点（/metrics）

位置：[webapp.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/webapp.py#L1168-L1184)

以 Prometheus 兼容格式暴露以下指标族：
- `searxng_engines_response_time_total_seconds`
- `searxng_engines_response_time_processing_seconds`
- `searxng_engines_response_time_http_seconds`
- `searxng_engines_result_count_total`
- `searxng_engines_request_count_total`
- `searxng_engines_reliability_total`

需要在 `settings.yml` 中同时启用 `enable_metrics` 和配置 `open_metrics` 密码，且需 HTTP Basic 认证。

### 5.4 错误详情 API（/stats/errors）

位置：[webapp.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/webapp.py#L1161-L1165)

以 JSON 格式返回每个引擎的完整错误分类统计。

---

## 六、异常情况下的降级显示机制

### 6.1 指标系统禁用时的空对象模式

当 `enable_metrics=False` 时，初始化使用空实现（[metrics/__init__.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/metrics/__init__.py#L76-L81)）：

```python
if enabled:
    counter_storage = CounterStorage()
    histogram_storage = HistogramStorage()
else:
    counter_storage = VoidCounterStorage()      # add() 空操作
    histogram_storage = HistogramStorage(histogram_class=VoidHistogram)  # observe() 空操作
```

**VoidHistogram**（[models.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/metrics/models.py#L161-L163)）：`observe()` 方法直接 `pass`，不产生任何开销。

**VoidCounterStorage**（[models.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/metrics/models.py#L166-L168)）：`add()` 方法直接 `pass`。

同时，`count_exception()` 和 `count_error()` 在入口处检查开关（[error_recorder.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/metrics/error_recorder.py#L174-L199)）：
```python
def count_exception(engine_name, exc, secondary=False):
    if not settings['general']['enable_metrics']:
        return  # 直接返回，跳过所有错误记录逻辑
```

### 6.2 无数据时的降级展示

**（1）stats.html 页面**（[stats.html](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/templates/simple/stats.html#L19-L21)）：
```html
{% if not engine_stats.get('time') %}
{{ _('There is currently no data available. ') }}
{% else %}
...
{% endif %}
```

**（2）/stats 路由中 sent_count==0 的引擎会被过滤**（[metrics/__init__.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/metrics/__init__.py#L175-L177)）：
```python
sent_count = counter('engine', engine_name, 'search', 'count', 'sent')
if sent_count == 0:
    continue  # 跳过无请求的引擎
```

**（3）可靠性为 None 时的处理**（[metrics/__init__.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/metrics/__init__.py#L151-L153)）：
```python
if sent_count == 0:
    reliability = None  # 标记为无数据
```

**（4）偏好设置页 engine_time 宏**（[preferences.html](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/templates/simple/preferences.html#L91-L106)）：
```html
{%- if stats[engine_name].time != None -%}
    {# 渲染时间条形图 #}
{%- endif -%}
{# 否则输出空 <td> #}
```

**（5）偏好设置页 engine_reliability 宏**（[preferences.html](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/templates/simple/preferences.html#L110-L145)）：
```html
{%- if r != None -%}
    {# 显示可靠性数值 + 按阈值着色 #}
{% else %}
    {%- set r = '' -%}  {# 降级为空字符串 #}
{%- endif -%}
```

### 6.3 异常类名到用户友好文本的映射

位置：[webutils.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/webutils.py#L36-L67)

`exception_classname_to_text` 字典将技术异常类名翻译为用户可理解的文本：

| 异常类 | 用户可见文本 |
|--------|-------------|
| `httpx.TimeoutException` / `asyncio.TimeoutError` | timeout |
| `httpx.HTTPStatusError` | HTTP error |
| `httpx.ConnectError` | HTTP connection error |
| `httpx.ProxyError` | proxy error |
| `SearxEngineCaptchaException` | CAPTCHA |
| `SearxEngineTooManyRequestsException` | too many requests |
| `SearxEngineAccessDeniedException` | access denied |
| `SearxEngineXPathException` / `JSONDecodeError` | parsing error |
| `ssl.SSLCertVerificationError` | SSL error: certificate validation has failed |
| **None（未知异常）** | unexpected crash |

降级策略：未知异常统一映射为 `exception_classname_to_text[None]` = "unexpected crash"。

### 6.4 可靠性阈值分级着色

在 `engine_reliability()` 宏中按阈值为单元格添加 CSS class：

| 可靠性范围 | CSS 类 | 视觉效果 |
|-----------|--------|---------|
| `r <= 50` | `danger` | 红色警示 |
| `50 < r < 80` | `warning` | 黄色警告 |
| `80 <= r < 90` | （无） | 默认样式 |
| `r >= 90` | `success` | 绿色正常 |

同时，若引擎存在错误记录，会显示告警图标并提供指向 `/stats?engine=xxx` 的错误详情链接。

### 6.5 排序中的空值保护

在 `/stats` 路由的排序逻辑中（[webapp.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/webapp.py#L1121-L1131)），对可靠性为 None 的引擎进行特殊处理：

```python
def get_key(engine_stat):
    reliability = engine_reliabilities.get(engine_stat['name'], {}).get('reliability', 0)
    reliability_order = 0 if reliability else 1  # None/0 的引擎排到后面
    # ...
    return (reliability_order, key, engine_stat['name'])
```

确保无数据的引擎始终排在有数据的引擎之后，避免干扰正常排序。

---

## 七、数据流转总结图

```
用户搜索请求
    │
    ▼
Search._get_requests() ──► counter_inc(sent)
    │
    ▼
多线程调用 Processor.search()
    │
    ├── 成功路径 ──► _extend_container_basic()
    │                     ├── counter_inc(successful)
    │                     ├── histogram_observe(time.total)
    │                     └── histogram_observe(time.http)
    │
    └── 失败路径 ──► handle_exception()
                          ├── counter_inc(error)
                          └── count_exception() / count_error()
                                │
                                ▼
                          errors_per_engines[engine][ErrorContext]++
    │
    ▼
ResultContainer.extend() ──► histogram_observe(result.count)
    │
    ▼
ResultContainer.close() ──► counter_add(score)
    │
    ▼
用户访问 /stats 或 /preferences
    │
    ▼
get_engines_stats() 按需聚合 ──► P50/P80/P95、得分、结果数
get_reliabilities() 按需聚合 ──► 可靠性百分比、错误分类
    │
    ▼
模板渲染（stats.html / preferences.html）
    │
    └──► 异常降级（无数据、禁用指标、友好错误文本）
```
