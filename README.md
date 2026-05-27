# API 管理器（类 newapi）

一个用 Python 写的轻量级 API 网关：把多个上游 API 统一收敛到一个本地 OpenAI 兼容入口，支持自动重试、故障切换、上游模型检测，以及 `default` 模型映射。

## 新增：模型检测与 default 映射

现在每个 API 设置页都支持：

- 检测该上游支持的模型（请求该上游的 `/v1/models`）
- 从检测到的模型列表中选择一个真实模型
- 设置一个本地别名，默认就是 `default`

之后你就可以统一请求：

```json
{"model": "default"}
```

程序会在真正转发到某个上游时，自动把 `default` 改写成这个上游已选中的真实模型。

举例：

- 上游 A 选择真实模型 `gpt-4.1`
- 上游 B 选择真实模型 `deepseek-chat`
- 客户端始终请求 `model=default`

那么：

- 命中 A 时，程序自动转成 `gpt-4.1`
- A 故障切到 B 时，程序自动转成 `deepseek-chat`

这就实现了你说的“通过请求 default 模型来请求所有模型”。

严格规则：

- 每个上游只允许使用它配置页里选中的那个真实模型
- 只有当你请求的模型名等于别名（默认 `default`）时，程序才会改写成该上游绑定模型
- 如果你请求的是其他模型名，只有和该上游绑定模型完全一致才允许转发
- 不一致时，程序会直接判定该上游模型不匹配并切换，不会误发到上游

## 故障流程

每个 API 都可以单独配置：

- 当发生以下条件时视为失败：
  - 超时 xx 秒
  - 返回内容为空
  - 返回内容包含关键词
  - 返回 5xx / 408 / 429
  - 连接异常
- 下一步：
  - 先重试 N 次
  - 如果还失败：切换下一个 API
  - 或切换到指定 API（支持按名称、按序号）

## 代理功能

支持给“上游请求”加代理：

- 可选启用
- 协议支持 `http`、`socks5`
- 默认主机 `127.0.0.1`
- 默认端口 `7897`

## OpenAI 兼容调用方式

上游可以填写：

- `https://api.openai.com/v1`
- `https://api.deepseek.com/v1`
- 其他兼容 OpenAI 的地址

客户端改成本地：

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="any")
```

### 固定请求 default

```python
resp = client.chat.completions.create(
    model="default",
    messages=[{"role": "user", "content": "你好"}]
)
```

程序会自动把 `default` 替换为当前上游配置好的真实模型。

## 安装

```bash
pip install -r requirements.txt
```

## 运行

```bash
python main.py
```

## GUI 配置说明

### 1. API 设置页

新增或编辑一个 API 时，可以直接填写：

- 名称
- API URL
- API Key
- 鉴权方式
- 模型设置：
  - default 别名
  - 检测支持模型
  - 选择真实模型
- 故障流程：
  - 当 A：超时 / 空响应 / 关键词
  - 下一步：重试 x 次 / 切换下一个 / 切换指定

### 2. 服务设置

可以设置：

- 本地监听地址和端口
- 默认超时
- 全局故障关键词
- 上游代理开关、协议、主机、端口

## 状态接口

可访问：

```text
http://127.0.0.1:8000/__status
```

查看当前 API 列表、策略、代理状态和模型映射配置。

## 当前限制

- 目前是“故障转移优先级”模式，不是负载均衡
- SSE 流式响应暂未做逐块透传优化
- 模型检测依赖上游实现 `/v1/models`
- `default` 改写只会在请求体是 JSON 且存在 `model` 字段时生效
