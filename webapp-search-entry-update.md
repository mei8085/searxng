# webapp-search-entry.md 主路径图执行顺序校准报告

## 校准背景

用户指出主路径图中 `post_search` 和 `close()` 的执行顺序与源码不符，需要修正以确保图示与源码完全一致。

## 源码确认

**实际执行顺序** ([searx/search/__init__.py:201-209](searx/search/__init__.py#L201-L209))：
```python
def search(self) -> ResultContainer:
    if searx.plugins.STORAGE.pre_search(self.request, self):
        super().search()      # 1. 执行引擎搜索（调用 extend 收集结果）
    
    searx.plugins.STORAGE.post_search(self.request, self)  # 2. post_search 钩子（可添加新结果）
    self.result_container.close()                          # 3. close() 计算分数 + 排序
    
    return self.result_container
```

**关键结论**：`post_search` 在 `close()` 之前执行，因此 post_search 添加的结果**参与分数计算和排序**。

## 修正内容

### ✅ 第一处：主路径图顺序修正

**修正前（错误顺序）**：
```
[results.py] ResultContainer
    ├─ extend() → 去重合并
    ├─ on_result 插件钩子 → 返回 False 丢弃该结果
    │   └─ 异常: 捕获+日志+跳过该插件，结果保留
    └─ close() → 计算分数                     ← close() 在此
    ↓
[search/__init__.py] post_search 插件钩子    ← post_search 在此（顺序错误！）
    ├─ 可返回 list[Result] 添加到结果集
    ...
```

**修正后（正确顺序）**：
```
[results.py] ResultContainer
    ├─ extend() → 去重合并 (on_result 插件钩子在此触发)
    │   └─ on_result 异常: 捕获+日志+跳过该插件，结果保留
    └─ 结果收集完成，等待 post_search 添加结果    ← 不调用 close()
    ↓
[search/__init__.py] post_search 插件钩子        ← post_search 先执行
    ├─ 可返回 list[Result] 添加到结果集 (engine 标记为 "plugin: <id>")
    ├─ 返回 None 不添加结果
    └─ 异常: 捕获+日志+跳过该插件，主流程继续 ✓
    ↓
[results.py] ResultContainer.close() → 计算分数 + 排序    ← close() 后执行
    └─ post_search 添加的结果也参与分数计算和排序
    ↓
[webapp.py] 结果渲染
    ├─ 格式分支 (html/json/csv/rss)
    ├─ 包含插件添加的结果 (已参与排序)        ← 明确标注"已参与排序"
    └─ 模板渲染 / 序列化
    ↓
HTTP 响应（插件异常不影响页面返回）
```

### ✅ 第二处：正文结果排序说明同步

**位置**：第 6.3 节 "结果排序"

**新增说明**：
```
> **注意**: `close()` 在 `post_search` 之后调用，插件 `post_search` 返回的结果也参与分数计算
```

**影响**：
- post_search 添加的结果的 `positions` 会被计入 score 计算
- 插件结果与搜索引擎结果一起参与排序
- 最终输出的结果列表中，插件结果可能出现在任意位置，而非固定在末尾

### ✅ 第三处：响应渲染层说明同步

**位置**：主路径图的结果渲染节点

**新增标注**：`包含插件添加的结果 (已参与排序)`

**对页面返回的影响**：
| 场景 | 影响 |
|-----|------|
| post_search 返回结果 | 结果正常进入结果池，参与统一排序，与引擎返回结果无区别对待 |
| post_search 抛出异常 | 仅跳过该插件，close() 正常执行，排序正常进行，页面正常返回 |
| 多个 post_search 插件 | 按顺序执行，异常不影响后续插件，所有成功返回的结果都参与排序 |

## 顺序修正的深层影响

### 数据完整性
- **修正前误解**：post_search 在 close() 之后 → 插件结果可能被跳过排序，或需要特殊处理
- **修正后事实**：post_search 在 close() 之前 → 插件结果完全融入标准结果流

### 插件开发影响
插件开发者可以依赖以下行为：
1. `post_search` 返回的 `Result` 对象会被正常计算 score
2. 插件结果会根据 score 被插入到合适的排序位置
3. 不需要在插件中手动处理排序或位置调整

### 错误隔离
- post_search 异常 → 仅跳过该插件的结果添加
- close() 仍然正常执行 → 已有结果的排序不受影响
- 顶层异常兜底不会被触发 → 用户无感知

## 校准后完整执行时序

```
时间轴 →
│
├─ super().search() 执行
│   ├─ 多线程调用各引擎
│   └─ 每个引擎结果 → extend() → on_result 钩子 → 加入容器
│
├─ post_search() 执行  ← 第 1 步
│   ├─ 遍历启用的插件
│   │   ├─ 插件 A 返回结果 → extend() 加入容器
│   │   ├─ 插件 B 抛出异常 → 日志 + continue
│   │   └─ 插件 C 返回结果 → extend() 加入容器
│   └─ 所有插件处理完毕
│
└─ close() 执行        ← 第 2 步
    ├─ 遍历所有结果（包括 post_search 添加的）
    ├─ 计算每个结果的 score
    └─ 排序 + 分组
```

## 修改的文件位置

| 校准点 | 文件 | 行号 |
|-------|------|------|
| 主路径图顺序修正 | `webapp-search-entry.md` | 411-429 |
| 结果排序注意事项新增 | `webapp-search-entry.md` | 250 |
| 结果渲染节点标注更新 | `webapp-search-entry.md` | 426 |
