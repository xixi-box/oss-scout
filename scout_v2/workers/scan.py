"""scan/reproduce/repair 的模式级实现从 core.execute 分派（P1 骨架，P2/P3 填充）。

保留独立模块文件以匹配计划目录结构；真实逻辑随阶段接入，避免在本阶段写假实现。
"""
