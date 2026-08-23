"""指标双格式导出：JSON（dict 序列化）与 Prometheus 文本（手写 exposition）。

不引入第三方依赖（D1）；Prometheus 文本格式符合 exposition 规范：
`# HELP <name> <help>` / `# TYPE <name> <type>` + 样本行（含 labels）。
timer 类型按 histogram 语义导出（`_bucket`/`_sum`/`_count` 后缀）。
"""

from __future__ import annotations

import json
from typing import Any, Iterable

__all__ = ["export_json", "export_prometheus_text"]


def _json_safe(value: Any) -> Any:
    """递归把 float('inf') / NaN 规约成 None，保证输出是合法 JSON。

    JSON 没有 Infinity / NaN 字面量，而 histogram/timer 的 +Inf bucket 上界
    le 就是 float('inf')。浏览器 JSON.parse 会拒绝，必须先规约。
    """
    if isinstance(value, float) and (value == float("inf") or value != value):
        return None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def export_json(samples: Iterable[dict[str, Any]]) -> str:
    """导出为 JSON 字符串（有序、缩进 2，便于 diff/对账）。"""
    return json.dumps(
        [_json_safe(dict(s)) for s in samples],
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def _format_label_value(value: Any) -> str:
    text = str(value)
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _label_suffix(label_names: list[str], labels: dict[str, Any]) -> str:
    if not label_names:
        return ""
    pairs = ",".join(
        f'{name}="{_format_label_value(labels.get(name, ""))}"' for name in label_names
    )
    return "{" + pairs + "}"


def _metric_line(name: str, suffix: str, label_suffix: str, value: Any) -> str:
    return f"{name}{suffix}{label_suffix} {value}"


def export_prometheus_text(samples: Iterable[dict[str, Any]]) -> str:
    """导出为 Prometheus 文本 exposition 格式。

    仅输出「有样本的指标」的 HELP/TYPE；每个样本行按 type 展开：
    - counter：`<name>{labels} <value>`
    - histogram/timer：`<name>_bucket{labels,le="<le>"} <count>`、`<name>_sum`、`<name>_count`
    """
    lines: list[str] = []
    emitted_meta: set[str] = set()
    for sample in samples:
        name = sample["name"]
        type_ = str(sample["type"])
        help_text = str(sample.get("help", ""))
        label_names = list(sample.get("label_names", []))
        # 每个指标名只输出一次 HELP/TYPE
        if name not in emitted_meta:
            if help_text:
                lines.append(f"# HELP {name} {_format_label_value(help_text)}")
            prom_type = "histogram" if type_ == "timer" else type_
            lines.append(f"# TYPE {name} {prom_type}")
            emitted_meta.add(name)
        labels = dict(sample.get("labels", {}))
        if type_ in {"histogram", "timer"}:
            for bucket in sample["buckets"]:
                le = bucket["le"]
                le_text = "+Inf" if le == float("inf") else _format_label_value(le)
                bucket_labels = dict(labels)
                bucket_labels["le"] = le_text
                label_suffix = _label_suffix(
                    list(label_names) + ["le"],
                    bucket_labels,
                )
                lines.append(_metric_line(name, "_bucket", label_suffix, bucket["count"]))
            lines.append(_metric_line(name, "_sum", _label_suffix(label_names, labels), sample["sum"]))
            lines.append(_metric_line(name, "_count", _label_suffix(label_names, labels), sample["count"]))
        else:
            lines.append(_metric_line(name, "", _label_suffix(label_names, labels), sample["value"]))
    return "\n".join(lines) + "\n"
