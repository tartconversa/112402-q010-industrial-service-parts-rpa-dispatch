"""版本号比较。

修订号（目录自己的版本）与固件版本都按"数字段按数值、其余按文本"比较，
所以 3.10 > 3.9，2 比 10 旧。无法解析的固件版本直接报错，不做模糊匹配。
"""

from __future__ import annotations

import re

from .errors import ValidationError

_NUMBER_RE = re.compile(r"(\d+)")


def revision_key(value):
    if value is None:
        return (0, ())
    text = str(value).strip()
    parts = []
    for chunk in _NUMBER_RE.split(text):
        if chunk == "":
            continue
        parts.append((0, int(chunk)) if chunk.isdigit() else (1, chunk.lower()))
    return (0, tuple(parts))


def firmware_key(value):
    text = (value or "").strip()
    if not text:
        raise ValidationError("缺少固件版本，无法判断兼容性", code="missing_firmware")
    numbers = _NUMBER_RE.findall(text)
    if not numbers:
        raise ValidationError(f"无法解析固件版本：{value!r}", code="bad_firmware")
    return tuple(int(n) for n in numbers)


def firmware_in_range(version, low, high):
    """端点为空表示厂商没有限定该方向。"""
    key = firmware_key(version)
    if low and key < firmware_key(low):
        return False
    if high and key > firmware_key(high):
        return False
    return True


def compare_revisions(left, right):
    lk, rk = revision_key(left), revision_key(right)
    return (lk > rk) - (lk < rk)
