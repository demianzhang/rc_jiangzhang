# rc_jiangzhang · API 通知系统

这是“AI Coding 作业：API 通知系统设计与实现”的最小可行实现。

**我的问题理解：业务系统不需要外部 API 的业务返回值，但需要明确的接收确认，并把供应商故障、重试和排障从业务主流程中移出去。** 因此本版聚焦一条可验证的可靠投递链路，不做供应商工作流平台。

## 1. 方案概览

```text
业务系统 ── HTTP 请求 + 幂等键 ──> FastAPI ── 事务提交 ──> SQLite
                                   │                       ↑
                                   └─ 返回 202             │ 领取 / 租约 / 结果
                                                       独立 Worker ── HTTP(S) ──> 供应商
```

采用 **Python + FastAPI + HTTPX + SQLite**。两个进程、一个本地持久化数据库，不需要额外部署消息中间件。

| 关键问题 | 本版决定 |
| --- | --- |
| 什么时候算接收成功？ | 数据库事务提交后才返回 202；202 不代表外部业务执行成功 |
| 如何避免丢掉进程内任务？ | 任务和尝试记录落库，Worker 使用可恢复的租约，不使用内存队列或 FastAPI 后台任务承载可靠性 |
| 如何处理重复？ | 入口以 `Idempotency-Key` 去重；出站 ID 稳定，但供应商的业务幂等仍需其配合 |
| 外部失败怎么办？ | 瞬时错误指数退避，最多 6 次尝试；永久错误或耗尽进入死信，保留历史，排查后人工重投 |
| 为什么不直接用消息队列？ | 当前没有高吞吐、多机高可用指标；SQLite 事务就能统一任务、幂等与状态，避免额外运维和双写一致性问题 |

**投递语义：允许重复的至少一次尝试机制，附带有限重试和死信边界；不是 exactly-once，也不承诺无限期自动送达。**
请求到达供应商后、成功状态落库前崩溃，仍可能重复执行。这是需要承认和管理的边界，而不是靠重试“解决掉”的问题。

## 2. 对照作业要求阅读

| PDF 关注点 | 对应内容 |
| --- | --- |
| 对问题的理解、整体架构 | 本页；[设计说明 §1～2](docs/DESIGN.md) |
| 系统解决什么、不解决什么及原因 | [设计说明 §1](docs/DESIGN.md) |
| 投递语义、失败和长期不可用策略 | [设计说明 §3～4](docs/DESIGN.md) |
| 为什么选当前组件，不用它有什么替代方案 | [设计说明 §5](docs/DESIGN.md) |
| 过度设计与未来演进 | [AI 使用说明](docs/AI_USAGE.md)、[设计说明 §7](docs/DESIGN.md) |
| 最小可行代码与可复现证据 | [核心代码](notify_service)、[测试](tests)、下方一键验收 |
| AI 帮助、未采纳建议、本人决策和原因 | [AI 使用说明](docs/AI_USAGE.md) |

建议先读本页，再看设计说明中的故障窗口表，最后结合测试阅读实现。

## 3. 三分钟复现

环境：Python 3.11+；推荐使用本次验证的 Python 3.12。以下在项目根目录执行。

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt -e .
.\.venv\Scripts\python.exe -m unittest tests.test_end_to_end -v
```

### macOS / Linux

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt -e .
.venv/bin/python -m unittest tests.test_end_to_end -v
```

**这个命令不是 Mock 演示。** 它自动生成临时密钥、选择本地空闲端口、创建临时数据库，启动真实 API / Worker 子进程和本地 HTTP 供应商，并逐项断言：

1. Worker 未启动时，API 仍能接收任务。
2. API 重启后，同一事件返回原通知 ID。
3. 供应商返回 503 后，Worker 自动重试至 204，方法、Body 和 Header 保持正确。
4. Worker 停止期间新任务不丢失，重启后继续执行。
5. 永久 400 进入死信，人工重投保留原 ID 和累计历史。
6. 供应商已收到请求时强杀 Worker；重启后租约到期，原尝试标记 `unknown`，再次投递成功。

成功以 `OK` 结束，退出前清理本次创建的进程和临时数据；不会调用真实第三方接口。
测试使用短退避和 6 秒租约，以便快速验证，**没有修改正常运行的默认配置**。

### 完整测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip check
```

macOS / Linux 使用同一虚拟环境中的 Python 执行上述模块命令。
采用标准库 `unittest`；[GitHub Actions](.github/workflows/tests.yml) 配置了 Windows / Ubuntu + Python 3.12。

本次本地验证：**Windows / Python 3.12.10，43 项测试全部通过，`pip check` 通过**；包含真实进程中断恢复。Linux 云端 CI 尚待推送后验证，不将配置了 CI 等同于已通过 CI。

如需手动调用接口、观察每一步日志或演示不同供应商，见[运行与接口参考](docs/USAGE.md)。启动 API 后可访问 `/docs` 查看 OpenAPI。

## 4. 面试中值得讨论的取舍

- **“6 次”不等于重试时间窗口。** 默认基数 30 秒、指数上限 3600 秒，五段重试等待合计 465～930 秒（约 8～16 分钟），另加请求、排队时间；有效 `Retry-After` 可能延长它。参数是 MVP 初值，不是未经验证的生产 SLO。
- **HTTP 2xx 不等于业务正确。** 本版不解析供应商业务响应；如果供应商用 HTTP 200 表示业务失败，需要后续适配，而不是悄悄改变通用层成功规则。
- **本地事务不能覆盖外部副作用。** token 只能隔离旧 Worker 的本地回写，不能撤销已发出的 HTTP；库存扣减等非幂等接口必须配合业务唯一键或补偿。
- **业务提交前的可靠性不属于本服务。** “订单已提交但未调用通知服务”需要业务方 transactional outbox；本服务从持久化接收成功开始负责。
- **不把 MVP 包装成生产平台。** SQLite 是单机故障域；故障告警、备份恢复、出站网络策略是上线前置条件。按真实瓶颈演进到有界并发、供应商隔离、PostgreSQL，再评估消息队列。

## 5. 代码阅读入口

| 文件 | 职责 |
| --- | --- |
| [api.py](notify_service/api.py) | 提交、查询、死信重投与 HTTP 错误契约 |
| [store.py](notify_service/store.py) | 幂等事务、领取、租约条件更新、尝试历史 |
| [worker.py](notify_service/worker.py) | HTTP 投递、超时、错误分类与退避 |
| [policy.py](notify_service/policy.py) | URL / origin / Header 校验 |
| [test_store.py](tests/test_store.py) | 并发、状态机与崩溃窗口 |
| [test_end_to_end.py](tests/test_end_to_end.py) | 真实进程和网络的可复现验收 |

## 6. 提交说明

按题目要求使用 GitHub 仓库名 **`rc_jiangzhang`**。提交代码、测试、依赖锁定清单和文档；不提交 `.venv`、数据库、真实凭据或本地运行产物。

AI 参与范围与已确认的人工决策如实记录在 [AI 使用说明](docs/AI_USAGE.md)。本仓库没有宣称生产压测、真实供应商联调或跨机高可用已完成。
