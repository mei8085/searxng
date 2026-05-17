# 网络请求超时分析报告

本文档深入分析 SearXNG 中网络请求超时的判定机制、配置来源、异常传递路径，以及不同引擎/调用场景下的分支差异。

---

## 一、超时配置来源与层级

SearXNG 的超时配置采用四层结构，从高到低优先级依次为：用户查询级 > 全局最大限制 > 引擎级 > 全局默认。

### 1.1 全局默认配置

**位置**：`searx/settings.yml:179-182`

```yaml
outgoing:
  request_timeout: 3.0        # 默认超时时间（秒）
  # max_request_timeout: 10.0 # 最大超时时间（秒），默认注释掉即无限制
```

**默认值定义**：`searx/settings_defaults.py:251-254`
- `request_timeout`: 默认 3.0 秒
- `max_request_timeout`: 默认 `None`（无上限）

### 1.2 引擎级配置

**位置**：`searx/engines/__init__.py:30-49`

引擎初始化时，`ENGINE_DEFAULT_ARGS` 定义了默认超时：
```python
ENGINE_DEFAULT_ARGS = {
    "timeout": settings["outgoing"]["request_timeout"],  # 继承全局默认
    ...
}
```

每个引擎可在 `settings.yml` 中单独配置 `timeout`：
```yaml
engines:
  - name: 360search
    timeout: 20.0          # 高优先级覆盖全局默认
  - name: ahmia
    timeout: 20.0          # Tor 网络引擎设置较高超时
  - name: cloudflareai
    timeout: 30            # AI 引擎设置更高超时
```

### 1.3 用户查询级配置

**位置**：`searx/query.py:43-69`

用户可通过搜索语法动态指定超时：
- `<3` 表示 3 秒超时（<100 单位为秒）
- `<850` 表示 850 毫秒超时（>=100 单位为毫秒）

解析逻辑：
```python
class TimeoutParser(QueryPartParser):
    def _parse(self, value):
        raw_timeout_limit = int(value)
        if raw_timeout_limit < 100:
            # 秒级
            self.raw_text_query.timeout_limit = float(raw_timeout_limit)
        else:
            # 毫秒级
            self.raw_text_query.timeout_limit = raw_timeout_limit / 1000.0
```

---

## 二、超时计算逻辑

**位置**：`searx/search/__init__.py:78-134`

### 2.1 计算流程

`Search._get_requests()` 方法中执行实际超时计算：

```python
def _get_requests(self) -> tuple[list[tuple[str, str, RequestParams]], float]:
    # 1. 计算默认超时：取所有选中引擎 timeout 的最大值
    default_timeout = 0
    for engineref in self.search_query.engineref_list:
        processor = PROCESSORS.get(engineref.name)
        default_timeout = max(default_timeout, processor.engine.timeout)
    
    # 2. 获取配置的最大超时和用户查询超时
    max_request_timeout = settings['outgoing']['max_request_timeout']
    query_timeout = self.search_query.timeout_limit
    
    # 3. 按优先级调整
    actual_timeout = default_timeout
    
    if max_request_timeout is None and query_timeout is None:
        pass  # 使用 default_timeout
    elif max_request_timeout is None and query_timeout is not None:
        actual_timeout = min(default_timeout, query_timeout)
    elif max_request_timeout is not None and query_timeout is None:
        actual_timeout = min(default_timeout, max_request_timeout)
    elif max_request_timeout is not None and query_timeout is not None:
        actual_timeout = min(query_timeout, max_request_timeout)
    
    return requests, actual_timeout
```

### 2.2 优先级总结

| 配置源 | 优先级 | 说明 |
|--------|--------|------|
| 用户查询级 (`<3`) | 最高 | 受 max_request_timeout 限制 |
| max_request_timeout | 次高 | 全局硬上限 |
| 引擎级 timeout | 次低 | 单个引擎配置 |
| request_timeout | 最低 | 全局默认值 |

---

## 三、超时传递路径

### 3.1 线程级超时设置

**位置**：`searx/search/processors/online.py:124-130`

```python
def init_network_in_thread(self, start_time: float, timeout_limit: float):
    # 设置线程级超时
    searx.network.set_timeout_for_thread(timeout_limit, start_time=start_time)
    # 重置 HTTP 计时
    searx.network.reset_time_for_thread()
    # 设置当前线程使用的网络实例
    searx.network.set_context_network_name(self.engine.name)
```

调用时机：
1. `OnlineProcessor.init_engine()` - 引擎初始化时
2. `OnlineProcessor.search()` - 每次搜索前

**存储位置**：`searx/network/__init__.py:28-44`

```python
THREADLOCAL = threading.local()

def set_timeout_for_thread(timeout: float, start_time: float | None = None):
    THREADLOCAL.timeout = timeout
    THREADLOCAL.start_time = start_time
```

### 3.2 网络请求中的超时应用

**位置**：`searx/network/__init__.py:73-108`

```python
def _get_timeout(start_time: float, kwargs: t.Any) -> float:
    # 1. 优先使用 kwargs 中的 timeout
    if 'timeout' in kwargs:
        timeout = kwargs['timeout']
    else:
        # 2. 回退到线程级 timeout
        timeout = getattr(THREADLOCAL, 'timeout', None)
        if timeout is not None:
            kwargs['timeout'] = timeout
    
    # 3. 无 timeout 时使用 120 秒兜底
    timeout = timeout or 120
    
    # 4. 调整超时：加上 0.2 秒开销，减去已用时间
    timeout += 0.2  # overhead
    if start_time:
        timeout -= default_timer() - start_time
    
    return timeout

def request(method: str, url: str, **kwargs: t.Any) -> SXNG_Response:
    with _record_http_time() as start_time:
        network = get_context_network()
        timeout = _get_timeout(start_time, kwargs)
        future = asyncio.run_coroutine_threadsafe(
            network.request(method, url, **kwargs),
            get_loop(),
        )
        try:
            return future.result(timeout)  # 应用超时
        except concurrent.futures.TimeoutError as e:
            raise httpx.TimeoutException('Timeout', request=None) from e
```

### 3.3 多请求场景的超时处理

**位置**：`searx/network/__init__.py:111-134`

```python
def multi_requests(request_list: list["Request"]) -> list[httpx.Response | Exception]:
    with _record_http_time() as start_time:
        network = get_context_network()
        loop = get_loop()
        future_list = []
        for request_desc in request_list:
            # 每个请求单独计算超时
            timeout = _get_timeout(start_time, request_desc.kwargs)
            future = asyncio.run_coroutine_threadsafe(
                network.request(request_desc.method, request_desc.url, **request_desc.kwargs), loop
            )
            future_list.append((future, timeout))
        
        responses = []
        for future, timeout in future_list:
            try:
                responses.append(future.result(timeout))
            except concurrent.futures.TimeoutError:
                responses.append(httpx.TimeoutException('Timeout', request=None))
            except Exception as e:
                responses.append(e)
        return responses
```

### 3.4 超时异常捕获与处理

**位置**：`searx/search/processors/online.py:257-282`

```python
def search(self, query: str, params: OnlineParams, result_container: "ResultContainer",
           start_time: float, timeout_limit: float):
    self.init_network_in_thread(start_time, timeout_limit)
    
    try:
        search_results = self._search_basic(query, params)
        self.extend_container(result_container, start_time, search_results)
    except ssl.SSLError as e:
        self.handle_exception(result_container, e, suspend=True)
        self.logger.error("SSLError {}, verify={}".format(e, ...))
    except (httpx.TimeoutException, asyncio.TimeoutError) as e:
        # 捕获超时异常
        self.handle_exception(result_container, e, suspend=True)
        self.logger.error(
            "HTTP requests timeout (search duration : {0} s, timeout: {1} s) : {2}".format(
                default_timer() - start_time, timeout_limit, e.__class__.__name__
            )
        )
    except (httpx.HTTPError, httpx.StreamError) as e:
        self.handle_exception(result_container, e, suspend=True)
        self.logger.exception(...)
    except (SearxEngineCaptchaException, ...) as e:
        self.handle_exception(result_container, e, suspend=True)
    except Exception as e:
        self.handle_exception(result_container, e)
```

### 3.5 异常处理流程

**位置**：`searx/search/processors/abstract.py:165-191`

```python
def handle_exception(self, result_container: "ResultContainer",
                     exception_or_message: BaseException | str, suspend: bool = False):
    # 1. 标记引擎无响应
    if isinstance(exception_or_message, BaseException):
        error_message = module_name + exception_class.__qualname__
    else:
        error_message = exception_or_message
    result_container.add_unresponsive_engine(self.engine.name, error_message)
    
    # 2. 指标统计
    counter_inc('engine', self.engine.name, 'search', 'count', 'error')
    if isinstance(exception_or_message, BaseException):
        count_exception(self.engine.name, exception_or_message)
    else:
        count_error(self.engine.name, exception_or_message)
    
    # 3. 可选：暂停引擎
    if suspend:
        suspended_time = None
        if isinstance(exception_or_message, SearxEngineAccessDeniedException):
            suspended_time = exception_or_message.suspended_time
        self.suspended_status.suspend(suspended_time, error_message)
```

### 3.6 主线程超时监控

**位置**：`searx/search/__init__.py:136-158`

```python
def search_multiple_requests(self, requests: list[tuple[str, str, RequestParams]]):
    search_id = str(uuid4())
    
    # 启动所有引擎线程
    for engine_name, query, request_params in requests:
        th = threading.Thread(
            target=copy_current_request_context(PROCESSORS[engine_name].search),
            args=(query, request_params, self.result_container, self.start_time, self.actual_timeout),
            name=search_id,
        )
        th._timeout = False
        th._engine_name = engine_name
        th.start()
    
    # 监控线程超时
    for th in threading.enumerate():
        if th.name == search_id:
            remaining_time = max(0.0, self.actual_timeout - (default_timer() - self.start_time))
            th.join(remaining_time)
            if th.is_alive():
                th._timeout = True  # 标记线程超时
                self.result_container.add_unresponsive_engine(th._engine_name, 'timeout')
                PROCESSORS[th._engine_name].logger.error('engine timeout')
```

---

## 四、不同引擎/场景的分支差异

### 4.1 处理器类型概览

**位置**：`searx/search/processors/__init__.py:39-45`

```python
processor_types: dict[str, type[EngineProcessor]] = {
    "online": OnlineProcessor,
    "offline": OfflineProcessor,
    "online_dictionary": OnlineDictionaryProcessor,
    "online_currency": OnlineCurrencyProcessor,
    "online_url_search": OnlineUrlSearchProcessor,
}
```

### 4.2 OnlineProcessor（在线引擎）

**适用场景**：绝大多数网络搜索引擎（Google、Bing、Wikipedia 等）

**超时特性**：
- 完整的超时处理流程
- 支持 `init_network_in_thread()` 设置线程级超时
- 捕获 `httpx.TimeoutException` 和 `asyncio.TimeoutError`
- 超时后可能暂停引擎（`suspend=True`）

**关键代码**：`searx/search/processors/online.py:113-282`

### 4.3 OfflineProcessor（离线引擎）

**适用场景**：本地数据库、SQLite、Elasticsearch 等不需要网络请求的引擎

**超时特性**：
- 不涉及网络请求
- 无 `init_network_in_thread()` 调用
- 不捕获超时异常
- 无暂停引擎逻辑

**关键代码**：`searx/search/processors/offline.py:11-32`

```python
class OfflineProcessor(EngineProcessor):
    def search(self, query: str, params: RequestParams, result_container: "ResultContainer",
               start_time: float, timeout_limit: float):
        try:
            search_results = self.engine.search(query, params)
            self.extend_container(result_container, start_time, search_results)
        except ValueError as e:
            self.logger.exception(...)
        except Exception as e:
            self.handle_exception(result_container, e)  # 不暂停
```

### 4.4 OnlineDictionaryProcessor（字典翻译）

**适用场景**：在线字典、翻译引擎（如 Lingva、LibreTranslate）

**超时特性**：
- 继承自 `OnlineProcessor`
- 相同的超时处理逻辑
- 额外增加查询语法解析（`en-de hello`）

**关键代码**：`searx/search/processors/online_dictionary.py:37-102`

### 4.5 OnlineCurrencyProcessor（货币转换）

**适用场景**：货币汇率查询引擎

**超时特性**：
- 继承自 `OnlineProcessor`
- 相同的超时处理逻辑
- 额外增加货币转换语法解析（`100 usd to eur`）

**关键代码**：`searx/search/processors/online_currency.py:50-109`

### 4.6 OnlineUrlSearchProcessor（URL 搜索）

**适用场景**：直接 URL 查询（TinEye 等）

**超时特性**：
- 继承自 `OnlineProcessor`
- 相同的超时处理逻辑
- 额外增加 URL 模式匹配

**关键代码**：`searx/search/processors/online_url_search.py:32-64`

### 4.7 各类处理器对比

| 处理器类型 | 网络请求 | 超时处理 | 引擎暂停 | 特殊处理 |
|-----------|---------|---------|---------|---------|
| OnlineProcessor | ✅ | ✅ 完整 | ✅ | 标准网络请求 |
| OfflineProcessor | ❌ | ❌ | ❌ | 本地查询 |
| OnlineDictionaryProcessor | ✅ | ✅ 完整 | ✅ | 语言对解析 |
| OnlineCurrencyProcessor | ✅ | ✅ 完整 | ✅ | 货币格式解析 |
| OnlineUrlSearchProcessor | ✅ | ✅ 完整 | ✅ | URL 模式匹配 |

### 4.8 特殊场景：Tor 网络引擎

**位置**：`searx/engines/__init__.py:196-199`

Tor 网络引擎会自动增加超时时长：

```python
def update_attributes_for_tor(engine: "Engine | types.ModuleType"):
    if using_tor_proxy(engine) and hasattr(engine, 'onion_url'):
        engine.search_url = engine.onion_url + getattr(engine, 'search_path', '')
        engine.timeout += settings['outgoing'].get('extra_proxy_timeout', 0)
```

配置示例（`settings.yml`）：
```yaml
outgoing:
  using_tor_proxy: true
  extra_proxy_timeout: 10  # Tor 引擎额外增加 10 秒超时
```

---

## 五、完整调用链总结

### 5.1 超时设置调用链

```
Search.search()
└── Search.search_standard()
    ├── Search._get_requests()          # 计算 actual_timeout
    └── Search.search_multiple_requests()
        └── [线程] OnlineProcessor.search()
            └── OnlineProcessor.init_network_in_thread()
                ├── set_timeout_for_thread()    # 线程级超时
                ├── reset_time_for_thread()
                └── set_context_network_name()
                    └── OnlineProcessor._search_basic()
                        └── OnlineProcessor._send_http_request()
                            └── searx.network.get()/post()
                                └── request()
                                    └── _get_timeout()        # 计算实际超时
                                        └── future.result(timeout)  # 应用超时
```

### 5.2 超时异常传递链

```
future.result(timeout) 超时
└── concurrent.futures.TimeoutError
    └── 转换为 httpx.TimeoutException
        └── OnlineProcessor.search() 捕获
            └── handle_exception()
                ├── add_unresponsive_engine()  # 标记无响应
                ├── count_error()              # 统计指标
                └── suspended_status.suspend() # 暂停引擎（可选）
```

### 5.3 主线程超时监控链

```
Search.search_multiple_requests()
└── th.join(remaining_time)
    └── 线程仍存活
        ├── th._timeout = True
        ├── add_unresponsive_engine()
        └── logger.error('engine timeout')
```

---

## 六、关键配置参考

### 6.1 settings.yml 超时相关配置

```yaml
outgoing:
  request_timeout: 3.0        # 全局默认超时（秒）
  max_request_timeout: 10.0   # 全局最大超时（秒），默认注释
  extra_proxy_timeout: 10     # Tor 代理额外超时（秒）

search:
  ban_time_on_fail: 5         # 引擎失败后暂停时间（秒）
  max_ban_time_on_fail: 120   # 最大暂停时间（秒）
```

### 6.2 引擎级配置示例

```yaml
engines:
  - name: wikipedia
    timeout: 3.0              # 正常引擎使用默认
  - name: ahmia
    timeout: 20.0             # Tor 引擎高超时
    enable_http: true
  - name: cloudflareai
    timeout: 30               # AI 引擎高超时
    inactive: true
```

---

## 七、代码位置索引

| 功能模块 | 文件路径 | 关键行号 |
|---------|---------|---------|
| 全局默认配置 | `searx/settings.yml` | 179-182 |
| 引擎默认参数 | `searx/engines/__init__.py` | 30-49 |
| 用户超时解析 | `searx/query.py` | 43-69 |
| 超时计算逻辑 | `searx/search/__init__.py` | 78-134 |
| 线程超时设置 | `searx/network/__init__.py` | 28-44 |
| 超时应用逻辑 | `searx/network/__init__.py` | 73-108 |
| 在线处理器超时 | `searx/search/processors/online.py` | 257-282 |
| 异常处理 | `searx/search/processors/abstract.py` | 165-191 |
| 主线程超时监控 | `searx/search/__init__.py` | 136-158 |
| Tor 超时调整 | `searx/engines/__init__.py` | 196-199 |
