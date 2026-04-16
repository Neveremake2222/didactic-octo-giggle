# Owl 自动化评测指南

## 一键命令

在仓库根目录执行：

```bat
run_eval_campaign.bat
```

默认行为：

- 连续运行 `20` 轮 fixed benchmark
- 为每一轮创建独立工作目录
- 自动扁平化收集所有 `run_*` 目录
- 自动生成 `resume_metrics.json` / `resume_metrics.md`
- 自动生成一份中文评测报告

## 常用用法

运行 20 轮完整评测：

```bat
run_eval_campaign.bat 20 full
```

运行 5 轮快速烟雾评测：

```bat
run_eval_campaign.bat 5 quick
```

指定实验名称：

```bat
run_eval_campaign.bat 20 full baseline-20260416
```

## 输出目录

评测结果会写入：

```text
artifacts/eval/<experiment-name>/
```

关键文件：

- `benchmark-artifacts/benchmark-XX.json`
- `flat-runs/`
- `metrics/resume_metrics.json`
- `metrics/resume_metrics.md`
- `metrics/campaign_summary.json`
- `reports/eval_report_zh.md`

## 推荐做法

- 修复前跑一次：`run_eval_campaign.bat 20 full before-fix`
- 修复后跑一次：`run_eval_campaign.bat 20 full after-fix`
- 对比两个目录中的：
  - `metrics/campaign_summary.json`
  - `metrics/resume_metrics.json`
  - `reports/eval_report_zh.md`
