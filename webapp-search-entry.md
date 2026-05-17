# SearXNG Web 搜索请求完整流程分析

## 概述

本文档详细梳理了 SearXNG 从 Web 搜索入口发起请求，到查询对象创建、引擎调度、结果返回页面的完整主路径，以及各环节的失败回退和错误处理机制。

---

## 一、主路径流程

### 1. HTTP 请求入口层 (`searx/webapp.py`)

#### 1.1 路由入口
- **入口点**: `@app.route('/search', methods=['GET', 'POST'])` → `search()` 函数 [webapp.py:619-785](searx/webapp.py#L619-L785)
- **前置处理**: `@app.before_request` → `pre_request()` [webapp.py:459-518]
  - 初始化请求计时
  - 加载用户偏好设置 (Preferences)
  - 合并 GET/POST 参数
  - 解析并验证用户偏好
  - 确定启用的插件列表

#### 1.2 输入验证
```python
# 验证输出格式
output_format = sxng_request.form.get('format', 'html')
if output_format not in OUTPUT_FORMATS:
    output_format = 'html'
if output_format not in settings['search']['formats']:
    flask.abort(403)

# 验证查询参数存在性
if not sxng_request.form.get('q'):
    # 返回空搜索页面或错误
```

---

### 2. 查询对象构建层 (`searx/webadapter.py`)

#### 2.1 核心转换函数
- **入口**: `get_search_query_from_webapp(preferences, form)` [webadapter.py:221-300]
- **输出**: `Tuple[SearchQuery, RawTextQuery, unknown_engines, no_token_engines, selected_locale]`

#### 2.2 原始查询解析 (`RawTextQuery`)
- **类定义**: [searx/query.py:250-349](searx/query.py#L250-L349)
- **解析流程**:
  1. 使用正则 `re.split(r'(\s+)', self.query)` 分割查询
  2. 依次应用解析器链 (`PARSER_CLASSES`):
     - `TimeoutParser`: 解析 `<` 开头的超时设置 (`<3` 或 `<850`)
     - `LanguageParser`: 解析 `:` 开头的语言设置 (`:en`, `:zh-CN`)
     - `ExternalBangParser`: 解析 `!!` 开头的外部 bang (`!!g`)
     - `BangParser`: 解析 `!` 开头的引擎/类别选择 (`!wikipedia`, `!images`)
     - `FeelingLuckyParser`: 解析 `!!` 手气不错

#### 2.3 参数解析与验证
| 参数类型 | 解析函数 | 验证逻辑 |
|---------|---------|---------|
| 页码 | `parse_pageno()` | 必须为正整数 |
| 语言 | `parse_lang()` | 匹配 `VALID_LANGUAGE_CODE` 或 'auto' |
| 安全搜索 | `parse_safesearch()` | 0/1/2 三级 |
| 时间范围 | `parse_time_range()` | None/day/week/month/year |
| 超时 | `parse_timeout()` | 浮点数或 None |
| 类别 | `get_selected_categories()` | 从表单/偏好/默认回退 |

#### 2.4 引擎引用列表构建
1. **显式引擎指定**: 表单 `engines` 参数 → 直接解析为 `EngineRef` 列表
2. **类别指定**: 表单 `categories` 或 `category_*` 参数 → 映射到对应引擎
3. **偏好回退**: 无指定时使用用户偏好 (cookie) 中的类别
4. **默认回退**: 仍为空时使用 `['general']` 类别
5. **去重与验证**:
   - `deduplicate_engineref_list()`: 按 `category|name` 去重
   - `validate_engineref_list()`: 验证引擎存在性和 token 权限

#### 2.5 SearchQuery 对象构建
- **类定义**: [searx/search/models.py:28-119](searx/search/models.py#L28-L119)
- **核心属性**:
  ```python
  SearchQuery(
      query: str,                     # 纯查询文本
      engineref_list: list[EngineRef],# 引擎引用列表
      lang: str,                      # 语言
      safesearch: Literal[0,1,2],     # 安全搜索级别
      pageno: int,                    # 页码
      time_range: Optional[str],      # 时间范围
      timeout_limit: Optional[float], # 超时限制
      external_bang: Optional[str],   # 外部 bang
      engine_data: dict,              # 引擎透传数据
      redirect_to_first_result: bool  # 手气不错
  )
  ```

---

### 3. 搜索执行层 (`searx/search/__init__.py`)

#### 3.1 SearchWithPlugins 类
- **继承**: `Search` 基类 + 插件钩子
- **初始化**: `SearchWithPlugins(search_query, request, user_plugins)`
  - 绑定 `on_result` 回调用于插件过滤
  - 保存 request 对象引用 (用于线程上下文)

#### 3.2 搜索主流程 (`search()` 方法)
```python
def search(self) -> ResultContainer:
    # 插件前置钩子: 可拦截搜索
    if searx.plugins.STORAGE.pre_search(self.request, self):
        super().search()  # 执行实际搜索
    
    # 插件后置钩子: 可添加结果 (返回 list[Result])，异常被捕获不中断主流程
    searx.plugins.STORAGE.post_search(self.request, self)
    self.result_container.close()  # 关闭并计算分数 (post_search 结果也参与排序)
    return self.result_container
```

#### 3.3 Search 基类执行逻辑
```python
def search(self) -> ResultContainer:
    self.start_time = default_timer()
    
    # 1. 外部 bang 检测: 命中则直接跳转
    if self.search_external_bang():
        return self.result_container
    
    # 2. 内置 answerer 检测: 如计算器、随机数等
    if self.search_answerers():
        return self.result_container
    
    # 3. 标准引擎搜索
    self.search_standard()
    return self.result_container
```

---

### 4. 引擎调度与并发层

#### 4.1 请求参数生成 (`_get_requests()`)
- **遍历**: `for engineref in self.search_query.engineref_list`
- **处理器获取**: `processor = PROCESSORS.get(engineref.name)`
- **挂起检查**: `processor.extend_container_if_suspended()` → 已挂起则跳过
- **参数构建**: `processor.get_params(search_query, engineref.category)`
  - 不支持分页且 pageno > 1 → 返回 None (跳过)
  - 不支持时间范围但指定了 → 返回 None (跳过)
  - 超过 max_page → 返回 None (跳过)
- **超时计算**:
  ```python
  actual_timeout = min(
      default_timeout,          # 所有选中引擎的最大 timeout
      query_timeout or INF,     # 用户指定的超时
      max_request_timeout or INF # 系统配置的最大超时
  )
  ```

#### 4.2 多线程并发执行 (`search_multiple_requests()`)
- **线程池管理**:
  - 每个引擎请求一个独立线程
  - 线程命名用 UUID `search_id` 标识组
  - 使用 `copy_current_request_context()` 保留 Flask 上下文
- **超时控制**:
  ```python
  for th in threading.enumerate():
      if th.name == search_id:
          remaining_time = max(0.0, actual_timeout - (default_timer() - start_time))
          th.join(remaining_time)
          if th.is_alive():
              th._timeout = True
              result_container.add_unresponsive_engine(th._engine_name, 'timeout')
  ```

---

### 5. 引擎处理器层 (`searx/search/processors/`)

#### 5.1 处理器类型体系
| 处理器类型 | 适用引擎 | 核心特性 |
|-----------|---------|---------|
| `OnlineProcessor` | 在线引擎 (默认) | HTTP 请求、网络超时、SSL 处理 |
| `OfflineProcessor` | 离线引擎 | 直接调用，无网络 |
| `OnlineDictionaryProcessor` | 在线词典 | 特殊参数处理 |
| `OnlineCurrencyProcessor` | 货币转换 | 汇率缓存 |
| `OnlineUrlSearchProcessor` | URL 搜索 | URL 解析 |

#### 5.2 OnlineProcessor 执行流程 (`search()` 方法)
```python
def search(self, query, params, result_container, start_time, timeout_limit):
    # 1. 初始化线程网络上下文
    self.init_network_in_thread(start_time, timeout_limit)
    
    try:
        # 2. 引擎构建请求: self.engine.request(query, params)
        # 3. 发送 HTTP 请求: self._send_http_request(params)
        # 4. 引擎解析响应: self.engine.response(response)
        search_results = self._search_basic(query, params)
        
        # 5. 结果入容器
        self.extend_container(result_container, start_time, search_results)
        
    except ssl.SSLError as e:
        self.handle_exception(result_container, e, suspend=True)
    except (httpx.TimeoutException, asyncio.TimeoutError) as e:
        self.handle_exception(result_container, e, suspend=True)
    except (httpx.HTTPError, httpx.StreamError) as e:
        self.handle_exception(result_container, e, suspend=True)
    except (SearxEngineCaptchaException, SearxEngineTooManyRequestsException,
            SearxEngineAccessDeniedException) as e:
        self.handle_exception(result_container, e, suspend=True)
    except Exception as e:
        self.handle_exception(result_container, e)  # 不挂起
```

---

### 6. 结果收集与处理层 (`searx/results.py`)

#### 6.1 ResultContainer 数据结构
```python
class ResultContainer:
    main_results_map: dict[int, MainResult]    # 主键: hash(result)
    infoboxes: list[LegacyResult]              # 信息框
    suggestions: set[str]                      # 搜索建议
    answers: AnswerSet                         # 直接答案
    corrections: set[str]                      # 拼写修正
    unresponsive_engines: set[UnresponsiveEngine]
    timings: list[Timing]                      # 性能计时
    redirect_url: Optional[str]                 # 外部跳转 URL
```

#### 6.2 结果合并逻辑 (`extend()`)
1. **插件过滤**: `self.on_result(result)` → 返回 False 则丢弃
2. **结果类型分发**:
   - `BaseAnswer` → 加入 `answers`
   - `MainResult` → 调用 `_merge_main_result()`
   - 含 `suggestion` → 加入 `suggestions`
   - 含 `correction` → 加入 `corrections`
   - 含 `infobox` → 调用 `_merge_infobox()`
   - 含 `engine_data` → 存入 `engine_data` 字典
3. **主结果去重合并** (`_merge_main_result()`):
   - 按 URL 哈希匹配重复结果
   - 合并引擎列表: `origin.engines.add(other.engine)`
   - 保留更长的 title 和 content
   - 优先使用 HTTPS 协议的 URL
   - 累积 positions 用于分数计算

#### 6.3 结果排序 (`get_ordered_results()`)
1. **关闭容器**: `close()` → 计算每个结果的 score
   ```python
   score = sum(weight / position for position in positions)
   # weight = product of engine weights * len(positions)
   ```
2. **第一遍排序**: 按 score 降序
3. **第二遍分组**: 按 `category:template:has_image` 分组，避免同类结果聚集
   - 每组最多 8 个结果
   - 组间最大距离 20 个位置

---

### 7. 响应渲染层 (`searx/webapp.py` 后半段)

#### 7.1 结果后处理
```python
# 检查外部 bang 跳转
if result_container.redirect_url:
    return redirect(result_container.redirect_url)

# 手气不错: 直接跳转到第一个结果
if search_query.redirect_to_first_result and results:
    return redirect(results[0]['url'], 302)

# HTML 高亮处理
for result in results:
    result['content'] = highlight_content(escape(result['content'][:1024]), query)
    result['title'] = highlight_content(escape(result['title']), query)
    
# 模板分组标记
# 设置 open_group / close_group 用于前端渲染
```

#### 7.2 格式分支
| 输出格式 | 处理方式 |
|---------|---------|
| `json` | `webutils.get_json_response()` → JSON 响应 |
| `csv` | `webutils.write_csv_response()` → CSV 下载 |
| `rss` | 渲染 `opensearch_response_rss.xml` 模板 |
| `html` | 渲染 `results.html` 模板，传递完整上下文 |

#### 7.3 HTML 模板上下文
传递给模板的关键数据:
- `results`: 排序后的结果列表
- `suggestions`: 搜索建议 (带 URL)
- `answers`: 直接答案
- `corrections`: 拼写修正
- `infoboxes`: 信息框
- `unresponsive_engines`: 失败的引擎列表 (已翻译)
- `timings`: 各引擎响应时间
- `number_of_results`: 结果总数 (格式化)

---

## 二、失败回退与错误处理机制

### 1. 参数验证失败回退

| 错误场景 | 处理方式 | 代码位置 |
|---------|---------|---------|
| 无效页码 | 抛出 `SearxParameterException` → 返回 400 | [webadapter.py:48-52](searx/webadapter.py#L48-L52) |
| 无效语言 | 抛出 `SearxParameterException` → 返回 400 | [webadapter.py:55-72](searx/webadapter.py#L55-L72) |
| 无效安全搜索 | 抛出 `SearxParameterException` → 返回 400 | [webadapter.py:75-92](searx/webadapter.py#L75-L92) |
| 无效时间范围 | 抛出 `SearxParameterException` → 返回 400 | [webadapter.py:95-101](searx/webadapter.py#L95-L101) |
| 无效超时 | 抛出 `SearxParameterException` → 返回 400 | [webadapter.py:104-114](searx/webadapter.py#L104-L114) |
| 偏好解析异常 | 记录错误消息，继续使用默认值 | [webapp.py:475-480](searx/webapp.py#L475-L480) |

### 2. 引擎挂起机制 (`SuspendedStatus`)

#### 2.1 挂起触发条件
- `SearxEngineAccessDeniedException`: 访问被拒绝 → 默认挂起 1 天
- `SearxEngineCaptchaException`: 遇到 CAPTCHA → 默认挂起 1 天
- `SearxEngineTooManyRequestsException`: 请求过多 → 默认挂起 1 小时
- `ssl.SSLError`: SSL 错误 → 挂起
- `httpx.TimeoutException`: 超时 → 挂起
- `httpx.HTTPError`: 其他 HTTP 错误 → 挂起

#### 2.2 挂起时长计算
```python
suspended_time = min(
    max_ban_time_on_fail,  # 系统最大挂起时间
    ban_time_on_fail       # 单次挂起基础时间
)
# 连续错误会累积，但受 max_ban_time_on_fail 限制
```

#### 2.3 挂起状态检查
- **检查点 1**: `_get_requests()` → `extend_container_if_suspended()` → 已挂起则不发起请求
- **检查点 2**: 结果中标记 `suspended=True`，前端显示为灰色
- **自动恢复**: `suspend_end_time` 到期后自动解除
- **手动恢复**: 成功请求后调用 `resume()` 重置计数器

### 3. 单引擎失败不影响整体
- **设计原则**: 每个引擎独立线程，异常捕获在 `OnlineProcessor.search()` 内
- **失败记录**: `result_container.add_unresponsive_engine(engine_name, error_type)`
- **指标收集**: 错误计数、异常类型、性能直方图
- **用户可见**: 结果页底部显示"无响应引擎"列表，带错误类型翻译

### 4. 线程超时处理
- **超时标记**: 主线程 join 超时后设置 `th._timeout = True`
- **延迟检测**: 子线程 `extend_container()` 检查 `threading.current_thread()._timeout`
- **结果丢弃**: 超时后即使引擎返回结果也会被丢弃，标记为 timeout 错误

### 5. 插件拦截点
| 钩子 | 可执行操作 | 返回值影响 | 异常处理 |
|-----|-----------|-----------|---------|
| `pre_search` | 修改查询、添加条件 | 返回 `False` 可终止整个搜索 | 异常被捕获并记录日志，跳过该插件，继续执行后续插件 |
| `on_result` | 修改结果字段、过滤结果 | 返回 `False` 丢弃该结果 | 异常被捕获并记录日志，跳过该插件，结果保留 |
| `post_search` | 后处理结果集、添加答案/结果 | 返回 `list[Result]` 添加到结果集，返回 `None` 不添加 | 异常被捕获并记录日志，跳过该插件，**主流程继续** |

#### post_search 执行细节
```python
# PluginStorage.post_search() 内部实现
for plugin in enabled_plugins:
    try:
        results = plugin.post_search(request, search) or []
        # 结果被添加到容器: engine_name 标记为 "plugin: <plugin_id>"
        search.result_container.extend(f"plugin: {plugin.id}", results)
    except Exception:
        plugin.log.exception("Exception while calling post_search")
        continue  # 异常插件被跳过，不影响其他插件和主流程
```

### 6. 顶层异常兜底
```python
try:
    search_query, raw_text_query, _, _, selected_locale = get_search_query_from_webapp(...)
    search_obj = searx.search.SearchWithPlugins(...)
    result_container = search_obj.search()
except SearxParameterException as e:
    return index_error(output_format, e.message), 400
except Exception as e:
    logger.exception(e)
    return index_error(output_format, gettext('search error')), 500
```

---

## 三、关键数据流转图

```
HTTP 请求
    ↓
[webapp.py] search() 路由
    ├─ 参数验证 → 无效 → index_error()
    └─ get_search_query_from_webapp()
        ├─ RawTextQuery 解析语法糖
        ├─ 参数合法性校验
        ├─ 引擎/类别解析
        └─ 构建 SearchQuery
    ↓
[search/__init__.py] SearchWithPlugins.search()
    ├─ pre_search 插件钩子 → 返回 False 则终止搜索
    │   └─ 异常: 捕获+日志+跳过该插件，继续下一个
    ├─ search_external_bang() → 命中则 redirect
    ├─ search_answerers() → 命中则直接返回
    └─ search_standard()
        ├─ _get_requests() → 跳过挂起/不支持的引擎
        └─ search_multiple_requests() → 多线程并发
            ↓ 每个引擎线程
[processors/online.py] OnlineProcessor.search()
    ├─ engine.request() → 构建请求
    ├─ HTTP 发送 → 异常 → handle_exception(挂起)
    ├─ engine.response() → 解析结果
    └─ extend_container() → 结果入容器
    ↓
[results.py] ResultContainer
    ├─ extend() → 去重合并 (on_result 插件钩子在此触发)
    │   └─ on_result 异常: 捕获+日志+跳过该插件，结果保留
    └─ 结果收集完成，等待 post_search 添加结果
    ↓
[search/__init__.py] post_search 插件钩子
    ├─ 可返回 list[Result] 添加到结果集 (engine 标记为 "plugin: <id>")
    ├─ 返回 None 不添加结果
    └─ 异常: 捕获+日志+跳过该插件，主流程继续 ✓
    ↓
[results.py] ResultContainer.close() → 计算分数 + 排序
    └─ post_search 添加的结果也参与分数计算和排序
    ↓
[webapp.py] 结果渲染
    ├─ 格式分支 (html/json/csv/rss)
    ├─ 包含插件添加的结果 (已参与排序)
    └─ 模板渲染 / 序列化
    ↓
HTTP 响应（插件异常不影响页面返回）
```

---

## 四、关键文件索引

| 模块 | 文件路径 | 核心职责 |
|-----|---------|---------|
| Web 入口 | `searx/webapp.py` | Flask 路由、请求处理、模板渲染 |
| 查询适配 | `searx/webadapter.py` | Web 参数 → SearchQuery 转换 |
| 查询解析 | `searx/query.py` | RawTextQuery 语法解析 |
| 搜索核心 | `searx/search/__init__.py` | Search、SearchWithPlugins 类 |
| 数据模型 | `searx/search/models.py` | SearchQuery、EngineRef 定义 |
| 结果容器 | `searx/results.py` | ResultContainer、结果合并排序 |
| 处理器 | `searx/search/processors/` | 各类型引擎的执行逻辑 |
| 异常定义 | `searx/exceptions.py` | 异常类型体系 |
| 插件系统 | `searx/plugins/__init__.py` | 插件钩子机制 |
