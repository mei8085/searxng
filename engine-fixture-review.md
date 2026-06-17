# SearXNG 搜索引擎适配层——测试夹具与回归样例审查

## 1. 全局架构概览

SearXNG 的搜索引擎适配层由以下核心组件构成：

| 层级 | 模块 | 职责 |
|------|------|------|
| 引擎注册与加载 | [engines/\_\_init\_\_.py](file:///d:/fz/0601-2/solo-dogfeeding/code/25-searxng/searx/engines/__init__.py) | `load_engines()` / `load_engine()`：读取 YAML 配置，加载 `.py` 模块，合并 `ENGINE_DEFAULT_ARGS`，注册到全局字典 `engines` |
| 通用引擎模板 | [engines/xpath.py](file:///d:/fz/0601-2/solo-dogfeeding/code/25-searxng/searx/engines/xpath.py)、[engines/json_engine.py](file:///d:/fz/0601-2/solo-dogfeeding/code/25-searxng/searx/engines/json_engine.py)、[engines/command.py](file:///d:/fz/0601-2/solo-dogfeeding/code/25-searxng/searx/engines/command.py) | 可配置式通用引擎，通过 XPath / JSON 路径 / shell 命令定义抓取逻辑 |
| 具体引擎实现 | `engines/tineye.py`、`engines/github_code.py` 等 ~180 个文件 | 各搜索引擎独立的 `request()` / `response()` 实现 |
| 搜索处理器 | [search/processors/online.py](file:///d:/fz/0601-2/solo-dogfeeding/code/25-searxng/searx/search/processors/online.py) | `OnlineProcessor`：组装 HTTP 参数、发起真实请求、调用引擎 `response()` 解析 |
| 测试基类 | [tests/\_\_init\_\_.py](file:///d:/fz/0601-2/solo-dogfeeding/code/25-searxng/tests/__init__.py) | `SearxTestCase`：初始化全局 `settings`、加载引擎、创建 Flask 测试客户端 |

---

## 2. 夹具数据的来源与组织

### 2.1 测试配置文件（YAML 夹具）

所有引擎测试配置均位于 [tests/unit/settings/](file:///d:/fz/0601-2/solo-dogfeeding/code/25-searxng/tests/unit/settings/) 目录，按需被不同测试类引用：

| 文件 | 用途 | 引用方 |
|------|------|--------|
| [test_settings.yml](file:///d:/fz/0601-2/solo-dogfeeding/code/25-searxng/tests/unit/settings/test_settings.yml) | 默认测试配置：仅加载 `demo_offline` 引擎（含一个带 token 的私有引擎） | `SearxTestCase` 基类默认、`TestOnlineProcessor` |
| [test_tineye.yml](file:///d:/fz/0601-2/solo-dogfeeding/code/25-searxng/tests/unit/settings/test_tineye.yml) | 仅加载 `tineye` 引擎 | `TinEyeTests` |
| [test_github_code.yml](file:///d:/fz/0601-2/solo-dogfeeding/code/25-searxng/tests/unit/settings/test_github_code.yml) | 仅加载 `github code` 引擎 | `GithubCodeTests` |
| [test_result_container.yml](file:///d:/fz/0601-2/solo-dogfeeding/code/25-searxng/tests/unit/settings/test_result_container.yml) | 仅保留 `google` 和 `duckduckgo` | 结果容器测试 |
| 其余如 `user_settings_*.yml`、`empty_settings.yml`、`syntaxerror_settings.yml` | 设置加载器自身的边界测试 | `TestSettingsLoader` |

**加载机制**：`SearxTestCase` 基类通过类属性 `TEST_SETTINGS` 指定 YAML 文件名，在 [init_test_settings()](file:///d:/fz/0601-2/solo-dogfeeding/code/25-searxng/tests/__init__.py#L57-L91) 中将 `SEARXNG_SETTINGS_PATH` 环境变量指向该文件，再调用 `searx.init_settings()` → `searx.search.initialize()` 完成全链路初始化。

**关键设计**：每个 YAML 文件都使用 `use_default_settings: engines: keep_only: []` 先清空默认引擎列表，再显式声明测试所需的引擎，确保测试之间互不干扰。

### 2.2 内联夹具数据（硬编码 Mock 数据）

除 YAML 配置外，大量测试夹具以类属性或局部变量形式硬编码在测试代码中：

#### (a) xpath 引擎测试 —— HTML 字符串

位于 [TestXpathEngine.html](file:///d:/fz/0601-2/solo-dogfeeding/code/25-searxng/tests/unit/engines/test_xpath.py#L16-L29)：

```python
html = """
<div>
    <div class="search_result">
        <a class="result" href="https://result1.com">Result 1</a>
        <p class="content">Content 1</p>
        <a class="cached" href="https://cachedresult1.com">Cache</a>
    </div>
    <div class="search_result">
        <a class="result" href="https://result2.com">Result 2</a>
        <p class="content">Content 2</p>
        <a class="cached" href="https://cachedresult2.com">Cache</a>
    </div>
</div>
"""
```

特点：手工构造的极简 HTML，仅包含与 XPath 选择器匹配的骨架结构，无真实页面的 CSS/JS/广告等噪音。

#### (b) json_engine 测试 —— JSON 字符串

位于 [TestJsonEngine](file:///d:/fz/0601-2/solo-dogfeeding/code/25-searxng/tests/unit/engines/test_json_engine.py#L16-L110)，包含两个变体：
- `self.json`：顶层数组格式（不带 `results_query`）
- `self.json_result_query`：嵌套 `data.results` 格式（带 `results_query`）

刻意包含异常数据：`url` 字段为整数 `2`、`thumb` 为非完整 URL、HTML 标签内容，用于验证 `to_string()` 和 `html_to_text` 的健壮性。

#### (c) command 引擎测试 —— 真实 shell 命令输出

位于 [TestCommandEngine](file:///d:/fz/0601-2/solo-dogfeeding/code/25-searxng/tests/unit/engines/test_command.py)：
- `test_basic_seq_command_engine`：调用系统 `seq` 命令生成序列
- `test_delimiter_parsing`：硬编码的 searx 日志文本作为 `echo` 输入
- `test_regex_parsing`：硬编码的 git log 输出格式
- `test_working_dir_path_query`：调用系统 `ls` 命令

这是唯一**实际执行外部命令**的夹具，其他测试均通过 `mock.Mock` 隔离。

#### (d) tineye 测试 —— `unittest.mock.Mock` 构造的响应

位于 [TinEyeTests](file:///d:/fz/0601-2/solo-dogfeeding/code/25-searxng/tests/unit/test_engine_tineye.py)：

```python
response = Mock()
response.json.return_value = {"suggestions": {"key": "Download Error"}}
response.status_code = 422
response.raise_for_status.side_effect = HTTPError()
```

或：

```python
response.json.return_value = {
    'matches': [{'backlinks': [{'crawl_date': '2020-05-25'}]}]
}
response.status_code = 200
```

测试数据是精简到极限的 JSON 骨架，只保留引擎 `response()` 方法实际读取的字段。

#### (e) github_code 测试 —— 精心构造的 API 响应

位于 [GithubCodeTests](file:///d:/fz/0601-2/solo-dogfeeding/code/25-searxng/tests/unit/test_engine_github_code.py)：

- `test_code_extraction`：使用 `@parameterized.expand` 传入多组 `code_matches` 列表，覆盖多 fragment 拼接、表格内容、纯数字行、无匹配高亮等场景
- `test_transforms_response`：构造了一个完整的 GitHub API 返回结构，包括 `items[].text_matches[].fragment` 和 `matches[].indices`，验证端到端解析

### 2.3 引擎模块状态作为夹具

一个容易忽略的夹具来源：**引擎模块的全局属性**。

`SearxTestCase.init_test_settings()` 调用 `searx.search.initialize()` 时，会执行 `load_engines()`，该函数通过 `load_module()` 加载引擎 `.py` 文件为模块对象，再通过 `update_engine_attributes()` 将 YAML 中的属性写入模块命名空间。

测试代码随后**直接修改模块属性**来配置测试条件：

```python
# test_xpath.py
xpath.search_url = 'https://url.com/{query}'
xpath.paging = False
xpath.url_xpath = '//div[@class="search_result"]//a[@class="result"]/@href'
```

```python
# test_json_engine.py
json_engine.results_query = 'data/results'
json_engine.url_prefix = 'https://example.com/url'
json_engine.title_html_to_text = True
```

这种模式意味着**引擎模块本身就是夹具的可变状态**，测试之间通过 `setUp` 和直接赋值来"重置"。

---

## 3. 夹具调用方式与测试代码关联

### 3.1 两类测试模式

项目中的引擎测试可分为两种截然不同的模式：

#### 模式 A：直接调用引擎函数（无引擎注册）

代表：`TestXpathEngine`、`TestJsonEngine`、`TestCommandEngine`

流程：
1. 直接 `from searx.engines import xpath` 导入引擎模块
2. 在测试方法中设置模块属性（`xpath.search_url = ...`）
3. 调用 `xpath.request(query, dicto)` 或 `xpath.response(mock_response)`
4. 断言返回值

不经过 `SearxTestCase.init_test_settings()` 的引擎注册流程，不创建 Flask 应用。引擎模块的 `logger` 在 `setUp` 中手动注入。

#### 模式 B：通过引擎注册表调用

代表：`TinEyeTests`、`GithubCodeTests`、`TestOnlineProcessor`

流程：
1. 类属性 `TEST_SETTINGS = "test_xxx.yml"` 指定配置文件
2. `setUp` 中 `super().setUp()` → `init_test_settings()` → `load_engines()` → 引擎注册到全局 `searx.engines.engines`
3. `self.tineye = searx.engines.engines['tineye']` 获取已初始化引擎实例
4. 构造 `Mock` 响应，调用 `self.tineye.response(response)`
5. `tearDown` 中 `searx.search.load_engines([])` 清空注册表

此模式走完了完整的引擎初始化链路（含 `ENGINE_DEFAULT_ARGS` 合并、`EngineTraitsMap` 注入、`setup()` 调用），更接近生产环境行为。

### 3.2 Mock 对象构造方式

| 测试 | Mock 构造 | 模拟的接口 |
|------|-----------|------------|
| xpath | `mock.Mock(text=self.html, status_code=200)` | `resp.text`、`resp.status_code` |
| json_engine | `mock.Mock(text=self.json, status_code=200)` | `resp.text`、`resp.status_code` |
| tineye | `Mock(); response.json.return_value = {...}; response.status_code = N` | `resp.json()`、`resp.status_code`、`resp.raise_for_status()` |
| github_code | `Mock(); response.json.return_value = {...}; response.status_code = 200` | `resp.json()`、`resp.status_code` |

注意：xpath 和 json_engine 使用旧式 `mock.Mock(text=..., status_code=...)`，而 tineye 和 github_code 使用新式 `unittest.mock.Mock`。前者模拟 `resp.text` 属性，后者模拟 `resp.json()` 方法——这反映了引擎实现中 `response()` 函数的接口差异：

- xpath 引擎：`html.fromstring(resp.text)` → 读 `resp.text`
- json_engine 引擎：`loads(resp.text)` → 读 `resp.text`
- tineye 引擎：`resp.json()` → 调用 `resp.json()` 方法
- github_code 引擎：`resp.json()` → 调用 `resp.json()` 方法

### 3.3 参数化测试

[GithubCodeTests.test_code_extraction](file:///d:/fz/0601-2/solo-dogfeeding/code/25-searxng/tests/unit/test_engine_github_code.py#L26-L105) 使用 `@parameterized.expand` 一次性定义 4 组输入-期望对，是最结构化的回归样例组织方式。

[TinEyeTests](file:///d:/fz/0601-2/solo-dogfeeding/code/25-searxng/tests/unit/test_engine_tineye.py#L33-L34) 中的 `@parameterized.expand([(400), (422)])` 则用于对多个 HTTP 状态码运行同一断言逻辑。

其他测试均采用独立的 `test_*` 方法，每组夹具一个方法。

---

## 4. 夹具对真实抓取行为的近似程度评估

### 4.1 近似程度分级

| 级别 | 描述 | 本项目对应 |
|------|------|-----------|
| **L0 — 纯函数测试** | 仅测试数据变换逻辑，无 HTTP 语义 | xpath `request()` URL 拼接测试、json_engine `request()` 测试 |
| **L1 — 骨架响应** | 手工构造极简 Mock，仅包含引擎读取的字段 | xpath HTML、json_engine JSON、tineye Mock、github_code Mock |
| **L2 — 录制回放** | 从真实 HTTP 交互录制响应，测试时回放 | **项目中不存在** |
| **L3 — 集成测试** | 对真实服务发起请求（需 API key/网络） | **项目中不存在于单元测试**（仅 robot 测试对本地服务器发起请求） |
| **L4 — 契约测试** | 验证 API schema / 响应结构稳定性 | **项目中不存在** |

### 4.2 当前夹具的局限性

#### (a) 响应结构过度简化

以 tineye 测试为例，真实 API 返回的 `matches` 数组中每个元素包含 `image_url`、`domain`、`score`、`width`、`height`、`backlinks`（含 `url`、`backlink`、`crawl_date`、`image_name`）等十余字段，但测试仅构造了：

```python
{'matches': [{'backlinks': [{'crawl_date': '2020-05-25'}]}]}
```

这导致 `parse_tineye_match()` 中对 `image_url`、`domain`、`score` 等字段的处理逻辑**完全未被测试覆盖**。

#### (b) 缺少边界与异常场景

- 无 HTTP 重定向场景的夹具（`soft_max_redirects`、`max_redirects`）
- 无 SSL 错误、超时、Captcha 等 `OnlineProcessor.search()` 捕获的异常场景
- 无 `resp.content`（字节型响应）的夹具——base.py 等使用 `etree.XML(resp.content)` 的引擎未被测试
- 无分页边界（`pageno=0`、`max_page` 限制）测试

#### (c) 引擎模块状态污染

测试通过直接修改引擎模块的全局属性来配置夹具（如 `xpath.search_url = ...`），但**缺少系统性的属性重置机制**。`TestXpathEngine.setUp()` 仅重置了 `logger`，其他属性（`paging`、`categories`、`cached_xpath` 等）的清理依赖于测试方法执行顺序的隐式假设。虽然 `SearxTestCase` 提供了 `setattr4test()` 辅助方法用于带清理的属性设置，但实际未被任何引擎测试使用。

#### (d) 无真实 HTML/JSON 响应的回归保护

当搜索引擎前端改版导致 HTML 结构变化时，现有的手工构造夹具无法发现断裂。缺乏 L2 级别的录制回放机制意味着：

- xpath 引擎的 XPath 选择器在生产环境失效时，单元测试仍会通过
- json_engine 的查询路径在新版 API 变更字段名时，单元测试无法预警

#### (e) command 引擎的真实执行风险

`TestCommandEngine` 是唯一执行真实系统命令的测试。`test_basic_seq_command_engine` 调用 `seq`、`test_working_dir_path_query` 调用 `ls`——这些在 Windows/最小化容器环境中可能不存在，导致测试可移植性问题。

### 4.3 夹具与生产环境的关键差异

| 维度 | 生产环境 | 测试夹具 |
|------|----------|----------|
| HTTP 客户端 | `httpx` via `searx.network`（含连接池、重试、超时） | `mock.Mock` / `unittest.mock.Mock` |
| 请求参数组装 | `OnlineProcessor.get_params()` → 引擎 `request()` → 完整 `OnlineParams` | 直接构造 `defaultdict(dict)` 或不经过处理器 |
| 响应对象 | `httpx.Response`（含 `history`、`headers`、`content`、`reason_phrase`） | `Mock`（仅设 `text`/`json`/`status_code`） |
| 引擎初始化 | `load_engine()` → `ENGINE_DEFAULT_ARGS` + YAML + `EngineTraitsMap` + `setup()` | 模式 A：跳过初始化；模式 B：完整初始化 |
| 并发与线程 | 多线程 `search_multiple_requests()` | 单线程同步调用 |
| 错误处理 | `OnlineProcessor.search()` 捕获 6 类异常并 suspend | 仅测试引擎自身的 `raise_for_httperror` 行为 |

---

## 5. 测试文件与夹具清单

| 测试文件 | 夹具类型 | 引用的 YAML 配置 | 覆盖的引擎 |
|----------|----------|-------------------|-----------|
| [test_xpath.py](file:///d:/fz/0601-2/solo-dogfeeding/code/25-searxng/tests/unit/engines/test_xpath.py) | 内联 HTML + `mock.Mock` | 默认 | xpath |
| [test_json_engine.py](file:///d:/fz/0601-2/solo-dogfeeding/code/25-searxng/tests/unit/engines/test_json_engine.py) | 内联 JSON + `mock.Mock` | 默认 | json_engine |
| [test_command.py](file:///d:/fz/0601-2/solo-dogfeeding/code/25-searxng/tests/unit/engines/test_command.py) | 真实 shell 命令 + 内联文本 | 默认 | command |
| [test_engine_tineye.py](file:///d:/fz/0601-2/solo-dogfeeding/code/25-searxng/tests/unit/test_engine_tineye.py) | `unittest.mock.Mock` | [test_tineye.yml](file:///d:/fz/0601-2/solo-dogfeeding/code/25-searxng/tests/unit/settings/test_tineye.yml) | tineye |
| [test_engine_github_code.py](file:///d:/fz/0601-2/solo-dogfeeding/code/25-searxng/tests/unit/test_engine_github_code.py) | `unittest.mock.Mock` + `@parameterized` | [test_github_code.yml](file:///d:/fz/0601-2/solo-dogfeeding/code/25-searxng/tests/unit/settings/test_github_code.yml) | github_code |
| [test_engines_init.py](file:///d:/fz/0601-2/solo-dogfeeding/code/25-searxng/tests/unit/test_engines_init.py) | 内联 engine_list 字典 | 默认 | 引擎注册逻辑（使用 `dummy` 引擎） |
| [test_online.py](file:///d:/fz/0601-2/solo-dogfeeding/code/25-searxng/tests/unit/processors/test_online.py) | 无 Mock 响应 | [test_settings.yml](file:///d:/fz/0601-2/solo-dogfeeding/code/25-searxng/tests/unit/settings/test_settings.yml) | OnlineProcessor + demo_offline |

---

## 6. 改进建议

1. **引入录制回放机制**：可用 `VCR.py` 或 `responses` 库录制真实 HTTP 交互为 YAML/JSON fixture 文件，作为 L2 级回归保护
2. **统一 Mock 构造方式**：将 `mock.Mock(text=...)` 迁移到 `unittest.mock.Mock`，并统一模拟 `resp.text` 与 `resp.json()` 两种接口
3. **补全响应字段**：现有夹具仅覆盖引擎读取路径上的最小字段集，应增加包含全部字段的"完整响应"夹具，验证 `parse_tineye_match()` 等函数的完整性
4. **使用 `setattr4test()` 管理引擎属性**：避免测试间全局状态泄漏
5. **参数化回归样例**：将 tineye/github_code 的 `@parameterized` 模式推广到 xpath/json_engine 测试，集中管理输入-期望对
6. **分离平台相关测试**：将 `TestCommandEngine` 中依赖系统命令的测试标记为 `@unittest.skipUnless`，并补充纯 Mock 版本
