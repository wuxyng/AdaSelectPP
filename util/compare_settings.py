#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compare_settings.py (with A-metrics support)

比较同一算法在不同配置/参数设置下的 exec、rec、trans、total 性能曲线，
并可选绘制新增统计量（A-metrics）：
  - what_if_calls（本轮触发的 what-if 调用次数）
  - reconf_add / reconf_drop（建议配置相对旧配置的增删数）
  - trans_create / trans_drop（创建/删除的归一化摊销，单位=无量纲）

注意：
- CSV 中若不存在上述列，会自动跳过对应图；
- exec/rec/trans 仍按 ms→s 转换；A-metrics 原样使用（不做单位变换）。

用法示例：
    python compare_settings.py \
      --inputs /path/to/run1.csv /path/to/run2.csv /path/to/run3.csv \
      --labels "a=0.8,b=1.5" "a=0.6,b=1.2" "a=1.0,b=2.0" \
      --benchmark tpch \
      --workload shifting \
      --output tpch_shifting_settings

输出文件（存在列时才会生成）：
    tpch_shifting_settings_exec.png
    tpch_shifting_settings_rec.png
    tpch_shifting_settings_trans.png
    tpch_shifting_settings_total.png
    tpch_shifting_settings_what_if_calls.png
    tpch_shifting_settings_reconf_add.png
    tpch_shifting_settings_reconf_drop.png
    tpch_shifting_settings_trans_create.png
    tpch_shifting_settings_trans_drop.png
"""

import os
import glob
import argparse
import pandas as pd
import matplotlib.pyplot as plt

METRICS_BASE = ['exec', 'rec', 'trans', 'total']
AMETRICS     = ['what_if_calls', 'reconf_add', 'reconf_drop', 'trans_create', 'trans_drop']


def find_csv(path: str, benchmark: str, workload: str) -> str:
    """
    如果 path 是文件，则直接返回；
    如果是目录，则查找符合模式 *_{benchmark}_{workload}_*.csv 的文件。
    """
    if os.path.isfile(path) and path.lower().endswith('.csv'):
        return path
    directory = path
    pattern = os.path.join(directory, f"*{benchmark}_{workload}*.csv")
    matches = glob.glob(pattern)
    if not matches:
        raise FileNotFoundError(f"No CSV found in {directory} matching pattern {pattern}")
    if len(matches) > 1:
        print(f"⚠️ Directory {directory} contains multiple matches, using {matches[0]}")
    return matches[0]


def load_df(path: str) -> pd.DataFrame:
    """
    读取 CSV：
    - 将 'round' 列转为数值，仅保留数值轮次（自动滤掉诸如 'summary' 的汇总行）；
    - 将 exec/rec/trans 从 ms 转为秒；
    - 计算 per-round 的 total = exec + rec + trans（不直接使用 CSV 自带 total，避免不一致）。
    - 若存在 A-metrics 列，直接读取为数值（不做单位变换）。
    """
    df = pd.read_csv(path)
    # 轮次：仅保留数值行，过滤掉非数值（如 'summary'）
    df['round'] = pd.to_numeric(df['round'], errors='coerce')
    df = df[df['round'].notna()].copy()

    # exec/rec/trans: ms -> s
    for col in ['exec', 'rec', 'trans']:
        if col not in df.columns:
            raise KeyError(f"Column '{col}' not found in {path}")
        df[col] = df[col] / 1000.0

    # per-round total 统一用三项相加得到
    df['total'] = df['exec'] + df['rec'] + df['trans']

    # A-metrics：若存在则转为数值（无单位变换）
    for col in AMETRICS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

    # 排序
    df = df.sort_values('round').reset_index(drop=True)
    return df


def plot_series(metric: str, data: dict, benchmark: str, workload: str, output: str, ylog: bool, ylabel: str):
    colors = ['tab:blue','tab:orange','tab:green','tab:red','tab:purple','tab:brown']
    markers = ['o','s','^','D','v','*']
    plt.figure(figsize=(10, 6))
    for idx, (label, df) in enumerate(data.items()):
        if metric not in df.columns:
            return  # 该指标不存在，直接跳过
        plt.plot(
            df['round'], df[metric],
            label=label,
            color=colors[idx % len(colors)],
            marker=markers[idx % len(markers)],
            markersize=5,
            linestyle='-'
        )
    plt.xlabel('Round')
    plt.ylabel(ylabel)
    plt.title(f'{benchmark.upper()} – {workload.capitalize()} – {metric.replace("_"," ").title()} Comparison')
    if ylog:
        plt.yscale('log')
    plt.grid(True, which='both', linestyle='--', linewidth=0.5)
    plt.legend(title='Setting')
    plt.tight_layout()
    if output:
        base, ext = os.path.splitext(output)
        save_path = f"{base}_{metric}{ext}"
        plt.savefig(save_path, dpi=300)
        print(f"Saved plot to {save_path}")
    else:
        plt.show()


def plot_comparison(inputs, labels, benchmark, workload, output=None):
    """
    对比绘图：针对多组设置(inputs)，绘制 exec、rec、trans 和 total 四个指标，
    若 CSV 中存在 A-metrics 列，追加绘制这些统计量。
    """
    # 加载数据
    data = {}
    for idx, path in enumerate(inputs):
        label = labels[idx]
        csv_path = find_csv(path, benchmark, workload)
        data[label] = load_df(csv_path)

    # 基础时间指标（log 轴）
    for metric in ['exec','rec','trans','total']:
        plot_series(metric, data, benchmark, workload, output, ylog=True, ylabel=f'{metric.capitalize()} Time (s)')

    # A-metrics（线性轴）：仅对所有输入都存在该列的指标绘图
    # 统计所有数据共有的列
    common_cols = set.intersection(*[set(df.columns) for df in data.values()])
    for metric in AMETRICS:
        if metric in common_cols:
            ylabel = {
                'what_if_calls': 'Count (per round)',
                'reconf_add':    '#Adds (per round)',
                'reconf_drop':   '#Drops (per round)',
                'trans_create':  'Normalized Create Cost (per round)',
                'trans_drop':    'Normalized Drop Cost (per round)'
            }[metric]
            plot_series(metric, data, benchmark, workload, output, ylog=False, ylabel=ylabel)
        else:
            # 若某些输入没有该列，给出提示（不报错）
            missing = [lbl for lbl, df in data.items() if metric not in df.columns]
            if missing:
                print(f"[skip] metric '{metric}' not found in: {', '.join(missing)}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Compare same algorithm under different configurations (with A-metrics support)'
    )
    parser.add_argument(
        '--inputs', nargs='+', required=True,
        help='CSV 文件路径或目录列表，如果是目录则 glob 对应 CSV'
    )
    parser.add_argument(
        '--labels', nargs='+', required=True,
        help='各输入的显示标签，顺序对应'
    )
    parser.add_argument(
        '--benchmark', choices=['tpch','tpchs','job'], required=True,
        help='基准名'
    )
    parser.add_argument(
        '--workload', choices=['shifting','noisy','random'], required=True,
        help='负载类型'
    )
    parser.add_argument(
        '--output', default=None,
        help='若指定，则以此前缀保存对比图，生成 _exec/_rec/_trans/_total 以及 A-metrics 后缀'
    )

    args = parser.parse_args()
    if len(args.inputs) != len(args.labels):
        parser.error('Number of --inputs must match number of --labels')

    plot_comparison(
        inputs=args.inputs,
        labels=args.labels,
        benchmark=args.benchmark,
        workload=args.workload,
        output=args.output
    )
