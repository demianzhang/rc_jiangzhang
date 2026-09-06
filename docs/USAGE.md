# 运行与接口参考

返回 [README](../README.md) · [设计说明](DESIGN.md) · [配置示例](../.env.example)

## 1. 快速运行

以下命令用于 Windows PowerShell。在项目根目录执行；不需要激活虚拟环境，因此不依赖 PowerShell 的脚本激活策略。

### 安装

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt -e .
```

如果系统使用 Python Launcher，可以将第一行改成 `py -3.12 -m venv .venv`。
`requirements.lock.txt` 固定本次验证的直接与传递依赖；依赖范围在 `pyproject.toml`。

### 终端 A：启动本地模拟供应商

```powershell
.\.venv\Scripts\python.exe -m uvicorn examples.provider:app --host 127.0.0.1 --port 9000
```

模拟路由：

| 路径 | 行为 |
| --- | --- |
| `/success` | 返回 204 |
| `/flaky` | 同一通知前两次返回 503，第三次返回 204 |
| `/always-fail` | 始终返回 503 |
| `/reject` | 返回 400，直接进入死信 |

模拟供应商只用于本地演示，计数器不持久化。

### 终端 B：启动 API

先生成随机密钥：

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))"
```

将输出设置到环境变量。**不要将真实密钥写入仓库。**

```powershell
$env:NOTIFY_API_KEY = "<替换为刚生成的随机密钥>"
$env:NOTIFY_ALLOWED_ORIGINS = "http://127.0.0.1:9000"
$env:NOTIFY_DB_PATH = Join-Path (Get-Location) "data\notifications.sqlite3"
.\.venv\Scripts\python.exe -m uvicorn notify_service.api:create_app --factory --host 127.0.0.1 --port 8000
```

访问：

- 健康检查：`http://127.0.0.1:8000/healthz`
- OpenAPI 交互文档：`http://127.0.0.1:8000/docs`，使用 Authorize 设置 `X-API-Key`

健康检查只检查 API 和数据库可读性，**不代表 Worker 在线或供应商健康**。

### 终端 C：启动 Worker

每个终端的环境变量独立，必须设置相同密钥、数据库路径和白名单：

```powershell
$env:NOTIFY_API_KEY = "<与终端 B 相同的随机密钥>"
$env:NOTIFY_ALLOWED_ORIGINS = "http://127.0.0.1:9000"
$env:NOTIFY_DB_PATH = Join-Path (Get-Location) "data\notifications.sqlite3"
# 仅为本地演示缩短等待；正常配置默认基数 30 秒、上限 3600 秒
$env:NOTIFY_RETRY_BASE_SECONDS = "2"
$env:NOTIFY_RETRY_CAP_SECONDS = "300"
.\.venv\Scripts\python.exe -m notify_service.worker
```

API 和 Worker 必须使用相同的数据库路径与目标白名单；上述重试间隔仅由 Worker 使用。默认 Worker 串行处理；停止 Worker 不会影响 API 接收新任务。用 Ctrl+C 停止进程，未完成的投递会在租约过期后恢复。

### 终端 D：提交并观察自动重试

```powershell
$env:NOTIFY_API_KEY = "<与终端 B 相同的随机密钥>"
.\examples\submit.ps1
```

首次运行预期得到 `pending → succeeded`，历史是 `503、503、204`。
再次运行相同命令会返回同一通知 ID，`deduplicated=true`，不会重复创建任务。
需要新建事件时使用 `.\examples\submit.ps1 -EventId "registration-1002"`。

若本机策略禁止运行脚本，可直接使用下方的 HTTP 调用示例，无需修改系统执行策略。

## 2. HTTP 接口

除健康检查和 OpenAPI 文档外，接口均要求 `X-API-Key`。

### 提交通知

```http
POST /v1/notifications
X-API-Key: <internal-service-key>
Idempotency-Key: crm:subscription-paid:order-2026-001
Content-Type: application/json

{
  "url": "https://crm.example.com/contacts/42",
  "method": "PATCH",
  "headers": {
    "Authorization": "Bearer <supplier-token>",
    "Content-Type": "application/json",
    "Idempotency-Key": "subscription-paid:order-2026-001"
  },
  "body": "{\"status\":\"paid\"}"
}
```

使用真实目标前，需要管理员将 `https://crm.example.com` 加入 `NOTIFY_ALLOWED_ORIGINS`，然后重启 API 和 Worker。

```json
{
  "id": "server-generated-uuid",
  "status": "pending",
  "deduplicated": false
}
```

- 支持 `GET / POST / PUT / PATCH / DELETE`，方法必须大写。
- `body` 是可空的原始 UTF-8 字符串，不会被再次 JSON 序列化。JSON、XML、表单等由调用方构造，并提供对应的 `Content-Type`；未提供时默认为 `application/octet-stream`。
- 不支持任意二进制文件、multipart 构造、动态签名或认证刷新。
- Body 最大 64 KiB（UTF-8 字节）；最多 32 个 Header，名称和值合计不超过 8 KiB；入口原始请求最大 512 KiB。
- URL 最大 2048 字符，仅接受无空白的 ASCII URL；非 ASCII 路径先做百分号编码。
- 调用方负责供应商 Header。`Host`、`Content-Length`、连接相关 Header 等由 HTTP 客户端管理，禁止手动覆盖。
- Header 值不能包含控制字符或首尾空格，避免接收 HTTP 客户端无法实际发出的请求。
- 服务附加稳定的 `X-Notification-Id`，自动重试、人工重投均不改变它。
- **入口** `Idempotency-Key` 用于去重提交，不会自动成为供应商的 `Idempotency-Key`；供应商支持幂等键时，应在嵌套的 `headers` 中显式传入。

入口幂等规则：

| 场景 | 结果 |
| --- | --- |
| 新 key | 事务提交后返回 202 |
| 同 key + 相同规范化请求 | 202，返回原 ID 和当前状态 |
| 同 key + 不同请求 | 409 |
| 没有 key / 格式非法 | 422 |
| 存储操作失败 | 503，不伪装为接收成功 |

幂等比较涵盖 URL、方法、Header、Body。Header 名忽略大小写；Body 按字符串比较，因此不同空白的 JSON 仍可能冲突。
本版只支持一个可信内部租户，共享 key 空间，建议使用“业务系统:事件类型:事件ID”命名。
幂等记录随任务保留，没有自动过期；重复提交已经死信的任务不会自动重投。

### 查询、死信列表和人工重投

```powershell
$headers = @{ "X-API-Key" = $env:NOTIFY_API_KEY }
$base = "http://127.0.0.1:8000"

# 替换为实际通知 ID
$id = "<notification-id>"
Invoke-RestMethod "$base/v1/notifications/$id" -Headers $headers
Invoke-RestMethod "$base/v1/notifications?status=dead&limit=20" -Headers $headers
Invoke-RestMethod "$base/v1/notifications/$id/redrive" -Method Post -Headers $headers
```

- 状态：`pending / in_flight / succeeded / dead`。
- 时间字段使用 Unix 秒（UTC）；`attempts` 是本轮领取次数，`total_attempts` 包含历史重投轮次。
- `history` 返回最近 100 次尝试，按尝试序号升序；完整历史仍在数据库中。
- 列表返回最近任务，默认 20 条，最多 100 条，可按状态筛选；MVP 不提供全量分页导出。
- 重投仅允许 `dead`，返回 202；其他状态返回 409，不存在返回 404。
- 重投重置本轮计数，但保留原 ID、请求内容、入口幂等键、最大尝试次数和历史记录。
- 重投是运维操作，不提供独立的操作幂等键；响应不确定时先查询状态和尝试历史，不要自动无限重发重投命令。
- 若请求内容本身错误，应修正内容后使用**新的事件 key** 提交，并先评估重复业务操作风险。
- 查询不返回供应商 Token、URL 查询参数、请求 Body 或响应 Body。

### 不运行脚本时的 PowerShell 提交示例

```powershell
$headers = @{
    "X-API-Key" = $env:NOTIFY_API_KEY
    "Idempotency-Key" = "demo:registration:1001"
}
$payload = @{
    url = "http://127.0.0.1:9000/flaky"
    method = "POST"
    headers = @{ "Content-Type" = "application/json" }
    body = '{"user_id":1001}'
} | ConvertTo-Json -Depth 4
Invoke-RestMethod "http://127.0.0.1:8000/v1/notifications" -Method Post `
    -Headers $headers -ContentType "application/json" -Body $payload
```

## 3. 配置

通过环境变量配置，`.env.example` 仅作参考，**不会自动加载**。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `NOTIFY_API_KEY` | 无，必填 | 至少 32 个非空白可打印 ASCII 字符，生产使用随机密钥 |
| `NOTIFY_ALLOWED_ORIGINS` | 无，必填 | 逗号分隔的精确 `scheme://host[:port]`；不接受路径或通配符 |
| `NOTIFY_DB_PATH` | `data\notifications.sqlite3` | API 和 Worker 使用同一个本地文件 |
| `NOTIFY_MAX_ATTEMPTS` | 6 | 首次尝试计入；范围 1～100；任务创建时固定 |
| `NOTIFY_ATTEMPT_TIMEOUT_SECONDS` | 10 | 单次请求总时限，并配置各 I/O 时限 |
| `NOTIFY_LEASE_SECONDS` | 30 | 必须至少为总时限 + 5 秒 |
| `NOTIFY_POLL_SECONDS` | 0.5 | 没有到期任务时的轮询间隔 |
| `NOTIFY_RETRY_BASE_SECONDS` | 30 | 指数退避基数；演示可覆盖为 2 |
| `NOTIFY_RETRY_CAP_SECONDS` | 3600 | 指数退避上限 |
| `NOTIFY_RETRY_AFTER_CAP_SECONDS` | 3600 | 供应商建议等待时间上限 |

配置非法时启动失败，不使用无鉴权、全网开放等隐式默认值。

## 4. 验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip check
```

测试使用标准库 `unittest`，不额外引入测试框架。
包含数据库事务与并发测试、FastAPI 接口测试、HTTPX 故障模拟，以及启动**真实 API 进程、Worker 进程、本地 HTTP 供应商**的端到端测试。

重点覆盖：

- 只有落库成功才返回 202；API 重启后幂等记录仍然存在。
- 多个并发提交只有一个任务，多个 Worker 不重复领取有效租约。
- 请求方法、Body、Header 真实发送，503 后自动重试成功。
- 过期租约恢复、旧 token 不能回写、供应商已接受但本地未确认时可能重复投递。
- HTTP 投递途中强杀 Worker，重启后实际等待租约到期，再以原 ID 投递成功。
- 等待数据库写锁不能缩短新租约，也不能让旧时间戳绕过过期校验。
- 超时、429/5xx、永久 4xx、重试耗尽、死信重投和历史保留。
- 不跟随重定向、不共享供应商 Cookie、超大请求和非法目标拒绝。

### 常见运行问题

- **未配置 Key 或白名单导致启动失败**：这是预期的安全默认值；设置进程环境变量，`.env.example` 不会自动加载。
- **任务一直 pending**：先确认 Worker 在线，API 与 Worker 数据库路径一致，再查看 `next_attempt_at`；正常配置不是秒级连续重试。
- **PowerShell 拒绝执行脚本**：使用本页提供的直接 HTTP 命令或 README 的 Python 一键验收，不必修改系统执行策略。
- **测试出现 Starlette/HTTPX 弃用提示**：当前锁定组合的兼容提示，不是失败；未为此额外引入第二套 HTTP 客户端，依赖升级时单独迁移。
- **死信原样重投仍失败**：鉴权、URL、Body 错误不会因为重试自行修复；先定位原因。签名过期的请求需要按供应商要求重新构造。

生产运行边界见[设计说明](DESIGN.md)，本版不承诺单机永久损坏后的自动恢复。
