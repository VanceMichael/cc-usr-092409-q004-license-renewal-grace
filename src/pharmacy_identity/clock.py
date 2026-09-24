"""业务时点工具。

全系统使用朴素 UTC 字符串 ``YYYY-MM-DDTHH:MM:SS``，字典序与时间序一致，
SQLite 可直接比较。所有期限（缓冲到期、补正期限）都是绝对时间戳，
不依赖进程内定时器——停服恢复后只需按当前时间扫描到期任务即可接续。
"""

from datetime import datetime, timedelta, timezone

FORMAT = "%Y-%m-%dT%H:%M:%S"


def now() -> str:
    return datetime.now(timezone.utc).strftime(FORMAT)


def parse(value: str) -> datetime:
    return datetime.strptime(value, FORMAT)


def add_days(value: str, days: int) -> str:
    return (parse(value) + timedelta(days=days)).strftime(FORMAT)


def add_hours(value: str, hours: int) -> str:
    return (parse(value) + timedelta(hours=hours)).strftime(FORMAT)
