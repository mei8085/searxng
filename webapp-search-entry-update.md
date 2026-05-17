# webapp-search-entry.md 正文校准更新报告

## 校准背景

针对 `webapp-search-entry.md` 正文中插件链路的三处不准确描述进行校准：
1. `post_search` 被误写为"无返回值"
2. 缺少 `post_search` 异常捕获后的失败回退逻辑说明
3. 主路径图需要同步体现上述变化对结果返回页面的影响

## 校准完成情况

### ✅ 第一处：修正 post_search 返回值结论

**位置**：第 3.2 节 + 第 5 节插件拦截点表格

**修改前**：
```
# 3.2 节注释
# 插件后置钩子: 可修改结果

# 5. 插件拦截点表格
| `post_search` | 后处理结果集、添加答案 | 无返回值 |
```

**修改后**：
```python
# 3.2 节代码注释
# 插件后置钩子: 可添加结果 (返回 list[Result])，异常被捕获不中断主流程
searx.plugins.STORAGE.post_search(self.request, self)
self.result_container.close()  # 关闭并计算分数 (post_search 结果也参与排序)

# 5. 插件拦截点表格 (新增异常处理列)
| `post_search` | 后处理结果集、添加答案/结果 | 返回 `list[Result]` 添加到结果集，返回 `None` 不添加 | 异常被捕获并记录日志，跳过该插件，**主流程继续** |
```

**源码依据**：[searx/plugins/_core.py:169-175](searx/plugins/_core.py#L169-L175)
```python
def post_search(
    self, request: SXNG_Request, search: "SearchWithPlugins"
) -> "None | list[Result | LegacyResult] | EngineResults":
```

---

### ✅ 第二处：补充 post_search 异常回退逻辑

**位置**：第 5 节插件拦截点（新增 `post_search` 执行细节小节）

**新增内容**：
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

**失败回退逻辑链**：
1. 单个 `post_search` 插件抛出异常 → `try-except` 捕获
2. 记录完整异常栈到日志（`plugin.log.exception`）
3. 执行 `continue` 跳过当前异常插件
4. **继续循环执行下一个插件** → 其他插件的结果正常添加
5. 所有插件遍历完成后 → 主流程继续执行 `close()` 和渲染
6. **对用户无影响** → 页面正常返回，不触发 500 错误

**源码依据**：[searx/plugins/_core.py:282-306](searx/plugins/_core.py#L282-L306)

---

### ✅ 第三处：同步修改主路径图

**位置**：第三部分"关键数据流转图"

**修改要点**：

1. **post_search 节点独立化**
   - 从 `[webapp.py] 结果渲染` 内部移出
   - 提升为 `[search/__init__.py]` 层的独立节点
   - 位于 `ResultContainer.close()` 之前（因为 post_search 添加的结果需要参与分数计算）

2. **异常路径显式标注**
   - 三个插件钩子（pre_search/on_result/post_search）都补充了异常分支说明
   - post_search 节点明确标注：`异常: 捕获+日志+跳过该插件，主流程继续 ✓`

3. **对结果页面的影响明确化**
   - 结果渲染节点标注：`包含插件添加的结果`
   - HTTP 响应节点标注：`（插件异常不影响页面返回）`

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

---

## 校准后插件链路完整对照表

| 钩子 | 返回值 | 异常处理 | 对主流程影响 |
|-----|-------|---------|-------------|
| `pre_search` | `bool` - `False` 终止搜索 | 捕获+日志+跳过该插件，继续下一个 | 单个插件异常不终止，仅返回 `False` 时终止 |
| `on_result` | `bool` - `False` 丢弃结果 | 捕获+日志+跳过该插件，结果保留 | 单个插件异常不影响结果，仅返回 `False` 时丢弃 |
| `post_search` | `None` 或 `list[Result]` | 捕获+日志+跳过该插件，主流程继续 ✓ | **异常完全不影响**，其他插件和结果正常 |

## 对结果返回页面的影响总结

### 正常流程
- `post_search` 返回的结果 → 加入 `ResultContainer`
- engine 标记为 `"plugin: <plugin_id>"`
- 参与 `close()` 的分数计算
- 参与去重、合并、排序
- 最终在 HTML/JSON/CSV/RSS 中正常显示

### 异常流程（插件报错）
- 仅日志可见异常 → `plugin.log.exception`
- **页面正常返回** → 用户无感知
- **其他插件继续执行** → 它们的结果正常添加
- **引擎原始结果不受影响** → 搜索引擎返回的结果全部保留
- **无 500 错误** → 顶层异常兜底不会被触发

## 修改的文件行号

| 校准点 | 文件 | 行号 |
|-------|------|------|
| post_search 返回值注释修正 | `webapp-search-entry.md` | 109-111 |
| 插件拦截点表格完整化 | `webapp-search-entry.md` | 348-353 |
| post_search 执行细节新增 | `webapp-search-entry.md` | 355-366 |
| 主路径图更新 | `webapp-search-entry.md` | 385-428 |
