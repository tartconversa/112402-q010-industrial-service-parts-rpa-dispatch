"""定时采集循环。

生产环境可由 cron / systemd timer 调用 `python -m spare_service.cli ingest`，
也可以启动这个循环常驻。任何一次采集失败都不会让上一批数据被当成新数据：
失败只记录，不更新"最后成功时间"。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from .db import to_iso


@dataclass
class Source:
    name: str
    kind: str  # inventory | catalog | tickets
    path: Path
    options: dict


class Scheduler:
    def __init__(self, service, sources, *, sleep=time.sleep, logger=print):
        self.service = service
        self.sources = [
            s if isinstance(s, Source) else Source(**s) for s in sources
        ]
        self.sleep = sleep
        self.logger = logger

    def run_once(self):
        report = []
        for source in self.sources:
            started = self.service.clock.now()
            try:
                if not Path(source.path).is_file():
                    raise FileNotFoundError(f"来源文件不存在：{source.path}")
                if source.kind == "inventory":
                    self.service.ingest_inventory(source.path, **source.options)
                elif source.kind == "catalog":
                    self.service.ingest_catalog(source.path, **source.options)
                elif source.kind == "tickets":
                    self.service.ingest_tickets(source.path, **source.options)
                else:
                    raise ValueError(f"未知来源类型：{source.kind}")
                ok, detail = True, "ok"
            except Exception as exc:  # 单个来源失败不影响其它来源
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            self.service.db.insert(
                "ingest_log",
                {
                    "job": f"scheduled:{source.name}",
                    "started_at": to_iso(started),
                    "finished_at": to_iso(self.service.clock.now()),
                    "ok": 1 if ok else 0,
                    "detail": detail,
                },
            )
            if ok:
                self.service.db.set_meta(f"last_success:{source.name}",
                                         to_iso(self.service.clock.now()))
            self.logger(f"[{source.kind}] {source.name}: {detail}")
            report.append({"source": source.name, "ok": ok, "detail": detail})
        return report

    def run_forever(self, interval=300, *, iterations=None):
        done = 0
        while iterations is None or done < iterations:
            self.run_once()
            done += 1
            if iterations is not None and done >= iterations:
                break
            self.sleep(interval)
        return done
