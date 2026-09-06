# DeepSearch Agents

> 基于开源项目 [didilili/deepsearch-agents](https://github.com/didilili/deepsearch-agents) 进行的工程化增强与个人实践版本。
>
> 这个项目保留了上游的技术骨架与研究场景，同时在安全、认证、租户隔离、任务控制和事件回放等方面做了进一步完善。
>
> 这里的定位是“基于学习 + 自主改造”，而不是完全从零独立开发，因此我会在说明中明确标注上游来源与作者权益。

## 项目简介

我做这个项目的核心目标，是把一个教学型的多智能体研究系统，往更接近真实工程实践的方向推进。

它的灵感来源于上游的「深度研搜」项目：围绕多智能体协作、网络检索、数据库查询、知识库问答和文件处理这条主线，构建一个能真正完成研究任务的应用。

在这个版本里，我重点关注的是：

- 主智能体如何做任务规划和调度；
- 多个子智能体如何协同完成不同类型的信息搜集；
- 后端如何通过 API 与 WebSocket 对外交付状态和结果；
- 前端如何实时展示任务进度与最终产物；
- 工程层面如何补上安全、稳定性和可验证性的能力。

从个人实践的角度看，这个项目不仅是一个 AI Agent demo，更像是一次把教学项目升级为“工程化应用”的尝试。

## 项目来源与声明

- 原始项目： [didilili/deepsearch-agents](https://github.com/didilili/deepsearch-agents)
- 对应教程： [AI 智能体实战速成指南-大模型入门](https://didilili.github.io/ai-agents-from-zero/#/)
- 对应实战章节： [实战项目-深度研搜](https://didilili.github.io/ai-agents-from-zero/#/%E5%AE%9E%E6%88%98%E9%A1%B9%E7%9B%AE-%E6%B7%B1%E5%BA%A6%E7%A0%94%E6%90%9C/0-%E5%89%8D%E8%A8%80)
- 参考博客： [项目 | wozwiiiii](https://wozwiiiii.github.io/projects/)

这个项目的改造基线是上游开源项目，不是对其进行“无中生有”的改写。我在这里明确说明来源，是为了保证表达真实、尊重原作者，也更符合开源协作中的规范。

## 我做了什么

这个版本相较于上游教学代码，重点补强了几个实际工程问题：

- 路径穿越与文件安全防护；
- SQL 只读校验与危险语句拦截；
- API 认证与租户隔离；
- fail-closed 模式与更稳健的后台任务控制；
- 事件持久化、断线重连与回放能力；
- 任务超时、限流与模型调用预算熔断；
- 更完整的测试覆盖，验证关键行为是否稳定。

换句话说，这个项目的价值不仅在于它能“跑起来”，更在于它尝试解决“真正在工程里会遇到的问题”。

## 项目能力概览

### 1. 多智能体研究链路

```text
用户任务
  -> FastAPI 接收请求
  -> 任务上下文与会话目录初始化
  -> 主智能体分析与规划
  -> 网络搜索 / 数据库 / RAGFlow / 文件读取工具调用
  -> 汇总多来源信息并生成输出
  -> 结果与事件通过 WebSocket 推送给前端
```

### 2. 当前支持的核心能力

- 多智能体任务协作；
- 公开网络检索；
- MySQL 结构化数据查询；
- RAGFlow 知识库问答；
- 用户上传文件解析；
- Markdown / PDF 产物生成；
- 任务状态实时监控与前端联动。

### 3. 个人实践意义

我希望这个项目体现的是：

- 能把 AI Agent 工程主链路跑通；
- 能把框架能力和工程能力结合起来；
- 能在实际代码中感受到系统设计与安全约束的重要性；
- 能把研究型项目进一步推进到更接近“真实应用”的状态。

## 技术栈

| 模块 | 说明 |
| --- | --- |
| 智能体框架 | DeepAgents / LangGraph / LangChain |
| 后端 | FastAPI / Uvicorn |
| 实时通信 | WebSocket |
| 前端 | React / Vite |
| 搜索 | Tavily |
| 结构化数据 | MySQL |
| 知识库 | RAGFlow |
| 文件处理 | PDF / Word / Markdown / 文本解析 |
| 依赖管理 | uv / pnpm |

## 项目结构

```text
deepsearch-agents/
├── app/
│   ├── agent/
│   ├── api/
│   ├── prompt/
│   ├── ragflow/
│   ├── tools/
│   ├── utils/
│   ├── output/
│   └── updated/
├── docker/
├── docs/
├── examples/
├── frontend/
├── tests/
├── .env.example
├── pyproject.toml
├── requirements.txt
├── README.md
├── uv.lock
└── ...
```

## 快速开始

### 1. 准备环境

- Python 3.12
- `uv`
- Docker / Docker Compose
- Node.js / pnpm
- OpenAI 兼容模型 API Key
- Tavily API Key
- 可选：RAGFlow 服务与 API Key

### 2. 安装依赖

```bash
python -m uv sync --group dev
```

### 3. 配置环境变量

```bash
cp .env.example .env
```

示例变量包括：

```bash
OPENAI_BASE_URL=...
OPENAI_API_KEY=...
TAVILY_API_KEY=...
MYSQL_HOST=localhost
MYSQL_PORT=3307
MYSQL_USER=root
MYSQL_PASSWORD=root
MYSQL_DATABASE=deepsearch_db
RAGFLOW_API_URL=http://...
RAGFLOW_API_KEY=...
```

### 4. 启动基础服务

```bash
docker compose -f docker/docker-compose.yaml up -d
```

> 该命令会同时启动 MySQL、Postgres（task 表）与 Redis（队列）三个服务。

**可选：任务队列模式**——默认 `TASK_QUEUE_MODE=inline`（任务在 API 进程内执行，零额外依赖）。设置 `TASK_QUEUE_MODE=redis` 后任务由独立 ARQ worker 执行（服务重启不丢、可水平扩容），需额外启动 worker：

```bash
.venv/Scripts/python -m arq app.queue.worker.WorkerSettings   # Windows
arq app.queue.worker.WorkerSettings                            # Linux/macOS
```

详见 `docs/status/PRODUCTION_NOTES.md` 与 `docs/design/TASK_QUEUE_DESIGN.md`。

### 5. 启动后端

```bash
python -m uv run uvicorn app.api.server:app --host 0.0.0.0 --port 8000 --reload
```

### 6. 启动前端

```bash
cd frontend
pnpm install
pnpm dev
```

## 能力边界

这个项目适合用于：

- 学习多智能体应用架构；
- 理解 Agent / Tool / API / Frontend 的联调方式；
- 做研究型 AI 应用原型和二次开发；
- 练习工程化思维，包括安全、状态和事件处理。

但它也不是一个“覆盖全部企业级治理能力”的完整产品。当前版本仍然有一定边界：

- 分布式任务队列与多节点扩展还需进一步完成；
- 生产监控、日志治理和链路追踪仍是后续工作；
- 对审计、权限、安全扫描和 CI/CD 还需要继续完善；
- 目前它更像是一个有工程实践价值的增强型原型，而不是所有上线约束都齐备的成品。

## 目前的状态

当前项目已经覆盖和验证了以下重点能力：

- 路径安全；
- SQL 只读校验；
- API 安全；
- 认证与租户隔离；
- 限流；
- 检查点持久化；
- 事件回放；
- 任务超时与 token 预算熔断。

这说明它已经从一个单纯学习样例，走到了更偏工程验证和落地思考的阶段，但仍然需要以真实边界和测试结论来衡量。

## 结语

这个项目对我来说，最大的意义在于：

- 它把一个很好的教学项目和真实工程问题结合起来；
- 它让我能够在实际代码里理解 Agent 系统的复杂性；
- 它让我学会把“能跑”与“可维护、可安全、可扩展”区分开来。

我把它定位为：

- 基于上游开源项目的个人研究与工程化实践；
- 在保留原始项目思路和价值的同时，补足工程层面的安全、状态和可验证性；
- 以真实、准确、保留原作者权益的方式表达这个项目。

