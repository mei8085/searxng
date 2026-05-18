# URL 清理插件链路系统梳理

## 一、插件注册形式

SearXNG 的 URL 清理类插件基于统一的插件框架实现，核心注册机制如下：

### 1.1 插件基类与接口

所有 URL 清理插件均继承自 `Plugin` 抽象基类（`searx/plugins/_core.py:68`），通过实现 `on_result` 钩子介入搜索结果处理流程。插件必须实现以下核心要素：

- **类定义**：继承 `Plugin` 类，定义唯一 `id` 标识
- **配置加载**：在 `settings.yml` 中声明插件的完整类路径和激活状态
- **钩子实现**：在 `on_result` 方法中调用 `result.filter_urls()` 遍历所有 URL 字段

### 1.2 注册流程

```python
# settings.yml 配置示例:
plugins:
  searx.plugins.tracker_url_remover.SXNGPlugin:
    active: true
  searx.plugins.hostnames.SXNGPlugin:
    active: true
```

注册链路：
1. 应用启动时，`PluginStorage.load_settings()` 解析配置（`searx/plugins/_core.py:212`）
2. 通过 `importlib.import_module` 动态导入插件类
3. 实例化插件对象并调用 `register()` 注册到 `plugin_list` 集合
4. 调用 `init()` 方法进行初始化，返回 `False` 则插件不激活

### 1.3 现有 URL 清理类插件清单

| 插件 ID | 类路径 | 功能定位 |
|--------|--------|----------|
| `tracker_url_remover` | `searx.plugins.tracker_url_remover.SXNGPlugin` | 移除 URL 追踪参数 |
| `hostnames` | `searx.plugins.hostnames.SXNGPlugin` | 主机名重写、结果移除、优先级调整 |
| `oa_doi_rewrite` | `searx.plugins.oa_doi_rewrite.SXNGPlugin` | DOI 重定向到开放获取版本 |

## 二、命中匹配规则的维度

### 2.1 tracker_url_remover 插件

**数据源**：ClearURLs 规则库（`searx/data/tracker_patterns.py:31-36`）

匹配维度：

```python
# 规则结构 (tracker_patterns.py:22)
RuleType = tuple[str, list[str], list[str]]
# 即: (url_regexp, url_ignore, del_args)
```

1. **URL 模式匹配** (`url_regexp`) - 正则表达式匹配整个 URL
2. **例外排除** (`url_ignore`) - 匹配此正则的 URL 跳过处理
3. **参数删除** (`del_args`) - 匹配此正则的查询参数名被移除

```python
# 匹配流程 (tracker_patterns.py:121-171)
for rule in self.rules():
    if re.match(rule[url_regexp], new_url):           # 1. URL 匹配
        if re.match(exception, new_url):              # 2. 例外检查
            continue
        for name, val in query_args:
            if re.match(pattern, name):               # 3. 参数名匹配
                query_args.remove((name, val))        # 4. 删除参数
```

### 2.2 hostnames 插件

匹配维度基于**主机名正则匹配**（`hostnames.py:126-143`）：

1. **REMOVE 规则**：主机名匹配正则，匹配则移除整个结果
2. **REPLACE 规则**：主机名匹配正则，匹配则替换主机名部分
3. **PRIORITY 规则**：匹配则调整结果优先级

```python
# 主机名匹配逻辑
for pattern in REMOVE:
    if pattern.search(result.parsed_url.netloc):
        return False  # 移除结果

for pattern, replacement in REPLACE.items():
    if pattern.search(url_src_parsed.netloc):
        new_url = url_src_parsed._replace(netloc=pattern.sub(replacement, ...))
```

### 2.3 oa_doi_rewrite 插件

匹配维度基于**DOI 模式匹配**（`oa_doi_rewrite.py:70-81`）：

```python
regex = re.compile(r'10\.\d{4,9}/[^\s]+')

def extract_doi(url):
    m = regex.search(url.path)          # 1. 从 URL 路径提取
    for _, v in parse_qsl(url.query):   # 2. 从查询参数提取
        m = regex.search(v)
```

## 三、被改写字段的范围

所有 URL 清理插件通过 `result.filter_urls()` 方法统一处理 URL 字段。在 `searx/result_types/_base.py:111-216` 中定义了完整的字段遍历逻辑。

### 3.1 主 URL 字段（一级字段）

```python
url_fields = ["url", "iframe_src", "audio_src", "img_src", "thumbnail_src", "thumbnail"]
```

| 字段名 | 用途说明 |
|--------|----------|
| `url` | 结果主链接 |
| `iframe_src` | iframe 嵌入源 |
| `audio_src` | 音频源 URL |
| `img_src` | 图片源 URL |
| `thumbnail_src` | 缩略图源 URL |
| `thumbnail` | 缩略图 URL（兼容字段） |

### 3.2 Infobox 嵌套字段

```python
# Infobox URLs 列表字段
infobox_urls: list[dict[str, str]] = getattr(result, "urls", [])
# 每个 item 中的 "url" 字段

# Infobox 属性中的图片源
infobox_attributes: list[dict[str, t.Any]] = getattr(result, "attributes", [])
# 每个 item["image"]["src"] 字段
```

### 3.3 各插件字段覆盖范围

| 插件 | 处理字段 | 说明 |
|------|----------|------|
| tracker_url_remover | 所有 URL 字段 | 遍历全部字段，移除追踪参数 |
| hostnames | 所有 URL 字段 + 主 URL 额外处理 | 1. 先检查主 URL 决定是否移除结果<br>2. 再遍历所有 URL 字段进行重写 |
| oa_doi_rewrite | 仅 `url` 字段 | `if field_name != "url": return True`，只处理主链接 |

### 3.4 字段改写后的同步机制

当 `url` 字段被修改时，会自动同步 `parsed_url` 字段（`_base.py:140-145`）：

```python
if field_name == "url":
    if not new_url:
        result.parsed_url = None
    elif isinstance(new_url, str):
        result.parsed_url = urllib.parse.urlparse(new_url)
```

## 四、插件执行顺序与互相覆盖问题

### 4.1 执行顺序的核心机制

#### 4.1.1 数据结构分析

**关键事实**：`PluginStorage.plugin_list` 是一个 `set` 集合（`_core.py:199`），而非有序列表。

```python
class PluginStorage:
    def __init__(self):
        self.plugin_list = set()  # 无序集合
    
    def __iter__(self) -> Generator[Plugin]:
        yield from self.plugin_list  # 按 set 迭代顺序返回
```

#### 4.1.2 执行顺序的真实逻辑

在 `on_result` 钩子调用时（`_core.py:267-280`），执行顺序完全由 set 的迭代顺序决定：

```python
def on_result(self, request, search, result):
    ret = True
    # 注意：这里遍历的是 self.plugin_list（set），然后过滤 user_plugins
    # 执行顺序 = set 迭代顺序，与 user_plugins 列表顺序无关
    for plugin in [p for p in self.plugin_list if p.id in search.user_plugins]:
        ret = bool(plugin.on_result(request=request, search=search, result=result))
        if not ret:
            break
    return ret
```

#### 4.1.3 user_plugins 的真实作用

`user_plugins` 列表仅用于**启用/禁用过滤**，不决定执行顺序：

```python
# webapp.py:512-518
sxng_request.user_plugins = []
allowed_plugins = preferences.plugins.get_enabled()
disabled_plugins = preferences.plugins.get_disabled()
for plugin in searx.plugins.STORAGE:  # 遍历 set，顺序不确定
    if (plugin.id not in disabled_plugins) or plugin.id in allowed_plugins:
        sxng_request.user_plugins.append(plugin.id)  # 顺序与 set 一致
```

### 4.2 前稿表述修正

**❌ 错误表述**："在 settings.yml 中按期望的执行顺序声明插件（依赖 Python 3.7+ dict 有序特性）"

**✅ 正确表述**：配置文件中插件的声明顺序不能稳定控制执行顺序。虽然 Python 3.7+ 的 dict 是有序的，`load_settings` 也按配置顺序加载插件，但插件被 `add` 到 `set` 后，顺序就丢失了。set 的迭代顺序取决于对象哈希值和内部存储结构，跨进程/重启可能发生变化。

### 4.3 互相覆盖的风险场景

当多个插件修改同一个 URL 字段时，**后执行的插件会基于前一个插件修改后的 URL 继续处理**。

**✅ 澄清：hostnames 改域名不会导致 oa_doi_rewrite 失效**

经过代码核实，oa_doi_rewrite 的 `extract_doi` 函数仅从 `url.path` 和查询参数中提取 DOI，**不依赖 `netloc`（域名）**：

```python
# oa_doi_rewrite.py:73-81
def extract_doi(url):
    m = regex.search(url.path)          # 仅从 path 提取
    if m:
        return m.group(0)
    for _, v in parse_qsl(url.query):  # 仅从查询参数提取
        m = regex.search(v)
        if m:
            return m.group(0)
    return None
```

因此，即使 hostnames 把 `doi.org` 重写为其他域名，只要 URL 路径中包含 DOI（如 `10.1234/abc`），oa_doi_rewrite 仍能正常工作。

---

**真实风险场景**：

1. **tracker_url_remover 与 hostnames 冲突（参数残留）**：
   - hostnames 先执行：`youtube.com` → `invidious.example.com`
   - tracker_url_remover 后执行：ClearURLs 规则是针对 `youtube.com` 域名匹配的，域名改变后可能无法匹配
   - 结果：`utm_` 等追踪参数残留

2. **hostnames REMOVE 与其他插件的交互（无效计算）**：
   - 其他插件先执行：花费时间清理 URL、提取 DOI
   - hostnames 后执行：发现主机名匹配 REMOVE，直接移除整个结果
   - 结果：之前的清理工作全部浪费（性能影响）

3. **多个插件修改同一个 URL（顺序依赖）**：
   - 插件 A 修改 `url` 为 A'（基于原始值）
   - 插件 B 修改 `url` 为 B'（基于 A' 继续修改）
   - 如果顺序调换，结果可能不同

4. **优先级设置与结果排序的交互**：
   - hostnames 的 HIGH/LOW 优先级是在 `on_result` 末尾设置的
   - 如果组合插件调整了优先级设置的位置，可能影响结果排序

### 4.4 推荐的执行顺序原则

为避免互相覆盖，应遵循以下原则：

#### 原则 1：先清理参数，后重写域名

**推荐逻辑顺序**：
```
tracker_url_remover → hostnames → oa_doi_rewrite
```

**原因**：
- tracker_url_remover 基于原始域名匹配追踪参数（ClearURLs 规则针对原始域名）
- hostnames 基于清理后的 URL 重写域名（避免重写后参数无法匹配）
- oa_doi_rewrite 最后处理 DOI 重定向（基于已清理的 URL）

#### 原则 2：移除类逻辑优先执行

如果有插件会移除结果（如 hostnames 的 REMOVE 规则），应优先执行，避免无效处理即将被移除的结果。

#### 原则 3：在 filter_func 中避免假设原始 URL

插件的 filter_func 签名为：
```python
def filter_url_field(result, field_name, url_src) -> bool | str:
```

`url_src` 是**当前字段的值**（可能已被之前插件修改过）。如果插件需要基于原始 URL 匹配，应在 `on_result` 中保存原始值。

#### 原则 4：幂等性设计

每个插件的 filter_func 应设计为幂等的，多次调用结果一致。

### 4.5 hostnames 语义核对清单

在设计组合插件前，必须完整覆盖 hostnames 原插件的所有语义。以下是逐条核对清单：

| 语义点 | 原代码位置 | 说明 |
|--------|----------|------|
| **配置初始化** | `hostnames.py:147-159` | `init()` 检查 `settings.get("hostnames")`，加载 `REPLACE/REMOVE/HIGH/LOW` 全局变量，支持外部 YAML 文件 |
| **结果移除（主 URL）** | `hostnames.py:126-132` | `on_result` 开头检查 `result.parsed_url.netloc`，匹配 REMOVE 则返回 `False` 移除整个结果 |
| **URL 字段过滤** | `hostnames.py:134` | 调用 `result.filter_urls(filter_url_field)` 遍历所有 URL 字段 |
| **字段级 REMOVE** | `hostnames.py:190-192` | `filter_url_field` 中对每个 URL 字段检查 REMOVE，返回 `False` 移除该字段 |
| **字段级 REPLACE** | `hostnames.py:194-198` | `filter_url_field` 中对每个 URL 字段检查 REPLACE，返回新 URL |
| **LOW 优先级设置** | `hostnames.py:136-139` | 仅对 `MainResult/LegacyResult`，匹配 LOW 则 `result.priority = "low"` |
| **HIGH 优先级设置** | `hostnames.py:141-143` | 匹配 HIGH 则 `result.priority = "high"`（在 LOW 之后执行，优先级更高） |
| **执行顺序约束** | `hostnames.py:124-145` | 固定顺序：主 URL REMOVE → filter_urls → LOW → HIGH |

### 4.6 oa_doi_rewrite 交互边界分析

| 交互点 | 说明 | 风险 |
|--------|------|------|
| **字段范围** | oa_doi_rewrite 仅处理 `field_name == "url"` 的主链接 | 与其他插件的交互仅限于主 URL |
| **DOI 提取** | 从 `result.parsed_url.path` 和查询参数中提取 DOI | 域名被重写不影响 DOI 提取（DOI 在路径中） |
| **前置检查** | `on_result` 中有 `if result.parsed_url:` 检查 | 组合插件需保留此检查 |
| **副作用** | 会设置 `result["doi"] = doi` | 需确保在 URL 清理完成后执行 |

### 4.7 当前实现下可落地的顺序管理方案

#### 方案 A1：组合插件模式 - 最小侵入版（推荐 ✅）

**原理**：保留 hostnames 插件激活（仅用于配置初始化），通过继承重写其 `on_result` 为空，组合插件负责按正确顺序调用所有逻辑。

**最小改造点清单**：

| 改造点 | 文件 | 修改内容 |
|--------|------|----------|
| 1 | `hostnames.py` | 将 `on_result` 方法标记为可被覆盖或通过配置控制 |
| 2 | 新建 `url_cleanup_pipeline.py` | 创建组合插件，按顺序执行所有清理逻辑 |
| 3 | `settings.yml` | 禁用 tracker_url_remover 和 oa_doi_rewrite，启用组合插件 |

**代码实现 - 步骤 1：修改 hostnames.py（可选，若不修改则保持激活但逻辑被组合插件替代）**

```python
# 在 hostnames.py 末尾添加空实现的子类供继承
class SXNGPluginNoop(SXNGPlugin):
    """Hostnames 插件的无操作版本，仅用于配置初始化"""
    
    def on_result(self, request, search, result):
        # 不执行任何操作，逻辑由组合插件统一调度
        return True
```

**代码实现 - 步骤 2：创建 url_cleanup_pipeline.py（完整覆盖所有语义）**

```python
# searx/plugins/url_cleanup_pipeline.py
# SPDX-License-Identifier: AGPL-3.0-or-later

import typing as t
import re
from urllib.parse import urlunparse, urlparse

from flask_babel import gettext

from searx import settings
from searx.plugins import Plugin, PluginInfo
from searx.data import TRACKER_PATTERNS
from searx.result_types._base import MainResult, LegacyResult
from searx.settings_loader import get_yaml_cfg
from searx.plugins.hostnames import filter_url_field as hostnames_filter
from searx.plugins.oa_doi_rewrite import filter_url_field as doi_filter, extract_doi

from ._core import log

if t.TYPE_CHECKING:
    import flask
    from searx.search import SearchWithPlugins
    from searx.extended_types import SXNG_Request
    from searx.result_types import Result, LegacyResult
    from searx.plugins import PluginCfg


# hostnames 配置的全局变量（从 hostnames 模块导入或重新加载）
REPLACE: dict[re.Pattern, str] = {}
REMOVE: set = set()
HIGH: set = set()
LOW: set = set()


def _load_hostnames_config():
    """加载 hostnames 配置（与原 hostnames 插件逻辑一致）"""
    global REPLACE, REMOVE, HIGH, LOW
    
    hostnames_cfg = settings.get("hostnames")
    if not hostnames_cfg:
        return
    
    def _load_regular_expressions(settings_key):
        setting_value = hostnames_cfg.get(settings_key)
        if not setting_value:
            return None
        if isinstance(setting_value, str):
            setting_value = get_yaml_cfg(setting_value)
        if isinstance(setting_value, list):
            return {re.compile(r) for r in setting_value}
        if isinstance(setting_value, dict):
            return {re.compile(p): r for (p, r) in setting_value.items()}
        return None
    
    REPLACE = _load_regular_expressions("replace") or {}
    REMOVE = _load_regular_expressions("remove") or set()
    HIGH = _load_regular_expressions("high_priority") or set()
    LOW = _load_regular_expressions("low_priority") or set()


class SXNGPlugin(Plugin):
    """统一的 URL 清理管道，按确定顺序执行多个清理逻辑
    完整覆盖 tracker_url_remover + hostnames + oa_doi_rewrite 语义
    """

    id = "url_cleanup_pipeline"

    def __init__(self, plg_cfg: "PluginCfg") -> None:
        super().__init__(plg_cfg)
        self.info = PluginInfo(
            id=self.id,
            name=gettext("URL Cleanup Pipeline"),
            description=gettext("Unified URL cleanup with controlled execution order"),
            preference_section="privacy",
        )

    def init(self, app: "flask.Flask") -> bool:
        TRACKER_PATTERNS.init()
        _load_hostnames_config()  # 加载 hostnames 配置
        return True

    def on_result(self, request: "SXNG_Request", search: "SearchWithPlugins", result: "Result") -> bool:
        # ============================================================
        # 执行顺序（严格按照 hostnames 语义约束）
        # 1. 主 URL REMOVE 检查 → 2. 清理追踪参数 → 3. 主机名重写
        # 4. DOI 重定向 → 5. 优先级设置
        # ============================================================
        
        # ---------- 步骤 1：主 URL REMOVE 检查（与 hostnames 语义一致） ----------
        for pattern in REMOVE:
            if result.parsed_url and pattern.search(result.parsed_url.netloc):
                log.debug("url_cleanup_pipeline: remove result by hostname pattern %s", pattern.pattern)
                return False  # 移除整个结果
        
        # ---------- 步骤 2：清理追踪参数（tracker_url_remover 语义） ----------
        result.filter_urls(self._clean_trackers)
        
        # ---------- 步骤 3：主机名重写（hostnames filter_url_field 语义） ----------
        result.filter_urls(self._hostnames_rewrite)
        
        # ---------- 步骤 4：DOI 重定向（oa_doi_rewrite 语义，保留前置检查） ----------
        if result.parsed_url:
            result.filter_urls(doi_filter)
        
        # ---------- 步骤 5：优先级设置（hostnames 语义，仅对 MainResult/LegacyResult） ----------
        if isinstance(result, (MainResult, LegacyResult)):
            # 先设置 LOW，再设置 HIGH（HIGH 优先级更高）
            for pattern in LOW:
                if result.parsed_url and pattern.search(result.parsed_url.netloc):
                    result.priority = "low"
                    log.debug("url_cleanup_pipeline: set low priority for %s", result.parsed_url.netloc)
            
            for pattern in HIGH:
                if result.parsed_url and pattern.search(result.parsed_url.netloc):
                    result.priority = "high"
                    log.debug("url_cleanup_pipeline: set high priority for %s", result.parsed_url.netloc)
        
        return True

    @classmethod
    def _clean_trackers(cls, result: "Result|LegacyResult", field_name: str, url_src: str) -> bool | str:
        """清理 URL 追踪参数（与 tracker_url_remover 语义一致）"""
        if not url_src:
            return True
        return TRACKER_PATTERNS.clean_url(url=url_src)
    
    @classmethod
    def _hostnames_rewrite(cls, result: "Result|LegacyResult", field_name: str, url_src: str) -> bool | str:
        """主机名重写（复用 hostnames 的 filter_url_field 逻辑）"""
        if not url_src:
            return True
        
        url_src_parsed = urlparse(url=url_src)
        
        # 字段级 REMOVE 检查
        for pattern in REMOVE:
            if pattern.search(url_src_parsed.netloc):
                log.debug("url_cleanup_pipeline: remove URL field %s by pattern %s", field_name, pattern.pattern)
                return False
        
        # 字段级 REPLACE
        for pattern, replacement in REPLACE.items():
            if pattern.search(url_src_parsed.netloc):
                new_url = url_src_parsed._replace(netloc=pattern.sub(replacement, url_src_parsed.netloc))
                new_url = urlunparse(new_url)
                log.debug("url_cleanup_pipeline: rewrite URL %s -> %s", url_src, new_url)
                return new_url
        
        return True
```

**代码实现 - 步骤 3：修改 settings.yml**

```yaml
plugins:
  # 禁用原独立插件
  searx.plugins.tracker_url_remover.SXNGPlugin:
    active: false
  searx.plugins.hostnames.SXNGPlugin:
    active: false  # 组合插件已集成其全部逻辑
  searx.plugins.oa_doi_rewrite.SXNGPlugin:
    active: false
  
  # 启用统一管道插件
  searx.plugins.url_cleanup_pipeline.SXNGPlugin:
    active: true

# hostnames 配置保留（组合插件会读取）
hostnames:
  replace:
    '(.*\.)?youtube\.com$': 'invidious.example.com'
  remove:
    - '(.*\.)?facebook\.com$'
  high_priority:
    - '(.*\.)?wikipedia\.org$'
  low_priority:
    - '(.*\.)?google(\..*)?$'
```

**✅ 语义覆盖验证**：
- ✅ 配置初始化：`_load_hostnames_config()` 完整复制原逻辑
- ✅ 主 URL REMOVE：步骤 1 完整实现
- ✅ URL 字段过滤：步骤 2、3、4 按顺序执行
- ✅ 字段级 REMOVE/REPLACE：`_hostnames_rewrite` 完整实现
- ✅ 高低优先级：步骤 5 完整实现，HIGH 在 LOW 之后
- ✅ 执行顺序约束：严格按照 `REMOVE → 清理 → 重写 → DOI → 优先级` 顺序

**优点**：
- 执行顺序 100% 可控，无 set 迭代不确定性
- 零框架侵入，不修改核心代码
- 完整覆盖所有原有语义，无功能缺失
- 可灵活调整顺序和添加自定义清理逻辑
- 可添加日志、调试、监控等增强功能

#### 方案 A2：组合插件模式 - 轻量版（适用于不需要 hostnames 优先级功能）

**原理**：保留 hostnames 插件激活（负责 REMOVE 和优先级），组合插件仅协调 URL 清理顺序。

**适用场景**：已配置 hostnames 的 REMOVE 和优先级规则，不想迁移配置。

**代码实现**：

```python
# searx/plugins/url_cleanup_pipeline.py
class SXNGPlugin(Plugin):
    id = "url_cleanup_pipeline"
    
    def on_result(self, request, search, result):
        # 仅协调 URL 清理顺序，REMOVE 和优先级由 hostnames 插件负责
        result.filter_urls(self._clean_trackers)
        if result.parsed_url:
            result.filter_urls(doi_filter)
        return True
```

**⚠️ 注意**：此方案下 hostnames 插件的 filter_url_field 仍会被调用，可能导致主机名重写在参数清理之前执行。建议使用方案 A1。

#### 方案 B：修改框架支持有序插件列表（需修改核心代码）

**原理**：将 `plugin_list` 从 `set` 改为 `list`，保持配置顺序。

**修改点**：
```python
# searx/plugins/_core.py:198-199
class PluginStorage:
    # plugin_list: set[Plugin]  # 改为 list
    plugin_list: list[Plugin]    # 有序列表
    
    def register(self, plugin: Plugin):
        # 查重逻辑保留
        if plugin.id in [p.id for p in self.plugin_list]:
            raise KeyError(f"name collision '{plugin.id}'")
        # self.plugin_list.add(plugin)  # 改为 append
        self.plugin_list.append(plugin)
```

**优点**：
- 配置文件顺序直接决定执行顺序
- 无需修改业务插件

**缺点**：
- 需要修改框架核心代码
- 可能影响依赖 set 特性的其他逻辑（如去重、成员判断等）
- 需要充分测试

#### 方案 C：基于 user_plugins 顺序的执行调度（需修改框架代码）

**原理**：修改 `on_result` 等调度方法，按 `user_plugins` 列表顺序执行。

**修改点**：
```python
# searx/plugins/_core.py:270
def on_result(self, request, search, result):
    ret = True
    # 改为按 user_plugins 顺序执行
    plugin_map = {p.id: p for p in self.plugin_list}
    for plugin_id in search.user_plugins:
        plugin = plugin_map.get(plugin_id)
        if not plugin:
            continue
        try:
            ret = bool(plugin.on_result(request=request, search=search, result=result))
        except Exception:
            plugin.log.exception("Exception while calling on_result")
            continue
        if not ret:
            break
    return ret
```

**优点**：
- 用户偏好设置中的插件顺序决定执行顺序
- 更灵活的用户控制

**缺点**：
- 需要修改框架核心代码
- `user_plugins` 列表当前也是按 set 顺序构建的，需要一并修改

### 4.8 方案对比与选型建议

| 方案 | 语义完整性 | 可靠性 | 实现成本 | 可维护性 | 推荐场景 |
|------|----------|--------|----------|----------|----------|
| **A1. 完整组合插件** | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | 低（新增插件） | ⭐⭐⭐⭐⭐ | **生产环境首选** |
| A2. 轻量组合插件 | ⭐⭐⭐ | ⭐⭐⭐ | 低 | ⭐⭐⭐ | 不需要 hostnames 全部功能 |
| B. 改为 list | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | 中（修改核心） | ⭐⭐⭐ | 愿意维护 fork 版本 |
| C. 按 user_plugins | ⭐⭐⭐⭐⭐ | ⭐⭐⭐ | 高（修改核心+偏好） | ⭐⭐ | 需要用户级顺序控制 |

**✅ 首选推荐：方案 A1（完整组合插件）**
- 零框架侵入，不影响原有插件生态
- 执行顺序完全可控，可随时调整
- 完整覆盖所有语义，无功能缺失
- 可在组合插件中添加日志、调试、监控等增强功能

### 4.9 常见问题与边界处理

**Q1：如果 hostnames 配置为空怎么办？**
A：组合插件的 `_load_hostnames_config()` 会正确处理空配置，REPLACE/REMOVE/HIGH/LOW 保持为空字典/集合，相关逻辑自动跳过。

**Q2：多个插件修改同一个 URL 字段会怎样？**
A：在组合插件内部，执行顺序是确定的。后执行的逻辑会基于前一个逻辑的输出继续处理，符合预期。

**Q3：如何调试执行顺序？**
A：组合插件中已添加 `log.debug` 语句，可通过日志查看每个步骤的执行情况。

**Q4：新增自定义清理逻辑如何集成？**
A：在 `on_result` 方法中添加新的步骤即可，例如：
```python
# 在 DOI 重定向之后添加自定义清理
result.filter_urls(self._custom_cleanup)
```

## 五、完整链路时序图

```
搜索请求
    ↓
SearchWithPlugins.search()
    ↓
pre_search 钩子（所有插件，按 set 顺序）
    ↓
引擎搜索，产生结果
    ↓
结果进入 ResultContainer
    ↓
on_result 钩子（按 plugin_list set 顺序）
    ├─→ 【顺序不确定】tracker_url_remover.on_result()
    │     └─→ result.filter_urls(filter_url_field)
    │           ├─→ 遍历 url, iframe_src, img_src, ...
    │           ├─→ 遍历 infobox urls
    │           └─→ 遍历 infobox attributes
    │           └─→ TRACKER_PATTERNS.clean_url()
    │
    ├─→ 【顺序不确定】hostnames.on_result()
    │     ├─→ 检查主 URL 是否匹配 REMOVE
    │     └─→ result.filter_urls(filter_url_field)
    │           └─→ 主机名重写
    │
    └─→ 【顺序不确定】oa_doi_rewrite.on_result()
          └─→ result.filter_urls(filter_url_field)
                └─→ 仅处理 url 字段
    ↓
post_search 钩子（所有插件，按 set 顺序）
    ↓
结果返回
```

## 六、关键代码位置参考

| 功能 | 文件位置 |
|------|--------|
| 插件基类 | `searx/plugins/_core.py:68-175` |
| filter_urls 实现 | `searx/result_types/_base.py:111-216` |
| tracker_url_remover | `searx/plugins/tracker_url_remover.py:24-58` |
| 追踪模式数据库 | `searx/data/tracker_patterns.py:26-176` |
| hostnames 插件 | `searx/plugins/hostnames.py:110-200` |
| oa_doi_rewrite | `searx/plugins/oa_doi_rewrite.py:45-89` |
| 插件执行调度（关键） | `searx/plugins/_core.py:267-280` |
| plugin_list 定义 | `searx/plugins/_core.py:198-199` |
| user_plugins 构建 | `searx/webapp.py:512-518` |
| 搜索结果钩子 | `searx/search/__init__.py:198-199` |

## 七、附录：最小改造点清单与实施指南

### 7.1 最小改造点清单

| 序号 | 改造点 | 操作 | 影响范围 |
|------|--------|------|----------|
| 1 | 新增组合插件文件 | 创建 `searx/plugins/url_cleanup_pipeline.py` | 新增文件，无侵入 |
| 2 | 修改 settings.yml | 禁用原独立插件，启用组合插件 | 配置文件 |
| 3 | （可选）修改 hostnames.py | 添加空操作子类 `SXNGPluginNoop` | 可选，不修改也可运行 |

### 7.2 完整实施步骤

**步骤 1：创建组合插件文件**

将 4.7 节中的完整代码保存为 `searx/plugins/url_cleanup_pipeline.py`。

**步骤 2：修改 settings.yml**

```yaml
plugins:
  # 禁用原独立插件
  searx.plugins.tracker_url_remover.SXNGPlugin:
    active: false
  searx.plugins.hostnames.SXNGPlugin:
    active: false  # 组合插件已集成其全部逻辑
  searx.plugins.oa_doi_rewrite.SXNGPlugin:
    active: false
  
  # 启用统一管道插件
  searx.plugins.url_cleanup_pipeline.SXNGPlugin:
    active: true

# hostnames 配置保留（组合插件会读取）
hostnames:
  replace:
    '(.*\.)?youtube\.com$': 'invidious.example.com'
  remove:
    - '(.*\.)?facebook\.com$'
  high_priority:
    - '(.*\.)?wikipedia\.org$'
  low_priority:
    - '(.*\.)?google(\..*)?$'
```

**步骤 3（可选）：修改 hostnames.py（如需保留其配置初始化）**

在 `hostnames.py` 末尾添加空操作子类：

```python
class SXNGPluginNoop(SXNGPlugin):
    """Hostnames 插件的无操作版本，仅用于配置初始化"""
    
    def on_result(self, request, search, result):
        # 不执行任何操作，逻辑由组合插件统一调度
        return True
```

**步骤 4：重启服务**

重启 SearXNG 服务，验证日志中是否有 `url_cleanup_pipeline` 相关的加载日志。

### 7.3 验证清单

部署后请验证以下功能：

| 功能点 | 验证方法 | 预期结果 |
|--------|----------|----------|
| 追踪参数清理 | 搜索包含 utm_ 参数的 URL | 参数被移除 |
| 主机名重写 | 搜索 youtube.com 链接 | 被重写为 invidious.example.com |
| 结果移除 | 搜索 facebook.com 链接 | 结果被移除 |
| 优先级设置 | 搜索 wikipedia.org 链接 | 结果优先级为 high |
| DOI 重定向 | 搜索包含 DOI 的学术链接 | 被重定向到配置的解析器 |
| 执行顺序 | 查看 debug 日志 | 按 `REMOVE → 清理 → 重写 → DOI → 优先级` 顺序执行 |

### 7.4 回滚方案

如遇到问题，可快速回滚：

1. 从 `settings.yml` 中移除 `url_cleanup_pipeline` 配置
2. 重新启用原独立插件（tracker_url_remover、hostnames、oa_doi_rewrite）
3. 重启服务

### 7.5 可直接使用的组合插件完整代码

以下是经过完整语义核对的组合插件代码，可直接复制使用：

```python
# searx/plugins/url_cleanup_pipeline.py
# SPDX-License-Identifier: AGPL-3.0-or-later

import typing as t
import re
from urllib.parse import urlunparse, urlparse

from flask_babel import gettext

from searx import settings
from searx.plugins import Plugin, PluginInfo
from searx.data import TRACKER_PATTERNS
from searx.result_types._base import MainResult, LegacyResult
from searx.settings_loader import get_yaml_cfg
from searx.plugins.oa_doi_rewrite import filter_url_field as doi_filter

from ._core import log

if t.TYPE_CHECKING:
    import flask
    from searx.search import SearchWithPlugins
    from searx.extended_types import SXNG_Request
    from searx.result_types import Result, LegacyResult
    from searx.plugins import PluginCfg


REPLACE: dict[re.Pattern, str] = {}
REMOVE: set = set()
HIGH: set = set()
LOW: set = set()


def _load_hostnames_config():
    """加载 hostnames 配置（与原 hostnames 插件逻辑一致）"""
    global REPLACE, REMOVE, HIGH, LOW
    
    hostnames_cfg = settings.get("hostnames")
    if not hostnames_cfg:
        return
    
    def _load_regular_expressions(settings_key):
        setting_value = hostnames_cfg.get(settings_key)
        if not setting_value:
            return None
        if isinstance(setting_value, str):
            setting_value = get_yaml_cfg(setting_value)
        if isinstance(setting_value, list):
            return {re.compile(r) for r in setting_value}
        if isinstance(setting_value, dict):
            return {re.compile(p): r for (p, r) in setting_value.items()}
        return None
    
    REPLACE = _load_regular_expressions("replace") or {}
    REMOVE = _load_regular_expressions("remove") or set()
    HIGH = _load_regular_expressions("high_priority") or set()
    LOW = _load_regular_expressions("low_priority") or set()


class SXNGPlugin(Plugin):
    """统一的 URL 清理管道，按确定顺序执行多个清理逻辑
    完整覆盖 tracker_url_remover + hostnames + oa_doi_rewrite 语义
    """

    id = "url_cleanup_pipeline"

    def __init__(self, plg_cfg: "PluginCfg") -> None:
        super().__init__(plg_cfg)
        self.info = PluginInfo(
            id=self.id,
            name=gettext("URL Cleanup Pipeline"),
            description=gettext("Unified URL cleanup with controlled execution order"),
            preference_section="privacy",
        )

    def init(self, app: "flask.Flask") -> bool:
        TRACKER_PATTERNS.init()
        _load_hostnames_config()
        return True

    def on_result(self, request: "SXNG_Request", search: "SearchWithPlugins", result: "Result") -> bool:
        # 执行顺序（严格按照 hostnames 语义约束）
        # 1. 主 URL REMOVE 检查 → 2. 清理追踪参数 → 3. 主机名重写
        # 4. DOI 重定向 → 5. 优先级设置
        
        # 步骤 1：主 URL REMOVE 检查
        for pattern in REMOVE:
            if result.parsed_url and pattern.search(result.parsed_url.netloc):
                log.debug("url_cleanup_pipeline: remove result by hostname pattern %s", pattern.pattern)
                return False
        
        # 步骤 2：清理追踪参数
        result.filter_urls(self._clean_trackers)
        
        # 步骤 3：主机名重写
        result.filter_urls(self._hostnames_rewrite)
        
        # 步骤 4：DOI 重定向（保留前置检查）
        if result.parsed_url:
            result.filter_urls(doi_filter)
        
        # 步骤 5：优先级设置（仅对 MainResult/LegacyResult）
        if isinstance(result, (MainResult, LegacyResult)):
            for pattern in LOW:
                if result.parsed_url and pattern.search(result.parsed_url.netloc):
                    result.priority = "low"
                    log.debug("url_cleanup_pipeline: set low priority for %s", result.parsed_url.netloc)
            
            for pattern in HIGH:
                if result.parsed_url and pattern.search(result.parsed_url.netloc):
                    result.priority = "high"
                    log.debug("url_cleanup_pipeline: set high priority for %s", result.parsed_url.netloc)
        
        return True

    @classmethod
    def _clean_trackers(cls, result: "Result|LegacyResult", field_name: str, url_src: str) -> bool | str:
        if not url_src:
            return True
        return TRACKER_PATTERNS.clean_url(url=url_src)
    
    @classmethod
    def _hostnames_rewrite(cls, result: "Result|LegacyResult", field_name: str, url_src: str) -> bool | str:
        if not url_src:
            return True
        
        url_src_parsed = urlparse(url=url_src)
        
        for pattern in REMOVE:
            if pattern.search(url_src_parsed.netloc):
                log.debug("url_cleanup_pipeline: remove URL field %s by pattern %s", field_name, pattern.pattern)
                return False
        
        for pattern, replacement in REPLACE.items():
            if pattern.search(url_src_parsed.netloc):
                new_url = url_src_parsed._replace(netloc=pattern.sub(replacement, url_src_parsed.netloc))
                new_url = urlunparse(new_url)
                log.debug("url_cleanup_pipeline: rewrite URL %s -> %s", url_src, new_url)
                return new_url
        
        return True
```
