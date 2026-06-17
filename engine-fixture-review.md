# SearXNG 搜索引擎适配层——测试夹具与回归样例审查

> 本文档从代码视角梳理 `tests/unit/engines/` 与 `tests/unit/test_engine_*.py` 中所有夹具数据的来源、调用方式、覆盖的解析路径，以及与真实抓取结果的差异，并对回归样例进行分层分类。

---

## 1. 目录与文件索引

所有路径均为相对于仓库根目录（即 `searx/` 与 `tests/` 所在目录）的可读引用。

| 路径 | 说明 |
|------|------|
| `searx/engines/__init__.py` | 引擎注册表：`load_engines()`、`load_engine()`、`ENGINE_DEFAULT_ARGS`、全局字典 `engines` |
| `searx/engines/xpath.py` | 通用 XPath 引擎：`request()`（L230）、`response()`（L275） |
| `searx/engines/json_engine.py` | 通用 JSON 引擎：`request()`（L315）、`extract_response_info()`（L362）、`response()`（L393） |
| `searx/engines/command.py` | 通用命令行引擎：`search()`（L129）、`_get_results_from_process()`（L156）、`__parse_single_result()`（L224） |
| `searx/engines/tineye.py` | TinEye 反图搜：`parse_tineye_match()`（L84）、`response()`（L152） |
| `searx/engines/github_code.py` | GitHub Code Search：`request()`（L144）、`extract_code()`（L162）、`response()`（L214） |
| `searx/search/processors/online.py` | 在线搜索处理器：`OnlineProcessor`、`_search_basic()`（L225）、`search()`（L241） |
| `searx/search/__init__.py` | 搜索入口：`initialize()`（L33）、`Search.search_multiple_requests()`（L136） |
| `tests/__init__.py` | 测试基类：`SearxTestCase`、`init_test_settings()`（L57）、`setattr4test()`（L46） |
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

## 2. 夹具数据的三层来源

### 2.1 YAML 配置夹具

所有 YAML 文件位于 `tests/unit/settings/`，由 `SearxTestCase.init_test_settings()` 读取。其统一模式为先清空默认引擎，再按需声明：

```yaml
use_default_settings:
  engines:
    keep_only: []          # 先清除默认引擎
engines:
  - name: ...              # 仅声明测试所需引擎
```

| YAML 文件 | 声明的引擎 | 引用方（测试类） |
|-----------|-----------|------------------|
| `test_settings.yml` | `dummy engine`（demo_offline）、`dummy private engine`（demo_offline + token） | `SearxTestCase` 默认、`TestOnlineProcessor` |
| `test_tineye.yml` | `tineye` | `TinEyeTests` |
| `test_github_code.yml` | `github code` | `GithubCodeTests` |
| `test_result_container.yml` | `google`、`duckduckgo`（保留默认） | 结果容器测试 |

### 2.2 内联数据夹具（硬编码 HTML/JSON/文本）

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

### 2.3 引擎模块全局属性（可变状态夹具）

测试代码通过**直接赋值引擎模块属性**来配置解析规则。这些赋值行为本身构成夹具：

| 测试方法 | 修改的模块属性 | 所在行 |
|----------|---------------|--------|
| `TestXpathEngine.test_request` | `xpath.search_url`、`xpath.categories`、`xpath.paging` | `test_xpath.py` L36-L52 |
| `TestXpathEngine.test_response` | `xpath.url_xpath`、`xpath.title_xpath`、`xpath.content_xpath`、`xpath.cached_xpath`、`xpath.categories` | `test_xpath.py` L59-L94 |
| `TestXpathEngine.test_response_results_xpath` | `xpath.results_xpath`、`xpath.url_xpath`、`xpath.title_xpath`、`xpath.content_xpath`、`xpath.cached_xpath`、`xpath.categories` | `test_xpath.py` L98-L136 |
| `TestJsonEngine.test_request` | `json_engine.search_url`、`json_engine.categories`、`json_engine.paging`、`json_engine.request_body` | `test_json_engine.py` L116-L146 |
| `TestJsonEngine.test_response` | `json_engine.results_query`、`json_engine.url_query`、`json_engine.url_prefix`、`json_engine.title_query`、`json_engine.content_query`、`json_engine.thumbnail_query`、`json_engine.thumbnail_prefix`、`json_engine.title_html_to_text`、`json_engine.content_html_to_text`、`json_engine.categories` | `test_json_engine.py` L150-L199 |
| `TestJsonEngine.test_response_results_json` | 同上全部属性 + `json_engine.suggestion_query` | `test_json_engine.py` L203-L254 |
| `TestCommandEngine` 各方法 | `command_engine.command`、`command_engine.delimiter`、`command_engine.result_separator`、`command_engine.parse_regex`、`command_engine.query_type`、`command_engine.query_enum` | `test_command.py` L13-L218 |

> **注意**：这类赋值没有配套的 cleanup，`SearxTestCase.setattr4test()` 虽已提供（`tests/__init__.py` L46-L55），但未被任何引擎测试使用。

---

## 3. 两类测试模式

### 模式 A：直接调用引擎函数（跳过引擎注册）

`TestXpathEngine`、`TestJsonEngine`、`TestCommandEngine` 采用此模式：

```
from searx.engines import xpath      # 直接 import 模块
xpath.search_url = '...'              # 手工配置属性
xpath.response(mock_response)         # 直接调用 response()
```

不调用 `searx.search.initialize()`，不创建 Flask 应用，不走 `ENGINE_DEFAULT_ARGS` 合并与 `EngineTraitsMap` 注入。

### 模式 B：通过引擎注册表完整初始化

`TinEyeTests`、`GithubCodeTests`、`TestOnlineProcessor` 采用此模式：

```
TEST_SETTINGS = "test_tineye.yml"
setUp() → super().setUp() → init_test_settings()
  → searx.search.initialize() → load_engines()
  → self.tineye = searx.engines.engines['tineye']
tearDown() → searx.search.load_engines([])
```

走完了完整的引擎初始化链路。

---

## 4. 测试样例关系表：输入夹具 → 断言目标 → 覆盖的解析路径

### 4.1 XPath 引擎（`tests/unit/engines/test_xpath.py`）

| # | 测试方法 | 输入夹具 | 断言目标 | 覆盖的引擎解析路径 | 与真实抓取的差异 |
|---|---------|---------|---------|-------------------|-----------------|
| 1 | `test_request` (L35) | `query='test_query'`、`dicto={'language':'all','pageno':1}`；`xpath.search_url`、`xpath.paging` 两组配置 | 返回 `params['url']` 正确拼接 `{query}` 与 `{pageno}` | `xpath.request()` L230-L272 中的 URL 模板替换、paging 计算分支 | 无 HTTP 客户端，仅测 URL 字符串拼接；真实环境还需经过 `OnlineProcessor.get_params()` 注入 User-Agent 等 header |
| 2 | `test_response` — 无 `results_xpath` (L57) | `mock.Mock(text=self.html, status_code=200)`；`xpath.url_xpath`、`title_xpath`、`content_xpath` | 抛出 `AttributeError`（None/[]/''/'[]' 输入）；空 HTML 返回空列表；正常 HTML 返回 2 条结果，`title`/`url`/`content` 字段值正确 | `xpath.response()` L275-L342 中：`no_result_for_http_status` 短路（未触发）→ `raise_for_httperror` → `dom = html.fromstring` → `results_xpath` 为假时走 L312-L335 的 `zip()` 分支 → `eval_xpath_list(dom, ...)` 顶层查询 → `extract_url` / `extract_text` | 真实 HTML 含广告、SSR 包裹、多种编码等噪音，此处极简骨架仅验证 XPath 选择器语义 |
| 3 | `test_response` — 含 `cached_xpath` (L82) | 同上夹具 + 设置 `xpath.cached_xpath` | 每条结果包含 `cached_url` 字段；`is_onion` 默认 False | `xpath.response()` L313-L328 `cached_xpath` 为真分支 | 真实 `cached_url` 前缀（`cached_url` 属性）未设置，测试中 `xpath.cached_url` 为空串默认值 |
| 4 | `test_response` — onion 分类 (L91) | 同上夹具 + `xpath.categories = ['onions']` | `is_onion = True` | `xpath.response()` L288、L307-L308、L325、L335 的 onion 标记分支 | 无真实 `.onion` URL 验证 |
| 5 | `test_response_results_xpath` (L96) | 同 L57 夹具，但设置了 `xpath.results_xpath` | 与 #2 相同的 2 条结果验证 | `xpath.response()` L290-L310 的 `results_xpath` 分支：对每个 result 子节点再做相对 XPath 查询 | 真实抓取时 `results_xpath` 选择器可能匹配到非结果节点（如推荐、相关搜索），此处无此类噪音 |
| 6 | `test_response_results_xpath` — cached (L124) | 同上 + `cached_xpath` | `cached_url` 字段正确 | `xpath.response()` L303-L306 | 同 #3 |
| 7 | `test_response_results_xpath` — onion (L133) | 同上 + `categories=['onions']` | `is_onion = True` | `xpath.response()` L307-L308 | 同 #4 |

### 4.2 JSON 引擎（`tests/unit/engines/test_json_engine.py`）

| # | 测试方法 | 输入夹具 | 断言目标 | 覆盖的引擎解析路径 | 与真实抓取的差异 |
|---|---------|---------|---------|-------------------|-----------------|
| 1 | `test_request` (L115) | 三组 `search_url`/`paging`/`request_body` 配置 | URL 模板替换正确；POST body 格式正确 | `json_engine.request()` L315-L355 的 URL 替换、`request_body` 格式化分支 | 未经过 `OnlineProcessor` header 注入；未测 `time_range`/`safe_search`/`lang` 模板变量 |
| 2 | `test_response` — 无 `results_query` (L148) | `mock.Mock(text=self.json, status_code=200)`；配置 `url_query`/`title_query`/`content_query`/`thumbnail_query` | 异常输入抛 `AttributeError`；空 JSON 返回空列表；正常 JSON 返回 3 条结果，含 `title`/`url`/`content`/`thumbnail` | `json_engine.response()` L393-L430 中：`raise_for_httperror` → `loads(resp.text)` → 无 `results_query` 直接遍历 `json` → `extract_response_info()` L362-L390 的 `query()` JSON 路径查询 → `to_string()` 处理非字符串字段 | 真实 API 响应含分页信息、rate limit header、元数据等大量冗余字段，此处仅保留被读取字段；但已刻意加入异常数据（整数 URL）验证鲁棒性 |
| 3 | `test_response` — prefix + suggestions (L182) | 同上夹具 + 设置 `url_prefix`、`thumbnail_query`（取 images/1）、`thumbnail_prefix` | 第 3 条结果的 URL 经 prefix 拼接为 `https://example.com/url2`；thumbnail 取数组第 2 项并加前缀；`is_onion=False` | `extract_response_info()` L369 `url_prefix + to_string(url)`、L385-L386 `thumbnail_prefix + to_string(...)`；`json_engine.response()` L421-L422 的 onion 分支 | 真实 API 中 suggestion 通常是独立字段，此处未设置 `suggestion_query`，suggestion 路径未覆盖 |
| 4 | `test_response` — onion (L196) | 同上 + `categories=['onions']` | `is_onion = True` | `json_engine.response()` L421-L422 | 同 xpath |
| 5 | `test_response_results_json` — 含 `results_query` (L201) | `mock.Mock(text=self.json_result_query, status_code=200)`；配置 `results_query='data/results'`、`title_html_to_text=True`、`content_html_to_text=True` | 3 条结果；HTML 标签被 `html_to_text` 剥离（`<h1>title1</h1>` → `title1`） | `json_engine.response()` L408-L414 的 `results_query` 分支 → `query(json, 'data/results')`；`extract_response_info()` L363-L364 的 `html_to_text` 过滤分支 | 真实响应中 HTML 可能嵌套更深、含转义字符，此处仅测简单标签 |
| 6 | `test_response_results_json` — prefix + suggestions (L235) | 同上 + 设置 `url_prefix`、`thumbnail_prefix`、`suggestion_query='data/suggestions'` | 共 4 条结果（3 条正常 + 1 条 suggestion）；suggestion 为 `['suggestion0', 'suggestion1']` | `json_engine.response()` L426-L429 的 suggestion 分支 | 真实 suggestion 结构可能更复杂（含 `metadata`） |
| 7 | `test_response_results_json` — onion (L251) | 同上 + `categories=['onions']` | `is_onion = True` | `json_engine.response()` L421-L422 | — |

### 4.3 Command 引擎（`tests/unit/engines/test_command.py`）

| # | 测试方法 | 输入夹具 | 断言目标 | 覆盖的引擎解析路径 | 与真实抓取的差异 |
|---|---------|---------|---------|-------------------|-----------------|
| 1 | `test_basic_seq_command_engine` (L12) | 真实系统命令 `seq 5`；`delimiter={'chars':' ','keys':['number']}` | 5 条 `KeyValue` 结果，值为 `"1"`-`"5"` | `command.search()` L129-L139 → `_get_command_to_run()` L142-L153 → `_get_results_from_process()` L156-L187 → `__parse_single_result()` L224-L234 的 delimiter 分支 | 真实命令执行存在权限、环境变量差异；此处依赖 POSIX `seq` 命令，Windows 不可用 |
| 2 | `test_delimiter_parsing` (L27) | 硬编码 searx 日志文本（12 行）通过 `echo` 输出；冒号分隔 delimiter；每页 10 条 | 第 1 页 10 条、第 2 页 3 条；每条的 `level`/`component`/`message` 正确 | `__parse_single_result()` L229-L234；`_get_results_from_process()` L175 的分页 `start/end` 过滤（`__get_results_limits()` L190-L193） | 真实日志含中文、特殊字符、多行堆栈，此处仅 ASCII 单行 |
| 3 | `test_regex_parsing` (L125) | 硬编码 git log 文本；`result_separator='\n\ncommit '`；`parse_regex` 4 组正则 | 3 条结果的 `commit`/`author`/`date`/`message` 正则匹配正确 | `__parse_single_result()` L236-L241 的 regex 分支；`_get_results_from_process()` L164 的 `result_separator` 分段 | 真实 git log 含 merge commit、签名、多 author 等复杂格式 |
| 4 | `test_working_dir_path_query` (L182) | `ls .` 与若干非法路径（`..`、`~`、`/var`）；`query_type='path'` | 合法路径返回结果；非法路径抛出 `ValueError` | `_get_command_to_run()` L142-L145 → `__check_query_params()` L196-L208 的 path 类型校验 | Windows 路径分隔符、权限模型差异未覆盖 |
| 5 | `test_enum_queries` (L202) | 允许枚举值与禁止值；`query_type='enum'`、`query_enum=[...]` | 允许值正常执行；禁止值抛 `ValueError` | `__check_query_params()` L205-L208 的 enum 类型校验 | — |

### 4.4 TinEye 引擎（`tests/unit/test_engine_tineye.py`）

| # | 测试方法 | 输入夹具 | 断言目标 | 覆盖的引擎解析路径 | 与真实抓取的差异 |
|---|---------|---------|---------|-------------------|-----------------|
| 1 | `test_status_code_raises` (L27) | `Mock(status_code=401)` + `raise_for_status.side_effect=HTTPError()` | 抛 `HTTPError` | `tineye.response()` L185-L186 的 `resp.raise_for_status()` 分支（未命中 L157 状态码白名单） | 真实响应含完整 headers、body，此处仅 status_code |
| 2 | `test_returns_empty_list` (L33) | 参数化：`status_code ∈ {400, 422}`；`json.return_value={"suggestions":{"key":"Download Error"}}` + `raise_for_status.side_effect=HTTPError()` | 返回空列表 `[]`，length=0；且产生日志输出 | `tineye.response()` L157-L183：命中状态码白名单 → 读取 `suggestions.key` → 匹配 `DOWNLOAD_ERROR` 常量 → `logger.info()` → 返回空 `EngineResults` | 真实错误响应含完整请求 ID、debug 信息等 |
| 3 | `test_logs_format_for_422` (L43) | `suggestions.key = "Invalid image URL"`、`status_code=422` | 日志包含 `FORMAT_NOT_SUPPORTED` 常量 | `tineye.response()` L162-L166 分支 | — |
| 4 | `test_logs_signature_for_422` (L53) | `suggestions.key = "NO_SIGNATURE_ERROR"` | 日志包含 `NO_SIGNATURE_ERROR` 常量 | `tineye.response()` L167-L169 分支 | — |
| 5 | `test_logs_download_for_422` (L63) | `suggestions.key = "Download Error"` | 日志包含 `DOWNLOAD_ERROR` 常量 | `tineye.response()` L170-L172 分支 | — |
| 6 | `test_logs_description_for_400` (L73) | `status_code=400`；`suggestions.description = [description_str]`、`suggestions.title="Oops!"` | 日志包含 description 字符串 | `tineye.response()` L175-L179 的 400 分支 | 真实 400 响应 description 可能是字符串而非列表 |
| 7 | `test_crawl_date_parses` (L84) | `status_code=200`；`json.return_value={'matches':[{'backlinks':[{'crawl_date':'2020-05-25'}]}]}` | `results[0]['publishedDate']` 等于 `datetime(2020,5,25)` | `tineye.response()` L188-L210 → `parse_tineye_match()` L115-L135 的 `crawl_date` `strptime` 解析；取 `backlinks[0]` 作为主 backlink | **未覆盖** `parse_tineye_match()` 中 L138-L148 返回的 `image_url`、`domain`、`score`、`width`、`height`、`size`、`image_format`、`filesize`、`overlay`、`tags` 共 10 个字段；未覆盖 backlinks 列表为空时 `continue` 分支（L193-L194）；未覆盖 `crawl_date` 为 None 时 `datetime.min` 回退分支（L125-L126） |

### 4.5 GitHub Code 引擎（`tests/unit/test_engine_github_code.py`）

| # | 测试方法 | 输入夹具 | 断言目标 | 覆盖的引擎解析路径 | 与真实抓取的差异 |
|---|---------|---------|---------|-------------------|-----------------|
| 1 | `test_code_extraction` 参数化 case 0 (L26) | 2 个 `code_matches` fragment，含 markdown 链接、匹配高亮 indices | 拆分出 7 行代码；高亮行为第 {2, 5} 行 | `github_code.extract_code()` L162-L211：多 fragment 遍历 → `ghc_insert_block_separator`（未启用）→ `ghc_strip_whitespace/new_lines`（默认开启）→ 逐字符匹配高亮 → 换行切分 | 真实 fragment 含未匹配上下文、非 UTF-8 字符、超长行等 |
| 2 | `test_code_extraction` 参数化 case 1 (L57) | 表格行 markdown，匹配关键词 "buffer" | 3 行表格；高亮第 2 行 | 同上 | 真实表格含对齐语法、嵌套代码块 |
| 3 | `test_code_extraction` 参数化 case 2 (L73) | 纯数字行 `1\n2\n3\n4`，匹配 "1" | 4 行；高亮第 1 行 | 同上，验证 offset 计算对纯数字内容的正确性 | — |
| 4 | `test_code_extraction` 参数化 case 3 (L88) | fragment="placeholder"，`matches=[]`（空高亮） | 1 行；高亮集合为空 | 同上，验证无高亮时 `highlighted_lines_index` 保持空 | — |
| 5 | `test_transforms_response` (L107) | 完整 API Mock：含 `items[].name/path/html_url`、`repository.full_name/html_url/description`、`text_matches[].object_type/property/fragment/matches` | 返回单个 `EngineResults.types.Code` 对象，其 `url`、`title`（`repo/path` 格式）、`content`、`repository`、`codelines`（6 行带编号）、`hl_lines={2,5,6}`、`strip_whitespace/new_lines` 全等于期望值 | `github_code.response()` L214-L249：`raise_for_httperror` → `resp.json()['items']` 遍历 → `repository` 子字典提取 → `text_matches` 按 `object_type=="FileContent" && property=="content"` 过滤 → `extract_code()` → `res.types.Code(...)` 构造 | **未覆盖** L217-L220 的 `status_code==422` 返回空结果分支；未覆盖 `ghc_auth` 非 `none` 分支（`request()` L154-L157）；未覆盖 `ghc_highlight_matching_lines=False` 分支（`response()` L232-L233）；未覆盖 `ghc_insert_block_separator=True` 分支；未覆盖 `ghc_strip_whitespace=True` 分支 |

### 4.6 引擎注册与 OnlineProcessor

| # | 测试方法 | 输入夹具 | 断言目标 | 覆盖的解析路径 | 与真实抓取的差异 |
|---|---------|---------|---------|----------------|-----------------|
| 1 | `test_initialize_engines_default` | 两个 dummy 引擎的字典列表 | 注册表含 2 个引擎 | `engines.load_engines()` L267-L286 | 未测真实网络引擎的 `setup()` 失败路径 |
| 2 | `test_initialize_engines_exclude_onions` | 含 onions 分类引擎 + `using_tor_proxy=False` | onions 引擎被排除，`engines.categories` 无 'onions' 键 | `engines.is_engine_active()` L221-L230 | — |
| 3 | `test_initialize_engines_include_onions` | 同上 + `using_tor_proxy=True` + onion_url + timeout | onions 引擎被保留；`search_url` 变为 onion URL；timeout += extra_proxy_timeout | `engines.update_attributes_for_tor()` L197-L200 | — |
| 4 | `test_missing_name_field` / `test_missing_engine_field` | 缺字段的 engine_data | 加载失败 + ERROR 日志 | `engines.load_engine()` L105-L122 | — |
| 5 | `TestOnlineProcessor.test_get_params_default_params` | `demo_offline` 引擎 + `SearchQuery` | 返回 params 含 `method/headers/data/url/cookies/auth` 全部键 | `OnlineProcessor.get_params()` L132-L164 → `default_request_params()` L94-L110 | 未发起真实 HTTP 请求 |
| 6 | `TestOnlineProcessor.test_get_params_useragent` | 同上 | headers 含 `User-Agent` | `OnlineProcessor.get_params()` L149-L150 `gen_useragent()` | — |

---

## 5. 回归样例分层：能否发现页面结构变化

### 5.1 分类标准

- **类型 S（结构敏感）**：夹具模拟了完整的目标数据结构，若真实 API/页面的结构层级、字段名、嵌套路径发生变化，断言将失败。典型特征：使用了 `results_xpath` / `results_query` 等复合查询；或断言了完整对象比较（如 `assertEqual(results, expected_results)`）。
- **类型 F（字段敏感）**：仅验证单个字段值的提取与转换（如 URL 前缀拼接、HTML 剥离、日期解析），即使页面结构改版，只要夹具中恰好存在对应字段名就仍能通过。典型特征：断言 `results[0]['title'] == '...'`、`results[0]['url'] == '...'` 等单字段检查。
- **类型 E（纯错误/边界）**：测试异常路径（HTTP 错误码、非法输入、权限校验），与真实抓取结构无关。

### 5.2 分类表

| 测试样例 | 类型 | 判断理由 |
|---------|------|---------|
| **XPath** | | |
| `test_response` (无 results_xpath) | F | 顶层 XPath 查询，仅断言 3 个字段值；真实页面若在 `.search_result` 外包一层新 div，夹具未模拟此层仍可通过 |
| `test_response_results_xpath` (有 results_xpath) | S | `results_xpath='//div[@class="search_result"]'` 模拟了真实页面的结果容器结构，若真实页面 class 名从 `search_result` 变为 `result-item`，该夹具的结构模拟就对应失效（但断言本身仍用字段值验证，需结合夹具结构变化才会失败）**注**：严格来说此样例仍属于 F，因为断言仅检查字段值，未断言"能匹配到结果节点数量"以外的结构性约束 |
| `test_request` | E | URL 字符串拼接，与抓取无关 |
| **JSON** | | |
| `test_response` (无 results_query) | F | 顶层数组遍历 + 单字段断言；真实 API 若改为 `{hits: [...]}` 包装但夹具仍用数组则无法预警 |
| `test_response_results_json` (有 results_query) | S | `results_query='data/results'` 模拟了嵌套路径，真实 API 若从 `data.results` 变为 `data.items`，该路径查询返回空，断言结果数量为 0 则失败；另外 HTML 剥离、suggestion 提取均涉及具体结构 |
| `test_request` | E | URL 拼接 |
| **Command** | | |
| `test_delimiter_parsing` | S | 模拟了完整日志的冒号分隔结构；真实命令输出格式若改为制表符分隔则失败 |
| `test_regex_parsing` | S | 模拟 git log 的段落 + 正则结构；输出格式变化（如 hash 长度、日期格式）则失败 |
| `test_basic_seq_command_engine` | F | 仅断言数字序列值 |
| `test_working_dir_path_query` / `test_enum_queries` | E | 纯权限/枚举校验 |
| **TinEye** | | |
| `test_crawl_date_parses` | F | 仅断言 `publishedDate` 单字段；未覆盖其余 10+ 字段，也未断言结果条数与 backlink 结构的完整性 |
| `test_status_code_raises` / `test_returns_empty_list` / `test_logs_*` | E | HTTP 错误码与日志分支，与成功响应结构无关 |
| **GitHub Code** | | |
| `test_transforms_response` | S | `assertEqual(results, expected_results)` 对完整 `Code` 对象做全量比较；`items → repository/text_matches → extract_code → Code` 全链路断言；任何字段缺失或结构变化都会导致对象不相等 |
| `test_code_extraction` (4 组参数化) | S | 断言代码行内容 + 高亮行号的精确匹配；fragment 结构或 matches.indices 语义变化直接失败 |
| **OnlineProcessor / engines_init** | | |
| 全部样例 | E | 初始化逻辑与参数组装，不涉及响应解析 |

### 5.3 汇总

| 类型 | 样例数 | 占比 | 典型风险 |
|------|--------|-----|---------|
| **S（结构敏感）** | 7 | ~30% | 覆盖了最关键的结构路径，但集中在 GitHub Code、Command、JSON 引擎的 results_query 分支；XPath 引擎与 TinEye 几乎无类型 S 样例 |
| **F（字段敏感）** | 9 | ~39% | 真实 API/页面改版时最易漏报——夹具中恰好存在同名字段但实际结构已大改 |
| **E（纯错误/边界）** | 8 | ~34% | 不涉及成功响应结构 |

---

## 6. 未覆盖的解析路径清单

以下引擎代码路径在现有夹具下**完全不被测试触及**：

### `searx/engines/xpath.py`
- `response()` L279-L280：`no_result_for_http_status` 命中时返回空列表的分支
- `response()` L284-L285：`resp.text` 为空时返回空列表的分支
- `response()` L298-L301：`thumbnail_xpath` 分支（含 results_xpath）
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
- `init()` L103-L104：`check_parsing_options` 异常分支（由 `check_parsing_options` 自身测试覆盖）

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

1. **对 XPath 和 TinEye 增补类型 S 样例**：当前两类引擎几乎没有能在页面/API 结构变化时失效的测试。XPath 应增加断言"结果节点数量"与"完整结果对象比较"；TinEye 应补全 `parse_tineye_match()` 全部 10+ 字段的断言。
2. **为每个引擎补充 L279/L397 的 `no_result_for_http_status` 夹具**：这是引擎抵御特定 HTTP 错误码的重要功能，现无覆盖。
3. **统一使用 `SearxTestCase.setattr4test()`** 替代直接修改引擎模块全局属性，避免测试间状态泄漏。
4. **引入录制回放机制**：可用 `VCR.py` 或 `responses` 库将真实 HTTP 交互录制为 JSON/YAML 夹具文件，作为 L2 级别回归保护。当前手工构造的 L1 骨架无法发现真实 API 字段增删。
5. **为 `command` 引擎补全 `mock.patch('subprocess.Popen')` 版本**：替代依赖 `seq`、`ls` 等 POSIX 命令的测试，保证在 Windows 上的可移植性。
6. **将 GitHub Code 的参数化模式推广**：`@parameterized.expand` 的"输入-期望对集中管理"模式应应用到 XPath、JSON、TinEye 测试，替代散落的 `test_*` 方法。
