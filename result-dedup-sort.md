# SearXNG 搜索结果去重与排序机制深度分析

## 一、整体架构概述

SearXNG 作为元搜索引擎，其结果处理流程包含以下核心阶段：

1. **查询入口层**：Web 请求接收、参数解析、查询构建
2. **搜索执行层**：多引擎并行请求、结果收集
3. **结果处理层**：去重、打分、排序、分组
4. **异常处理层**：超时、重试、熔断机制

---

## 二、查询入口与搜索流程初始化

### 2.1 Web 入口点

**文件**：`searx/webapp.py` (第 620 行)

```python
def search():
    # ...
    search_query, raw_text_query, _, _, selected_locale = get_search_query_from_webapp(...)
    search_obj = searx.search.SearchWithPlugins(search_query, sxng_request, sxng_request.user_plugins)
```

### 2.2 查询构建流程

**文件**：`searx/webadapter.py`

1. **参数解析**：
   - `parse_pageno()`: 页码解析，最小为 1
   - `parse_lang()`: 语言解析，支持查询中的 `!lang` 语法
   - `parse_safesearch()`: 安全搜索级别 (0, 1, 2)
   - `parse_time_range()`: 时间范围过滤 (day/week/month/year)
   - `parse_timeout()`: 超时限制

2. **引擎引用构建**：
   ```python
   EngineRef(name: str, category: str)  # searx/search/models.py
   ```
   
3. **去重与验证**：
   ```python
   # webadapter.py:16
   def deduplicate_engineref_list(engineref_list: List[EngineRef]) -> List[EngineRef]:
       engineref_dict = {q.category + '|' + q.name: q for q in engineref_list}
       return list(engineref_dict.values())
   ```

### 2.3 SearchQuery 模型

**文件**：`searx/search/models.py` (第 28-119 行)

```python
@typing.final
class SearchQuery:
    def __init__(
        self,
        query: str,
        engineref_list: list[EngineRef],
        lang: str = 'all',
        safesearch: typing.Literal[0, 1, 2] = 0,
        pageno: int = 1,
        time_range: typing.Literal["day", "week", "month", "year"] | None = None,
        timeout_limit: float | None = None,
        external_bang: str | None = None,
        engine_data: dict[str, dict[str, str]] | None = None,
        redirect_to_first_result: bool | None = None,
    ):
```

**重要修正 - 关于可变性**：

> ⚠️ **`@typing.final` 装饰器仅表示「禁止被继承」，不代表对象不可变**
> 
> SearchQuery 对象实际上是**可变**的：
> - 未使用 `frozen=True` (msgspec) 或 `__slots__` 进行不可变约束
> - 所有属性（query, lang, pageno 等）均可直接修改
> - 提供 `__copy__` 方法暗示需要复制而非直接复用
> - 实现 `__hash__` 和 `__eq__` 是为了支持缓存键比较，与不可变性无关
> 
> **设计意图**：SearchQuery 在单次搜索生命周期内保持一致，但不强制不可变，便于插件和中间件在 `pre_search` 钩子中调整参数。

**核心特性**：
- 禁止继承（`@typing.final`）
- 支持哈希，可用于缓存键
- 包含完整的搜索上下文
- 支持浅拷贝（`__copy__`）

---

## 三、结果模型体系

### 3.1 结果类型层级

**文件**：`searx/result_types/_base.py`

```
Result (msgspec.Struct, kw_only=True)
├── MainResult (主搜索结果)
├── Answer (问答类结果)
│   ├── Translations
│   └── WeatherAnswer
├── KeyValue (键值对结果)
├── Code (代码片段)
├── Paper (学术论文)
├── File (文件结果)
└── LegacyResult (dict 子类，向后兼容)

EngineResults (结果列表容器)
└── 内部维护 types 命名空间，便于引擎开发者使用
```

### 3.2 MainResult 核心结构

```python
class MainResult(Result):
    template: str = "default.html"      # 渲染模板
    title: str = ""                     # 标题
    content: str = ""                   # 内容摘要
    img_src: str = ""                   # 图片源
    publishedDate: datetime | None = None  # 发布时间
    engines: set[str] = set()           # 来源引擎集合（合并后）
    positions: list[int] = []           # 在各引擎中的排名位置
    score: float = 0                    # 计算得分
    category: str = ""                  # 分类
```

### 3.3 哈希与相等性（去重基础）

**关键实现** (`_base.py` 第 405-418 行)：

```python
def __hash__(self) -> int:
    """基于 URL 去重，忽略协议差异"""
    if not self.parsed_url:
        raise ValueError(f"missing a value in field 'parsed_url': {self}")
    
    url = self.parsed_url
    return hash(
        f"{self.template}"
        + f"|{url.netloc}|{url.path}|{url.params}|{url.query}|{url.fragment}"
        + f"|{self.img_src}"
    )
```

**去重逻辑说明**：
- 仅使用 `netloc + path + params + query + fragment`
- **忽略 URL scheme** (http/https 视为相同)
- 结合 `template` 和 `img_src` 进一步区分
- LegacyResult 对图片结果有特殊哈希逻辑（包含完整 URL）

---

## 四、网络层协作机制详解

### 4.1 Network 核心类

**文件**：`searx/network/network.py`

```python
class Network:
    def __init__(
        self,
        enable_http: bool = True,
        verify: bool = True,
        enable_http2: bool = False,
        max_connections: int = None,
        max_keepalive_connections: int = None,
        keepalive_expiry: float = None,
        proxies: str | dict | None = None,
        using_tor_proxy: bool = False,
        local_addresses: str | list | None = None,
        retries: int = 0,
        retry_on_http_error: bool = False,
        max_redirects: int = 30,
    ):
```

### 4.2 多网络隔离

```python
NETWORKS: dict[str, "Network"] = {}  # 网络实例字典

# 预定义网络
NETWORKS[DEFAULT_NAME] = Network()           # 默认
NETWORKS['ipv4'] = Network(local_addresses='0.0.0.0')  # IPv4 强制
NETWORKS['ipv6'] = Network(local_addresses='::')       # IPv6 强制
NETWORKS['image_proxy'] = Network(enable_http2=False)  # 图片代理专用
```

**每个引擎可独立配置网络**：
- 支持独立代理设置
- 支持独立源 IP 绑定
- 支持独立重试策略

### 4.3 线程本地上下文与超时链路

**文件**：`searx/network/__init__.py` (第 28-108 行)

```python
THREADLOCAL = threading.local()  # 线程本地存储
```

**关键链路函数**：

```python
# 1. 初始化线程上下文（处理器层调用）
def init_network_in_thread(self, start_time, timeout_limit):
    searx.network.set_timeout_for_thread(timeout_limit, start_time=start_time)
    searx.network.reset_time_for_thread()
    searx.network.set_context_network_name(self.engine.name)

# 2. 动态计算实际超时
def _get_timeout(start_time, kwargs):
    timeout = kwargs.get('timeout') or getattr(THREADLOCAL, 'timeout', None) or 120
    timeout += 0.2  # 预留开销
    if start_time:
        timeout -= default_timer() - start_time  # 减去已消耗时间
    return timeout

# 3. 请求执行（同步 → 异步桥接）
def request(method, url, **kwargs):
    with _record_http_time() as start_time:
        network = get_context_network()  # 从线程本地获取网络实例
        timeout = _get_timeout(start_time, kwargs)
        future = asyncio.run_coroutine_threadsafe(
            network.request(method, url, **kwargs),
            get_loop(),  # 全局异步事件循环
        )
        try:
            return future.result(timeout)  # 阻塞等待异步结果
        except concurrent.futures.TimeoutError as e:
            raise httpx.TimeoutException('Timeout', request=None) from e
```

### 4.4 在线处理器完整流程

**文件**：`searx/search/processors/online.py`

```
search() 入口
    ├─ init_network_in_thread()  # 线程级网络初始化
    │   ├─ set_timeout_for_thread(limit, start_time)  # 写入线程本地
    │   ├─ reset_time_for_thread()                    # 清零 HTTP 计时
    │   └─ set_context_network_name(engine_name)      # 绑定网络实例
    │
    └─ _search_basic()
        ├─ engine.request(query, params)  # 引擎自定义请求构建
        ├─ _send_http_request(params)     # 发送 HTTP 请求
        │   └─ searx.network.get/post(...)  # 调用网络层
        │       └─ _record_http_time 上下文
        │           ├─ 动态计算剩余超时
        │           └─ 异步桥接执行
        └─ engine.response(response)     # 解析响应 → EngineResults
```

### 4.5 全局事件循环模型

```
┌─────────────────────────────────────────────────┐
│  主线程 (Flask 请求处理)                        │
│  ├─ Search.search_standard()                    │
│  │  └─ 为每个引擎启动独立线程                    │
│  │     └─ thread.join(actual_timeout)           │
│  │                                               │
└─────────────────────┬───────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────┐
│  引擎线程 (每个引擎一个)                         │
│  ├─ init_network_in_thread()  ← 线程本地上下文   │
│  ├─ engine.request()                            │
│  ├─ network.request()                            │
│  │  └─ asyncio.run_coroutine_threadsafe()       │
│  │     └─ 桥接至全局事件循环                     │
│  └─ engine.response()                           │
└─────────────────────┬───────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────┐
│  全局异步事件循环线程 (唯一)                     │
│  └─ 执行所有 httpx.AsyncClient 请求              │
└─────────────────────────────────────────────────┘
```

---

## 五、结果去重算法详解

### 5.1 ResultContainer 核心容器

**文件**：`searx/results.py` (第 53 行)

```python
class ResultContainer:
    main_results_map: dict[int, MainResult | LegacyResult]  # 哈希 → 结果
    infoboxes: list[LegacyResult]
    suggestions: set[str]
    answers: AnswerSet
    corrections: set[str]
```

### 5.2 结果合并流程

```
extend(engine_name, results) 入口
    │
    ├─ 遍历每个结果
    │   ├─ Result 类型 → normalize_result_fields()
    │   ├─ LegacyResult 类型 → 向后兼容处理
    │   └─ 检查 on_result 插件钩子
    │
    └─ 按类型分发:
        ├─ 建议 → suggestions 集合（自动去重）
        ├─ 答案 → answers.add()
        ├─ 修正 → corrections 集合
        ├─ 信息框 → _merge_infobox()
        ├─ engine_data → 存储到 engine_data 字典
        ├─ number_of_results → 追加到统计列表
        └─ 主结果 → _merge_main_result()
```

### 5.3 主结果去重实现 (`_merge_main_result`)

```python
# results.py:173-187
def _merge_main_result(self, result: MainResult | LegacyResult, position: int):
    result_hash = hash(result)  # 计算哈希键
    
    with self._lock:  # 线程安全（多引擎并发写入）
        merged = self.main_results_map.get(result_hash)
        if not merged:
            # 无重复，直接添加
            result.positions = [position]
            self.main_results_map[result_hash] = result
            return
        
        # 存在重复，合并两个结果
        merge_two_main_results(merged, result)
        merged.positions.append(position)  # 追加位置信息
```

### 5.4 合并策略 (`merge_two_main_results`)

```python
# results.py:357-381
def merge_two_main_results(origin, other):
    # 内容：选择更长的文本
    if len(other.content or "") > len(origin.content or ""):
        origin.content = other.content
    
    # 标题：选择更长的标题
    if len(other.title or "") > len(origin.title or ""):
        origin.title = other.title
    
    # 字段合并：缺失字段从另一个结果补充
    origin.defaults_from(other)
    
    # 引擎集合合并（重要！追踪所有来源）
    origin.engines.add(other.engine or "")
    
    # URL 规范化：优先使用 HTTPS
    if origin.parsed_url and not origin.parsed_url.scheme.endswith("s"):
        if other.parsed_url and other.parsed_url.scheme.endswith("s"):
            origin.parsed_url = origin.parsed_url._replace(scheme=other.parsed_url.scheme)
            origin.url = origin.parsed_url.geturl()
```

### 5.5 Infobox 合并策略

```python
# results.py:297-354
def merge_two_infoboxes(origin, other):
    # 权重比较：选择高权重引擎的结果作为基准
    weight1 = getattr(engines[origin.engine], 'weight', 1)
    weight2 = getattr(engines[other.engine], 'weight', 1)
    
    if weight2 > weight1:
        origin.engine = other.engine  # 替换主导引擎
    
    # URL 列表合并（去重）
    for url2 in other.urls:
        unique_url = True
        for url1 in origin.urls:
            # 比较 entity 或 url 字符串
            if (entity_url2 is not None and entity_url2 == url1.get("entity")) or \
               (url1.get("url") == url2.get("url")):
                unique_url = False
                break
        if unique_url:
            origin.urls.append(url2)
    
    # 属性合并（label/entity 去重）
    for attr in other.attributes:
        if attr.get("label") not in attr_names and attr.get('entity') not in attr_names:
            origin.attributes.append(attr)
    
    # 图片：优先选择高权重引擎的图片
    if other.img_src and (not origin.img_src or weight2 > weight1):
        origin.img_src = other.img_src
```

---

## 六、排序算法与打分机制

### 6.1 得分计算函数

**文件**：`searx/results.py` (第 17-38 行)

```python
def calculate_score(
    result: MainResult | LegacyResult,
    priority: MainResult.PriorityType,
) -> float:
    weight = 1.0

    # 步骤1：累积引擎权重
    for result_engine in result['engines']:
        if hasattr(searx.engines.engines.get(result_engine), 'weight'):
            weight *= float(searx.engines.engines[result_engine].weight)

    # 步骤2：乘以出现次数
    weight *= len(result['positions'])
    score = 0

    # 步骤3：基于位置的加权（排名越靠前权重越高）
    for position in result['positions']:
        if priority == 'low':
            continue  # 低优先级不计分
        if priority == 'high':
            score += weight  # 高优先级不考虑位置
        else:
            score += weight / position  # 默认：1/位置权重

    return score
```

**得分公式解析**：
```
score = Σ [ (Π engine_weights) × position_count × (1 / position) ]
```

**权重计算规则**：
1. **引擎权重相乘**：多个引擎同时命中时，权重累乘（增强可信度）
2. **出现次数乘数**：被越多引擎找到，得分越高
3. **位置倒数加权**：排名第 1 位得全分，第 2 位得 1/2，第 3 位得 1/3...

### 6.2 排序阶段

**文件**：`searx/results.py` (第 197-253 行)

```python
def get_ordered_results(self) -> list:
    # 阶段1：按得分降序排序
    results = sorted(self.main_results_map.values(), 
                     key=lambda x: x.score, 
                     reverse=True)

    # 阶段2：分组重排（避免同类结果聚集）
    gresults = []
    categoryPositions = {}
    max_count = 8       # 每组最大数量
    max_distance = 20   # 分组距离阈值

    for res in results:
        # 分类键：category + template + 是否有图片
        category = f"{res.category}:{res.template}:{'img_src' if (res.thumbnail or res.img_src) else ''}"
        grp = categoryPositions.get(category)

        if grp is not None and grp["count"] > 0 and len(gresults) - grp["index"] < max_distance:
            # 插入到组内（保持多样性）
            index = grp["index"]
            gresults.insert(index, res)
            
            # 更新所有组的位置索引
            for item in categoryPositions.values():
                if item["index"] >= index:
                    item["index"] += 1
            
            grp["count"] -= 1
        else:
            # 创建新组
            gresults.append(res)
            categoryPositions[category] = {"index": len(gresults), "count": max_count}

    return gresults
```

**分组排序目的**：
- 避免同一类结果（如同一网站、同一模板）连续出现
- 提高结果页面视觉多样性
- 提升用户浏览体验

---

## 七、异常回退与信号传播机制

### 7.1 多级超时控制

**超时优先级（从高到低）** (`search/__init__.py:111-126`):

```python
# 优先级1：查询参数 timeout_limit
# 优先级2：配置 outgoing.max_request_timeout
# 优先级3：各引擎自身 timeout 配置

actual_timeout = min(default_timeout, query_timeout, max_request_timeout)
```

**线程级超时监控** (`search/__init__.py:151-158`):

```python
for th in threading.enumerate():
    if th.name == search_id:
        remaining_time = max(0.0, actual_timeout - (default_timer() - start_time))
        th.join(remaining_time)
        if th.is_alive():
            th._timeout = True  # 标记超时
            result_container.add_unresponsive_engine(th._engine_name, 'timeout')
```

**双重超时保护**：
1. **线程 join 超时**：主线程最多等待 actual_timeout 秒
2. **HTTP 动态超时**：网络层动态计算剩余可用时间

### 7.2 异常层级与信号传播

```
HTTP 层异常
    │
    ├─ httpx.TimeoutException
    │   └─ 捕获于 network/__init__.py:107-108
    │      └─ 封装为 httpx.TimeoutException 重新抛出
    │
    ├─ httpx.RemoteProtocolError (服务器断开)
    │   └─ 捕获于 network/network.py:288-295
    │      └─ 自动重试（不计入重试次数）
    │
    └─ httpx.HTTPStatusError (>= 400)
        └─ 捕获于 network/network.py:298-300
            └─ 进入重试逻辑
                └─ 重试耗尽后抛出
                    │
                    ▼
        raise_for_httperror()  ← 检查响应内容
            │
            ├─ 检测 Cloudflare CAPTCHA → SearxEngineCaptchaException (15天)
            ├─ 检测 Cloudflare 防火墙 → SearxEngineAccessDeniedException (1天)
            ├─ 检测 ReCAPTCHA → SearxEngineCaptchaException (7天)
            ├─ HTTP 402/403 → SearxEngineAccessDeniedException (180秒)
            ├─ HTTP 429 → SearxEngineTooManyRequestsException (180秒)
            └─ 其他 → 抛出 httpx.HTTPStatusError
                │
                ▼
        处理器层捕获 (online.py:253-282)
            │
            ├─ ssl.SSLError → handle_exception(..., suspend=True)
            ├─ httpx.TimeoutException → handle_exception(..., suspend=True)
            ├─ httpx.HTTPError → handle_exception(..., suspend=True)
            ├─ SearxEngineCaptchaException → handle_exception(..., suspend=True)
            ├─ SearxEngineTooManyRequestsException → handle_exception(..., suspend=True)
            ├─ SearxEngineAccessDeniedException → handle_exception(..., suspend=True)
            └─ 其他 Exception → handle_exception(..., suspend=False)
                │
                ▼
        handle_exception() (abstract.py:165-191)
            ├─ 记录到 result_container.unresponsive_engines
            ├─ metrics 计数
            └─ 调用 suspended_status.suspend()  ← 熔断信号
                │
                ▼
        下次搜索时
            └─ extend_container_if_suspended() 检查
                └─ 若熔断中 → 跳过引擎并标记
```

### 7.3 引擎熔断机制

**文件**：`searx/search/processors/abstract.py` (第 77-108 行)

```python
class SuspendedStatus:
    def __init__(self):
        self.lock = threading.Lock()
        self.continuous_errors = 0       # 连续错误计数
        self.suspend_end_time = 0        # 熔断结束时间
        self.suspend_reason = ""         # 熔断原因

    @property
    def is_suspended(self):
        return self.suspend_end_time >= default_timer()

    def suspend(self, suspended_time, suspend_reason):
        with self.lock:
            self.continuous_errors += 1
            
            # 基础熔断时间计算（仅通用异常使用）
            if suspended_time is None:
                max_ban = get_setting("search.max_ban_time_on_fail")  # 默认 120s
                ban_fail = get_setting("search.ban_time_on_fail")     # 默认 5s
                suspended_time = min(max_ban, ban_fail * self.continuous_errors)
            
            self.suspend_end_time = default_timer() + suspended_time
            self.suspend_reason = suspend_reason
```

**熔断时间配置** (`settings.yml:69-81`):

| 异常类型 | 默认熔断时间 | 说明 |
|---------|-------------|------|
| `SearxEngineAccessDenied` | 180 秒 | 访问被拒绝、HTTP 403 |
| `SearxEngineCaptcha` | 3600 秒 | 遇到 CAPTCHA |
| `SearxEngineTooManyRequests` | 180 秒 | 请求限流、HTTP 429 |
| `cf_SearxEngineCaptcha` | 1296000 秒 (15天) | Cloudflare CAPTCHA |
| `cf_SearxEngineAccessDenied` | 86400 秒 (1天) | Cloudflare 防火墙 |
| `recaptcha_SearxEngineCaptcha` | 604800 秒 (7天) | Google ReCAPTCHA |
| 通用异常 (超时、网络错误) | 5 × 连续错误次数，上限 120 秒 | 指数退避 |

### 7.4 HTTP 请求重试机制

**文件**：`searx/network/network.py` (第 272-301 行)

```python
async def call_client(self, stream, method, url, **kwargs):
    retries = self.retries
    was_disconnected = False
    
    while retries >= 0:
        client = await self.get_client(**kwargs_clients)
        try:
            if stream:
                return client.stream(...)
            response = await client.request(...)
            if self.is_valid_response(response) or retries <= 0:
                return self.patch_response(...)
        except httpx.RemoteProtocolError:
            # 服务器主动断开：重试但不计入重试次数
            if not was_disconnected:
                was_disconnected = True
                await client.aclose()
                continue
            if retries <= 0:
                raise
        except (httpx.RequestError, httpx.HTTPStatusError):
            if retries <= 0:
                raise
        retries -= 1
```

### 7.5 结果数量统计

```python
@property
def number_of_results(self) -> int:
    """平均结果数，小于实际数量时返回 0"""
    resultnum_sum = sum(self._number_of_results)
    if not resultnum_sum or not self._number_of_results:
        return 0
    
    average = int(resultnum_sum / len(self._number_of_results))
    if average < len(self.get_ordered_results()):
        average = 0  # 平均小于实际结果数，说明统计不可靠
    return average
```

---

## 八、完整数据流图

```
  Web 请求
     │
     ▼
  webapp.search()
     │
     ├─ get_search_query_from_webapp()  ◄── 参数解析
     │   ├─ RawTextQuery 解析 bang 语法
     │   ├─ parse_lang/pageno/safesearch
     │   └─ EngineRef 列表构建与去重
     │
     ▼
  SearchWithPlugins
     │
     ├─ pre_search() 插件钩子（可修改 SearchQuery）
     │
     ├─ search_external_bang() 外部跳转
     │
     ├─ search_answerers() 内置问答
     │
     └─ search_standard()
         │
         ├─ _get_requests()  ◄── 实际超时计算
         │   ├─ 收集各引擎请求参数
         │   └─ actual_timeout = min(default, query, max)
         │
         └─ search_multiple_requests()  ◄── 多线程执行
             │
             ├─ 每个线程：OnlineProcessor.search()
             │   ├─ init_network_in_thread()  ← 线程本地上下文
             │   ├─ engine.request() 构建请求
             │   ├─ _send_http_request()
             │   │   └─ network.request()
             │   │       ├─ 动态超时计算
             │   │       └─ 异步桥接执行
             │   └─ engine.response() 解析结果
             │
             └─ 线程 join 超时控制
                 ├─ th.join(remaining_time)
                 └─ 超时则标记 th._timeout = True
                     │
                     ▼
            ResultContainer.extend()  ◄── 结果合并入口
                 │
                 ├─ on_result 插件钩子
                 ├─ 结果类型分发
                 │   ├─ suggestions (set 去重)
                 │   ├─ answers
                 │   ├─ corrections (set 去重)
                 │   ├─ infoboxes 合并
                 │   └─ main_results 哈希去重 + 合并
                 │
                 └─ post_search() 插件钩子
                     └─ close()  ◄── 计算得分
                         │
                         ▼
                    calculate_score()
                         │
                         ▼
                    get_ordered_results()
                         ├─ 按 score 降序排序
                         └─ 分组重排优化展示
```

---

## 九、机制协同与可靠性判断

### 9.1 异常对去重的影响

**熔断机制保护去重可靠性**：

1. **避免部分失败导致的去重遗漏**：
   - 若引擎 A 超时，其结果未进入容器
   - 但相同 URL 被引擎 B、C 返回，仍能正确去重
   - 缺少 A 的位置信息仅影响得分，不影响去重本身

2. **熔断后的静默期避免重复失败**：
   - 连续失败的引擎被暂停，不再参与后续请求
   - 避免因同一引擎反复失败导致结果集中缺失
   - 保持结果多样性，间接维持去重池的丰富度

3. **HTTPS 优先策略的边界情况**：
   - 去重时忽略 scheme，http://x 和 https://x 视为同一结果
   - 合并时优先使用 HTTPS URL
   - 但若两个 URL 有不同 query 参数（即使指向同一资源），仍视为不同结果

### 9.2 异常对排序的影响

**得分计算的鲁棒性设计**：

1. **多源投票机制**：
   - 即使部分引擎失败，只要有多个引擎返回同一结果
   - 引擎权重乘积仍能反映可信度
   - 单引擎结果因权重乘积较小，排名自然靠后

2. **位置信息的降权效应**：
   - 超时导致某些引擎的位置信息缺失
   - `positions` 列表长度减小，`weight *= len(positions)` 乘数降低
   - 得分自动下降，反映信息不完整

3. **熔断对排序多样性的间接影响**：
   - 高权重引擎熔断后，低权重引擎结果比例上升
   - 分组排序算法确保结果仍保持类别多样性
   - 避免同类结果聚集

### 9.3 可靠性边界与降级策略

| 场景 | 系统行为 | 用户感知 |
|------|---------|---------|
| 单个引擎超时 | 跳过该引擎，结果可能减少 | 无明显感知，结果仍可用 |
| 多个引擎超时 | 结果数量减少，得分分布偏移 | 可能看到不常见结果排名上升 |
| 高权重引擎熔断 | 该引擎结果全部缺失，排名重排 | 结果相关性可能下降 |
| 所有在线引擎熔断 | 仅返回本地 answerer 结果 | 提示 "无结果" 或极少结果 |
| 网络层重试触发 | 响应延迟增加但结果完整 | 仅表现为查询变慢 |
| 部分结果去重遗漏 | 重复结果偶尔出现 | 罕见场景，可手动刷新 |

**设计原则**：
- **优雅降级**：优先返回部分可用结果，而非完全失败
- **信息透明**：通过 `unresponsive_engines` 列表向用户披露失败引擎
- **自我修复**：熔断期过后自动恢复，无需人工干预
- **统计置信度**：`number_of_results` 在统计不可靠时返回 0，避免误导

---

## 十、关键设计要点总结

### 10.1 去重设计亮点
1. **URL 规范化忽略协议**：http/https 视为同一结果
2. **多维度哈希键**：template + url + img_src 确保准确
3. **智能字段合并**：选择更丰富的内容、优先 HTTPS
4. **线程安全**：`RLock` 保护并发写入

### 10.2 排序设计亮点
1. **多引擎信任累积**：引擎权重累乘，多来源增强可信度
2. **位置敏感打分**：排名越靠前权重越高（1/position 衰减）
3. **多样性分组**：避免同类结果连续展示，提升体验
4. **插件可扩展**：`priority` 字段支持插件调整排名

### 10.3 可靠性保障
1. **多级超时控制**：全局、引擎、查询三层超时
2. **线程级隔离**：每个引擎独立线程，互不影响
3. **分级熔断机制**：不同异常类型对应不同熔断时长
4. **自动重试**：网络异常透明重试，特殊断开不计重试
5. **全局事件循环**：同步/异步桥接，高效复用 HTTP 连接

---

## 十一、核心代码位置速查

| 功能模块 | 文件位置 | 关键行号 |
|---------|---------|---------|
| SearchQuery 模型 | `searx/search/models.py` | 28-119 |
| 结果哈希计算 | `searx/result_types/_base.py` | 405-418 |
| ResultContainer | `searx/results.py` | 53-295 |
| 去重合并逻辑 | `searx/results.py` | 173-187, 357-381 |
| 得分计算 | `searx/results.py` | 17-38 |
| 排序与分组 | `searx/results.py` | 197-253 |
| 搜索执行器 | `searx/search/__init__.py` | 47-179 |
| 在线处理器 | `searx/search/processors/online.py` | 113-282 |
| 网络核心类 | `searx/network/network.py` | 45-441 |
| 线程本地网络上下文 | `searx/network/__init__.py` | 28-108 |
| HTTP 错误检测 | `searx/network/raise_for_httperror.py` | 61-79 |
| 引擎熔断状态 | `searx/search/processors/abstract.py` | 77-108 |
| 异常类型定义 | `searx/exceptions.py` | 60-111 |
| 熔断时间配置 | `searx/settings.yml` | 66-81 |

---

*报告生成时间：2026-05-17*
*修订版本：v2.0*
