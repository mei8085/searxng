# webapp-search-entry.md 最终对齐更新报告

## 更新背景

补齐主文档最后一处不一致：确保结果排序章节、主路径图、时序说明三者完全对齐，明确 `close()` 在 `post_search` 之后调用的执行顺序，保证排序与页面返回的叙述前后一致。

## 已完成的三处对齐

### ✅ 第一处：结果排序章节增强

**位置**：第 6.3 节 "结果排序 (`get_ordered_results()`)

**更新内容**：

1. **新增执行时序说明**：
   ```
   **执行时序**：`post_search()` → `close()` → `get_ordered_results()`（参见主路径图和 3.2 节搜索主流程）
   ```

2. **关键对齐标注**：
   ```
   > **关键对齐**: `close()` 在 `post_search` 之后调用，插件 `post_search` 返回的结果已通过 `extend()` 加入容器，因此**也参与分数计算**
   ```

3. **排序步骤补充**：
   - 第 2 步标注："按 score 降序（插件结果与引擎结果按同一规则混合排序）

4. **页面返回关联**：
   ```
   > **与页面返回的关联**: `get_ordered_results()` 返回的列表直接传递给模板渲染，post_search 插件添加的结果可能出现在结果页的任意位置（取决于 score 计算），而非固定在末尾。
   ```

### ✅ 第二处：响应渲染层时序对齐

**位置**：第 7.3 节 "HTML 模板上下文

**更新内容**：

1. **results 字段说明增强**：
   ```
   - `results`: **排序后的最终结果列表**（已通过 `ResultContainer.close()` 计算分数并排序，包含引擎返回结果和 `post_search` 插件添加的结果）
   ```

2. **执行时序对齐标注**：
   ```
   > **执行时序对齐**: `get_ordered_results()` 在 `close()` 之后调用，此时 post_search 插件添加的结果已完成分数计算和排序，与引擎结果无区别地融入结果列表。
   ```

### ✅ 第三处：主路径图验证（已在此前更新中完成）

**当前正确顺序**：
```
[results.py] ResultContainer
    ├─ extend() → 去重合并 (on_result 插件钩子在此触发)
    │   └─ on_result 异常: 捕获+日志+跳过该插件，结果保留
    └─ 结果收集完成，等待 post_search 添加结果
    ↓
[search/__init__.py] post_search 插件钩子        ← 第 1 步
    ├─ 可返回 list[Result] 添加到结果集
    └─ 异常: 捕获+日志+跳过该插件，主流程继续 ✓
    ↓
[results.py] ResultContainer.close() → 计算分数 + 排序    ← 第 2 步
    └─ post_search 添加的结果也参与分数计算和排序
    ↓
[webapp.py] 结果渲染
    └─ 包含插件添加的结果 (已参与排序)
```

## 三处叙述完全对齐验证

| 文档位置 | 叙述内容 | 是否一致 |
|---------|---------|---------|
| 3.2 节搜索主流程代码注释 | `post_search` → `close()` 顺序 | ✅ |
| 6.3 节结果排序执行时序 | `post_search()` → `close()` → `get_ordered_results()` | ✅ |
| 主路径图节点顺序 | ResultContainer → post_search → close() → 渲染 | ✅ |
| 7.3 节模板上下文说明 | results 包含 post_search 结果且已排序 | ✅ |
| 异常处理说明 | post_search 异常不影响 close() 和排序 | ✅ |

## 对页面返回的最终影响确认

### 正常流程（插件返回结果）
1. 引擎搜索 → 结果入容器（extend）
2. post_search 插件返回结果 → 调用 extend() 加入容器
3. close() → **所有结果（含插件）计算 score
4. get_ordered_results() → **所有结果混合排序
5. 模板渲染 → 插件结果可能出现在任意位置

### 异常流程（插件报错）
1. 引擎搜索 → 结果入容器
2. post_search 插件抛出异常 → 捕获 + 日志 + continue
3. close() → **正常计算已有结果的 score**（不受插件异常影响）
4. get_ordered_results() → **正常排序**
5. 模板渲染 → **页面正常返回**，用户无感知

## 源码依据交叉验证

| 行为 | 源码位置 |
|-----|---------|
| post_search 在 close() 之前 | [searx/search/__init__.py:206-207](searx/search/__init__.py#L206-L207) |
| post_search 调用 extend() 添加结果 | [searx/plugins/_core.py:306](searx/plugins/_core.py#L306) |
| close() 计算所有结果的 score | [searx/results.py:192-195](searx/results.py#L192-L195) |
| get_ordered_results() 在 close() 之后调用 | [searx/webapp.py:696](searx/webapp.py#L696) |

## 修改的文件行号

| 更新点 | 文件 | 行号 |
|-------|------|------|
| 结果排序章节时序说明新增 | `webapp-search-entry.md` | 246 |
| 结果排序关键对齐标注 | `webapp-search-entry.md` | 253 |
| 排序步骤混合排序说明 | `webapp-search-entry.md` | 254 |
| 页面返回关联说明新增 | `webapp-search-entry.md` | 259 |
| 模板上下文 results 字段增强 | `webapp-search-entry.md` | 289 |
| 模板上下文时序对齐标注 | `webapp-search-entry.md` | 298 |
