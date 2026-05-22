# util/data_normalizer.py

import numpy as np
from scipy import stats
from sklearn.preprocessing import MinMaxScaler, StandardScaler, RobustScaler, Normalizer
import math


class DataNormalizer:
    """
    数据归一化工具类，提供多种归一化方法
    """

    def __init__(self):
        """初始化归一化器"""
        self.scalers = {
            'minmax': MinMaxScaler(),
            'standard': StandardScaler(),
            'robust': RobustScaler(),
            'normalizer': Normalizer()
        }

    def min_max_normalization(self, data):
        """
        Min-Max归一化
        将数据缩放到[0, 1]区间
        """
        data = np.array(data)
        min_val = np.min(data)
        max_val = np.max(data)
        if max_val == min_val:
            return np.array([0.5 for _ in data])
        return (data - min_val) / (max_val - min_val)

    def z_score_normalization(self, data):
        """
        Z-Score标准化
        将数据转换为均值为0，标准差为1的分布
        """
        data = np.array(data)
        mean = np.mean(data)
        std = np.std(data)
        if std == 0:
            return np.zeros_like(data)
        return (data - mean) / std

    def decimal_scaling(self, data):
        """
        小数定标规范化
        通过移动小数点位置实现归一化
        """
        data = np.array(data)
        max_abs = np.max(np.abs(data))
        if max_abs == 0:
            return data
        j = len(str(int(max_abs)))
        return data / (10 ** j)

    def exponential_normalization(self, data, lambda_param=1.0):
        """
        指数归一化
        使用指数函数进行非线性转换
        """
        data = np.array(data)
        return 1 - np.exp(-lambda_param * data)

    def log_transformation(self, data, base=math.e):
        """
        对数变换
        压缩数据范围，处理偏斜分布
        """
        data = np.array(data)
        min_val = np.min(data)
        if min_val <= 0:
            offset = abs(min_val) + 1
            return np.log(data + offset) / np.log(base)
        return np.log(data) / np.log(base)

    def l2_normalization(self, data):
        """
        L2范数归一化
        将样本缩放到单位范数
        """
        data = np.array(data)
        norm = np.sqrt(np.sum(data ** 2))
        if norm == 0:
            return np.zeros_like(data)
        return data / norm

    def analyze_data(self, data):
        """
        分析数据特征，推荐合适的归一化方法
        """
        data = np.array(data)

        # 检查数据是否全为零
        if np.all(data == 0):
            return 'minmax'

            # 计算基本统计量
        skewness = stats.skew(data)
        kurtosis = stats.kurtosis(data)

        # 检查异常值
        q1, q3 = np.percentile(data, [25, 75])
        iqr = q3 - q1
        outliers = np.sum((data < (q1 - 1.5 * iqr)) | (data > (q3 + 1.5 * iqr)))

        # 根据数据特征选择方法
        if outliers > len(data) * 0.1:
            return 'robust'
        elif abs(skewness) > 1:
            return 'log'
        elif abs(kurtosis) < 2:
            return 'standard'
        elif np.max(np.abs(data)) > 1000:
            return 'decimal'
        else:
            return 'minmax'

    def normalize(self, data, method=None):
        """
        根据指定方法或自动分析结果进行归一化

        参数:
            data: 输入数据，可以是列表或numpy数组
            method: 归一化方法，可选值包括：
                   'minmax': Min-Max归一化
                   'standard': Z-Score标准化
                   'robust': 稳健缩放
                   'decimal': 小数定标规范化
                   'exp': 指数归一化
                   'log': 对数变换
                   'l2': L2范数归一化
                   None: 自动选择最适合的方法

        返回:
            归一化后的数据（numpy数组）
        """
        try:
            data = np.array(data, dtype=float)

            # 检查数据有效性
            if len(data) == 0:
                raise ValueError("输入数据不能为空")

            if np.any(np.isnan(data)) or np.any(np.isinf(data)):
                raise ValueError("数据中包含无效值(NaN或Inf)")

                # 如果没有指定方法，自动选择
            if method is None:
                method = self.analyze_data(data)

                # 执行归一化
            if method == 'minmax':
                return self.min_max_normalization(data)
            elif method == 'standard':
                return self.z_score_normalization(data)
            elif method == 'decimal':
                return self.decimal_scaling(data)
            elif method == 'exp':
                return self.exponential_normalization(data)
            elif method == 'log':
                return self.log_transformation(data)
            elif method == 'l2':
                return self.l2_normalization(data)
            elif method in self.scalers:
                return self.scalers[method].fit_transform(data.reshape(-1, 1)).ravel()
            else:
                raise ValueError(f"未知的归一化方法: {method}")

        except Exception as e:
            raise Exception(f"归一化过程中出错: {str(e)}")

    def get_available_methods(self):
        """
        获取所有可用的归一化方法
        """
        return ['minmax', 'standard', 'robust', 'decimal', 'exp', 'log', 'l2'] + list(self.scalers.keys())

    def visualize_normalization(self, data, methods=None):
        """
        可视化不同归一化方法的结果
        """
        import matplotlib.pyplot as plt

        if methods is None:
            methods = ['minmax', 'standard', 'robust', 'log']

        fig, axes = plt.subplots(len(methods) + 1, 1, figsize=(10, 2 * (len(methods) + 1)))

        # 绘制原始数据
        axes[0].hist(data, bins=30)
        axes[0].set_title('Original Data')

        # 绘制归一化后的数据
        for i, method in enumerate(methods, 1):
            try:
                normalized_data = self.normalize(data, method)
                axes[i].hist(normalized_data, bins=30)
                axes[i].set_title(f'{method} Normalized')
            except Exception as e:
                axes[i].text(0.5, 0.5, f'Error: {str(e)}', ha='center')

        plt.tight_layout()
        plt.show()