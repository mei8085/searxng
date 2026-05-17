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
        exception_class = exception_or_message.__class__
        module_name = getattr(exception_class, '__module__', 'builtins')
        module_name = '' if module_name == 'builtins' else module_name + '.'
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
        th._timeout = False  # 初始化超时标记
        th._engine_name = engine_name
        th.start()
    
    # 监控线程超时（阶段1：主线程立即处理）
    for th in threading.enumerate():
        if th.name == search_id:
            remaining_time = max(0.0, self.actual_timeout - (default_timer() - self.start_time))
            th.join(remaining_time)
            if th.is_alive():
                th._timeout = True  # 设置标记，子线程后续会读取
                self.result_container.add_unresponsive_engine(th._engine_name, 'timeout')
                PROCESSORS[th._engine_name].logger.error('engine timeout')
```

> **注意**：主线程设置 `th._timeout = True` 后立即返回响应用户，但子线程仍在运行。子线程完成后会在 `extend_container()` 中读取此标记，调用 `handle_exception('timeout', False)` 进入错误统计流程（详见第六章）。

---

## 四、超时异常到用户可见结果的完整映射

### 4.1 unresponsive_engines 数据结构

**位置**：`searx/results.py:47-50`

```python
class UnresponsiveEngine(t.NamedTuple):
    engine: str          # 引擎名称
    error_type: str      # 错误类型（类名或自定义消息）
    suspended: bool      # 是否已被暂停
```

存储位置：`ResultContainer.unresponsive_engines: set[UnresponsiveEngine]`

### 4.2 添加无响应引擎的三个入口

**入口 1：处理器内部异常捕获**（`searx/search/processors/abstract.py:179`）
```python
# 处理 httpx.TimeoutException 等异常时
result_container.add_unresponsive_engine(self.engine.name, error_message)
# error_message 格式："httpx.TimeoutException"
```

**入口 2：主线程 join 超时**（双阶段处理）

阶段 1（`searx/search/__init__.py:157`）：主线程立即标记
```python
# 线程仍存活，超过 actual_timeout 时
th._timeout = True  # 设置标记供子线程读取
self.result_container.add_unresponsive_engine(th._engine_name, 'timeout')
# error_message 格式："timeout"（纯字符串）
```

阶段 2（`searx/search/processors/abstract.py:216-218`）：子线程完成后二次处理
```python
if getattr(threading.current_thread(), '_timeout', False):
    # 子线程检测到主线程已放弃等待
    self.handle_exception(result_container, 'timeout', False)
    # 再次 add_unresponsive_engine（set 自动去重）
    # counter_inc 错误计数+1，count_error 记录错误详情
```

**入口 3：引擎已被暂停**（`searx/search/processors/abstract.py:227-228`）
```python
# 搜索前检查到引擎已被暂停
result_container.add_unresponsive_engine(
    self.engine.name, self.suspended_status.suspend_reason, suspended=True
)
```

### 4.3 错误类型翻译映射

**位置**：`searx/webutils.py:36-67`

```python
timeout_text = gettext('timeout')
exception_classname_to_text = {
    None: gettext('unexpected crash'),
    'timeout': timeout_text,
    'asyncio.TimeoutError': timeout_text,
    'httpx.TimeoutException': timeout_text,
    'httpx.ConnectTimeout': timeout_text,
    'httpx.ReadTimeout': timeout_text,
    'httpx.WriteTimeout': timeout_text,
    'httpx.HTTPStatusError': gettext('HTTP error'),
    'httpx.ConnectError': gettext("HTTP connection error"),
    'ssl.SSLCertVerificationError': gettext("SSL error: certificate validation has failed"),
    'searx.exceptions.SearxEngineCaptchaException': gettext("CAPTCHA"),
    'searx.exceptions.SearxEngineTooManyRequestsException': gettext("too many requests"),
    'searx.exceptions.SearxEngineAccessDeniedException': gettext("access denied"),
    ...
}
```

### 4.4 JSON 输出落点

**位置**：`searx/webutils.py:162-175`

```python
def get_json_response(sq: "SearchQuery", rc: "ResultContainer") -> str:
    data = {
        'query': sq.query,
        'number_of_results': rc.number_of_results,
        'results': [_.as_dict() for _ in rc.get_ordered_results()],
        'answers': [_.as_dict() for _ in rc.answers],
        'corrections': list(rc.corrections),
        'infoboxes': rc.infoboxes,
        'suggestions': list(rc.suggestions),
        'unresponsive_engines': get_translated_errors(rc.unresponsive_engines),
    }
    return json.dumps(data, cls=JSONEncoder)
```

**翻译函数**（`searx/webutils.py:70-82`）：
```python
def get_translated_errors(unresponsive_engines: "Iterable[UnresponsiveEngine]"):
    translated_errors = []
    for unresponsive_engine in unresponsive_engines:
        error_user_text = exception_classname_to_text.get(
            unresponsive_engine.error_type,
            exception_classname_to_text[None]  # fallback: unexpected crash
        )
        error_msg = gettext(error_user_text)
        if unresponsive_engine.suspended:
            error_msg = gettext('Suspended') + ': ' + error_msg
        translated_errors.append((unresponsive_engine.engine, error_msg))
    return sorted(translated_errors, key=lambda e: e[0])
```

**JSON 输出示例**：
```json
{
  "query": "test",
  "unresponsive_engines": [
    ["google", "timeout"],
    ["bing", "Suspended: access denied"]
  ],
  ...
}
```

### 4.5 页面渲染落点

**位置**：`searx/webapp.py:771-773`

```python
return render(
    ...
    unresponsive_engines = webutils.get_translated_errors(
        result_container.unresponsive_engines
    ),
    ...
)
```

**模板渲染**（`searx/templates/simple/elements/engines_msg.html:10-20`）：
```html
<table class="engine-stats" id="engines_msg-table">
  {%- for engine_name, error_type in unresponsive_engines -%}
  <tr>
    <td class="engine-name">
      <a href="{{ url_for('stats', engine=engine_name|e) }}"
         title="{{ _('View error logs and submit a bug report') }}">
         {{- engine_name -}}
      </a>
    </td>
    <td class="response-error">{{- error_type -}}</td>
  </tr>
  {%- endfor -%}
  ...
</table>
```

### 4.6 完整映射链

```
异常发生
    │
    ├─→ add_unresponsive_engine(engine_name, error_type, suspended)
    │     └─→ 存入 ResultContainer.unresponsive_engines 集合
    │
    └─→ 输出阶段
          ├─→ JSON: get_translated_errors() → 翻译 → unresponsive_engines 字段
          │     格式: [["engine1", "timeout"], ["engine2", "Suspended: access denied"]]
          │
          └─→ HTML: get_translated_errors() → 翻译 → 模板渲染
                格式: 表格行展示，引擎名可链接到统计页面
```

---

## 五、绕过网络超时主链的场景

### 5.1 搜索主流程总览

**位置**：`searx/search/__init__.py:174-179`

```python
def search(self) -> ResultContainer:
    self.start_time = default_timer()
    if not self.search_external_bang():      # 分支 1: external bang
        if not self.search_answerers():      # 分支 2: answerers
            self.search_standard()           # 主链: 正常网络搜索
    return self.result_container
```

**优先级**：External Bang > Answerers > 标准搜索

### 5.2 External Bang 完全绕过

**触发条件**：用户输入 `!!g test`（双感叹号前缀）

**位置**：`searx/search/__init__.py:59-69`

```python
def search_external_bang(self) -> bool:
    """Check if there is a external bang.  If yes, update
    self.result_container and return True."""
    if self.search_query.external_bang:
        self.result_container.redirect_url = get_bang_url(self.search_query)
        # 有效 bang，直接返回 True，跳过后续所有搜索
        if isinstance(self.result_container.redirect_url, str):
            return True
    return False
```

**解析流程**（`searx/query.py:151-175`）：
```python
class ExternalBangParser(QueryPartParser):
    @staticmethod
    def check(raw_value):
        return raw_value.startswith('!!') and len(raw_value) > 2
    
    def _parse(self, value):
        bang_definition, bang_ac_list = get_bang_definition_and_autocomplete(value)
        if bang_definition is not None:
            self.raw_text_query.external_bang = value  # 标记 external bang
            found = True
        return found, bang_ac_list
```

**URL 生成**（`searx/external_bang.py:93-109`）：
```python
def get_bang_url(search_query: "SearchQuery", ...) -> str | None:
    if search_query.external_bang:
        bang_definition, _ = get_bang_definition_and_ac(EXTERNAL_BANGS, search_query.external_bang)
        if bang_definition and isinstance(bang_definition, str):
            ret_val = resolve_bang_definition(bang_definition, search_query.query)[0]
    return ret_val
```

**Web 层重定向**（`searx/webapp.py:666-667`）：
```python
# 1. check if the result is a redirect for an external bang
if result_container.redirect_url:
    return redirect(result_container.redirect_url)
```

**关键特性**：
- ✅ 不创建任何引擎线程（源码依据：`search_external_bang()` 返回 `True` 跳过线程创建）
- ✅ 不调用 `search_standard()`（源码依据：`Search.search()` 短路逻辑）
- ✅ 不计算 `actual_timeout`（源码依据：`_get_requests()` 未执行）
- ✅ 无 SearXNG 发起的网络请求（源码依据：`get_bang_url()` 为纯字符串拼接）
- ✅ 无 SearXNG 侧超时风险（源码依据：不进入网络请求主链）

### 5.3 Answerers 本地计算绕过

**触发条件**：查询第一个词匹配 answerer 关键词（如 `calc 1+1`, `weather beijing`）

**位置**：`searx/search/__init__.py:71-75`

```python
def search_answerers(self):
    results = searx.answerers.STORAGE.ask(self.search_query.query)
    self.result_container.extend(None, results)
    return bool(results)  # 有结果返回 True，跳过标准搜索
```

**Answerer 调度**（`searx/answerers/_core.py:143-164`）：
```python
def ask(self, query: str) -> list[BaseAnswer]:
    results = []
    keyword = None
    for keyword in query.split():
        if keyword:
            break
    
    if not keyword or keyword not in self:
        return results
    
    for answerer in self[keyword]:
        for answer in answerer.answer(query):
            answer.engine = f"answerer: {keyword}"
            results.append(answer)
    
    return results
```

**Answerer 基类**（`searx/answerers/_core.py:43-55`）：
```python
class Answerer(abc.ABC):
    keywords: list[str]
    
    @abc.abstractmethod
    def answer(self, query: str) -> list[BaseAnswer]:
        """纯本地计算，无网络请求"""
```

**关键特性**：
- ✅ 不创建引擎线程
- ✅ 不调用 `search_standard()`
- ✅ 纯本地计算，无网络 I/O
- ✅ 无超时概念
- ✅ 结果直接进入 `result_container.answers`

### 5.4 两类绕过对比

| 特性 | External Bang | Answerers | 标准搜索 |
|-----|--------------|-----------|---------|
| 触发条件 | `!!keyword` 前缀 | 首词匹配关键词 | 默认路径 |
| 线程创建 | ❌ 无 | ❌ 无 | ✅ 每个引擎一个线程 |
| 网络请求 | ❌ 无（仅重定向） | ❌ 纯本地 | ✅ 大量 |
| 超时处理 | ❌ 无 | ❌ 无 | ✅ 完整超时链 |
| 结果来源 | 外部网站跳转 | 本地计算 | 引擎搜索结果 |
| 响应速度 | 极快（<10ms） | 快（<100ms） | 慢（取决于引擎） |

---

## 六、主线程 join 超时后的 _timeout 标记分析

### 6.1 标记设置点

**位置**：`searx/search/__init__.py:148-158`

```python
def search_multiple_requests(self, requests: list[tuple[str, str, RequestParams]]):
    # ... 创建线程 ...
    th._timeout = False  # 初始标记
    th._engine_name = engine_name
    th.start()
    
    # ... 监控超时 ...
    for th in threading.enumerate():
        if th.name == search_id:
            remaining_time = max(0.0, self.actual_timeout - (default_timer() - self.start_time))
            th.join(remaining_time)
            if th.is_alive():
                th._timeout = True  # 关键：设置超时标记
                self.result_container.add_unresponsive_engine(th._engine_name, 'timeout')
                PROCESSORS[th._engine_name].logger.error('engine timeout')
```

### 6.2 _timeout 标记的完整读取链路

**关键发现**：`th._timeout` 标记不是死代码！它会在子线程的 `extend_container()` 阶段被读取。

**标记读取位置**（`searx/search/processors/abstract.py:216-223`）：
```python
def extend_container(self, result_container, start_time, search_results):
    if getattr(threading.current_thread(), '_timeout', False):
        # the main thread is not waiting anymore
        self.handle_exception(result_container, 'timeout', False)
    else:
        if search_results is not None:
            self._extend_container_basic(result_container, start_time, search_results)
        self.suspended_status.resume()
```

### 6.3 完整时序与双阶段处理

**时序图**：
```
主线程                          子线程
  │                              │
  ├─ th._timeout = False         │
  ├─ th.start()                  ├─ 执行 _search_basic()
  │                              │  发送 HTTP 请求...
  ├─ th.join(remaining_time)     │
  │  等待超时...                 │
  ├─ th.is_alive() == True       │
  ├─ th._timeout = True ◀───┐    │
  ├─ add_unresponsive_engine()  │  请求仍在进行...
  ├─ logger.error()             │
  │  主线程继续                 │  请求最终完成
  │                              ├─ 调用 extend_container()
  │                              │  读取 current_thread()._timeout → True
  │                              └─ 调用 handle_exception('timeout', False)
```

**阶段 1：主线程立即处理**（`searx/search/__init__.py:156-158`）
```python
if th.is_alive():
    th._timeout = True  # 设置标记，供子线程后续读取
    self.result_container.add_unresponsive_engine(th._engine_name, 'timeout')  # 立即标记无响应
    PROCESSORS[th._engine_name].logger.error('engine timeout')  # 立即记录日志
```

**阶段 2：子线程延迟处理**（`searx/search/processors/abstract.py:216-218`）
```python
if getattr(threading.current_thread(), '_timeout', False):
    # 子线程检测到主线程已超时放弃等待
    self.handle_exception(result_container, 'timeout', False)
```

**handle_exception 执行内容**（`suspend=False`）：
```python
def handle_exception(..., suspend=False):
    result_container.add_unresponsive_engine(...)  # 再次添加（set 自动去重）
    counter_inc('engine', name, 'search', 'count', 'error')  # ✅ 错误计数+1
    count_error(name, 'timeout')  # ✅ 记录错误详情（用于 /stats）
    # suspend=False → 不暂停引擎
```

### 6.4 与处理器内部超时的统计差异

**路径 A：处理器内部捕获超时**（`httpx.TimeoutException`）
```python
# searx/search/processors/online.py:257-259
except (httpx.TimeoutException, asyncio.TimeoutError) as e:
    self.handle_exception(result_container, e, suspend=True)
    # suspend=True → 可能触发引擎暂停
```

**路径 B：主线程 join 超时**（两阶段处理）
```python
# 阶段1：主线程立即标记无响应和记录日志
# 阶段2：子线程后续调用 handle_exception(..., suspend=False)
# suspend=False → 不会触发引擎暂停
```

**统计差异对比表**：

| 统计项 | 内部捕获 (Path A) | 主线程超时 (Path B) |
|-------|------------------|-------------------|
| 用户可见错误 | ✅ | ✅（阶段1添加） |
| `search.count.error` 计数 | ✅ +1 | ✅ +1（阶段2 handle_exception） |
| `/stats` 异常详情 | ✅ 有完整栈信息 | ✅ 有错误记录（无栈） |
| 引擎自动暂停 | ✅ 可能（suspend=True） | ❌ 不会（suspend=False） |
| 日志记录 | ✅ | ✅（阶段1记录） |

**设计意图**：
`th._timeout` 标记实现了"超时后子线程继续运行但结果被忽略"的模式：
1. 主线程超时后立即响应用户，不再等待
2. 子线程继续运行直到完成，通过 `_timeout` 标记知道主线程已放弃
3. 子线程完成后不添加结果，而是调用 `handle_exception` 计入统计
4. 不暂停引擎（suspend=False），因为这是超时不是引擎错误

---

## 七、不同引擎/场景的分支差异

### 7.1 处理器类型概览

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

### 7.2 OnlineProcessor（在线引擎）

**适用场景**：绝大多数网络搜索引擎（Google、Bing、Wikipedia 等）

**超时特性**：
- 完整的超时处理流程
- 支持 `init_network_in_thread()` 设置线程级超时
- 捕获 `httpx.TimeoutException` 和 `asyncio.TimeoutError`
- 超时后可能暂停引擎（`suspend=True`）

**关键代码**：`searx/search/processors/online.py:113-282`

### 7.3 OfflineProcessor（离线引擎）

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

### 7.4 OnlineDictionaryProcessor（字典翻译）

**适用场景**：在线字典、翻译引擎（如 Lingva、LibreTranslate）

**超时特性**：
- 继承自 `OnlineProcessor`
- 相同的超时处理逻辑
- 额外增加查询语法解析（`en-de hello`）

**关键代码**：`searx/search/processors/online_dictionary.py:37-102`

### 7.5 OnlineCurrencyProcessor（货币转换）

**适用场景**：货币汇率查询引擎

**超时特性**：
- 继承自 `OnlineProcessor`
- 相同的超时处理逻辑
- 额外增加货币转换语法解析（`100 usd to eur`）

**关键代码**：`searx/search/processors/online_currency.py:50-109`

### 7.6 OnlineUrlSearchProcessor（URL 搜索）

**适用场景**：直接 URL 查询（TinEye 等）

**超时特性**：
- 继承自 `OnlineProcessor`
- 相同的超时处理逻辑
- 额外增加 URL 模式匹配

**关键代码**：`searx/search/processors/online_url_search.py:32-64`

### 7.7 各类处理器对比

| 处理器类型 | 网络请求 | 超时处理 | 引擎暂停 | 特殊处理 |
|-----------|---------|---------|---------|---------|
| OnlineProcessor | ✅ | ✅ 完整 | ✅ | 标准网络请求 |
| OfflineProcessor | ❌ | ❌ | ❌ | 本地查询 |
| OnlineDictionaryProcessor | ✅ | ✅ 完整 | ✅ | 语言对解析 |
| OnlineCurrencyProcessor | ✅ | ✅ 完整 | ✅ | 货币格式解析 |
| OnlineUrlSearchProcessor | ✅ | ✅ 完整 | ✅ | URL 模式匹配 |

### 7.8 特殊场景：Tor 网络引擎

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

## 八、完整调用链总结

### 8.1 标准搜索超时设置调用链

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

### 8.2 处理器内部超时异常传递链

```
future.result(timeout) 超时
└── concurrent.futures.TimeoutError
    └── 转换为 httpx.TimeoutException
        └── OnlineProcessor.search() 捕获
            └── handle_exception()
                ├── add_unresponsive_engine()  # 标记无响应
                ├── counter_inc(error)         # 错误计数
                ├── count_exception()          # 记录异常栈
                └── suspended_status.suspend() # 暂停引擎（可选）
```

### 8.3 主线程超时监控链

```
Search.search_multiple_requests()
└── th.join(remaining_time)
    └── 线程仍存活
        ├── th._timeout = True        # 设置标记供子线程读取
        ├── add_unresponsive_engine() # 立即标记无响应（用户可见）
        └── logger.error()            # 立即记录日志

        [子线程继续运行...]
        └── OnlineProcessor.extend_container()
            └── getattr(current_thread(), '_timeout') → True
                └── handle_exception('timeout', False)
                    ├── counter_inc(error)      # 错误计数+1
                    ├── count_error('timeout')   # 记录错误详情
                    └── 不暂停引擎（suspend=False）
```

### 8.4 External Bang 绕过链

```
用户输入 !!g test
└── RawTextQuery._parse_query()
    └── ExternalBangParser.__call__()
        └── raw_text_query.external_bang = 'g'
            └── Search.search()
                └── search_external_bang()
                    └── get_bang_url() → 生成重定向 URL
                        └── 返回 True → 跳过 search_answerers() 和 search_standard()
                            └── webapp.py → redirect()
```

### 8.5 Answerers 绕过链

```
用户输入 calc 1+1
└── Search.search()
    ├── search_external_bang() → 返回 False
    └── search_answerers()
        └── AnswerStorage.ask('calc 1+1')
            └── Calculator.answer('calc 1+1') → 返回结果
                └── 返回 True → 跳过 search_standard()
                    └── 结果直接渲染
```

---

## 九、关键配置参考

### 9.1 settings.yml 超时相关配置

```yaml
outgoing:
  request_timeout: 3.0        # 全局默认超时（秒）
  max_request_timeout: 10.0   # 全局最大超时（秒），默认注释
  extra_proxy_timeout: 10     # Tor 代理额外超时（秒）

search:
  ban_time_on_fail: 5         # 引擎失败后暂停时间（秒）
  max_ban_time_on_fail: 120   # 最大暂停时间（秒）
```

### 9.2 引擎级配置示例

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

### 9.3 用户查询语法

| 语法 | 含义 | 单位 |
|-----|------|------|
| `<3` | 3 秒超时 | 秒 |
| `<850` | 850 毫秒超时 | 毫秒 |
| `!!g test` | External Bang，跳转到 Google | 无超时 |
| `calc 1+1` | 触发 answerer，本地计算 | 无超时 |

---

## 十、代码位置索引

| 功能模块 | 文件路径 | 关键行号 |
|---------|---------|---------|
| 全局默认配置 | `searx/settings.yml` | 179-182 |
| 引擎默认参数 | `searx/engines/__init__.py` | 30-49 |
| 用户超时解析 | `searx/query.py` | 43-69 |
| External Bang 解析 | `searx/query.py` | 151-175 |
| 超时计算逻辑 | `searx/search/__init__.py` | 78-134 |
| External Bang 检测 | `searx/search/__init__.py` | 59-69 |
| Answerers 检测 | `searx/search/__init__.py` | 71-75 |
| 主线程超时监控 | `searx/search/__init__.py` | 136-158 |
| 搜索主流程分支 | `searx/search/__init__.py` | 174-179 |
| 线程超时设置 | `searx/network/__init__.py` | 28-44 |
| 超时应用逻辑 | `searx/network/__init__.py` | 73-108 |
| 在线处理器超时 | `searx/search/processors/online.py` | 257-282 |
| 异常处理 | `searx/search/processors/abstract.py` | 165-191 |
| UnresponsiveEngine 定义 | `searx/results.py` | 47-50 |
| add_unresponsive_engine | `searx/results.py` | 274-280 |
| 错误翻译映射 | `searx/webutils.py` | 36-67 |
| JSON 输出 | `searx/webutils.py` | 162-175 |
| 页面模板 | `searx/templates/simple/elements/engines_msg.html` | 1-34 |
| Tor 超时调整 | `searx/engines/__init__.py` | 196-199 |
| Answerer 核心 | `searx/answerers/_core.py` | 143-164 |
| External Bang URL 生成 | `searx/external_bang.py` | 93-109 |
| Web 层重定向 | `searx/webapp.py` | 666-667 |
