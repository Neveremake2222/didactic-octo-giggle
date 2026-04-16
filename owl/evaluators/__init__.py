"""四层评估器包。

- outcome:  结果正确性评估（pass/fail, stop_reason, 文件修改）
- process:  过程质量评估（重复调用、循环、工具选择）
- efficiency: 效率评估（耗时、token、上下文大小）
- safety:   安全与边界评估（policy 拦截、路径越界）
"""

from .outcome import OutcomeEvaluator
from .process import ProcessEvaluator
from .efficiency import EfficiencyEvaluator
from .safety import SafetyEvaluator

__all__ = [
    "OutcomeEvaluator",
    "ProcessEvaluator",
    "EfficiencyEvaluator",
    "SafetyEvaluator",
]
