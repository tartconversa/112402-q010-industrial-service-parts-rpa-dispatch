# 设备备件目录采集与保修服务派单

合成设备服务库存、厂商备件适配及保修派单样例。库存数量是查询时点快照。

All example identifiers and values are synthetic. CSV files use UTF-8, the first row contains column names, and an empty cell means the source supplied no value.

## Sample files

- `inventory.csv` contains 经销商可用库存与查询时间。 Columns are `stock`, `partner`, `item`, `quantity`, `checked`.
- `compatibility.csv` contains 备件、设备配置和有效固件之间的厂商适配范围。 Columns are `part`, `model`, `firmware_from`, `firmware_to`, `replacement`, `revision`.
- `tickets.csv` contains 报修、保修核定、门户预留和派单状态。 Columns are `ticket`, `asset`, `model`, `part`, `firmware`, `fault`, `warranty`, `reservation`, `dispatch`.

Python 3 standard library is sufficient to inspect the sample files. From this directory, run `python3 -m unittest discover -s tests -v` to verify only that the supplied files are well-formed and their sample references resolve.
