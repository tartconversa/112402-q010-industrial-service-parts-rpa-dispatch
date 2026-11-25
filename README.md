# 设备备件目录采集与保修服务派单

采集授权经销商库存、厂商备件目录和服务工单回执，协助客服在经销商门户预留库存并派单。
库存数量是**查询时点快照**；系统不会把过期快照、未确认的兼容关系或结果不明的预留
当成派单依据。

All example identifiers and values are synthetic. CSV files use UTF-8, the first row
contains column names, and an empty cell means the source supplied no value.

## 快速开始

```bash
# 用样例数据跑一遍：采集 → 三类确认 → 预留 → 派单 → 复核
python3 -m spare_service.cli demo

# 常驻采集（每 5 分钟读一次 samples/sources.json 中列出的来源）
python3 -m spare_service.cli --db data.db watch --config samples/sources.json --interval 300

# 客服工作台 / 单张工单的依据与回执
python3 -m spare_service.cli --db data.db board
python3 -m spare_service.cli --db data.db explain T1

# 测试
python3 -m unittest discover -s tests -v
```

Python 3.11+ 标准库即可运行，无第三方依赖。

## 业务规则如何落到代码

| 要求 | 实现 |
| --- | --- |
| 库存带查询时间与来源 | `inventory_snapshots` 每条都存 `checked_at`、`source`、`ingested_at`，`Inventory.select` 返回被选中的来源和所有候选的排除理由 |
| 库存可能已过期 | `clock.assess` 按**查询时间**判断新鲜度（默认 30 分钟），时钟晚于当前时间也拒绝 |
| 目录修订保留有效版本 | `catalog_revisions` 不覆盖：新修订关闭旧版本（`valid_to`），历史全部可查；同一修订号内容不同直接拒绝 |
| 替代件只适用某个固件 | 兼容性同时校验备件工程师确认的适用固件范围、现场已确认固件、厂商目录中的范围和替代方向 |
| 现场配置由服务人员回填 | `Service.set_site_config` 只记录"上报值"，未确认前不参与兼容判断 |
| 三类确认各司其职 | 角色校验：`asset_admin`、`warranty_specialist`、`part_engineer`；缺少任一确认只给待核选项 |
| 有库存 ≠ 兼容 | 兼容门禁未通过时，库存门禁不会给出可派单来源 |
| 派单前四项检查 | 保修、兼容、库存新鲜度、预约时段 + 门户预留回执，任何一项不过都不能派单 |
| 预留结果不明先查询 | 超时/响应不可解析 → `pending`；`retry` 强制先 `reconcile`，确认门户没有预留才重发 |
| 幂等 | 每个 `(ticket, partner, item)` 一个 `request_key`，门户按它去重 |
| 取消释放库存 | `release_on_cancel` 释放仍持有的预留；已发货的不释放，并在结果里明确标出 |
| 已发货不算未使用库存 | `consumed` 预留不参与释放，且计入库存占用 |
| 目录撤回/配置更正 | 未派出 → `needs_recheck` 并重新评估；已派出 → `supervisor_review` 交服务主管 |
| 客服可追溯 | `dispatch_checks` 保存每次结论，`events` 保存确认、预留、对账、派单全过程；`explain` 汇总展示 |

### 派单结论的取值

- `ready`：四项检查 + 门户预留回执全部通过，可以派单
- `reserve_needed`：四项检查通过，但还没有拿到门户预留回执（先去预留）
- `stale`：其余都通过，只是库存快照过期（先去重新查询经销商库存）
- `pending`：缺少确认或必要输入，只给出待核选项
- `blocked`：有明确阻断（保修不覆盖、停产、固件不匹配、预约过期等）

## 目录结构

```
spare_service/
  db.py            SQLite 存储与事务（线程本地连接，WAL）
  ingest.py        CSV 解析与校验（空值不猜测）
  catalog.py       目录修订历史、有效版本、替代关系撤回
  inventory.py     库存快照选择、新鲜度、预留占用
  clock.py         业务时钟与新鲜度判断
  versions.py      修订号/固件版本比较
  tickets.py       工单、现场配置、三类确认
  evaluation.py    四项门禁 + 预留回执，生成客服可读依据
  reservations.py  门户预留/对账/重试/释放/发货
  receipts.py      服务工单回执采集（保留原始状态，不直接驱动派单）
  portal.py        门户适配接口 + 内存演示实现
  scheduler.py     定时采集，单个来源失败不影响其它来源
  service.py       面向上层的服务门面
  cli.py           命令行入口
samples/
  inventory.csv compatibility.csv tickets.csv sources.json
tests/
```

### 接入真实经销商门户

实现 `spare_service.portal.PortalAdapter` 的 `reserve` / `query` / `release` / `shipment`
四个方法（HTTP 客户端或浏览器自动化均可），再通过 `Service(portal=...)` 注入。

- `reserve` 必须带 `request_key` 幂等；响应用 `{"state": "held"|"conflict"|
  "out_of_stock"..., "portal_ref": ...}` 表达，无法识别的响应会被当作"结果不明"。
- `query` 返回门户当前预留状态：`held` / `consumed` / `released` / `none`。
- `shipment` 返回 `shipped` / `pending` / `unknown`；`unknown` 不会被乐观当成未发货。

## 已知边界

- 演示门户 `DemoPortal` 只存在内存中，进程退出后预留回执丢失；生产必须接入真实门户。
- 调度器读取的是已落盘的 CSV/文件来源；对接门户 API 时在 `Scheduler` 前加一层抓取即可。
- 没有做用户登录/鉴权，角色以调用参数传入；生产应由网关注入可信身份。
