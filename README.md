# OCS 自建 AI 题库（精简版）

自建的题库服务，替代 [OCS 网课助手](https://github.com/ocsjs/ocsjs) 默认的收费题库。
自己接 DeepSeek 大模型答题，答案缓存在本地，答一门课约几毛钱。

**只用 Python 标准库，不需要 pip 安装任何包。**

```
题目 ──> 本地 SQLite 缓存 ──命中──> 直接返回（0 费用）
                │未命中
                v
          DeepSeek API ──> 清洗答案 ──> 写入缓存
```

## 快速开始

```bash
# 1. 配置
copy .env.example .env
# 用记事本或 VS Code 打开 .env，填入 DEEPSEEK_API_KEY

# 2. 启动
python run.py
```

或直接双击 `启动.bat`。

## 项目结构

前后端分离，后端只输出 JSON，页面是独立的静态文件。

```
run.py                启动入口：横幅 + 拉起 HTTP 服务
server/               后端
├── config.py           读/写 .env、运行时常量、prompt
├── store.py            SQLite 答案缓存 + 统计
├── llm.py              调模型、拼 prompt、清洗答案
├── matching.py         归一化、Dice 相似度、答案对齐到选项
├── detector.py         非题目识别（课件正文拦截）
├── service.py          一次查询的业务编排
└── api.py              路由 + 静态文件 + /api/status
web/                  前端
├── index.html          页面结构
├── style.css           样式
└── app.js              拉 /api/status 填数据、保存 Key、手动测试
```

依赖方向是单向的，只能从上往下：

```
api -> service -> llm / matching / detector / store -> config
```

`config` 最底层，谁都能用它，它不 import 任何人。这样不会出现循环导入。

**改配置别写 `from .config import API_KEY`**——那是快照，网页上保存新 Key 后不生效，
要用 `config.API_KEY` 这种属性访问。原因写在 `config.py` 的模块注释里。

## 两项关键适配（针对 OCS 的匹配与误抓）

### 1. 答案对齐：避免「选项长得不一样」被跳过

OCS 拿到答案后不是直接采用，而是要过一道四阶段匹配
（源码 `packages/core/src/core/worker/resolvers/single.ts`）：

| 阶段 | 规则 |
|---|---|
| 1 | 归一化后 `答案 === 选项`，或 **`答案.includes(选项)`** |
| 2 | Dice 相似度 **> 0.6** |
| 3 | 答案是单个 `A`/`B`/`C` 字母 |
| 4 | 多片段合并后重试 |

注意阶段 1 的方向是「**答案包含选项**」。所以当选项是「爱因斯坦的相对论」而模型答「相对论」时，
方向相反，相似度仅约 0.33，**OCS 会直接丢弃这道题**——这就是"答案和选项长得不一样就跳过"的原因。

服务端因此加了 `_align_answer_to_options()`，在答案入库前按优先级改造：

1. 答案已能精确匹配某选项 → 原样返回
2. 答案是某选项的子串 → **返回该选项原文**（补全上下文，如「相对论」→「爱因斯坦的相对论」）
3. 相似度已 > 0.6 → 原样返回
4. 找到包含答案且不太长的选项 → 返回选项原文
5. 与某选项有部分重合 → 返回选项标号（走 OCS 阶段 3）
6. 字面完全不沾边 → 取字符重合度最高的选项；相似度极低则保持原样，宁可让 OCS 跳过也不乱选

同时在 prompt 里明确要求"**答案必须是选项原文照抄，禁止同义改写或概括**"。

实测 10/10 通过（用 OCS 官方源码的算法当裁判）。

### 2. 非题目拦截：避免把 PPT 正文当题目搜

OCS 的 `buildAnswererEnv` 里有这么一句：

```ts
const env = { type: ..., title: params.title, options };
```

**标题文本原样透传，不做任何过滤**。所以课件页/说明页上选择器抓到的 PPT 正文，
会被整段传给题库，服务端只能对着大段课件问模型，既浪费额度又答非所问。

服务端加了 `_looks_like_question()` 提前拦截，命中则返回 `code=0`（OCS 会回退其他题库）：

| 规则 | 说明 |
|---|---|
| 选择/判断题必须有 ≥2 个选项 | 目录项「4.1 条件语句」被抓时通常不带选项 |
| 文本长度 ≤ 120 字 | 真实题目很短，超长基本是正文段落 |
| 行数 ≤ 3 | 多行基本是课件正文 |
| 不命中课件关键词 | 「本章」「教学目标」「课件」「主讲人」「谢谢观看」等 |
| 填空题须以 `___`/`。`/`？` 等结尾 | 格式明显不符的是说明文字 |

控制台新增「**非题目跳过**」计数，可以直观看到拦了多少。

实测 15/15 通过：6 个真实题目全部放行，9 个课件正文全部拦下。

> 说明：OCS 侧的元素选择器行为无法从服务端改变，这里是**在服务端兜底**。
> 如果仍有漏网，把被抓到的文本特征补充进 `_NON_QUESTION_HINTS` 即可。

## 接入 OCS

启动后，在 OCS 网课助手的设置里，题库地址填：

```
http://127.0.0.1:8000/query
```

若在 `.env` 里设置了 `API_TOKEN`，OCS 的题库设置里也要填同一个值。

> 服务同时支持 GET 和 POST，OCS 用哪种都能正常工作。

### 一键复制配置（推荐）

打开控制台 `http://127.0.0.1:8000/`，在「接入 OCS」区块点**复制**按钮，
会得到下面这段 JSON，粘贴到 OCS 设置的「题库配置填写/修改区」保存即可：

```json
[
  {
    "url": "http://127.0.0.1:8000/query",
    "name": "本地AI题库",
    "method": "get",
    "responseType": "json",
    "type": "GM_xmlhttpRequest",
    "data": {
      "title": "${title}",
      "options": "${options}",
      "type": "${type}"
    },
    "handler": "return (res)=> res.code === 1 ? [res.data.question, res.data.answer] : undefined"
  }
]
```

### 三个字段为什么这样写

按 [OCS 官方文档](https://docs.ocsjs.com/docs/other/api) 和
[源码 `answer.wrapper.handler.ts`](https://github.com/ocsjs/ocsjs/blob/5.0/packages/core/src/core/answer-wrapper/answer.wrapper.handler.ts) 逐一确认：

| 字段 | 值 | 原因 |
|---|---|---|
| `type` | `GM_xmlhttpRequest` | **最关键**。默认的 `fetch` 受浏览器同源策略限制，OCS 页面在 `chaoxing.com` 等域名下时请求不到 `127.0.0.1`。必须用油猴的跨域 API。 |
| `responseType` | `json` | 源码第 154 行实际读取的是 `responseType`，`contentType` 只是历史遗留的兜底。显式写 `responseType` 更稳妥。 |
| `handler` | 见上 | OCS 用 `Function(handler)()` 构造并立即调用，传入请求响应。我的服务返回 `{code, data:{question, answer}}`，所以取出 `[question, answer]` 即可。 |

另外两点：

- **多选题答案用 `#` 连接**。OCS 要求多选题答案必须是特殊符号分隔的字符串而非数组，
  服务端的 `_clean()` 已统一处理成 `#` 分隔，直接满足要求。
- **`data` 里的 `${title}` 等占位符**由 OCS 替换成真实题目；token 则直接拼在 `url`
  查询串上（`resolvePlaceHolder` 只替换占位符，静态值原样保留，且 GET 请求时
  `data` 会覆盖 URL 同名参数，故 token 不放 `data` 里）。

### 配好后怎么验证

1. 控制台「手动测试」框输一道题 → 能出答案，说明 Key 和服务正常
2. 看控制台「运行数据」的**累计查询**有没有涨 → 涨了说明 OCS 真的在请求你的服务
3. **缓存命中**跟着涨 → 重复题目在走缓存，没在重复花钱

如果第 2 步数字不涨，说明 OCS 没连上，问题在跨域或服务地址，不在答题逻辑。

## 网页控制台

浏览器打开 `http://127.0.0.1:8000/`，可以看到：

- 服务状态、当前模型
- 累计查询 / 缓存命中 / 实际调用 API / 已缓存题目 / 非题目跳过 / 失败次数
- 一个手动测试框，可自己输题目验证答案质量

页面是 `web/` 下的三个静态文件，数据由 `GET /api/status` 一次取回。
后端不拼 HTML，所以改样式只动 `style.css`，改布局只动 `index.html`。

## 接口

### `GET/POST /query`

| 参数 | 说明 |
|---|---|
| `title` | 题目文本 |
| `options` | 选项，每行一个 |
| `type` | `single` 单选 / `multiple` 多选 / `judgement` 判断 / `completion` 填空，也兼容数字 `1`~`4` |
| `token` | 访问令牌（设置了 `API_TOKEN` 时必填） |

```bash
curl "http://127.0.0.1:8000/query?title=%E9%B8%A6%E7%89%87%E6%88%98%E4%BA%89%E5%8F%91%E7%94%9F%E5%9C%A8%E5%93%AA%E4%B8%80%E5%B9%B4%EF%BC%9F&options=A.%201840%E5%B9%B4%0AB.%201842%E5%B9%B4&type=single"
```

```json
{"code": 1, "data": {"question": "鸦片战争发生在哪一年？", "answer": "1840年"}, "message": "请求成功"}
```

`code=0` 表示未答上，OCS 会回退到其他题库，不会中断刷课。

### `GET /health`

健康检查，返回 `{"status":"ok","model":"...","has_key":true}`。

### `GET /stats`

查询统计，JSON 格式。

### `GET /api/status`

控制台用的汇总：是否就绪、模型、端口、令牌状态、脱敏 Key、六个统计数，
以及可直接粘贴到 OCS 的配置 JSON（`ocs.url` 和 `ocs.config`）。

### `POST /api/config`

网页上保存配置。请求体：

```json
{"api_key": "sk-...", "base_url": "https://api.deepseek.com/v1", "token": ""}
```

写回 `.env`（先备份成 `.env.bak`），立即生效不用重启，并顺手发一次真实请求测试连通性。
返回 `{"code":1,"tested":true,"message":"已保存"}`。

## 成本

按 [DeepSeek 官方定价](https://api-docs.deepseek.com/quick_start/pricing)：

- `deepseek-chat` 输入约 0.27 元/百万 token，输出约 1.1 元/百万 token
- 一门课的题目总量通常几千 token，**答完一门课约几毛钱**
- 本地缓存命中后完全不花钱，重复遇到的题目直接返回

## 注意事项

- **建议设置 `API_TOKEN`**。服务默认监听 `0.0.0.0`，同局域网的人知道你 IP 就能盗用额度。
- `.env` 含 API Key，不要提交到公开仓库。`.gitignore` 已经把 `.env`、`.env.bak`、
  `cache.db`、`__pycache__/`、`.workbuddy/` 全部排除。
- 已内置代理绕过（`ProxyHandler({})`），本机装有代理软件也不会影响请求。
- 端口被占用时，改 `.env` 里的 `PORT` 即可。

## 与完整版的区别

| | 精简版（本目录） | 完整版（上级目录） |
|---|---|---|
| 依赖 | 零依赖，纯标准库 | FastAPI + openai + pillow 等 |
| 自动答题 | ✅ | ✅ |
| 手写模拟 | ❌ | ✅ |
| 图片 OCR 答题 | ❌ | ✅ |
| 网页控制台 | ✅ 内置 | ✅ |
| 适用场景 | 只想自动答题 | 需要手写作答 |
