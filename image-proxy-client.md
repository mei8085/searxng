# SearXNG 图片代理最后一公里：客户端图片显示链路深入分析

代理 URL 生成后进入浏览器，还有一整套客户端逻辑保证图片稳定显示。本文从前端源码层面完整拆解这条链路。

---

## 一、模板层：两个 img 标签的分工

### 1.1 图片搜索结果模板结构

[images.html](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/templates/simple/result_templates/images.html)

每个图片搜索结果包含 **两个** `<img>` 标签，各司其职：

```html
<article class="result result-images">
  <!-- 第 1 个 img：缩略图（始终加载） -->
  <a href="{{ result.img_src }}">
    <img class="image_thumbnail"
         src="{% if result.thumbnail_src %}
                {{ image_proxify(result.thumbnail_src) }}
              {% else %}
                {{ image_proxify(result.img_src) }}
              {% endif %}"
         alt="{{ result.title|striptags }}"
         loading="lazy"
         width="200" height="200">
  </a>

  <!-- 第 2 个 img：详情大图（延迟加载） -->
  <div class="detail">
    <a class="result-images-source" href="{{ result.img_src }}">
      <img src=""
           data-src="{{ image_proxify(result.img_src) }}"
           alt="{{ result.title|striptags }}">
    </a>
    <!-- 元数据标签省略 -->
  </div>
</article>
```

### 1.2 两个 img 的分工

| 属性 | 缩略图 `image_thumbnail` | 详情大图 `result-images-source img` |
|------|--------------------------|--------------------------------------|
| **CSS 类** | `.image_thumbnail` | `.result-images-source img` |
| **src** | 立即设置（代理 URL） | 初始为空 `src=""` |
| **data-src** | 无 | 存储代理 URL |
| **loading** | `lazy`（浏览器原生懒加载） | 无（由 JS 手动控制） |
| **尺寸** | `width="200" height="200"` 固定 | CSS `object-fit: contain`，自适应 |
| **加载时机** | 页面渲染时 | 用户点击打开详情时 |
| **图片源** | `thumbnail_src`（优先）或 `img_src` | 始终 `img_src`（原图） |
| **可见性** | 网格中始终可见 | 详情面板打开时才可见 |

### 1.3 缩略图的图片源选择逻辑

```jinja2
{% if result.thumbnail_src %}
  {{ image_proxify(result.thumbnail_src) }}
{% else %}
  {{ image_proxify(result.img_src) }}
{% endif %}
```

- 如果搜索引擎返回了专用缩略图 URL（`thumbnail_src`），优先使用
- 否则降级使用原图 URL（`img_src`）作为缩略图
- 无论哪种，都经过 `image_proxify()` 代理

---

## 二、缩略图的双层 Fallback 机制

### 2.1 初始化阶段：`naturalWidth === 0` 检测

[results.ts L38-L48](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/client/simple/src/js/main/results.ts#L38-L48)

```typescript
const imageThumbnails: NodeListOf<HTMLImageElement> =
  document.querySelectorAll<HTMLImageElement>("#urls img.image_thumbnail");
for (const thumbnail of imageThumbnails) {
  if (thumbnail.complete && thumbnail.naturalWidth === 0) {
    thumbnail.src = `${settings.theme_static_path}/img/img_load_error.svg`;
  }

  thumbnail.onerror = (): void => {
    thumbnail.src = `${settings.theme_static_path}/img/img_load_error.svg`;
  };
}
```

**双层 fallback 的两层防线**：

| 层级 | 检测时机 | 检测条件 | 处理方式 |
|------|---------|---------|---------|
| **第 1 层** | 页面加载后立即 | `thumbnail.complete && thumbnail.naturalWidth === 0` | 替换为错误占位 SVG |
| **第 2 层** | 后续任意时刻 | `onerror` 事件触发 | 替换为错误占位 SVG |

### 2.2 `naturalWidth === 0` 的判断时机与原理

**为什么用 `naturalWidth` 而非其他属性？**

`naturalWidth` 是 HTMLImageElement 的只读属性，表示图片的**固有宽度**（即图片文件的实际像素宽度）：

| 状态 | `complete` | `naturalWidth` | 含义 |
|------|-----------|----------------|------|
| 尚未开始加载 | `false` | `0` | 等待中 |
| 正在加载中 | `false` | `0` | 等待中 |
| 加载成功 | `true` | `> 0`（实际宽度） | 正常 |
| 加载失败 | `true` | `0` | **失败** |
| `src=""` 空 | `true` | `0` | 无图 |

**`complete && naturalWidth === 0` 组合的含义**：图片已经完成了加载过程（无论成功或失败），但实际像素宽度为 0，说明**加载失败**。

**为什么需要第 1 层？**

- 浏览器可能在 JS 执行前就已经尝试加载图片并失败了（缓存、网络错误等）
- 此时 `onerror` 事件已经触发过，不会再触发第二次
- `complete && naturalWidth === 0` 检查能捕获这类"已经失败但事件已过"的情况

**判断时机**：这段代码在模块顶层执行，属于 `results.ts` 的初始化逻辑。由于 `results.ts` 在 `index.ts` 中被 `import.meta.glob` eager 加载，所以**在页面 DOM 就绪后立即执行**。

### 2.3 错误占位图

[img_load_error.svg](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/client/simple/src/brand/img_load_error.svg)

SVG 内容是一个带斜杠的圆圈（类似"禁止"符号），中间有一个问号，灰色配色。传达"图片无法加载"的语义。

**占位图路径的动态获取**：

```typescript
`${settings.theme_static_path}/img/img_load_error.svg`
```

[webapp.py L378](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/searx/webapp.py#L378)

```python
'theme_static_path': custom_url_for('static', filename='themes/simple'),
```

`theme_static_path` 由后端注入，通过 `<script client_settings="...">` 传递给前端。路径类似 `/static/themes/simple`，拼接后得到 `/static/themes/simple/img/img_load_error.svg`。

---

## 三、详情大图的 `data-src` 懒加载与一秒防抖

### 3.1 `imageLoader` 函数完整逻辑

[results.ts L9-L36](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/client/simple/src/js/main/results.ts#L9-L36)

```typescript
const imageLoader = (resultElement: HTMLElement): void => {
  // 步骤 0：清除上一次的防抖定时器
  if (imgTimeoutID) clearTimeout(imgTimeoutID);

  const imgElement = resultElement.querySelector<HTMLImageElement>(".result-images-source img");
  if (!imgElement) return;

  // 步骤 1：获取缩略图作为占位
  const thumbnail = resultElement.querySelector<HTMLImageElement>(".image_thumbnail");
  if (thumbnail) {
    // 步骤 1a：如果缩略图已经是错误占位，直接放弃
    if (thumbnail.src === `${settings.theme_static_path}/img/img_load_error.svg`) return;

    // 步骤 1b：大图加载失败时，回退到缩略图
    imgElement.onerror = (): void => {
      imgElement.src = thumbnail.src;
    };

    // 步骤 1c：先用缩略图填充大图区域
    imgElement.src = thumbnail.src;
  }

  // 步骤 2：读取 data-src（代理 URL）
  const imgSource = imgElement.getAttribute("data-src");
  if (!imgSource) return;

  // 步骤 3：1 秒防抖后加载大图
  imgTimeoutID = setTimeout(() => {
    imgElement.src = imgSource;
    imgElement.removeAttribute("data-src");
  }, 1000) as unknown as number;
};
```

### 3.2 完整的加载时序

```
用户点击图片缩略图
    │
    ▼
selectImage(resultElement)              [results.ts L54]
    │
    ├─ #results 添加 "image-detail-open" 类    → CSS 显示详情面板
    ├─ window.location.hash = "#image-viewer"  → 浏览器历史记录
    ├─ scrollPageToSelected()                  → 滚动到选中项
    │
    └─ imageLoader(resultElement)              [results.ts L9]
         │
         ├─ clearTimeout(imgTimeoutID)          → 取消上一个图片的防抖
         │
         ├─ 找到 .result-images-source img     → 详情大图元素
         │
         ├─ 找到 .image_thumbnail              → 缩略图元素
         │   ├─ 缩略图是错误占位？ → return（放弃加载大图）
         │   ├─ 设置 onerror fallback           → 大图失败时回退缩略图
         │   └─ imgElement.src = thumbnail.src  → 立即显示缩略图
         │
         ├─ 读取 data-src                      → 代理 URL
         │
         └─ setTimeout(1000)                   → 1秒防抖
              ├─ imgElement.src = imgSource     → 加载大图
              └─ removeAttribute("data-src")    → 清除 data-src
```

### 3.3 一秒防抖的设计意图

**为什么延迟 1 秒才加载大图？**

1. **避免快速连续点击的重复请求**：用户在图片网格中快速浏览时，可能连续点击多张图片。防抖确保只有最后一张图片被加载，中间的点击被取消（`clearTimeout`）。

2. **减少不必要的带宽消耗**：每张大图可能数百 KB 到 5MB。如果用户 1 秒内点击了 3 张图片，没有防抖会同时发起 3 个大图请求；有防抖只会发起 1 个。

3. **视觉体验优化**：
   - 前 1 秒：显示缩略图（低分辨率，已有缓存），用户能立刻看到内容
   - 1 秒后：如果用户仍停留在此图片，才开始加载高清大图
   - 如果用户在 1 秒内切换了图片，取消前一张的加载，新图片的 1 秒倒计时重新开始

4. **CSS 加载动画配合**：在 1 秒等待期间，详情面板顶部有一个旋转的 `.loader` CSS 动画（[detail.less L198-L206](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/client/simple/src/less/detail.less#L198-L206)），视觉上告诉用户"正在加载"。

### 3.4 `clearTimeout` 的跨结果状态管理

```typescript
let imgTimeoutID: number;  // 模块级变量

const imageLoader = (resultElement: HTMLElement): void => {
  if (imgTimeoutID) clearTimeout(imgTimeoutID);  // 取消上一个
  // ...
  imgTimeoutID = setTimeout(() => { ... }, 1000);  // 设置新的
};
```

`imgTimeoutID` 是模块顶层变量，在所有 `imageLoader` 调用间共享。这保证了：
- 用户快速点击图片 A → 图片 B → 图片 C
- A 的 1 秒定时器被 B 的调用取消
- B 的 1 秒定时器被 C 的调用取消
- 只有 C 的大图在 1 秒后被加载

---

## 四、大图加载失败时的回退链路

### 4.1 详情大图的三级降级

```
第 1 级：原图 (img_src via data-src)
    │ 加载失败 (onerror)
    ▼
第 2 级：缩略图 (thumbnail_src)
    │ 缩略图本身也是错误占位
    ▼
第 3 级：放弃，不加载
```

**代码逻辑**：

```typescript
// 前置检查：缩略图已经是错误占位 → 直接放弃
if (thumbnail.src === `${settings.theme_static_path}/img/img_load_error.svg`) return;

// onerror fallback：大图失败 → 回退缩略图
imgElement.onerror = (): void => {
  imgElement.src = thumbnail.src;
};

// 立即先用缩略图占位
imgElement.src = thumbnail.src;

// 1 秒后尝试加载大图
setTimeout(() => {
  imgElement.src = imgSource;  // 如果失败，触发 onerror → 回退缩略图
}, 1000);
```

**降级场景分析**：

| 场景 | 缩略图状态 | 大图状态 | 最终显示 |
|------|-----------|---------|---------|
| 正常 | 缩略图加载成功 | 大图加载成功 | 大图 |
| 大图失败 | 缩略图加载成功 | 大图加载失败 | 缩略图（via onerror） |
| 双重失败 | 缩略图已是错误 SVG | — | 放弃（return，不加载大图） |
| 缩略图失败但未被初始检测到 | 后续 onerror 替换为 SVG | 大图加载失败 | 缩略图的 SVG 占位图（因为 onerror 回退到 thumbnail.src） |
| 代理关闭 | 原始外部 URL | 原始外部 URL | 取决于外部服务器是否可达 |

### 4.2 为何详情大图没有错误占位？

缩略图有 `img_load_error.svg` 占位，但详情大图没有。原因：

1. 大图失败时回退到缩略图（`imgElement.src = thumbnail.src`），缩略图本身就可能已经是错误占位
2. 如果缩略图已经是错误占位，`imageLoader` 在开头就直接 `return` 了，根本不会尝试加载大图
3. 因此大图的失败总会"收敛"到缩略图的状态，无需额外占位

---

## 五、详情面板的 CSS 显隐控制

### 5.1 CSS 选择器驱动

[detail.less L12-L14](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/client/simple/src/less/detail.less#L12-L14)

```less
article.result-images .detail {
  display: none;  // 默认隐藏
}
```

[detail.less L16-L28](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/client/simple/src/less/detail.less#L16-L28)

```less
#results.image-detail-open article.result-images[data-vim-selected] .detail {
  display: flex;        // 显示详情
  flex-direction: column;
  position: fixed;
  // ... 固定定位在右侧
}
```

**CSS 显隐条件**（必须同时满足）：
1. `#results` 有 `image-detail-open` 类 → JS 在 `selectImage()` 中添加
2. `article.result-images` 有 `data-vim-selected` 属性 → JS 在 `highlightResult()` 中设置
3. 内部有 `.detail` 子元素

### 5.2 CSS 加载动画

[detail.less L198-L206](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/client/simple/src/less/detail.less#L198-L206)

```less
.loader {
  position: absolute;
  top: 1rem;
  right: 50%;
  border-top: 0.5em solid var(--color-result-detail-loader-border);
  border-right: 0.5em solid var(--color-result-detail-loader-border);
  border-bottom: 0.5em solid var(--color-result-detail-loader-border);
  border-left: 0.5em solid var(--color-result-detail-loader-borderleft);
  // 旋转动画继承自 toolkit.less 的 @keyframes load8
}
```

这个旋转加载器在详情面板打开、大图尚未加载完成时显示。1 秒防抖期间 + 大图传输期间，用户看到缩略图 + 旋转动画。

---

## 六、触发入口：用户交互到 imageLoader 的完整链路

### 6.1 鼠标点击触发

[keyboard.ts L439-L449](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/client/simple/src/js/main/keyboard.ts#L439-L449)

```typescript
listen("click", ".result", function (this: HTMLElement, event: PointerEvent) {
  if (!isElementInDetail(event.target as HTMLElement)) {
    highlightResult(this)(true, true);

    const resultElement = getResultElement(event.target as HTMLElement);

    if (resultElement && isImageResult(resultElement)) {
      event.preventDefault();
      mutable.selectImage?.(resultElement);
    }
  }
});
```

**调用链**：

```
用户点击图片结果
    │
    ▼
click 事件冒泡到 .result
    │
    ├─ 排除：点击在 .detail 内部（关闭/导航按钮）→ 不触发
    │
    ├─ highlightResult(this)(true, true)
    │   └─ 设置 data-vim-selected 属性 → CSS 可显示详情面板
    │
    └─ isImageResult(resultElement) → true
         │
         ├─ event.preventDefault() → 阻止链接默认跳转
         │
         └─ mutable.selectImage?.(resultElement)
              │
              └─ imageLoader(resultElement) → 加载大图
```

### 6.2 键盘焦点触发

[keyboard.ts L453-L471](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/client/simple/src/js/main/keyboard.ts#L453-L471)

```typescript
listen(
  "focus",
  ".result a",
  (event: FocusEvent) => {
    if (!isElementInDetail(event.target as HTMLElement)) {
      const resultElement = getResultElement(event.target as HTMLElement);

      if (resultElement && !resultElement.hasAttribute("data-vim-selected")) {
        highlightResult(resultElement)(true);
      }

      if (resultElement && isImageResult(resultElement)) {
        event.preventDefault();
        mutable.selectImage?.(resultElement);
      }
    }
  },
  { capture: true }
);
```

Tab 键或方向键导航到图片结果时也会触发 `selectImage`，效果与点击相同。

### 6.3 关闭详情面板

[results.ts L73-L83](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/client/simple/src/js/main/results.ts#L73-L83)

```typescript
mutable.closeDetail = (): void => {
  const resultsElement = document.getElementById("results");
  resultsElement?.classList.remove("image-detail-open");

  if (window.location.hash === "#image-viewer") {
    window.history.back();
  }

  mutable.scrollPageToSelected?.();
};
```

关闭触发方式：
- 点击 `.result-detail-close` 按钮
- 浏览器后退按钮（`hashchange` 事件）
- 按 Escape 键

### 6.4 `selectImage` 的 `mutable` 桥接模式

[toolkit.ts L44-L50](file:///d:/fz/0601-1/solo-dogfeeding/code/65-searxng/client/simple/src/js/toolkit.ts#L44-L50)

```typescript
export const mutable = {
  closeDetail: undefined as (() => void) | undefined,
  scrollPageToSelected: undefined as (() => void) | undefined,
  selectImage: undefined as ((resultElement: HTMLElement) => void) | undefined,
  selectNext: undefined as ((openDetailView?: boolean) => void) | undefined,
  selectPrevious: undefined as ((openDetailView?: boolean) => void) | undefined
};
```

`mutable` 是一个共享对象，用于跨模块通信：
- `results.ts` 定义 `mutable.selectImage`、`mutable.closeDetail`
- `keyboard.ts` 调用 `mutable.selectImage?.()`
- 解决了模块间的循环依赖问题：keyboard 不需要直接 import results

---

## 七、完整图片显示链路全景图

```
服务端渲染
    │
    ├─ image_proxify(thumbnail_src / img_src) → 缩略图代理 URL
    ├─ image_proxify(img_src) → 大图代理 URL（放入 data-src）
    │
    ▼
HTML 到达浏览器
    │
    ├─ <img class="image_thumbnail" src="代理URL" loading="lazy">
    │   │
    │   ├─ 浏览器加载代理图片
    │   │   ├─ 成功 → 显示缩略图
    │   │   └─ 失败 → onerror → img_load_error.svg
    │   │
    │   └─ JS 初始化检查: complete && naturalWidth === 0
    │       └─ 已失败但 onerror 已过 → img_load_error.svg
    │
    ├─ <img src="" data-src="代理URL"> (隐藏在 .detail 中)
    │
    ▼
用户交互（点击 / Tab / 方向键）
    │
    ├─ highlightResult() → data-vim-selected → CSS 显示详情面板
    │
    └─ selectImage() → imageLoader()
         │
         ├─ 检查缩略图是否已是错误占位 → 是则 return
         │
         ├─ 设置 onerror fallback: 大图失败 → 缩略图
         │
         ├─ imgElement.src = thumbnail.src  ← 立即显示缩略图占位
         │
         └─ setTimeout(1000)                ← 1秒防抖
              │
              ├─ 被下一次点击取消？ → 什么都不做
              │
              └─ 1秒后执行
                   ├─ imgElement.src = data-src  ← 请求代理大图
                   │   │
                   │   ├─ 代理成功 → 显示大图
                   │   │
                   │   └─ 代理失败 → onerror → 回退缩略图
                   │
                   └─ removeAttribute("data-src")  ← 清理
```

---

## 八、关键设计决策总结

| 设计决策 | 实现方式 | 设计意图 |
|---------|---------|----------|
| **双 img 标签** | 缩略图始终加载 + 大图 data-src 延迟 | 缩略图即时可见，大图按需加载，节省带宽 |
| **缩略图双层 fallback** | `complete && naturalWidth === 0` + `onerror` | 覆盖"已失败"和"将失败"两种时机 |
| **`naturalWidth === 0` 检测** | 只在 `complete === true` 时判断 | 区分"尚未加载"和"加载失败" |
| **1 秒防抖** | `setTimeout(1000)` + 模块级 `imgTimeoutID` | 快速浏览只加载最终停留的图片 |
| **先缩略图后大图** | `imgElement.src = thumbnail.src` → `setTimeout → imgSource` | 用户立即看到内容，大图后台加载 |
| **大图 onerror 回退缩略图** | `imgElement.onerror = () => { imgElement.src = thumbnail.src }` | 大图失败仍有内容显示 |
| **错误占位阻断大图加载** | 缩略图是 SVG 占位时直接 return | 避免对已知不可用的图片源发起无用请求 |
| **CSS 驱动显隐** | `image-detail-open` + `data-vim-selected` | 无需 JS 操作 DOM 显隐，纯 CSS 切换 |
| **`mutable` 桥接** | 共享可变对象 | 解耦 keyboard.ts 与 results.ts 的循环依赖 |
| **loading="lazy" 仅缩略图** | 缩略图用浏览器原生懒加载 | 视口外的缩略图延迟加载，大图由 JS 完全控制 |
