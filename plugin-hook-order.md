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
[搜索前] pre_search() → 返回 False 可跳过引擎搜索
    ↓
[引擎搜索] 多线程并发请求各搜索引擎（pre_search 返回 True 时才执行）
    ↓
[结果逐个处理] on_result() → 每个结果触发一次，可过滤
    ↓
[搜索后] post_search() → 始终执行，可追加结果
    ↓
返回响应给用户
```

> **重要修正**：`post_search` 无论 `pre_search` 返回什么都会执行。

---

## 二、插件遍历顺序的重要说明

**核心事实**：`PluginStorage.plugin_list` 是 **`set[Plugin]`** 类型（见 `searx/plugins/_core.py:195`）。

```python
# searx/plugins/_core.py:195
plugin_list: set[Plugin]
```

**关键结论**：
- `set` 是无序集合，**工程上完全不可依赖其遍历顺序**
- 无论配置顺序如何，插件遍历顺序都是不确定的
- 即使是相同的配置、相同的机器、相同的 Python 版本，不同进程实例的遍历顺序也可能不同
- **不要假设任何插件执行顺序，不要基于顺序编写任何业务逻辑**

**为什么同配置下顺序也可能变化**：
- Python `set` 的内部哈希顺序受多种因素影响，包括对象内存地址、哈希随机化（Python 3.3+ 默认启用）、插入时机等
- 不同进程启动时，插件对象的内存地址不同，导致哈希值不同，遍历顺序自然不同
- 即使同一进程重启，内存布局也会变化，顺序可能改变
- 这是 Python 语言规范明确不保证的行为

**对短路逻辑的影响**：
- `pre_search` 的短路优先级**完全不可控**：如果多个插件都可能返回 `False`，无法预知哪个会先触发
- `on_result` 的过滤优先级**完全不可控**：如果多个插件都可能过滤同一结果，无法预知哪个会先生效

---

## 三、各阶段详细说明

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

**异常影响**：
- `load_settings` 阶段：导入模块异常会被捕获并记录日志，但如果插件类不存在（`cls is None`）会抛出 `ValueError`，**导致应用启动失败**
- `init` 方法调用：`PluginStorage.init()` 中没有异常捕获，如果插件的 `init()` 抛出异常，**会导致应用启动失败**
- 只有插件 `init()` 返回 `False` 是安全的（仅停用该插件，不影响启动）

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
# searx/search/__init__.py:201-206
def search(self) -> ResultContainer:
    if searx.plugins.STORAGE.pre_search(self.request, self):
        super().search()  # 只有 pre_search 返回 True 才执行引擎搜索
    
    searx.plugins.STORAGE.post_search(self.request, self)  # 始终执行！
    ...
```

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
- 遍历 `self.plugin_list`（set 类型），**顺序完全不可控**
- 任何插件返回 `False` 会立即终止循环，**跳过引擎搜索**，但 `post_search` 仍会执行
- 单个插件抛出异常仅记录日志，不影响其他插件和主流程（搜索继续）
- 可用于：查询重写、权限检查、提前返回答案等

**关于短路的重要说明**：
> ⚠️ **短路优先级不可控**。如果有多个插件可能返回 `False`，无法预知哪个插件会先触发短路。设计时必须假设：任何一个可能返回 `False` 的插件都有可能成为第一个触发者。如果业务需要确定的优先级，必须在单个插件内完成所有逻辑判断，或者重构为单插件方案。

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
- 遍历 `self.plugin_list`（set 类型），**顺序完全不可控**
- 任何插件返回 `False` 会立即终止循环，该结果被**丢弃**
- 单个插件抛出异常仅记录日志，该结果继续传递给后续插件
- 可用于：结果过滤、URL 重写、内容修改、字段补充等
- 插件可以直接修改 `result` 对象的属性

**关于结果过滤的重要说明**：
> ⚠️ **过滤优先级不可控**。如果有多个插件都可能过滤同一结果，无法预知哪个插件会先生效。设计时必须假设：任何一个可能返回 `False` 的插件都有可能成为第一个过滤者。如果业务需要确定的过滤顺序，必须在单个插件内组合所有过滤逻辑，或者确保各插件的过滤条件互斥不冲突。

---

### 阶段 5：搜索后 Hook（post_search）

**触发时机**：所有引擎搜索完成且结果处理完毕后执行（或 pre_search 返回 False 后立即执行）。

**调用链路**：
1. `searx/search/__init__.py:206` → `searx.plugins.STORAGE.post_search()`
2. `searx/plugins/_core.py:282-306` → `PluginStorage.post_search()`

**核心代码**：
```python
# searx/plugins/_core.py:282-306
def post_search(self, request, search):
    keyword = None
    for keyword in search.search_query.query.split():
        if keyword:
            break

    for plugin in [p for p in self.plugin_list if p.id in search.user_plugins]:
        if plugin.keywords:
            # 有关键词的插件：只有 keyword 存在且不匹配时才跳过
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
- **始终执行**：无论 `pre_search` 返回 `True` 还是 `False`，`post_search` 都会被调用
- 遍历 `self.plugin_list`（set 类型），**顺序完全不可控**
- **关键词逻辑**：
  - 当查询为空时，`keyword = None`，`if keyword and ...` 条件为 `False`，不会执行 `continue`
  - 因此**空查询时，有关键词的插件也会运行**
  - 只有当查询非空且首词不在插件关键词列表中时，才会跳过该插件
- 返回值会被追加到最终结果列表中
- 单个插件抛出异常仅记录日志，不影响其他插件
- 可用于：添加自定义答案、补充 infobox、结果重排序等

**关于结果追加的重要说明**：
> ⚠️ **结果追加顺序不可控**。多个插件返回的结果追加顺序是不确定的。如果需要确定的结果展示顺序，必须在单个插件内统一构造并返回，或者在返回结果后通过 `result_container` 进行排序处理。

---

## 四、异常处理机制

| Hook 类型 | 异常捕获位置 | 对主流程影响 | 对后续插件影响 |
|----------|-------------|-------------|---------------|
| `init()` | **无捕获** | **致命**：插件 `init()` 抛出异常会导致应用启动失败 | 后续插件不会初始化 |
| `pre_search()` | `_core.py:259-261` | 无影响（搜索继续） | 当前插件被跳过，继续执行下一个 |
| `on_result()` | `_core.py:273-275` | 无影响（当前结果继续处理） | 当前插件被跳过，继续执行下一个 |
| `post_search()` | `_core.py:301-303` | 无影响（结果正常返回） | 当前插件被跳过，继续执行下一个 |

**关键设计原则**：
- 初始化阶段异常是致命的（会导致启动失败）
- 请求处理阶段的单个插件故障不会导致整个搜索失败
- 异常仅记录日志，不向上层抛出
- 仅 `pre_search` 返回 `False` 能跳过引擎搜索（异常不会）

---

## 五、完整时序图

```
SearchWithPlugins.search()
        │
        ├─→ pre_search() 遍历插件链（顺序完全不可控！）
        │     ├─ 任意插件.pre_search() → True
        │     ├─ 任意插件.pre_search() → True
        │     └─ 任意插件.pre_search() → False → 终止，后续插件不执行
        │
        ├─ 条件分支：
        │     ├─ 若 pre_search 返回 True：
        │     │     ├─→ super().search()
        │     │     │     ├─ search_external_bang()
        │     │     │     ├─ search_answerers()
        │     │     │     └─ search_standard()
        │     │     │           └─ 多线程并发请求引擎
        │     │     │                 └─ 每个结果返回时触发 on_result 链（顺序完全不可控！）
        │     │     │                       ├─ 任意插件.on_result(result1)
        │     │     │                       ├─ 任意插件.on_result(result1)
        │     │     │                       └─ ...
        │     │     └─ post_search() 执行（顺序完全不可控！）
        │     │
        │     └─ 若 pre_search 返回 False：
        │           └─ post_search() 仍执行 ←────── 关键点！
        │
        └─ result_container.close()
```

---

## 六、关键注意事项

1. **plugin_list 是 set 类型**：遍历顺序完全不可控，工程上不可依赖任何执行顺序
2. **同配置下顺序也可能变化**：受内存地址、哈希随机化等因素影响，不同进程实例顺序不同
3. **短路逻辑**：`pre_search` 和 `on_result` 支持短路，但短路优先级完全不可控
4. **post_search 始终执行**：无论 `pre_search` 返回什么，`post_search` 都会被调用
5. **空查询时关键词插件仍运行**：查询为空时，有关键词限制的插件不会被跳过
6. **初始化异常致命**：插件 `init()` 抛出异常会导致应用启动失败
7. **线程安全**：`on_result` 在多线程环境下被调用，插件需保证线程安全
8. **结果修改**：`on_result` 中修改 `result` 对象会直接影响最终输出
9. **性能影响**：`on_result` 对每个结果都执行，注意避免耗时操作
10. **顺序不可控**：多个插件的短路、过滤、结果追加顺序均不可控，必要时应重构为单插件方案
