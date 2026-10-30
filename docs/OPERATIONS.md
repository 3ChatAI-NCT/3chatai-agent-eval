# 运行配置

本仓库不包含现用生产部署清单、内部服务地址、云资源标识或访问凭据。所有连接地址与秘密均由安装者填写；`.env.example` 中这些字段保持为空。不要将实际 `.env`、Secret 清单、对象内容或采集报告提交到 Git。

## 本地启动

按 [README](../README.zh-CN.md) 安装依赖、复制 `.env.example` 并创建 PostgreSQL 数据库。启动前必须填写 `EVAL_DATABASE_URL`；例如本地开发可自行配置连接到回环地址的测试数据库。`EVAL_STORAGE_BACKEND=local` 使用 `EVAL_OBJECT_ROOT` 目录。

`agent-eval api` 与 `agent-eval worker` 从仓库根目录执行，均会自动迁移数据库。默认 API 监听 `127.0.0.1:8080`。Vite 开发服务器监听回环地址，在 `/agent-eval/` 提供页面并将业务 API 代理到本地后端。

## 配置分组

| 变量 | 含义 |
| --- | --- |
| `EVAL_DATABASE_URL` | PostgreSQL 连接字符串；必填。数据库用户需要执行迁移的权限 |
| `EVAL_ENVIRONMENT`、`EVAL_DEV_IDENTITY` | 开发或生产模式；开发身份会跳过认证，只用于本地回环访问 |
| `EVAL_STORAGE_BACKEND`、`EVAL_OBJECT_ROOT` | `local` 或 `oss`；生产要求 `oss`，本地目录只用于开发 |
| `EVAL_INTERNAL_TENANT_ID`、`EVAL_MEMBER_STAFF_IDS` | 授权的平台租户与成员 JSON 数组；`["*"]` 表示该租户内所有已登录成员 |
| `EVAL_PUBLIC_ORIGIN`、`EVAL_GATEWAY_TOKEN` | HTTPS 页面 Origin 与可信网关共享口令；生产必须配置并隔离 API 的直接访问 |
| `EVAL_CREDENTIAL_KEY` | Fernet 加密密钥，用于服务端保存候选模型 API Key；应持久保存并保护 |
| `EVAL_MODEL_GATEWAY_URL` | 模型网关 API 根地址，按实际协议包含版本前缀 |
| `EVAL_ENTITLEMENT_URL`、`EVAL_ENTITLEMENT_PRODUCT_SOURCE` | 网关租户 key 的授权服务根地址及产品标识 |
| `EVAL_MODEL_MAPPING_URL`、`EVAL_MAPPING_FROM_MODEL` | 模型映射服务根地址及被替换的 Agent 模型名；不提供默认服务或模型配置 |
| `EVAL_JUDGE_MODEL`、`EVAL_ANNOTATION_MODEL` | Judge 默认模型与可选 AI 草稿模型；必须能通过模型网关访问 |
| `EVAL_PLATFORM_BASE_URL` | 3Chat 平台 API 根地址 |
| `EVAL_MEDIA_ALLOWED_HOSTS` | 图片预览允许的精确域名，逗号分隔；空值禁止出站预览，不支持通配符 |
| `EVAL_FEISHU_APP_ID`、`EVAL_FEISHU_APP_SECRET` | 仅用于安装者飞书台账的应用身份 |
| `EVAL_FEISHU_BASE_APP_TOKEN`、`EVAL_FEISHU_BASE_TABLE_ID`、`EVAL_FEISHU_BASE_VIEW_ID` | 空间台账的 Base、数据表与可选视图标识 |
| `EVAL_OSS_ENDPOINT`、`EVAL_OSS_BUCKET`、`EVAL_OSS_PREFIX` | HTTPS OSS 服务地址、私有 Bucket 和独立对象前缀 |
| `EVAL_OSS_ACCESS_KEY_ID`、`EVAL_OSS_ACCESS_KEY_SECRET`、`EVAL_OSS_SECURITY_TOKEN` | OSS 身份、密钥与可选临时令牌；限定到所需 Bucket/前缀 |
| `SLS_ENDPOINT`、`SLS_PROJECT`、`SLS_LOGSTORE` | HTTPS SLS 服务地址与待查询日志资源 |
| `ALIBABA_CLOUD_ACCESS_KEY_ID`、`ALIBABA_CLOUD_ACCESS_KEY_SECRET`、`ALIBABA_CLOUD_SECURITY_TOKEN` | SLS 只读查询身份、密钥与可选临时令牌 |

`EVAL_*` 由 Settings 自动读取根目录 `.env`；SLS 采集器从进程环境读取 `SLS_*` 和 `ALIBABA_CLOUD_*`。使用日志采集时，需通过 shell、容器或部署系统将后两组变量导出到进程环境，仅填写 `.env` 不会自动将它们导出。

Fernet 密钥可在自己的环境生成，不要复制公开示例中的固定字符串作为真实密钥：

```bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

其他并发、超时和留存配置见 [Settings](../src/agent_eval/config.py)。更换加密密钥前应规划已有加密记录的可读性；不要在运行时临时生成并丢弃密钥。

## 接入合同

Judge 与 AI 草稿通过模型网关调用；网关 key 从授权服务按租户查询，没有静态兜底 key。外部候选接口支持平台规定的公网 HTTPS 模型协议，不等同于任意 OpenAI 兼容服务都可直接使用。

空间台账需要名称、源空间 ID、基准空间 ID、状态、环境、源空间 AccessToken、基准空间 AccessToken 等现有字段合同。准确字段名和校验逻辑见 [registry.py](../src/agent_eval/registry.py)。只有通过校验且来源与 EVAL 空间及凭据均独立的登记项才可用。凭据按任务在服务端解析，不复制到公开示例。

SLS 需要符合现有采集器的日志字段合同，真实逐轮执行需要符合平台适配器的 API。安装者应准备独立 EVAL 环境和安全外部依赖；仓库不创建这些资源，也不提供凭据。

## 容器与生产路由

可分别使用根目录和 `frontend/` 的 Dockerfile 构建后端及静态工作台镜像。镜像本身不包含数据库、网关、对象存储或空间配置。

生产部署需要 API、Worker、PostgreSQL、OSS，以及可信认证网关和页面反向代理。后端健康路径为 `/health/live`、`/health/ready`；就绪检查仅验证数据库结构，不代表外部模型、台账或业务场景可用。静态容器只提供页面，由安装者将 `/api/agent-eval/` 路由到受保护 API。

不得在生产启用开发身份。生产 Secret 由安装者自己的密钥系统注入；不要将地址、账号、口令或云资源标识直接写入可公开的镜像、示例或清单。

## 维护命令

```bash
# 同步已配置的空间台账
agent-eval sync-registry

# 检查可清理项，不执行删除
agent-eval retention

# 在核对输出后，显式执行满足条件的数据清理
agent-eval retention --apply
```

外部请求超时可能代表投递状态未知。保留原始状态与证据，由评测人员决定是否另建评测，不把 HTTP 成功、任务完成或模型提出工具调用直接当作业务成功。
