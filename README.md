# 修复承载力情景误用生态指标基础平台

本项目是一套可离线运行的 Python 服务端平台，供国家森林公园管理局、保护站、生态监测人员和巡护队管理野生动植物观察、采样标本、保护站资源、巡护路线、风险告警与处置工单。业务状态、角色权限、幂等结果和审计事件保存在 SQLite 中，可在单个 Linux 应用容器内运行。

## 目录

- src/collection_logistics/：保护站、巡护路线、应急资源、调拨计划和治理情景；
- src/taxonomy_lab/：调查协议、观察记录、异常复核、分析任务租约和生态结论；
- src/biosafety_ops/：园区监测、风险告警、处置工单和资源分配；
- fixtures/：离线验收使用的调查协议与结构化观察记录；
- tests/：领域规则、事务边界、权限、HTTP API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时只依赖 Python 标准库与 SQLite

## 测试

~~~bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
~~~

## 构建检查

~~~bash
python3 -m compileall -q src tests
~~~

## 离线验收

~~~bash
PYTHONPATH=src python3 -m collection_logistics.acceptance --workspace .
PYTHONPATH=src python3 -m taxonomy_lab.acceptance --workspace .
PYTHONPATH=src python3 -m biosafety_ops.acceptance
~~~

三条命令会在临时 SQLite 数据库中完成保护站和路线登记、资源调拨、生态观察分析与风险处置，不访问外部网络。

## HTTP 服务

~~~bash
PYTHONPATH=src python3 -m collection_logistics.api --database park.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m taxonomy_lab.api --database ecology.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m biosafety_ops.api --database safety.sqlite3 --host 127.0.0.1 --port 8082
~~~

服务提供浏览器无关的 JSON 接口和健康检查。进程重启后可以继续读取 SQLite 中的业务状态与审计历史。

## 情景观测输入约束

治理情景在创建时必须通过 `observation` 固定三项输入约束：指标系列 `risk_index`、来源修订
`source_revision` 和适用日期 `duty_date`（支持 `CUSTOM:<名称>` 形式的自定义指标系列）：

~~~json
{"scenario_id": "trail-reopen", "name": "雨后步道开放",
 "risk_index_drop_percent": "0", "route_capacity_changes": {}, "demand_changes": {},
 "observation": {"risk_index": "HUMIDITY", "source_revision": "soil-rev-1", "duty_date": "2026-09-26"}}
~~~

情景经 `POST /scenarios/{id}/approve` 批准后，用 `POST /scenarios/{id}/run` 运行。运行时只读取
与三项约束完全相符的观测版本（同日存在“游客踩踏压力”等其它系列观测也不会被误用）；没有匹配
数据时计算明确停止并写入 `scenario.execution_blocked` 审计事件，绝不回退到“最新可得”指标。

每次运行都会固化完整输入快照（采用的观测记录、选择规则、当时的路线与库存）、`input_sha256`
和结果。未来补录或改写监测记录不会影响旧结果：观测修订只追加新行，旧运行按
`(情景, 观测版本, 输入摘要)` 幂等回放。管理人员可通过：

- `GET /scenarios/runs/{run_id}`：查看一次计算采用了哪条观测（记录号、系列、日期、修订、取值）
  及为何符合情景约束，并取回完整输入快照；
- `POST /scenarios/runs/{run_id}/verify`：仅用该快照重新计算，校验输出与已存结果逐字节一致、
  快照摘要与情景定义摘要均未被篡改。
