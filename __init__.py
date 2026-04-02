"""
HLSL2Material — HLSL / GLSL 着色器代码 ↔ UE4 材质节点图转换工具

支持的转换路径:
  HLSL Custom Node → UE4 材质节点图
  UE4 材质节点图 → HLSL Custom Node (反向转换)
  Shadertoy GLSL → HLSL → UE4 材质节点图

核心模块:
  hlsl_parser.py            — HLSL 词法/语法解析器
  node_mapper.py            — AST → MaterialGraph 映射
  reverse_converter.py      — MaterialGraph → HLSL 反向转换
  auto_input_generator.py   — 自动创建输入变量节点
  shadertoy_converter.py    — Shadertoy GLSL → HLSL 转换
  ue4_codegen.py            — UE4 Python 脚本生成
  t3d_generator.py          — T3D 剪贴板格式生成
  graph_visualizer.py       — 节点图可视化和布局计算
  web_server.py             — Web 交互界面

使用方式:
  命令行:  python hlsl2material.py --example fresnel
  Web UI:  python web_server.py --port 8080
"""

__version__ = '1.1.0'
__author__ = 'HLSL2Material Team'
