# SearXNG 插件 Hook 执行顺序报告

## 一、整体生命周期概览

SearXNG 插件系统通过三个核心 Hook 介入搜索请求流程，按照执行顺序排列：

```
应用启动
    ↓
[初始化阶段] Plugin.init() → 仅在应用启动时执行一次
    ↓
用户请求到达
    ↓
[请求预处理] 确定 user_plugins 列表（用户启用的插件）
    ↓
[搜索前] pre_search() → 可终止搜索
    ↓
[引擎搜索] 多线程并发请求各搜索引擎
    ↓
[结果逐个处理] on_result() → 每个结果触发一次，可过滤
    ↓
[搜索后] post_search() → 可追加结果
    ↓
返回响应给用户
```

---

## 二、各阶段详细说明

### 阶段 1：初始化阶段

**触发时机**：Web 应用启动时执行，仅运行一次。

**调用链路**：
1. `searx/webapp.py:1377` → `searx.plugins.initialize(app)`
2. `searx/plugins/__init__.py:107-109` → `STORAGE.load_settings()` + `STORAGE.init(app)`
3. `searx/plugins/_core.py:244-251` → 遍历所有插件调用 `plugin.init(app)`

**核心代码**：
```python
# searx/plugins/_core.py:129-138
def init(self, app: flask.Flask) -> bool:
    """返回 True 表示插件激活，False 表示停用该插件"""
    return True
```

**行为特点**：
- 每个插件的 `init()` 仅在应用启动时调用一次
- 返回值决定插件是否被加入可用列表（返回 False 会被移除）
- 可用于初始化数据库连接、加载资源等一次性操作
- 异常会导致该插件被跳过，但不影响应用启动

---

### 阶段 2：请求预处理（确定 user_plugins）

**触发时机**：每个 HTTP 请求到达时，在 `before_request` 中执行。

**调用链路**：
1. `searx/webapp.py:459` → `pre_request()`
2. `searx/webapp.py:512-518` → 根据用户偏好构建 `sxng_request.user_plugins`

**核心代码**：
```python
# searx/webapp.py:512-518
sxng_request.user_plugins = []
allowed_plugins = preferences.plugins.get_enabled()
disabled_plugins = preferences.plugins.get_disabled()
for plugin in searx.plugins.STORAGE:
    if (plugin.id not in disabled_plugins) or plugin.id in allowed_plugins:
        sxng_request.user_plugins.append(plugin.id)
```

**行为特点**：
- 每个请求都会重新计算用户启用的插件列表
- 基于用户 Cookie 中的偏好设置
- 后续所有 Hook 只会调用此列表中的插件

---

### 阶段 3：搜索前 Hook（pre_search）

**触发时机**：搜索开始前执行。

**调用链路**：
1. `searx/search/__init__.py:201-204` → `SearchWithPlugins.search()`
2. `searx/plugins/_core.py:253-265` → `PluginStorage.pre_search()`

**核心代码**：
```python
# searx/plugins/_core.py:253-265
def pre_search(self, request, search):
    ret = True
    for plugin in [p for p in self.plugin_list if p.id in search.user_plugins]:
        try:
            ret = bool(plugin.pre_search(request=request, search=search))
        except Exception:
            plugin.log.exception("Exception while calling pre_search")
            continue
        if not ret:
            break  # 第一个返回 False 的插件终止后续调用
    return ret
```

**行为特点**：
- 按插件在 `plugin_list` 中的顺序依次调用
- 任何插件返回 `False` 会立即终止循环，且**整个搜索被取消**
- 单个插件抛出异常仅记录日志，不影响其他插件和主流程
- 可用于：查询重写、权限检查、提前返回答案等

---

### 阶段 4：结果逐个处理 Hook（on_result）

**触发时机**：每个搜索引擎返回结果后，逐个结果处理时执行。

**调用链路**：
1. 各引擎处理器将结果添加到 `ResultContainer`
2. `searx/search/__init__.py:188` → `self.result_container.on_result = self._on_result`
3. `searx/search/__init__.py:198-199` → `_on_result()` 调用插件链
4. `searx/plugins/_core.py:267-280` → `PluginStorage.on_result()`

**核心代码**：
```python
# searx/plugins/_core.py:267-280
def on_result(self, request, search, result):
    ret = True
    for plugin in [p for p in self.plugin_list if p.id in search.user_plugins]:
        try:
            ret = bool(plugin.on_result(request=request, search=search, result=result))
        except Exception:
            plugin.log.exception("Exception while calling on_result")
            continue
        if not ret:
            break  # 第一个返回 False 的插件标记该结果被过滤
    return ret
```

**行为特点**：
- 每个结果对象都会触发一次完整的插件链遍历
- 任何插件返回 `False` 会立即终止循环，该结果被**丢弃**
- 单个插件抛出异常仅记录日志，该结果继续传递给后续插件
- 可用于：结果过滤、URL 重写、内容修改、字段补充等
- 插件可以直接修改 `result` 对象的属性

---

### 阶段 5：搜索后 Hook（post_search）

**触发时机**：所有引擎搜索完成且结果处理完毕后执行。

**调用链路**：
1. `searx/search/__init__.py:206` → `searx.plugins.STORAGE.post_search()`
2. `searx/plugins/_core.py:282-306` → `PluginStorage.post_search()`

**核心代码**：
```python
# searx/plugins/_core.py:282-306
def post_search(self, request, search):
    keyword = search.search_query.query.split()[0] if search.search_query.query.split() else None
    
    for plugin in [p for p in self.plugin_list if p.id in search.user_plugins]:
        if plugin.keywords:
            # 有关键词的插件，只有查询首词匹配时才执行
            if keyword and keyword not in plugin.keywords:
                continue
        try:
            results = plugin.post_search(request=request, search=search) or []
        except Exception:
            plugin.log.exception("Exception while calling post_search")
            continue
        
        search.result_container.extend(f"plugin: {plugin.id}", results)
```

**行为特点**：
- 按插件顺序依次调用，所有插件都会被执行
- 有关键词（`plugin.keywords`）的插件仅在查询首词匹配时执行
- 返回值会被追加到最终结果列表中
- 单个插件抛出异常仅记录日志，不影响其他插件
- 可用于：添加自定义答案、补充 infobox、结果重排序等

---

## 三、异常处理机制

| Hook 类型 | 异常捕获位置 | 对主流程影响 | 对后续插件影响 |
|----------|-------------|-------------|---------------|
| `init()` | 启动时无捕获，异常会导致应用启动失败 | 致命 | 后续插件不会初始化 |
| `pre_search()` | `_core.py:259-261` | 无影响（搜索继续） | 当前插件被跳过，继续执行下一个 |
| `on_result()` | `_core.py:273-275` | 无影响（当前结果继续处理） | 当前插件被跳过，继续执行下一个 |
| `post_search()` | `_core.py:301-303` | 无影响（结果正常返回） | 当前插件被跳过，继续执行下一个 |

**关键设计原则**：
- 单个插件故障不会导致整个搜索失败
- 异常仅记录日志，不向上层抛出
- 仅 `pre_search` 返回 `False` 能终止搜索（异常不会）

---

## 四、完整时序图

```
SearchWithPlugins.search()
        │
        ├─→ pre_search() 遍历插件链
        │     ├─ plugin_A.pre_search() → True
        │     ├─ plugin_B.pre_search() → True
        │     └─ plugin_C.pre_search() → False → 终止，后续插件不执行
        │
        ├─ 如果 pre_search 返回 True：
        │     ├─→ super().search()
        │     │     ├─ search_external_bang()
        │     │     ├─ search_answerers()
        │     │     └─ search_standard()
        │     │           └─ 多线程并发请求引擎
        │     │                 └─ 每个结果返回时触发 on_result 链
        │     │                       ├─ plugin_A.on_result(result1)
        │     │                       ├─ plugin_B.on_result(result1)
        │     │                       └─ ...
        │     │
        │     └─→ post_search() 遍历插件链
        │           ├─ plugin_A.post_search() → 追加结果
        │           ├─ plugin_B.post_search() → 追加结果
        │           └─ plugin_C.post_search() → 追加结果
        │
        └─ result_container.close()
```

---

## 五、关键注意事项

1. **插件执行顺序**：依赖于 `plugin_list` 集合的遍历顺序，与配置加载顺序相关
2. **短路逻辑**：`pre_search` 和 `on_result` 支持短路，第一个返回 `False` 的插件终止后续调用
3. **关键词插件**：`post_search` 阶段的关键词匹配是基于查询的第一个词
4. **线程安全**：`on_result` 在多线程环境下被调用，插件需保证线程安全
5. **结果修改**：`on_result` 中修改 `result` 对象会直接影响最终输出
6. **性能影响**：`on_result` 对每个结果都执行，注意避免耗时操作
