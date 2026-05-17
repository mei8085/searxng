# webapp-search-entry.md 插件链路校准更新报告

## 更新背景

原报告中对插件 `post_search` 钩子的描述存在两处不准确：
1. 误将 `post_search` 写成"无返回值"，实际可以返回结果列表
2. 缺少 `post_search` 异常捕获和失败回退逻辑的说明

## 校准内容

### 一、post_search 返回值纠正

**原错误描述**：
> `post_search` | 后处理结果集、添加答案 | 无返回值 |

**修正后描述**：
> `post_search` | 后处理结果集、添加答案/结果 | 返回 `list[Result]` 添加到结果集，返回 `None` 不添加 | 异常被捕获并记录日志，跳过该插件，**主流程继续** |

**实际源码依据** ([searx/plugins/_core.py:169-175](searx/plugins/_core.py#L169-L175))：
```python
def post_search(
    self, request: SXNG_Request, search: "SearchWithPlugins"
) -> "None | list[Result | LegacyResult] | EngineResults":
    """Runs AFTER the search request.  Can return a list of
    Result objects to be added to the final result list."""
    return
```

### 二、post_search 异常处理机制补充

**新增说明**：
`PluginStorage.post_search()` 内部对每个插件调用都有独立的 try-except 包裹：
```python
for plugin in enabled_plugins:
    try:
        results = plugin.post_search(request, search) or []
        # 结果被添加到容器: engine_name 标记为 "plugin: <plugin_id>"
        search.result_container.extend(f"plugin: {plugin.id}", results)
    except Exception:
        plugin.log.exception("Exception while calling post_search")
        continue  # 异常插件被跳过，不影响其他插件和主流程
```

**失败回退逻辑**：
- 单个插件 `post_search` 抛出异常 → 仅记录错误日志，跳过该插件
- 其他插件继续正常执行
- 主搜索流程不受影响，正常返回结果页面
- 插件异常不会导致 500 错误

### 三、主路径流程图更新

**两处关键修改在流程图中的体现**：

1. **post_search 节点独立化**：从 [webapp.py] 结果渲染中移出，提升为独立流程节点
2. **异常路径显式标注**：三个插件钩子都补充了异常处理分支

**更新后流程图关键段**：
```
[results.py] ResultContainer
    ├─ extend() → 去重合并
    ├─ on_result 插件钩子 → 返回 False 丢弃该结果
    │   └─ 异常: 捕获+日志+跳过该插件，结果保留
    └─ close() → 计算分数
    ↓
[search/__init__.py] post_search 插件钩子  ← 独立节点
    ├─ 可返回 list[Result] 添加到结果集 (engine 标记为 "plugin: <id>")
    ├─ 返回 None 不添加结果
    └─ 异常: 捕获+日志+跳过该插件，主流程继续 ✓  ← 显式标注
    ↓
[webapp.py] 结果渲染
    ├─ 格式分支 (html/json/csv/rss)
    ├─ 包含插件添加的结果  ← 明确数据来源
    └─ 模板渲染 / 序列化
    ↓
HTTP 响应（插件异常不影响页面返回）  ← 最终影响说明
```

### 四、插件拦截点表格完整化

新增"异常处理"列，统一三个钩子的错误处理说明：

| 钩子 | 可执行操作 | 返回值影响 | 异常处理 |
|-----|-----------|-----------|---------|
| `pre_search` | 修改查询、添加条件 | 返回 `False` 可终止整个搜索 | 异常被捕获并记录日志，跳过该插件，继续执行后续插件 |
| `on_result` | 修改结果字段、过滤结果 | 返回 `False` 丢弃该结果 | 异常被捕获并记录日志，跳过该插件，结果保留 |
| `post_search` | 后处理结果集、添加答案/结果 | 返回 `list[Result]` 添加到结果集，返回 `None` 不添加 | 异常被捕获并记录日志，跳过该插件，**主流程继续** |

## 对结果返回页面的影响

### 正常流程
- 插件 `post_search` 返回的结果会被正常纳入结果集
- 这些结果的 `engine` 字段标记为 `"plugin: <plugin_id>"`
- 参与统一的去重、合并和排序流程
- 最终在 HTML/JSON/CSV/RSS 各格式输出中正常显示

### 异常流程（插件报错）
- 仅错误日志中可见异常信息
- 页面正常返回，用户无感知
- 其他插件的结果正常添加
- 搜索引擎返回的原始结果不受影响
- **无 500 错误，无页面中断**

## 校准涉及的文件位置

| 修改点 | 原文件位置 |
|-------|-----------|
| post_search 方法签名 | [searx/plugins/_core.py:169-175](searx/plugins/_core.py#L169-L175) |
| PluginStorage.post_search 实现 | [searx/plugins/_core.py:282-306](searx/plugins/_core.py#L282-L306) |
| pre_search 异常处理 | [searx/plugins/_core.py:253-265](searx/plugins/_core.py#L253-L265) |
| on_result 异常处理 | [searx/plugins/_core.py:267-280](searx/plugins/_core.py#L267-L280) |
