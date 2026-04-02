# HLSL2Material

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.6+](https://img.shields.io/badge/python-3.6+-green.svg)](https://www.python.org/)
[![UE4 4.27](https://img.shields.io/badge/UE4-4.27-orange.svg)](https://www.unrealengine.com/)

> GLSL / HLSL / UE4 材质节点 三方互转工具

**HLSL2Material** 是一个 Python 工具，打通了 **Shadertoy GLSL**、**HLSL Custom Node** 和 **UE4 材质节点图** 之间的双向转换。通过 Web UI 或 CLI，复制粘贴代码即可自动生成完整的材质节点（含输入参数节点和连线），粘贴到 UE4.27 材质编辑器直接可用。

## 功能特性

- **GLSL → HLSL → UE4 节点**：从 Shadertoy 复制 GLSL 代码，自动识别并转换为 UE4 材质节点
- **HLSL → UE4 节点**：将 Custom HLSL 代码转换为原生 MaterialExpression 节点
- **UE4 节点 → HLSL**：反向转换，将材质节点图转回 HLSL 代码
- **自动输入节点**：自动识别外部变量，创建 ScalarParameter / VectorParameter / TextureSample 并连线
- **T3D 一键复制**：生成的 T3D 包含完整节点和连线，Ctrl+V 粘贴到 UE4 即可使用
- **Web 交互界面**：浏览器中实时输入代码、预览节点图、复制 T3D
- **自动代码识别**：粘贴任意代码，自动判断是 GLSL / HLSL / T3D 格式

## 使用流程

```
从 Shadertoy 复制 GLSL 代码
         ↓
粘贴到 Web UI（自动识别为 GLSL）
         ↓
自动转换: GLSL → HLSL → 材质节点图 + 输入参数节点
         ↓
一键复制 T3D → 在 UE4.27 材质编辑器 Ctrl+V
         ↓
完整节点图 + 所有连线，直接可用
```

## 前置要求

- **Python 3.6+**（纯标准库实现，无第三方依赖）
- **Unreal Engine 4.27**（T3D 格式仅在 4.27 验证）

## 快速开始

### Web UI（推荐）

```bash
# 克隆仓库
git clone https://github.com/misketnfeos/hlsl2material.git
cd hlsl2material

# 启动 Web 服务器
python web_server.py --port 8080

# 在浏览器中打开 http://127.0.0.1:8080
```

Web UI 支持：
- **T3D 节点粘贴**：从 UE4 复制节点 → 可视化 → 反向转为 HLSL → 复制回 UE4
- **HLSL / GLSL 模式**：粘贴任意代码 → 自动识别类型 → 转换为节点图 → 复制 T3D

### 命令行

```bash
# HLSL 转换
python hlsl2material.py my_shader.hlsl
python hlsl2material.py --code "float3 c = lerp(a, b, uv.x); return c;"

# 使用内置示例
python hlsl2material.py --example fresnel
python hlsl2material.py --example dissolve -n M_Dissolve

# 自动创建输入参数节点
python hlsl2material.py --example fresnel --auto-input

# GLSL/Shadertoy 转换
python hlsl2material.py --glsl my_shader.glsl
python hlsl2material.py --glsl-code "void mainImage(out vec4 o, in vec2 u){o=vec4(u/iResolution.xy,0,1);}"

# 反向转换: 材质节点图 → HLSL
python hlsl2material.py --reverse --example fresnel

# 查看所有内置示例
python hlsl2material.py --list-examples
```

## 架构概览

```
hlsl2material/
├── hlsl2material.py            # 主入口 CLI 工具
├── web_server.py               # Web 交互界面（推荐使用方式）
├── hlsl_parser.py              # HLSL 词法分析 & 语法解析 → AST
├── hlsl_preprocessor.py        # HLSL 预处理（宏展开等）
├── node_mapper.py              # AST → UE4 MaterialExpression 节点图
├── shadertoy_converter.py      # Shadertoy GLSL → HLSL 转换
├── reverse_converter.py        # 反向转换: MaterialGraph → HLSL
├── auto_input_generator.py     # 自动识别输入变量并创建参数节点
├── t3d_generator.py            # MaterialGraph → T3D 剪贴板格式
├── t3d_parser.py               # T3D 格式解析
├── graph_visualizer.py         # 节点图 → 交互式 HTML 可视化
├── ue4_codegen.py              # 节点图 → UE4 Editor Python 脚本
├── custom_converter.py         # Custom HLSL 节点转换器
└── material_expressions.json   # UE4 材质表达式映射数据
```

## 支持的 HLSL 子集

| 类别 | 支持内容 |
|------|----------|
| **类型** | `float`, `float2`, `float3`, `float4`, `half`, `half3`, `half4`, `int`, `bool` |
| **运算符** | `+` `-` `*` `/` `%` `>` `<` `>=` `<=` `==` `!=` `&&` `\|\|` `!` `? :` |
| **内置函数** | `lerp`, `saturate`, `clamp`, `dot`, `cross`, `normalize`, `pow`, `sin`, `cos`, `tan`, `abs`, `sign`, `floor`, `ceil`, `round`, `frac`, `sqrt`, `min`, `max`, `step`, `smoothstep`, `length`, `distance`, `tex2D`, `mul` 等 |
| **语法** | 变量声明与赋值, Swizzle (`.xyz`, `.rg`), 构造函数 (`float3(...)`), `return` 语句, 三元运算符 |

> **注意**：`for`/`while` 循环、自定义函数、`struct`、预处理器指令等暂不支持，这些会保留为 Custom Expression 节点。

## GLSL → HLSL 转换规则

| GLSL | HLSL |
|------|------|
| `vec2` / `vec3` / `vec4` | `float2` / `float3` / `float4` |
| `mat2` / `mat3` / `mat4` | `float2x2` / `float3x3` / `float4x4` |
| `mix(a, b, t)` | `lerp(a, b, t)` |
| `fract(x)` | `frac(x)` |
| `mod(a, b)` | `fmod(a, b)` |
| `texture()` / `texture2D()` | `tex2D()` |
| `vec3(1.0)` | `float3(1.0, 1.0, 1.0)` |
| `iTime` | Time 节点 |
| `iResolution` | ViewSize |
| `fragCoord / iResolution` | UV (0~1) |

## 内置示例

| 示例 | 效果 |
|------|------|
| `fresnel` | 菲涅尔效果 |
| `dissolve` | 溶解效果 |
| `simple_blend` | 简单颜色混合 |
| `rim_light` | 边缘光效果 |
| `uv_distortion` | UV 扭曲效果 |

## 贡献

欢迎提交 Issue 和 Pull Request！

## 许可证

本项目基于 [MIT License](LICENSE) 开源。
