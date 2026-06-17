# SearXNG 搜索引擎适配层——测试夹具与回归样例审查

> 本文档基于代码逐行核对，梳理 `tests/unit/engines/` 与 `tests/unit/test_engine_*.py` 中所有测试类的初始化生命周期、夹具数据来源、解析路径覆盖情况，并从三维度（静态夹具结构覆盖、解析逻辑验证、真实页面/API 改版发现能力）对回归样例进行分类。

---

## 1. 目录与文件索引

所有路径均为相对于仓库根目录（即 `searx/` 与 `tests/` 所在目录）的可读引用。

| 路径 | 说明 |
|------|------|
| `searx/__init__.py` | 模块顶层：定义全局 `settings = {}`、`logger`；L146 在模块导入时立即执行 `init_settings()` |
| `searx/utils.py` | 通用工具：`load_module()`（L428）使用 `importlib.util.spec_from_file_location` + `exec_module` 新建模块对象，**不注册到 `sys.modules`** |
| `searx/engines/__init__.py` | 引擎注册表：`load_engines()`（L267）、`load_engine()`（L82）、`update_engine_attributes()`（L179）、全局字典 `engines`；`load_engine()` L124 调用 `load_module()` 创建新模块副本 |
| `searx/engines/xpath.py` | 通用 XPath 引擎：`request()`（L230）、`response()`（L275） |
| `searx/engines/json_engine.py` | 通用 JSON 引擎：`request()`（L315）、`extract_response_info()`（L362）、`response()`（L393） |
| `searx/engines/command.py` | 通用命令行引擎：`search()`（L129）、`_get_results_from_process()`（L156）、`__parse_single_result()`（L224） |
| `searx/engines/tineye.py` | TinEye 反图搜：`parse_tineye_match()`（L84）、`response()`（L152） |
| `searx/engines/github_code.py` | GitHub Code Search：`request()`（L144）、`extract_code()`（L162）、`response()`（L214） |
| `searx/search/processors/online.py` | 在线搜索处理器：`OnlineProcessor`、`_search_basic()`（L225）、`search()`（L241） |
| `searx/search/__init__.py` | 搜索入口：`initialize()`（L33）、`Search.search_multiple_requests()`（L136） |
| `tests/__init__.py` | 测试基类：`SearxTestCase`、`init_test_settings()`（L57）、`setattr4test()`（L46）；L9-L10 在模块级清空默认 settings 路径变量并禁用 /etc 配置 |
| `tests/unit/__init__.py` | 模块级初始化：L10 设置默认 `SEARXNG_SETTINGS_PATH` 指向 `test_settings.yml` |
| `tests/unit/settings/` | YAML 夹具配置目录 |
| `tests/unit/settings/test_settings.yml` | 默认配置：仅加载 `demo_offline` 两个实例 |
| `tests/unit/settings/test_tineye.yml` | 仅加载 `tineye` 引擎 |
| `tests/unit/settings/test_github_code.yml` | 仅加载 `github code` 引擎 |
| `tests/unit/settings/test_result_container.yml` | 保留 `google`、`duckduckgo` 两个默认引擎 |
| `tests/unit/engines/test_xpath.py` | XPath 引擎测试 |
| `tests/unit/engines/test_json_engine.py` | JSON 引擎测试 |
| `tests/unit/engines/test_command.py` | Command 引擎测试 |
| `tests/unit/test_engine_tineye.py` | TinEye 引擎测试 |
| `tests/unit/test_engine_github_code.py` | GitHub Code 引擎测试 |
| `tests/unit/test_engines_init.py` | 引擎注册/加载逻辑测试 |
| `tests/unit/processors/test_online.py` | OnlineProcessor 参数组装测试 |

---

## 2. 初始化三层模型与测试类生命周期

### 2.1 三层初始化模型

测试运行期间存在三个层级的初始化，发生在不同时间点，务必区分：

| 层级 | 名称 | 触发时机 | 执行内容 | 作用范围 |
|------|------|---------|---------|---------|
| **L1** | searx 导入时的基础 settings 初始化 | Python 解释器首次 `import searx` 时（`searx/__init__.py` L146） | 读取 `SEARXNG_SETTINGS_PATH` 指向的 YAML → `settings_loader.load_settings()` → `apply_schema()` → 填充全局 `searx.settings` 字典 → 配置 logging | 进程全局；任何测试文件只要导入链经过 `searx.*` 即触发 |
| **L2** | SearxTestCase.init_test_settings 执行的完整搜索初始化 | `SearxTestCase.setUp()`（`tests/__init__.py` L43-L44）每个测试方法执行前 | ① 重新设置 `SEARXNG_SETTINGS_PATH` ② `searx.init_settings()`（重新加载 L1） ③ `searx.plugins.initialize(app)` ④ `searx.search.initialize(...)`（见 L3） ⑤ 配置 Flask `TESTING=True` + 创建 test client | 每个测试方法执行一次；会**覆盖** L1 的 settings 内容 |
| **L3** | 引擎注册表加载（`load_engines()`） | `SearxTestCase.init_test_settings()` 内部的 `searx.search.initialize()`（`searx/search/__init__.py` L33-L44），或测试方法内直接调用 | `load_engines(settings['engines'])` → 清空 `searx.engines.engines` 字典 → 遍历 YAML 中的 engine 列表 → `load_engine()` → `load_module()` 加载 `.py` 模块 → `update_engine_attributes()` 注入 YAML 属性 → `setup()` 回调 → 注册到全局 `engines` 字典 | 同时初始化 network、metrics、processors |

关键关联：
- L3 是 L2 的子集：每次 L2 执行必定伴随一次 L3
- L1 独立发生：只要某测试文件的 import 链经过 `searx`（例如 `from searx.engines import json_engine` 间接触发 `import searx`），L1 就在模块导入阶段先于任何 `setUp` 完成
- `searx.init_settings()`（L2 步骤 ②）会再次执行 L1 的完整流程，覆盖 L1 阶段加载的 settings 内容

### 2.2 两种模块加载路径与对象身份差异

L3 中 `load_engine()` 通过 `searx.utils.load_module()`（`searx/utils.py` L428-L439）加载引擎 `.py` 文件，其核心实现为：

```python
spec = importlib.util.spec_from_file_location(modname, modpath)  # 不查 sys.modules
module = importlib.util.module_from_spec(spec)                   # 全新对象
spec.loader.exec_module(module)                                   # 执行源码
return module                                                     # 未写入 sys.modules
```

这与测试代码中 `from searx.engines import xpath` 的**正常 Python 导入机制**（写入 `sys.modules`、同进程复用唯一对象）是两条完全独立的路径。由此产生**两类完全不同生命周期的模块对象**：

| 维度 | 路径 A：`from searx.engines import xxx`（导入模块） | 路径 B：`load_engines()` → `load_module()`（注册表模块） |
|------|--------------------------------------------------|------------------------------------------------------|
| **对象创建机制** | Python `import` 语句，经 `sys.modules` 缓存 | `importlib.util.spec_from_file_location` 直接执行 `.py` |
| **`sys.modules` 注册** | ✅ 注册为 `searx.engines.xxx`，进程全局唯一 | ❌ 不注册，每次调用都是独立的新对象 |
| **对象复用性** | 同进程内多次 import 返回同一对象引用 | 每次 `load_engine()` 调用都创建独立副本 |
| **YAML 属性注入** | ❌ 从未被 `update_engine_attributes()` 处理 | ✅ L179-L194 注入 YAML 属性 + `ENGINE_DEFAULT_ARGS` |
| **Tor 属性更新** | ❌ 不经过 `update_attributes_for_tor()` | ✅ 按 `using_tor_proxy` 更新 `search_url`/`timeout` |
| **EngineTraitsMap 注入** | ❌ 未执行 `trait_map.set_traits()` | ✅ L138-L141 设置语言/网络 traits |
| **`setup()` 回调** | ❌ 未调用 | ✅ L233-L249 调用 `engine.setup()` |
| **logger 初始化** | 通常由 `set_loggers()` L168-L176 在其他引擎加载时补全，但非时机确定 | ✅ L149 `set_loggers()` 显式设置 |

**根本结论**：路径 A 的导入模块与路径 B 的注册表模块是**两个独立的 Python 对象**，对其中一方修改属性不会影响另一方。二者仅共享模块顶层定义的常量（如 `tineye.FORMAT_NOT_SUPPORTED` 字面量值相同，但对象不相同——每次 `exec_module` 都会重新创建这些常量的新实例）。

对测试体系的直接影响：
- `TestXpathEngine`/`TestJsonEngine`/`TestCommandEngine` 使用路径 A（`from searx.engines import xxx`），操作的是 `sys.modules` 中**持久存在的唯一对象**
- `TinEyeTests`/`GithubCodeTests` 使用路径 B（`searx.engines.engines['xxx']`），操作的是**每次 `setUp()` 新建的独立副本**
- `TinEyeTests.tearDown()` 中 `load_engines([])` 仅清空路径 B 的注册表，与路径 A 对象毫无关系
- `TestEnginesInit` 中替换 `searx.engines.engines` 字典仅影响路径 B 的使用者

### 2.3 测试运行时的实际执行顺序

以一次完整的测试运行为例，时序如下：

```
[Python 启动]
  → import tests           → tests/__init__.py L9-L10: 清空默认 SEARXNG_SETTINGS_PATH，禁用 /etc
  → import tests.unit      → tests/unit/__init__.py L10: 设置 SEARXNG_SETTINGS_PATH = test_settings.yml
  → import tests.unit.engines.test_json_engine
     → from searx.engines import json_engine
        → import searx     → searx/__init__.py L25: settings = {}
                           → searx/__init__.py L146: init_settings() 【L1 发生】
                                               ↑ 此时 SEARXNG_SETTINGS_PATH 已由 tests/unit/__init__.py
                                                 设置为 test_settings.yml，因此加载的是默认 YAML
  → import tests.unit.engines.test_xpath
     → from searx.engines import xpath
        → searx 已导入，L1 不再重复执行

[unittest 收集测试类并逐个执行 test_* 方法]
  → TestXpathEngine.test_request 开始
     → setUp() → super().setUp() → SearxTestCase.setUp()
        → init_test_settings() 【L2 发生】
           → 重设 SEARXNG_SETTINGS_PATH → init_settings()（覆盖 L1 settings）
           → plugins.initialize()
           → searx.search.initialize() 【L3 发生：加载 demo_offline 两个引擎】
           → 创建 Flask test client
     → 测试体执行
     → tearDown()（默认空实现）
  → TestXpathEngine.test_response 开始
     → 同上 L2 + L3 再次完整执行

  → TestJsonEngine.test_request 开始
     → setUp() → json_engine.logger = ...  【未调用 super().setUp()】
        → ❌ L2 不执行 ❌
        → ❌ L3 不执行 ❌
        → 但 L1 早已在模块导入阶段完成，searx.settings 已存在且包含 test_settings.yml 内容
     → 测试体执行（仅依赖 json_engine 模块，不依赖全局引擎注册表）
     → tearDown()（默认空实现）

  → TinEyeTests.test_status_code_raises 开始
     → setUp() → super().setUp() → init_test_settings(TEST_SETTINGS="test_tineye.yml")【L2 + L3 发生】
        → 加载 tineye 引擎到 searx.engines.engines
     → 测试体执行
     → tearDown() → searx.search.load_engines([]) 【清空 L3 注册表】
```

### 2.4 各测试类生命周期对照表

> "使用路径"列说明：A = 通过 `from searx.engines import xxx`（路径 A，`sys.modules` 持久对象）；B = 通过 `searx.engines.engines['xxx']`（路径 B，`load_module()` 新建副本）

| 测试类 | 使用路径 | 覆盖 `setUp()` | 调用 `super().setUp()` | 覆盖 `TEST_SETTINGS` | 覆盖 `tearDown()` | L1 基础 settings | L2 完整搜索初始化 | L3 引擎注册表状态 | 模块属性残留范围 |
|--------|---------|---------------|----------------------|---------------------|-------------------|----------------|-------------------|----------------|----------------|
| **TestXpathEngine** | **A**（`from searx.engines import xpath`） | ✅ L31 | ✅ L32 | ❌ 否 | ❌ 否 | ✅ 是 | ✅ 每个方法前完整执行 | setUp 后：demo_offline 2 实例（路径 B 对象，不被测试使用） | **高风险（路径 A）**：A 对象在 `sys.modules` 中持久存在；`xpath.search_url`/`paging`/`categories`/`url_xpath` 等赋值类内所有方法间、跨测试类持续残留，至进程终止 |
| **TestJsonEngine** | **A**（`from searx.engines import json_engine`） | ✅ L112 | ❌ 未调用 | ❌ 否 | ❌ 否 | ✅ 是 | ❌ 不执行 | ❌ 不执行（测试仅用路径 A） | **高风险（路径 A）**：A 对象持久存在；所有 `json_engine.*` 属性类内方法间、跨测试类持续残留 |
| **TestCommandEngine** | **A**（`from searx.engines import command`） | ❌ 否 | 继承默认 | ❌ 否 | ❌ 否 | ✅ 是 | ✅ 继承默认 | setUp 后：demo_offline 2 实例（路径 B，不被测试使用） | **高风险（路径 A）**：A 对象持久存在；`command_engine.command`/`delimiter` 等类内方法间、跨测试类持续残留 |
| **TinEyeTests** | **B**（`searx.engines.engines['tineye']`） | ✅ L19 | ✅ L20 | ✅ `"test_tineye.yml"` | ✅ L24 `load_engines([])` | ✅ 是 | ✅ 加载 tineye YAML | setUp 后：`{tineye}`（路径 B 新对象）；tearDown 后：`{}` | **无风险（路径 B）**：每个方法 `self.tineye` 都是 `load_module` 新建的独立副本；前一方法修改属性仅存活至 tearDown；路径 A（若被 import）完全不受影响 |
| **GithubCodeTests** | **B**（`searx.engines.engines['github code']`） | ✅ L18 | ✅ L19 | ✅ `"test_github_code.yml"` | ✅ L23 `load_engines([])` | ✅ 是 | ✅ 加载 github_code YAML | setUp 后：`{"github code": B新对象}`；tearDown 后：`{}` | **无风险（路径 B）**：同 TinEyeTests；注册表对象每次全新；路径 A 不受影响 |
| **TestEnginesInit** | 混合（仅操作路径 B 注册表字典） | ❌ 否 | 继承默认 | ❌ 否 | ❌ 否 | ✅ 是 | ✅ 加载 demo_offline | setUp → 方法体 `load_engines(自定义)` 覆盖 → 下一 setUp 重置 | **中风险（路径 B 字典引用）**：方法内直接替换全局 `engines` 字典，但下一 setUp 总会重置；仅同方法体内生效；路径 A 完全不受影响 |
| **TestOnlineProcessor** | 仅路径 B（只读注册表） | ❌ 否 | 继承默认 | ❌ 否 | ❌ 否 | ✅ 是 | ✅ 继承默认 | setUp 后：demo_offline 2 实例 | **低风险**：从不修改引擎对象属性，仅读取注册表 |

### 2.5 生命周期关键发现（修正版）

1. **所有测试类运行时 `searx.settings` 全局字典均已存在**：此前错误地认为 `TestJsonEngine` 无 settings。实际核对代码：`json_engine.py` 通过 `from searx.utils import to_string` 间接触发 `import searx`，而 `searx/__init__.py` L146 的顶层 `init_settings()` 调用会在模块导入阶段完成 settings 初始化。`TestJsonEngine` 跳过的是 **L2（完整搜索初始化：Flask app、plugins、网络层、引擎注册表）**，不是 L1。

2. **全局属性残留风险严格按路径 A/B 分界（根本性修正）**：
   - **路径 A 类（TestXpathEngine / TestJsonEngine / TestCommandEngine）：高风险**。三者均使用 `from searx.engines import xxx`，操作的是 `sys.modules` 中进程全局唯一的持久对象。这些属性在**同一类的所有方法间、跨所有使用该引擎模块的测试类间**持续残留，直至 Python 进程终止。由于 3 个类各自操作不同的模块（xpath / json_engine / command），互相不污染；但若未来有其他测试类 `from searx.engines import xpath`，会立即继承 TestXpathEngine 留下的所有属性副作用。
   - **路径 B 类（TinEyeTests / GithubCodeTests）：无风险**。二者均从 `searx.engines.engines['xxx']` 获取对象，而每次 `setUp()` 的 `load_engines()` 都会通过 `load_module()` 创建**全新的独立模块副本**，前一方法通过 `self.tineye.foo = ...` 修改的属性对下一个方法完全不可见。`tearDown()` 中的 `load_engines([])` 甚至是多余的——即使不调用，下一个 `setUp()` 的 `load_engines()` 第一行也会执行 `engines.clear()`（`searx/engines/__init__.py` L269）。
   - **全局 `searx.engines.engines` 字典替换（TestEnginesInit）：仅影响路径 B 使用者**。该类在方法体内部调用 `load_engines(自定义列表)` 替换的是路径 B 注册表，对路径 A 对象完全无影响。由于每个方法的 `setUp()` 都会重新 `load_engines(YAML_default)` 重置注册表，方法间不存在字典层面的残留，但**通过方法体中 `load_engine()` 新建的路径 B 对象会作为独立模块一直存在于内存中直到 GC**（无 `sys.modules` 引用但可能被其他引用链持有）。

3. **路径 A 类从未经过 `update_engine_attributes()` 注入（根本性修正）**：
   - 路径 A 的导入模块**从未被 `update_engine_attributes()`（`searx/engines/__init__.py` L179-L194）处理**，因此缺少所有 YAML 配置属性注入（如 `categories` 默认值 `['general']`、`timeout`、`paging`、`safesearch`、`shortcut` 等 `ENGINE_DEFAULT_ARGS` 中的字段）。测试代码中显式赋值的 `xpath.categories = ['general']` 等属性实际上是**手动补全了本该由 L3 完成的注入**——这意味着若引擎 `response()` 代码未来引用任何由 YAML 注入的新属性，路径 A 类的测试会因该属性不存在而崩溃，但实际运行时（走路径 B）属性存在。
   - 路径 B 类无此问题：`load_engine()` L133 显式调用 `update_engine_attributes()`，`ENGINE_DEFAULT_ARGS` 中所有缺省属性在 L192-L194 自动补全。

4. **`SearxTestCase.setattr4test()`（`tests/__init__.py` L46-L55）虽已提供但对路径 A/B 价值不对称**：
   - 对**路径 A 类**（高风险的 3 个类），该方法通过 `addCleanup` 注册恢复原值，是唯一正确的属性管理方式——但 3 个类 0 个使用。
   - 对**路径 B 类**（无风险的 2 个类），该方法无实际价值，因为每次 setUp 都会获取全新对象，无需 cleanup。

5. **`TestEnginesInit` 的双重初始化是设计特性而非缺陷**：它测试的就是 `load_engines()` 本身的行为，因此 setUp 加载的 demo_offline 在方法体中被主动覆盖是预期行为。在路径 B 语义下，方法间不存在注册表层面的状态泄漏——但 `load_engine()` 创建的每一个路径 B 对象都是独立模块，若测试用例数量极多可能有轻微的内存累积影响（实际可忽略）。

---

## 3. 夹具数据的三层来源

### 3.1 YAML 配置夹具

所有 YAML 文件位于 `tests/unit/settings/`。除 `test_result_container.yml` 保留默认引擎外，其余统一模式为先清空默认引擎再按需声明：

```yaml
use_default_settings:
  engines:
    keep_only: []          # 先清除默认引擎
engines:
  - name: ...              # 仅声明测试所需引擎
```

| YAML 文件 | 声明的引擎 | 引用方（通过 `TEST_SETTINGS` 覆盖或默认） |
|-----------|-----------|----------------------------------------|
| `test_settings.yml` | `dummy engine`（demo_offline）、`dummy private engine`（demo_offline + token） | `TestXpathEngine`、`TestCommandEngine`、`TestEnginesInit`、`TestOnlineProcessor`（默认）；**同时被 L1 阶段导入时使用** |
| `test_tineye.yml` | `tineye` | `TinEyeTests` |
| `test_github_code.yml` | `github code` | `GithubCodeTests` |
| `test_result_container.yml` | `google`、`duckduckgo`（保留默认） | 结果容器测试（本文档范围外） |

### 3.2 内联数据夹具（硬编码 HTML/JSON/文本）

| 测试类 | 夹具载体 | 数据形态 | 所在行 |
|--------|---------|---------|--------|
| `TestXpathEngine` | 类属性 `self.html` | 两段 `search_result` 的极简 HTML 骨架 | `test_xpath.py` L16-L29 |
| `TestJsonEngine` | 类属性 `self.json` | 顶层数组 JSON，含异常值（整数 url、非完整 thumb URL、HTML 标签内容） | `test_json_engine.py` L16-L58 |
| `TestJsonEngine` | 类属性 `self.json_result_query` | 嵌套 `data.results` + `data.suggestions` 的 JSON | `test_json_engine.py` L60-L110 |
| `TestCommandEngine` | 局部变量 `searx_logs` | 模拟的 searx 日志输出（12 行冒号分隔记录） | `test_command.py` L28-L40 |
| `TestCommandEngine` | 局部变量 `txt` | 模拟的 git log 输出（3 段 commit 记录） | `test_command.py` L126-L144 |
| `TinEyeTests` | 局部变量 `response.json.return_value` | 手工构造的 API 错误/成功响应字典 | `test_engine_tineye.py` 各测试方法内 |
| `GithubCodeTests` | `@parameterized.expand` 入参 | 4 组 `code_matches` 列表，覆盖多 fragment / 表格 / 纯数字 / 无高亮 | `test_engine_github_code.py` L26-L101 |
| `GithubCodeTests` | 局部变量 `response.json.return_value` | 完整 GitHub Search API v3 返回结构 | `test_engine_github_code.py` L107-L141 |

### 3.3 引擎模块属性（可变状态夹具）

测试代码通过**直接赋值引擎模块属性**来配置解析规则。这些赋值行为本身构成夹具。需严格按路径 A/B 区分残留行为：

| 测试方法 | 对象路径 | 修改的模块属性 | 所在行 | 属性残留范围 |
|----------|---------|---------------|--------|-------------|
| `TestXpathEngine.test_request` | **A**（sys.modules） | `xpath.search_url`、`xpath.categories`、`xpath.paging` | `test_xpath.py` L36-L52 | **进程级**：至 Python 终止，跨所有类/方法 |
| `TestXpathEngine.test_response` | **A**（sys.modules） | `xpath.url_xpath`、`xpath.title_xpath`、`xpath.content_xpath`、`xpath.cached_xpath`、`xpath.categories` | `test_xpath.py` L59-L94 | **进程级**：同上 |
| `TestXpathEngine.test_response_results_xpath` | **A**（sys.modules） | `xpath.results_xpath`、`xpath.url_xpath`、`xpath.title_xpath`、`xpath.content_xpath`、`xpath.cached_xpath`、`xpath.categories` | `test_xpath.py` L98-L136 | **进程级**：同上 |
| `TestJsonEngine.test_request` | **A**（sys.modules） | `json_engine.search_url`、`json_engine.categories`、`json_engine.paging`、`json_engine.request_body` | `test_json_engine.py` L116-L146 | **进程级**：同上 |
| `TestJsonEngine.test_response` | **A**（sys.modules） | `json_engine.results_query`、`json_engine.url_query`、`json_engine.url_prefix`、`json_engine.title_query`、`json_engine.content_query`、`json_engine.thumbnail_query`、`json_engine.thumbnail_prefix`、`json_engine.title_html_to_text`、`json_engine.content_html_to_text`、`json_engine.categories` | `test_json_engine.py` L150-L199 | **进程级**：同上 |
| `TestJsonEngine.test_response_results_json` | **A**（sys.modules） | 同上全部 + `json_engine.suggestion_query` | `test_json_engine.py` L203-L254 | **进程级**：同上 |
| `TestCommandEngine` 各方法 | **A**（sys.modules） | `command_engine.command`、`command_engine.delimiter`、`command_engine.result_separator`、`command_engine.parse_regex`、`command_engine.query_type`、`command_engine.query_enum` | `test_command.py` L13-L218 | **进程级**：同上 |
| `TinEyeTests.setUp` 方法内 | **B**（load_module 新副本） | `self.tineye.logger.setLevel` | `test_engine_tineye.py` L22 | **方法级**：仅当前 `self.tineye` 对象，tearDown 后注册表清空，对象可 GC |
| `GithubCodeTests.setUp` 方法内 | **B**（load_module 新副本） | `self.ghc.logger.setLevel` | `test_engine_github_code.py` L21 | **方法级**：同上 |

**补充说明**：路径 A 类中的属性赋值也**未经过 `update_engine_attributes()`**——例如 `xpath.categories = ['general']` 与实际 YAML 注入的语义不同：YAML 注入时若 `categories` 为字符串会先 `split(',')` 为列表（`searx/engines/__init__.py` L182-L185），而测试直接赋列表绕过了该分支。这些属性由测试代码手动补全，与生产环境中路径 B 的注入结果在语义上等价但到达路径不同。

---

## 4. 测试样例关系表：输入夹具 → 断言目标 → 覆盖的解析路径

> **统计口径说明**：以下按"同一 `def test_` 方法内不同输入配置（不同属性赋值 / 不同 Mock）"拆分为独立条目，共 37 个断言场景。相比之下，代码中实际的 `def test_` 方法数为 27，参数化展开后的独立运行用例数为 33，`self.assert*` 调用总数为 143。

### 4.1 XPath 引擎（`tests/unit/engines/test_xpath.py`）

断言调用总数：42；`def test_` 方法数：3；拆分断言场景数：7

| # | 断言场景 | 输入夹具 | 断言目标 | 覆盖的引擎解析路径 | 与真实抓取的差异 |
|---|---------|---------|---------|-------------------|-----------------|
| 1 | `test_request` URL 拼接 (L35) | `query='test_query'`、`dicto={'language':'all','pageno':1}`；`xpath.search_url`、`xpath.paging` 两组配置 | 返回 `params['url']` 正确拼接 `{query}` 与 `{pageno}` | `xpath.request()` L230-L272 中的 URL 模板替换、paging 计算分支 | 无 HTTP 客户端，仅测 URL 字符串拼接；真实环境还需经过 `OnlineProcessor.get_params()` 注入 User-Agent 等 header |
| 2 | `test_response` — 异常输入 + 空输入 + 正常无 cached (L57) | `mock.Mock(text=self.html, status_code=200)`；`xpath.url_xpath`、`title_xpath`、`content_xpath` | 抛出 `AttributeError`（None/[]/''/'[]' 输入）；空 HTML 返回空列表；正常 HTML 返回 2 条结果，`title`/`url`/`content` 字段值正确 | `xpath.response()` L275-L342：`no_result_for_http_status` 短路（未触发）→ `raise_for_httperror` → `dom = html.fromstring` → `results_xpath` 为假时走 L312-L335 的 `zip()` 分支 → `eval_xpath_list(dom, ...)` 顶层查询 → `extract_url` / `extract_text` | 真实 HTML 含广告、SSR 包裹、多种编码、相对 URL 等噪音；此处极简骨架仅验证 XPath 选择器语义 |
| 3 | `test_response` — 含 cached_xpath (L82) | 同上夹具 + 设置 `xpath.cached_xpath` | 每条结果包含 `cached_url` 字段；`is_onion` 默认 False | `xpath.response()` L313-L328 `cached_xpath` 为真分支 | 真实 `cached_url` 前缀属性 `xpath.cached_url` 未设置（默认为空串），测试未覆盖拼接有前缀的情况 |
| 4 | `test_response` — onion 分类 (L91) | 同上夹具 + `xpath.categories = ['onions']` | `is_onion = True` | `xpath.response()` L288、L325、L335 的 onion 标记分支 | 无真实 `.onion` URL 验证 |
| 5 | `test_response_results_xpath` — 正常无 cached (L96) | 同 L57 夹具，但设置了 `xpath.results_xpath` | 与 #2 相同的 2 条结果验证 | `xpath.response()` L290-L310 的 `results_xpath` 分支：对每个 result 子节点再做相对 XPath 查询 | 真实抓取时 `results_xpath` 选择器可能匹配到非结果节点（如推荐、相关搜索），此处无此类噪音 |
| 6 | `test_response_results_xpath` — cached (L124) | 同上 + `cached_xpath` | `cached_url` 字段正确 | `xpath.response()` L303-L306 | 同 #3 |
| 7 | `test_response_results_xpath` — onion (L133) | 同上 + `categories=['onions']` | `is_onion = True` | `xpath.response()` L307-L308 | 同 #4 |

### 4.2 JSON 引擎（`tests/unit/engines/test_json_engine.py`）

断言调用总数：53；`def test_` 方法数：3；拆分断言场景数：7

| # | 断言场景 | 输入夹具 | 断言目标 | 覆盖的引擎解析路径 | 与真实抓取的差异 |
|---|---------|---------|---------|-------------------|-----------------|
| 1 | `test_request` URL/body 拼接 (L115) | 三组 `search_url`/`paging`/`request_body` 配置 | URL 模板替换正确；POST body 格式正确 | `json_engine.request()` L315-L355 的 URL 替换、`request_body` 格式化分支 | 未经过 `OnlineProcessor` header 注入；未测 `time_range`/`safe_search`/`lang` 模板变量 |
| 2 | `test_response` — 异常输入 + 空输入 + 正常无 results_query (L148) | `mock.Mock(text=self.json, status_code=200)`；配置 `url_query`/`title_query`/`content_query`/`thumbnail_query` | 异常输入抛 `AttributeError`；空 JSON 返回空列表；正常 JSON 返回 3 条结果，含 `title`/`url`/`content`/`thumbnail` | `json_engine.response()` L393-L430：`raise_for_httperror` → `loads(resp.text)` → 无 `results_query` 直接遍历 `json` → `extract_response_info()` L362-L390 的 `query()` JSON 路径查询 → `to_string()` 处理非字符串字段 | 真实 API 响应含分页信息、rate limit header、元数据等大量冗余字段；但此处已刻意加入异常数据（整数 URL）验证鲁棒性 |
| 3 | `test_response` — prefix + thumbnail 数组索引 (L182) | 同上夹具 + 设置 `url_prefix`、`thumbnail_query`（取 images/1）、`thumbnail_prefix` | 第 3 条结果的 URL 经 prefix 拼接为 `https://example.com/url2`；thumbnail 取数组第 2 项并加前缀；`is_onion=False` | `extract_response_info()` L369 `url_prefix + to_string(url)`、L385-L386 `thumbnail_prefix + to_string(...)`；`json_engine.response()` L421-L422 的 onion 分支 | 真实 API 中 suggestion 通常是独立字段，此处未设置 `suggestion_query`，suggestion 路径未覆盖 |
| 4 | `test_response` — onion (L196) | 同上 + `categories=['onions']` | `is_onion = True` | `json_engine.response()` L421-L422 | — |
| 5 | `test_response_results_json` — 含 results_query + HTML 剥离 (L201) | `mock.Mock(text=self.json_result_query, status_code=200)`；配置 `results_query='data/results'`、`title_html_to_text=True`、`content_html_to_text=True` | 3 条结果；HTML 标签被 `html_to_text` 剥离（`<h1>title1</h1>` → `title1`） | `json_engine.response()` L408-L414 的 `results_query` 分支 → `query(json, 'data/results')`；`extract_response_info()` L363-L364 的 `html_to_text` 过滤分支 | 真实响应中 HTML 可能嵌套更深、含转义字符、CDATA 等，此处仅测简单标签 |
| 6 | `test_response_results_json` — prefix + suggestions (L235) | 同上 + 设置 `url_prefix`、`thumbnail_prefix`、`suggestion_query='data/suggestions'` | 共 4 条结果（3 条正常 + 1 条 suggestion）；suggestion 为 `['suggestion0', 'suggestion1']` | `json_engine.response()` L426-L429 的 suggestion 分支 | 真实 suggestion 结构可能更复杂（含 `metadata`、`category`） |
| 7 | `test_response_results_json` — onion (L251) | 同上 + `categories=['onions']` | `is_onion = True` | `json_engine.response()` L421-L422 | — |

### 4.3 Command 引擎（`tests/unit/engines/test_command.py`）

断言调用总数：7；`def test_` 方法数：5；拆分断言场景数：5

| # | 断言场景 | 输入夹具 | 断言目标 | 覆盖的引擎解析路径 | 与真实抓取的差异 |
|---|---------|---------|---------|-------------------|-----------------|
| 1 | `test_basic_seq_command_engine` (L12) | 真实系统命令 `seq 5`；`delimiter={'chars':' ','keys':['number']}` | 5 条 `KeyValue` 结果，值为 `"1"`-`"5"` | `command.search()` L129-L139 → `_get_command_to_run()` L142-L153 → `_get_results_from_process()` L156-L187 → `__parse_single_result()` L224-L234 的 delimiter 分支 | 依赖 POSIX `seq` 命令，Windows 不可用；真实命令执行存在权限、环境变量差异 |
| 2 | `test_delimiter_parsing` (L27) | 硬编码 searx 日志文本（12 行）通过 `echo` 输出；冒号分隔 delimiter；每页 10 条 | 第 1 页 10 条、第 2 页 3 条；每条的 `level`/`component`/`message` 正确 | `__parse_single_result()` L229-L234；`_get_results_from_process()` L175 的分页 `start/end` 过滤（`__get_results_limits()` L190-L193） | 真实日志含中文、特殊字符、多行堆栈，此处仅 ASCII 单行 |
| 3 | `test_regex_parsing` (L125) | 硬编码 git log 文本；`result_separator='\n\ncommit '`；`parse_regex` 4 组正则 | 3 条结果的 `commit`/`author`/`date`/`message` 正则匹配正确 | `__parse_single_result()` L236-L241 的 regex 分支；`_get_results_from_process()` L164 的 `result_separator` 分段 | 真实 git log 含 merge commit、签名、多 author 等复杂格式 |
| 4 | `test_working_dir_path_query` (L182) | `ls .` 与若干非法路径（`..`、`~`、`/var`）；`query_type='path'` | 合法路径返回结果；非法路径抛出 `ValueError` | `_get_command_to_run()` L142-L145 → `__check_query_params()` L196-L208 的 path 类型校验 | Windows 路径分隔符、权限模型差异未覆盖 |
| 5 | `test_enum_queries` (L202) | 允许枚举值与禁止值；`query_type='enum'`、`query_enum=[...]` | 允许值正常执行；禁止值抛 `ValueError` | `__check_query_params()` L205-L208 的 enum 类型校验 | — |

### 4.4 TinEye 引擎（`tests/unit/test_engine_tineye.py`）

断言调用总数：12；`def test_` 方法数：7（含 1 个 2 组参数化）；拆分断言场景数：7

| # | 断言场景 | 输入夹具 | 断言目标 | 覆盖的引擎解析路径 | 与真实抓取的差异 |
|---|---------|---------|---------|-------------------|-----------------|
| 1 | `test_status_code_raises` (L27) | `Mock(status_code=401)` + `raise_for_status.side_effect=HTTPError()` | 抛 `HTTPError` | `tineye.response()` L185-L186 的 `resp.raise_for_status()` 分支（未命中 L157 状态码白名单） | 真实响应含完整 headers、body，此处仅 status_code |
| 2 | `test_returns_empty_list` 参数化 400/422 (L33) | `status_code ∈ {400, 422}`；`json.return_value={"suggestions":{"key":"Download Error"}}` + `raise_for_status.side_effect=HTTPError()` | 返回空列表，length=0；且产生日志输出 | `tineye.response()` L157-L183：命中状态码白名单 → 读取 `suggestions.key` → 匹配 `DOWNLOAD_ERROR` 常量 → `logger.info()` → 返回空 `EngineResults` | 真实错误响应含完整请求 ID、debug 信息等 |
| 3 | `test_logs_format_for_422` (L43) | `suggestions.key = "Invalid image URL"`、`status_code=422` | 日志包含 `FORMAT_NOT_SUPPORTED` 常量 | `tineye.response()` L162-L166 分支 | — |
| 4 | `test_logs_signature_for_422` (L53) | `suggestions.key = "NO_SIGNATURE_ERROR"` | 日志包含 `NO_SIGNATURE_ERROR` 常量 | `tineye.response()` L167-L169 分支 | — |
| 5 | `test_logs_download_for_422` (L63) | `suggestions.key = "Download Error"` | 日志包含 `DOWNLOAD_ERROR` 常量 | `tineye.response()` L170-L172 分支 | — |
| 6 | `test_logs_description_for_400` (L73) | `status_code=400`；`suggestions.description = [description_str]`、`suggestions.title="Oops!"` | 日志包含 description 字符串 | `tineye.response()` L175-L179 的 400 分支 | 真实 400 响应 description 可能是字符串而非列表 |
| 7 | `test_crawl_date_parses` (L84) | `status_code=200`；`json.return_value={'matches':[{'backlinks':[{'crawl_date':'2020-05-25'}]}]}` | `results[0]['publishedDate']` 等于 `datetime(2020,5,25)` | `tineye.response()` L188-L210 → `parse_tineye_match()` L115-L135 的 `crawl_date` `strptime` 解析；取 `backlinks[0]` 作为主 backlink | **未覆盖** `parse_tineye_match()` L138-L148 的 `image_url`/`domain`/`score`/`width`/`height`/`size`/`image_format`/`filesize`/`overlay`/`tags` 共 10 个字段；未覆盖 backlinks 为空时 `continue`（L193-L194）；未覆盖 `crawl_date` 为 None 时回退 `datetime.min`（L125-L126） |

### 4.5 GitHub Code 引擎（`tests/unit/test_engine_github_code.py`）

断言调用总数：3；`def test_` 方法数：2（含 1 个 4 组参数化）；拆分断言场景数：5

| # | 断言场景 | 输入夹具 | 断言目标 | 覆盖的引擎解析路径 | 与真实抓取的差异 |
|---|---------|---------|---------|-------------------|-----------------|
| 1 | `test_code_extraction` 参数化 case 0 (L26) | 2 个 `code_matches` fragment，含 markdown 链接、匹配高亮 indices | 拆分出 7 行代码；高亮行为第 {2, 5} 行 | `github_code.extract_code()` L162-L211：多 fragment 遍历 → `ghc_insert_block_separator`（未启用）→ `ghc_strip_whitespace/new_lines`（默认开启）→ 逐字符匹配高亮 → 换行切分 | 真实 fragment 含未匹配上下文、非 UTF-8 字符、超长行等 |
| 2 | `test_code_extraction` 参数化 case 1 (L57) | 表格行 markdown，匹配关键词 "buffer" | 3 行表格；高亮第 2 行 | 同上 | 真实表格含对齐语法、嵌套代码块 |
| 3 | `test_code_extraction` 参数化 case 2 (L73) | 纯数字行 `1\n2\n3\n4`，匹配 "1" | 4 行；高亮第 1 行 | 同上，验证 offset 计算对纯数字内容的正确性 | — |
| 4 | `test_code_extraction` 参数化 case 3 (L88) | fragment="placeholder"，`matches=[]`（空高亮） | 1 行；高亮集合为空 | 同上，验证无高亮时 `highlighted_lines_index` 保持空 | — |
| 5 | `test_transforms_response` (L107) | 完整 API Mock：含 `items[].name/path/html_url`、`repository.full_name/html_url/description`、`text_matches[].object_type/property/fragment/matches` | 返回单个 `EngineResults.types.Code` 对象，其 `url`、`title`（`repo/path` 格式）、`content`、`repository`、`codelines`（6 行带编号）、`hl_lines={2,5,6}`、`strip_whitespace/new_lines` 全等于期望值 | `github_code.response()` L214-L249：`raise_for_httperror` → `resp.json()['items']` 遍历 → `repository` 子字典提取 → `text_matches` 按 `object_type=="FileContent" && property=="content"` 过滤 → `extract_code()` → `res.types.Code(...)` 构造 | **未覆盖** L217-L220 的 `status_code==422` 返回空结果分支；未覆盖 `ghc_auth` 非 `none` 分支（`request()` L154-L157）；未覆盖 `ghc_highlight_matching_lines=False`（`response()` L232-L233）；未覆盖 `ghc_insert_block_separator=True`；未覆盖 `ghc_strip_whitespace=True` |

### 4.6 引擎注册与 OnlineProcessor

断言调用总数：26；`def test_` 方法数：7；拆分断言场景数：6

| # | 断言场景 | 输入夹具 | 断言目标 | 覆盖的引擎解析路径 | 与真实抓取的差异 |
|---|---------|---------|---------|----------------|-----------------|
| 1 | `test_initialize_engines_default` | 两个 dummy 引擎的字典列表 | 注册表含 2 个引擎 | `engines.load_engines()` L267-L286 | 未测真实网络引擎的 `setup()` 失败路径 |
| 2 | `test_initialize_engines_exclude_onions` | 含 onions 分类引擎 + `using_tor_proxy=False` | onions 引擎被排除，`engines.categories` 无 'onions' 键 | `engines.is_engine_active()` L221-L230 | — |
| 3 | `test_initialize_engines_include_onions` | 同上 + `using_tor_proxy=True` + onion_url + timeout | onions 引擎被保留；`search_url` 变为 onion URL；timeout += extra_proxy_timeout | `engines.update_attributes_for_tor()` L197-L200 | — |
| 4 | `test_missing_name_field` / `test_missing_engine_field` | 缺字段的 engine_data | 加载失败 + ERROR 日志 | `engines.load_engine()` L105-L122 | — |
| 5 | `TestOnlineProcessor.test_get_params_default_params` | `demo_offline` 引擎 + `SearchQuery` | 返回 params 含 `method/headers/data/url/cookies/auth` 全部键 | `OnlineProcessor.get_params()` L132-L164 → `default_request_params()` L94-L110 | 未发起真实 HTTP 请求 |
| 6 | `TestOnlineProcessor.test_get_params_useragent` | 同上 | headers 含 `User-Agent` | `OnlineProcessor.get_params()` L149-L150 `gen_useragent()` | — |

---

## 5. 回归能力三维分类

### 5.1 分类标准定义

从三个独立维度评估每个测试样例的能力：

| 维度 | 名称 | 含义 | 判定方法 |
|------|------|------|---------|
| **A** | 静态夹具结构覆盖 | 夹具数据（HTML/JSON/文本）是否模拟了目标引擎协议中多个字段与层级结构，而不是单字段。衡量夹具本身的丰富度。 | 统计夹具模拟的字段数量与嵌套层级数。 |
| **B** | 解析逻辑验证 | 测试是否覆盖引擎代码中实际的解析分支与数据变换逻辑（如 `html_to_text`、`strptime`、正则匹配、XPath 相对查询、分页、状态码分支等）。 | 对照引擎 `response()`/`search()` 源码，检查断言是否触达具体分支而非仅返回空/非空。 |
| **C** | 真实页面/API 改版发现能力 | 当真实搜索引擎的 HTML 结构或 API schema 发生变化时，该样例能否在 CI 中失败。这是最关键的质量指标。 | **核心判断**：夹具数据是否来源于真实抓取。若夹具为人工构造（即使结构复杂），则真实改版时夹具未同步更新，样例仍通过，**C=无**。仅当夹具来自录制回放（L2 及以上）时 C=有。 |

> 重要说明：本项目所有夹具均为**人工构造的骨架数据**，无任何样例使用录制回放（VCR.py 等）。因此从严格意义上讲，**C 维度全部为"无"**。但以下分类进一步区分"如果夹具数据恰好与真实结构同步更新，能否发现变更"（即 C'——改版发现的潜力）。

### 5.2 样例三维分类表

> 分类对象为 §4 的 37 个拆分断言场景。

| 断言场景 | A 静态夹具结构覆盖 | B 解析逻辑验证 | C 真实改版发现（严格） | C' 改版发现潜力（夹具已同步） |
|---------|-------------------|---------------|----------------------|---------------------------|
| **XPath（7 场景）** | | | | |
| `test_request` URL 拼接 | 低（仅字符串模板） | 中（覆盖 paging 分支） | 无 | 无 |
| `test_response` 无 results_xpath + 字段断言 | 中（2 条结果，各含 3-4 字段） | 中（覆盖顶层 XPath 查询分支、`zip()` 分支、异常输入） | 无 | 弱（仅断言字段值；若真实页面在结果外包新容器但 class 名不变，样例不失败） |
| `test_response` cached_xpath / onion | 中（同上） | 低（仅附加字段检查） | 无 | 弱（仅验证附加字段存在） |
| `test_response_results_xpath` 含 results_xpath | 中（同上） | 高（覆盖 results_xpath 相对查询分支） | 无 | **强**（若真实页面 class `search_result` 改名，`eval_xpath_list` 返回空，结果条数断言隐含失败） |
| **JSON（7 场景）** | | | | |
| `test_request` URL/body 拼接 | 低（仅模板字符串） | 中（覆盖 POST body 分支） | 无 | 无 |
| `test_response` 无 results_query | 高（3 条结果，含异常值：整数 URL、HTML 标签、非完整 URL） | 高（覆盖 `to_string()` 非字符串处理、prefix 拼接、thumbnail 数组索引） | 无 | 弱（顶层数组遍历；若真实 API 改嵌套但夹具仍用数组，样例通过） |
| `test_response` prefix/onion | 中 | 低-中（仅 prefix 拼接 / onion 标记） | 无 | 弱 / 无 |
| `test_response_results_json` 含 results_query + HTML 剥离 | 高（嵌套 data.results/data.suggestions，含 HTML 内容） | 高（覆盖 `results_query` 路径查询分支、`html_to_text` 剥离分支） | 无 | **强**（若真实 API `data.results` 改名为 `data.items`，`query()` 返回空，结果数量为 0，断言失败） |
| `test_response_results_json` + suggestions | 高（同上 + suggestions 数组） | 高（覆盖 suggestion_query 分支） | 无 | **强**（suggestion 结构路径变化直接影响断言） |
| **Command（5 场景）** | | | | |
| `test_basic_seq_command_engine` | 低（仅数字序列） | 中（覆盖 delimiter 分支 + 子进程执行） | 无 | 弱（仅断言单字段值） |
| `test_delimiter_parsing` | 高（12 行真实格式日志、3 字段、跨 2 页分页） | 高（覆盖 delimiter 分支 + 分页过滤 + 每页 10 条边界） | 无 | **强**（若输出格式改为制表符分隔，解析失败，断言失败） |
| `test_regex_parsing` | 高（3 段 commit，4 组正则匹配字段） | 高（覆盖 `result_separator` 分段 + `parse_regex` 分支） | 无 | **强**（git log 格式变化，正则不匹配，`__parse_single_result` 返回 `{}`，结果数量减少） |
| `test_working_dir_path_query` / `test_enum_queries` | 低（仅路径字符串） | 中（覆盖 `__check_query_params` 两个分支） | 无 | 无 |
| **TinEye（7 场景）** | | | | |
| `test_status_code_raises` | 低（仅 status_code） | 中（覆盖 `raise_for_status` 异常分支） | 无 | 无 |
| `test_returns_empty_list`（参数化 400/422） | 中（含 `suggestions.key` 嵌套结构） | 高（覆盖 400/422 状态码分支、日志） | 无 | 弱（仅断言空列表 + 日志存在） |
| `test_logs_format_for_422` / signature / download | 中 | 中（覆盖 3 个 suggestion.key 分支） | 无 | 弱（若 API 改错误 key 名，样例走到 else 分支但未断言 else 行为） |
| `test_logs_description_for_400` | 中 | 中（覆盖 400 description 列表分支） | 无 | 弱（description 从列表改字符串时样例仍通过） |
| `test_crawl_date_parses` | **极低**（仅 `matches[0].backlinks[0].crawl_date` 一个字段） | 中（覆盖 `strptime` 解析） | 无 | **极弱**（仅断言单字段；其余 10+ 字段完全缺失，即使 API 删除 `image_url`/`score` 等字段样例仍通过） |
| **GitHub Code（5 场景）** | | | | |
| `test_code_extraction` 参数化 case 0/1/2/3 | 高（多 fragment、表格、数字、空高亮；含 `fragment`/`matches[].indices` 嵌套结构） | 高（覆盖 `extract_code` 逐字符高亮计算、换行切分、多 fragment 拼接） | 无 | **强**（`matches[].indices` 语义变化或 fragment 格式变化直接影响行拆分与高亮计算，断言精确到每行内容和行号） |
| `test_transforms_response` | **极高**（完整 API 结构：items → repository/text_matches → fragment/matches 四层嵌套，10+ 字段） | 高（覆盖 `response()` 全链路：items 遍历、repository 提取、text_matches 类型过滤、`extract_code` 调用、`Code` 对象构造） | 无 | **强**（`assertEqual(results, expected_results)` 对 Code 对象全量比较；任何字段缺失或结构变化都会导致对象不相等） |
| **OnlineProcessor / engines_init（6 场景）** | | | | |
| 全部场景 | 低-中 | 中（覆盖加载/注册表/tor/缺字段/header 注入等分支） | 无 | 无 |

### 5.3 汇总统计（校准口径）

| 指标 | 数量 | 统计口径 | 说明 |
|------|------|---------|------|
| **`def test_` 方法数** | 27 | 代码中实际 `def test_*` 函数定义 | xpath:3, json:3, command:5, tineye:7, github:2, engines_init:5, online:2 |
| **参数化展开后独立用例** | 33 | 27 方法中，`test_returns_empty_list` 展开 ×2、`test_code_extraction` 展开 ×4，合计 27 - 2 + 2 + 4 = 33 | unittest/pytest 报告中显示的独立测试数量 |
| **`self.assert*` 调用总数** | 143 | grep `self\.assert|assertEqual|assertRaises|assertIs|assertIn` | xpath:42, json:53, command:7, tineye:12, github:3, engines_init:18, online:8 |
| **本文档拆分断言场景** | 37 | 同一方法内不同输入配置（不同属性赋值 / 不同 Mock）独立计为一个场景 | 与 §4 关系表行数一致 |
| **A 静态夹具结构覆盖 = 高** | 14 / 37（38%） | 按拆分断言场景统计 | JSON results_query、Command 解析、GitHub Code 全部 5 个等 |
| **A 静态夹具结构覆盖 = 中** | 15 / 37（41%） | 同上 | XPath 响应测试、TinEye 日志/错误码测试等 |
| **A 静态夹具结构覆盖 = 低** | 8 / 37（21%） | 同上 | URL 拼接、权限校验、单一 status_code 等 |
| **B 解析逻辑验证 = 高** | 13 / 37（35%） | 同上 | XPath results_xpath、JSON HTML 剥离/路径查询、Command 正则/分页、GitHub Code 全部 5 个等 |
| **B 解析逻辑验证 = 中** | 18 / 37（49%） | 同上 | 大部分样例覆盖若干分支 |
| **B 解析逻辑验证 = 低** | 6 / 37（16%） | 同上 | 仅 onion 标记、单字段检查等 |
| **C 真实改版发现（严格）** | **0 / 37（0%）** | 同上 | 无任何夹具来源于真实抓取，全部为人工构造 |
| **C' 改版发现潜力 = 强** | 9 / 37（24%） | 同上 | XPath results_xpath、JSON results_query、Command 2 个、GitHub Code 全部 5 个 |
| **C' 改版发现潜力 = 弱/无** | 28 / 37（76%） | 同上 | 其余所有样例 |

### 5.4 关键发现

1. **严格的 C 维度全部为零**：这是最根本的问题。所有夹具（包括看起来结构最完整的 GitHub Code Mock）均由测试作者手工构造，不是从真实 API 响应录制而来。真实搜索引擎改版时，只要开发者未同步更新夹具，测试就不会失败——夹具成为了"与自身保持一致"的自证循环。

2. **TinEye 是最弱的一环**：其唯一的成功响应测试 `test_crawl_date_parses` 仅验证了 `publishedDate` 一个字段（A=极低）。`parse_tineye_match()` 返回的其余 10 个字段（`image_url`/`domain`/`score`/`width`/`height`/`size`/`image_format`/`filesize`/`overlay`/`tags`）以及 `response()` 中 `template`/`url`/`thumbnail_src`/`source`/`title`/`img_src`/`format`/`width`/`height` 共 9 个字段的拼接逻辑**完全未被测试覆盖**。

3. **XPath 引擎的 `results_xpath` 分支与非 `results_xpath` 分支在改版发现潜力上有本质区别**：前者模拟了"结果容器 → 字段"的两层结构，如果真实页面的容器 class 名变化且夹具同步更新了 class 名，XPath 查询返回空，结果条数会隐含失败；后者仅从顶层文档直接查询各字段独立 XPath，即使页面结构重组（只要字段所在节点 class 不变）也可能通过。

4. **GitHub Code 是 B 维度和 C' 维度最强的样例**：`test_transforms_response` 使用 `assertEqual(results, expected_results)` 对完整的 `Code` 对象进行深层比较——这意味着不仅字段值要相等，对象类型、所有属性、嵌套的 `codelines` 列表与 `hl_lines` 集合都必须完全匹配，任何缺失或结构性变化都会导致失败。

5. **断言调用密度差异极大**：JSON 引擎 53 个断言覆盖 7 个场景（平均 7.6/场景），Command 引擎仅 7 个断言覆盖 5 个场景（平均 1.4/场景），GitHub Code 仅 3 个断言覆盖 5 个场景（平均 0.6/场景）——后者依赖全对象比较的 `assertEqual`，单条断言的信息密度远高于逐字段断言。

---

## 6. 未覆盖的解析路径清单

以下引擎代码路径在现有夹具下**完全不被测试触及**：

### `searx/engines/xpath.py`
- `response()` L279-L280：`no_result_for_http_status` 命中时返回空列表的分支
- `response()` L284-L285：`resp.text` 为空时返回空列表的分支
- `response()` L298-L301：`thumbnail_xpath` 分支（含 results_xpath 时）
- `response()` L337-L339：`suggestion_xpath` 分支
- `request()` L232-L244：`lang`、`time_range`、`safe_search` 非默认值分支

### `searx/engines/json_engine.py`
- `response()` L397-L398：`no_result_for_http_status` 分支
- `response()` L402-L403：`resp.text` 为空分支
- `response()` L410-L411：`results_query` 命中但返回空的分支
- `extract_response_info()` L377-L381：`content_query` 异常回退空串分支
- `extract_response_info()` L383-L388：`thumbnail_query` 异常吞掉分支
- `request()` L321-L338：`time_range`、`safe_search`、`lang` 分支

### `searx/engines/command.py`
- `_get_results_from_process()` L184-L186：命令非零返回码抛 `RuntimeError` 分支
- `_get_results_from_process()` L171-L173：单条解析失败跳过（debug log）分支
- `search()` L131-L133：`cmd` 为空时返回空结果的分支

### `searx/engines/tineye.py`
- `parse_tineye_match()` L118-L120：`backlink_json` 非 dict 时 `continue` 分支
- `parse_tineye_match()` L123-L126：`crawl_date` 为 None 时回退到 `datetime.min` 的分支
- `parse_tineye_match()` L138-L148：`image_url`、`domain`、`score`、`width`、`height`、`size`、`image_format`、`filesize`、`overlay`、`tags` 共 10 个字段的提取逻辑
- `response()` L173-L174：未知 `suggestions.key` 的 `logger.warning` 分支
- `response()` L193-L194：`backlinks` 为空时 `continue` 跳过分支
- `response()` L197-L210：成功结果中除 `publishedDate` 外的 `template`、`url`、`thumbnail_src`、`source`、`title`、`img_src`、`format`、`width`、`height` 共 9 个字段的断言
- `request()` 全函数：无任何测试覆盖

### `searx/engines/github_code.py`
- `response()` L217-L220：`status_code == 422` 返回空结果分支
- `response()` L232-L233：`ghc_highlight_matching_lines=False` 清空高亮分支
- `extract_code()` L173-L174：`ghc_insert_block_separator=True` 插入 `"..."` 分隔符分支
- `extract_code()` L181-L191：`ghc_strip_whitespace=True` 的左右 strip 分支
- `request()` L154-L157：`ghc_auth.type ∈ {personal_access_token, bearer}` 的 header 注入分支

---

## 7. 改进建议

1. **引入录制回放机制（解决 C = 0 的根本问题）**：使用 `VCR.py` 或 `responses` 库，将真实 HTTP 交互录制为 YAML/JSON 夹具文件。对于 XPath/JSON 这类通用引擎，录制一个真实的基于 xpath/json_engine 配置的搜索引擎响应；对于 TinEye/GitHub Code 这类专用引擎，录制一次成功 API 响应的完整 JSON。这是唯一能让测试在真实改版时自动失败的方法。

2. **为 TinEye 补全结构敏感样例**：当前 `test_crawl_date_parses` 仅覆盖 1 个字段。应将夹具补全为包含 `parse_tineye_match()` 全部 10 个字段（`image_url`/`domain`/`score`/`width`/`height`/`size`/`image_format`/`filesize`/`overlay`/`tags`）以及 `response()` 拼接的 9 个结果字段，并使用类似 `GithubCodeTests.test_transforms_response` 的"完整对象比较"断言方式。

3. **路径 A 类优先迁移至路径 B**：TestXpathEngine、TestJsonEngine、TestCommandEngine 三个高风险类应效仿 TinEyeTests/GithubCodeTests 的模式，改为：① 为每个引擎创建独立的 YAML 夹具（`test_xpath.yml` 等）；② `setUp()` 中从 `searx.engines.engines['engine_name']` 获取对象（路径 B，每次 `load_module()` 新建副本）；③ 使用 `TEST_SETTINGS` 声明引擎实例。这样自动解决属性残留问题和 `update_engine_attributes()` 注入缺失问题。

4. **迁移至路径 B 前，先在路径 A 类使用 `setattr4test()`**：路径 A 类的属性残留风险最高，立即将所有 `xpath.foo = ...`、`json_engine.foo = ...`、`command_engine.foo = ...` 赋值替换为 `self.setattr4test(xpath, 'foo', ...)`。`TestJsonEngine` 也应补调用 `super().setUp()`，避免 Flask app/网络层缺失导致未来代码引入依赖时静默出错。

5. **路径 B 类移除多余的 `tearDown()`，在 setUp 前先 `load_engines([])` 防御性清理**：当前 TinEyeTests/GithubCodeTests 的 `tearDown` 调用 `load_engines([])` 是多余的（下一 setUp 首行就是 `engines.clear()`），但建议在 setUp 中 `super().setUp()` 前额外加一行 `searx.search.load_engines([])` 作为防御性清理——避免 `TestEnginesInit` 等测试类在前一方法替换了全局 `engines` 字典而该类 setUp 依赖注册表为空时出现问题。

6. **为 XPath 增补显式断言覆盖 `results_xpath` 结构**：在 `test_response_results_xpath` 中增加 `self.assertEqual(len(results), 2)` 的显式断言（目前仅间接地通过 `results[0]`/`results[1]` 索引访问隐含了这一点），并增加"当 HTML 中结果容器 class 名错误时返回空列表"的负面用例。

7. **为每个引擎补充 `no_result_for_http_status` 夹具**：这是通用引擎抵御特定 HTTP 错误码的重要功能，当前 XPath、JSON、Command 的测试均未覆盖该分支。

8. **为 `command` 引擎补全 `mock.patch('subprocess.Popen')` 版本**：`test_basic_seq_command_engine` 和 `test_working_dir_path_query` 依赖 `seq`、`ls` 等 POSIX 命令，在 Windows 与最小化容器中会失败。应将真实系统命令执行作为可选的集成测试，默认走 Mock 版本。

9. **将 GitHub Code 的参数化 + 全对象比较模式推广**：`@parameterized.expand` 的"输入-期望对集中管理"模式与完整对象深层比较的断言方式（`assertEqual(results, expected_results)`）应应用到 XPath、JSON、TinEye 测试中，替代当前散落的 `test_*` 方法和单字段断言。
