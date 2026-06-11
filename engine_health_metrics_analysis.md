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

### 5.5 指标关闭时的列隐藏逻辑（enable_metrics）

`enable_metrics` 是全局模板上下文变量，在每次渲染模板时注入。注入入口位于 [webapp.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/webapp.py#L424)：

```python
kwargs['enable_metrics'] = get_setting('general.enable_metrics')
```

#### 5.5.1 偏好设置页的列隐藏

偏好设置的引擎表格通过 **双重条件判断** 实现整列（表头 + 单元格）的隐藏。

**表头隐藏**（[engines.html](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/templates/simple/preferences/engines.html#L22-L37)）：
```html
<tr>
  <th class="checkbox-col">{{- _("Allow") -}}</th>
  <th class="name">{{- _("Engine name") -}}</th>
  ...
  <th>{{- _("Weight") }}</th>
  {%- if enable_metrics -%}
    <th>{{- _("Response time") -}}</th>       {# 指标启用时才渲染表头 #}
  {%- endif -%}
  <th>{{- _("Max time") -}}</th>
  {%- if enable_metrics -%}
    <th>{{- _("Reliability") }}</th>          {# 指标启用时才渲染表头 #}
  {%- endif -%}
</tr>
```

**单元格隐藏**（[engines.html](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/templates/simple/preferences/engines.html#L83-L91)）：
```html
<td>{{- search_engine.weight or '1.0' -}}</td>
{%- if enable_metrics -%}
  {{- engine_time(search_engine.name) -}}      {# 指标启用时才调用宏渲染时间 #}
{%- endif -%}
<td class="{{ 'danger' if stats[search_engine.name]['warn_timeout'] else '' }}">
  {{- search_engine.timeout -}}
</td>
{%- if enable_metrics -%}
  {{- engine_reliability(search_engine.name) -}}  {# 指标启用时才调用宏渲染可靠性 #}
{%- endif -%}
```

当 `enable_metrics=False` 时，"Response time" 和 "Reliability" 两列的 `<th>` 与对应的 `<td>` 完全不输出 HTML，表格列数自动减少。

#### 5.5.2 独立统计页的隐藏

`/stats` 页面本身是一个专门的统计展示页，**不通过 `enable_metrics` 条件隐藏内容**，始终调用 `get_engines_stats()` 和 `get_reliabilities()`。即便指标系统关闭，采样虽不发生，但页面仍可访问（只是表格中没有数据行，显示 "There is currently no data available."）。

#### 5.5.3 OpenMetrics 端点的隐藏

`/metrics` 端点在 [webapp.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/webapp.py#L1172-L1173) 中做硬拦截：

```python
if not (settings['general'].get("enable_metrics") and password):
    return Response('open metrics is disabled', status=404, mimetype='text/plain')
```

当 `enable_metrics=False` 或未配置密码时，直接返回 HTTP 404。

---

## 六、两个管理页面的统计聚合口径对比

`/preferences` 与 `/stats` 虽然都展示引擎指标，但聚合逻辑存在显著差异：

| 对比维度 | /preferences（偏好设置页） | /stats（独立统计页） |
|---------|---------------------------|---------------------|
| **聚合入口** | webapp.py 内联代码（L902-L954） | metrics 模块 `get_engines_stats()` + `get_reliabilities()` |
| **引擎过滤** | 所有引擎始终展示（含无数据） | `sent_count==0` 的引擎被 `continue` 跳过 |
| **响应时间维度** | 仅 `time.total` 的 P50/P80/P95 | `total`、`http`、`processing` 三个维度，各含 P50/P80/P95 |
| **结果数量算法** | 算术平均值：`result_count_sum / successful_count` | 中位数：`histogram.percentage(50)` |
| **得分展示** | 不展示 | 展示累计 `score` 和 `score_per_result` |
| **错误信息展示** | 仅 primary 异常且含 `exception_classname`，经友好翻译 | 全部错误（含 secondary），原始异常类名/日志消息 |
| **排序能力** | 按字母顺序固定排序 | 支持按 name/score/result_count/time/reliability 排序 |

### 6.1 结果数量的算法差异

**/preferences**（[webapp.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/webapp.py#L912-L914)）：
```python
result_count_sum = histogram('engine', e.name, 'result', 'count').sum   # 所有请求的结果数总和
successful_count = counter('engine', e.name, 'search', 'count', 'successful')  # 成功请求数
result_count = int(result_count_sum / float(successful_count)) if successful_count else 0
```
使用算术平均：总结果数 ÷ 成功请求数。当某些请求返回结果数极高时，平均值容易被拉高。

**/stats**（[metrics/__init__.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/metrics/__init__.py#L179)）：
```python
result_count = histogram('engine', e.name, 'result', 'count').percentage(50)  # P50 中位数
```
使用中位数（P50）：结果分布的中间值，不受极端值影响，更能代表典型单次请求的结果数量。

### 6.2 响应时间维度差异

**/preferences**（[webapp.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/webapp.py#L905-L908)）：
```python
h = histogram('engine', e.name, 'time', 'total')
median = round(h.percentage(50), 1) if h.count > 0 else None   # 仅 total 的 P50
rate80 = round(h.percentage(80), 1) if h.count > 0 else None   # 仅 total 的 P80
rate95 = round(h.percentage(95), 1) if h.count > 0 else None   # 仅 total 的 P95
```
只聚合 `time.total`，以堆叠条形图展示 median/P80/P95 三层，不区分 HTTP 耗时和结果解析耗时。

**/stats**（[metrics/__init__.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/metrics/__init__.py#L209-L232)）：
```python
time_http = histogram('engine', engine_name, 'time', 'http').percentage(50)
time_total = histogram('engine', engine_name, 'time', 'total').percentage(50)
# ... P80/P95 同样处理
stats['processing'] = round(time_total - (time_http or 0), 1)  # 解析时间 = 总时间 - HTTP 时间
```
同时展示 `total`（总耗时）、`http`（网络耗时）、`processing`（结果解析耗时 = total - http）三个维度，每个维度都有 P50/P80/P95，以 tooltip 表格形式详细展示。

### 6.3 引擎过滤差异

**/preferences**：遍历 `filtered_engines` 中的全部引擎，即便 `sent_count == 0` 也保留行记录，只是对应指标值为 `None`，由模板决定不渲染条形图。用户总能看到所有引擎的配置行。

**/stats**：在 `get_engines_stats()`（[metrics/__init__.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/metrics/__init__.py#L175-L177)）中直接跳过无请求引擎：
```python
sent_count = counter('engine', engine_name, 'search', 'count', 'sent')
if sent_count == 0:
    continue  # 无请求数据的引擎完全不出现在结果列表中
```
导致当所有引擎都无数据时，`engine_stats['time']` 为空列表，模板显示 "There is currently no data available."。

---

## 七、异常情况下的降级显示机制

### 7.1 指标系统禁用时的空对象模式

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

### 7.2 无请求数据时两页面的降级差异

无请求数据（`sent_count == 0`）时，两个页面采用完全不同的降级策略：

#### 7.2.1 /stats 独立统计页：整行过滤 + 页面级提示

**第一层（后端过滤）**：在 `get_engines_stats()`（[metrics/__init__.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/metrics/__init__.py#L175-L177)）中无请求引擎被直接跳过，不出现在数据列表中：
```python
sent_count = counter('engine', engine_name, 'search', 'count', 'sent')
if sent_count == 0:
    continue
```

**第二层（模板提示）**：当全部引擎都被过滤后，`engine_stats['time']` 为空列表，[stats.html](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/templates/simple/stats.html#L19-L21) 显示页面级提示：
```html
{% if not engine_stats.get('time') %}
{{ _('There is currently no data available. ') }}
{% else %}
... 渲染表格 ...
{% endif %}
```

**可靠性的特殊处理**：即便引擎有数据，`get_reliabilities()` 中无请求引擎的 `reliability` 也被设为 `None`（[metrics/__init__.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/metrics/__init__.py#L151-L153)），该值在排序时会让无数据引擎排到末尾。

#### 7.2.2 /preferences 偏好设置页：单元格级留白 + 空值降级

偏好设置页后端**不过滤无请求引擎**，所有引擎都会出现在列表中，降级完全由模板宏处理。

**响应时间降级**（[preferences.html](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/templates/simple/preferences.html#L89-L108)）：
```html
{%- macro engine_time(engine_name) -%}
  <td class="{{ label }}">{{- '' -}}
    {%- if stats[engine_name].time != None -%}
      <span class="stacked-bar-chart-value">{{- stats[engine_name].time -}}</span>
      <span class="stacked-bar-chart">...条形图...</span>
      <div class="engine-tooltip">...P50/P80/P95详情...</div>
    {%- endif -%}
  </td>
{%- endmacro -%}
```
- 当 `time == None` 时，宏不输出数值、条形图和 tooltip，仅输出一个空白 `<td>`，保留表格布局对齐。

**可靠性降级**（[preferences.html](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/templates/simple/preferences.html#L110-L145)）：
```html
{%- macro engine_reliability(engine_name) -%}
  {%- set r = reliabilities.get(engine_name, {}).get('reliability', None) -%}
  {%- if r != None -%}
    {%- if r <= 50 -%}{% set label = 'danger' -%}
    {%- elif r < 80 -%}{%- set label = 'warning' -%}
    ...
    {%- endif -%}
  {% else %}
    {%- set r = '' -%}       {# 关键：可靠性为 None 时降级为空字符串 #}
  {%- endif -%}
  {%- if errors -%}
    <td class="{{ label }} column-reliability"><a href="...">告警图标 + {{ r }}</a></td>
  {%- else -%}
    <td class="{{ label }}">{% if r %}<span>{{ r }}</span>{%- endif -%}</td>
  {%- endif -%}
{%- endmacro -%}
```
- 当 `reliability == None` 时，`r` 被降级为空字符串 `''`，同时 `label` 不被赋值（无颜色样式），最终 `<td>` 内什么都不渲染。
- 当存在错误但无可靠性数值时，仍显示告警图标并可跳转到 `/stats?engine=xxx`。

### 7.3 完整错误展示路径：偏好设置页 vs 统计页

#### 7.3.1 错误数据的两种来源

错误记录分为两类，由不同的 API 产生，数据字段有本质区别：

| 来源 API | 触发场景 | `exception_classname` | `log_message` |
|---------|---------|----------------------|---------------|
| `count_exception()`（[error_recorder.py#L174-L184](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/metrics/error_recorder.py#L174-L184)） | 捕获到 Python 异常对象时 | **有值**（如 `httpx.ConnectTimeout`） | `None` |
| `count_error()`（[error_recorder.py#L187-L200](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/metrics/error_recorder.py#L187-L200)） | 手动记录错误消息时（如"不支持的返回格式"） | `None` | **有值**（自定义消息字符串） |

两类错误都通过 `get_error_context()`（[error_recorder.py#L160-L171](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/metrics/error_recorder.py#L160-L171)）收集调用栈信息（文件名、行号、函数、代码行），最终以 `ErrorContext` 为 key 存储到 `errors_per_engines[engine_name]` 字典中计数。

#### 7.3.2 异常友好文本映射字典

技术异常类名到用户友好文本的映射由 `exception_classname_to_text` 字典（[webutils.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/webutils.py#L36-L67)）统一维护：

| 异常类 | 用户可见文本 |
|--------|-------------|
| `httpx.TimeoutException` / `asyncio.TimeoutError` | timeout |
| `httpx.HTTPStatusError` | HTTP error |
| `httpx.ConnectError` | HTTP connection error |
| `httpx.ProxyError` | proxy error |
| `SearxEngineCaptchaException` | CAPTCHA |
| `SearxEngineTooManyRequestsException` | too many requests |
| `SearxEngineAccessDeniedException` | access denied |
| `SearxEngineXPathException` / `JSONDecodeError` / `KeyError` | parsing error |
| `ssl.SSLCertVerificationError` | SSL error: certificate validation has failed |
| **None（未知异常）** | unexpected crash |

降级策略：字典中不存在的异常类名统一映射为 `exception_classname_to_text[None]` = "unexpected crash"。

---

#### 7.3.3 /preferences 偏好设置页：完整展示路径

**数据流动路径：**
```
errors_per_engines（原始存储）
    │
    ▼
get_engine_errors() → 计算 percentage，返回原始字典列表
    │
    ▼
webapp.py 内联代码（L944-L954）→ 三重过滤 + 翻译 + 去重
    │
    ▼
reliabilities[e.name]['errors'] = 友好文本字符串列表
    │
    ▼
模板 engine_reliability 宏 → tooltip 展示
```

**第一步：后端三重过滤与翻译**（[webapp.py](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/webapp.py#L944-L954)）

```python
reliabilities[e.name] = {'reliability': reliability, 'errors': []}
reliabilities_errors = []
for error in errors:
    # 过滤条件1：跳过 secondary 异常 或 无 exception_classname 的错误
    # 即：count_error() 产生的消息类错误在这里被完全过滤掉！
    if error.get('secondary') or 'exception_classname' not in error:
        continue
    # 过滤条件2：异常类名必须在 error 字典中（且非空）
    # 翻译：查字典获取友好文本
    error_user_text = exception_classname_to_text.get(error.get('exception_classname'))
    # 降级：字典查不到 → "unexpected crash"
    if not error_user_text:  # 注意：原代码写的是 if not error，疑似 bug，实际应为 if not error_user_text
        error_user_text = exception_classname_to_text[None]
    # 去重：同类错误合并，只保留一个
    if error_user_text not in reliabilities_errors:
        reliabilities_errors.append(error_user_text)
reliabilities[e.name]['errors'] = reliabilities_errors
```

**关键过滤逻辑：**
- `secondary=True` → **跳过**（如软重定向超限等警告类错误）
- 无 `exception_classname` → **跳过**（`count_error()` 产生的消息类错误完全不展示）
- 字典查不到 → **降级**为 "unexpected crash"
- 同类重复 → **去重**

**最终传给模板的数据结构：**
```python
reliabilities['google'] = {
    'reliability': 85,
    'errors': ['timeout', 'CAPTCHA']   # 注意：是字符串列表，不是原始字典！
}
```

**第二步：模板 tooltip 展示**（[preferences.html](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/templates/simple/preferences.html#L110-L145)）

```jinja2
{%- macro engine_reliability(engine_name) -%}
  {%- set r = reliabilities.get(engine_name, {}).get('reliability', None) -%}
  {%- set errors = reliabilities.get(engine_name, {}).get('errors', []) -%}

  {%- if r != None -%}
    {%- if r <= 50 -%}{% set label = 'danger' -%}
    {%- elif r < 80 -%}{%- set label = 'warning' -%}
    ...
    {%- endif -%}
  {% else %}
    {%- set r = '' -%}       {# 无数据 → 空字符串 #}
  {%- endif -%}

  {%- if errors -%}  {# 有错误才显示告警图标和超链接 #}
    <td class="{{ label }} column-reliability">
      <a href="{{ url_for('stats', engine=engine_name|e) }}">
        <span>{{ icon_big('alert', '...') }} {{ r }}</span>
      </a>
      <div class="engine-tooltip" role="tooltip">
        {%- if errors -%}<p>{{ _('Errors:') }}</p>{%- endif -%}
        {%- for error in errors -%}
          <p>{{ error }}</p>  {# 直接输出友好文本字符串 #}
        {%- endfor -%}
      </div>
    </td>
  {%- else -%}
    <td class="{{ label }}">
      {% if r %}<span>{{ r }}</span>{%- endif -%}
    </td>
  {%- endif -%}
{%- endmacro -%}
```

**展示逻辑：**
- `errors` 非空 → 单元格渲染为超链接（跳转到 `/stats?engine=xxx`），显示告警图标 + 可靠性数值
- hover 时 tooltip 逐行显示翻译后的错误类型（如 "timeout"、"CAPTCHA"）
- `errors` 为空 → 普通单元格，仅显示可靠性数值（无超链接、无告警图标）

---

#### 7.3.4 /stats 统计页：完整展示路径 + 单引擎展开逻辑

**数据流动路径：**
```
errors_per_engines（原始存储）
    │
    ▼
get_engine_errors() → 计算 percentage，返回原始字典列表
    │
    ▼
get_reliabilities() → 不做过滤和翻译，直接赋值给 errors
    │
    ▼
reliabilities[engine_name]['errors'] = 原始字典列表（含完整技术信息）
    │
    ├─► 列表视图：仅显示可靠性数值（[stats.html#L84](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/templates/simple/stats.html#L84)）
    │
    └─► 单引擎展开视图（URL带?engine=xxx）：完整错误详情
```

**第一步：后端不做任何过滤**（[metrics/__init__.py#L142-L163](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/metrics/__init__.py#L142-L163)）

```python
def get_reliabilities(engline_name_list):
    reliabilities = {}
    engine_errors = get_engine_errors(engline_name_list)  # 原始错误数据

    for engine_name in engline_name_list:
        errors = engine_errors.get(engine_name) or []
        # ... 计算 reliability ...

        reliabilities[engine_name] = {
            'reliability': reliability,
            'sent_count': sent_count,
            'errors': errors,  # 直接赋值，不做任何过滤或翻译！
        }
    return reliabilities
```

**传给模板的数据结构：**
```python
reliabilities['google'] = {
    'reliability': 85,
    'sent_count': 100,
    'errors': [
        {
            'filename': 'searx/engines/google.py',
            'function': 'parse_html',
            'line_no': 128,
            'code': 'results = root.xpath(xpath_str)',
            'exception_classname': 'httpx.ConnectTimeout',
            'log_message': None,
            'log_parameters': ('google.com', 443),
            'secondary': False,
            'percentage': 15,
        },
        ...  # 更多原始错误记录
    ]
}
```

**第二步：列表视图 — 仅显示可靠性数值**（[stats.html#L84](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/templates/simple/stats.html#L84)）

```jinja2
<td class="engine-reliability">
    {{ engine_reliabilities.get(engine_stat.name, {}).get('reliability') }}
</td>
```
列表视图非常简洁，仅显示可靠性百分比数值，不展示任何错误详情。

**第三步：单引擎展开 — URL 参数触发**

当 URL 包含 `?engine=google` 时，后端（[webapp.py#L1103-L1111](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/webapp.py#L1103-L1111)）：
```python
selected_engine_name = sxng_request.args.get('engine', default=None, type=str)
filtered_engines = dict(...)  # 所有引擎
if selected_engine_name:
    if selected_engine_name not in filtered_engines:
        selected_engine_name = None
    else:
        filtered_engines = [selected_engine_name]  # 仅查询该引擎
```

同时，后端还会拼接 `technical_report` 字符串（[webapp.py#L1133-L1146](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/webapp.py#L1133-L1146)），便于复制提交 bug：
```python
technical_report = []
for error in engine_reliabilities.get(selected_engine_name, {}).get('errors', []):
    technical_report.append(
        f"Error: {error['exception_classname'] or error['log_message']} "
        f"Parameters: {error['log_parameters']} "
        f"File name: {error['filename']}:{error['line_no']} "
        f"Error Function: {error['function']} "
        f"Code: {error['code']}"
    )
technical_report = ' '.join(technical_report)
```

**第四步：模板渲染完整错误详情**（[stats.html#L90-L127](file:///d:/fz/0601-1/solo-dogfeeding/code/14-searxng/searx/templates/simple/stats.html#L90-L127)）

```jinja2
{% if selected_engine_name %}
    <div class="engine-errors">
        {# 外层按 secondary 分组遍历：先 primary，后 secondary #}
        {% for secondary in [False, True] %}
            {% set ns = namespace(first=true) %}
            {% for error in engine_reliabilities[selected_engine_name].errors %}
                {% if secondary == error.secondary %}
                    {# 每组第一个错误前显示标题 #}
                    {% if ns.first %}
                        {% set ns.first = false %}
                        <h2>
                            {% if secondary %}
                                {{ _('Warnings') }}
                            {% else %}
                                {{ _('Errors and exceptions') }}
                            {% endif %}
                        </h2>
                    {% endif %}

                    <table class="engine-error">
                        <tbody>
                            <tr>
                                {# 有 exception_classname → 显示原始异常类名 #}
                                {%- if error.exception_classname -%}
                                    <th scope="row">{{ _('Exception') }}</th>
                                    <td>{{ error.exception_classname }}</td>
                                {# 有 log_message → 显示原始消息文本 #}
                                {%- elif error.log_message -%}
                                    <th scope="row">{{ _('Message') }}</th>
                                    <td>{{ error.log_message }}</td>
                                {%- endif -%}
                                <th scope="row">{{ _('Percentage') }}</th>
                                <td>{{ error.percentage }}</td>
                            </tr>
                            {# 有参数且不是 (None, None, None) → 显示参数行 #}
                            {% if error.log_parameters and error.log_parameters != (None, None, None) %}
                            <tr>
                                <th scope="row">{{ _('Parameter') }}</th>
                                <td colspan="3">
                                    {%- for param in error.log_parameters -%}
                                        <span class="log_parameters">{{ param }}</span>
                                    {%- endfor -%}
                                </td>
                            </tr>
                            {% endif %}
                            <tr><th scope="row">{{ _('Filename') }}</th>
                                <td colspan="3">{{ error.filename }}:{{ error.line_no }}</td></tr>
                            <tr><th scope="row">{{ _('Function') }}</th>
                                <td colspan="3">{{ error.function }}</td></tr>
                            <tr><th scope="row">{{ _('Code') }}</th>
                                <td colspan="3">{{ error.code }}</td></tr>
                        </tbody>
                    </table>
                {% endif %}
            {% endfor %}
        {% endfor %}
    </div>
{% endif %}
```

**单引擎展开逻辑：**
- **触发条件**：URL 带 `?engine=xxx` 参数（来自 preferences 页的告警图标超链接，或 stats 页引擎名称的超链接）
- **标题显示**：按 secondary 分组，先显示 "Errors and exceptions"（primary 错误），后显示 "Warnings"（secondary 错误），每组为空则不显示标题
- **异常名称显示**：
  - 有 `exception_classname` → 直接显示原始类名（如 `httpx.ConnectTimeout`），**不做友好翻译**
  - 有 `log_message` → 直接显示原始消息文本（如 "unsupported response format"）
- **完整上下文**：每个错误都显示百分比、参数、文件名:行号、函数名、出错代码行
- **特殊处理**：`log_parameters == (None, None, None)` 时不显示参数行

---

#### 7.3.5 友好文案 vs 原始详情：切换条件总览

| 场景 | /preferences 行为 | /stats 行为 |
|------|------------------|------------|
| **count_exception() 产生的异常（有 exception_classname）** | 查 `exception_classname_to_text` 字典 → 友好文本 | 直接显示 `exception_classname` 原始字符串 |
| **字典中不存在的未知异常** | 降级为 `"unexpected crash"` | 直接显示原始类名（如 `requests.exceptions.SSLError`） |
| **count_error() 产生的消息（有 log_message，无 exception_classname）** | **被过滤，完全不显示** | 直接显示 `log_message` 原始文本 |
| **secondary=True 的次要错误** | **被过滤，完全不显示** | 归入 "Warnings" 分组，显示完整技术详情 |
| **同类错误多次出现** | 去重，tooltip 中只显示一次 | 按 `ErrorContext` 分组，不同上下文（不同行号/参数）分开显示 |
| **无任何错误** | 无告警图标，无超链接，无 tooltip | 列表视图正常显示可靠性，单引擎展开无错误区块 |

---

#### 7.3.6 两页面对比总结表

| 维度 | /preferences | /stats |
|------|-------------|--------|
| **后端处理** | 三重过滤 + 翻译 + 去重 | 不做任何处理，原始透传 |
| **传给模板的 errors** | 友好文本字符串列表（如 `['timeout']`） | 原始字典列表（含完整技术字段） |
| **错误范围** | 仅 primary 且含 exception_classname | primary + secondary，全量展示 |
| **消息类错误（count_error）** | 完全过滤不显示 | 归入 Message 行展示 |
| **异常名称** | 友好翻译（"timeout"、"CAPTCHA"…） | 原始类名（"httpx.ConnectTimeout"…） |
| **未知异常降级** | 翻译为 "unexpected crash" | 直接显示原始类名 |
| **展示形式** | 单元格超链接 + hover tooltip | 列表缩略（仅数值）+ 单引擎展开（完整详情） |
| **单引擎展开入口** | 可靠性单元格的超链接 | 引擎名称的超链接 |
| **面向用户** | 普通用户，快速判断引擎状态 | 管理员/开发者，问题定位与 bug 报告 |

### 7.4 可靠性阈值分级着色

在 `engine_reliability()` 宏中按阈值为单元格添加 CSS class：

| 可靠性范围 | CSS 类 | 视觉效果 |
|-----------|--------|---------|
| `r <= 50` | `danger` | 红色警示 |
| `50 < r < 80` | `warning` | 黄色警告 |
| `80 <= r < 90` | （无） | 默认样式 |
| `r >= 90` | `success` | 绿色正常 |

同时，若引擎存在错误记录，会显示告警图标并提供指向 `/stats?engine=xxx` 的错误详情链接。

### 7.5 排序中的空值保护

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

## 八、数据流转总结图

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
