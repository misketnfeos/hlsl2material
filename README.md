# HLSL2Material

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.6+](https://img.shields.io/badge/python-3.6+-green.svg)](https://www.python.org/)
[![UE4 4.27](https://img.shields.io/badge/UE4-4.27-orange.svg)](https://www.unrealengine.com/)

> Bi-directional converter between GLSL / HLSL / UE4 Material Nodes

[中文文档](README_CN.md)

**HLSL2Material** is a Python tool that bridges **Shadertoy GLSL**, **HLSL Custom Nodes**, and **UE4 Material Node Graphs** with full bi-directional conversion. Through a Web UI or CLI, simply paste your shader code and get a complete material node graph (with input parameter nodes and wiring) ready to paste into the UE4.27 Material Editor.

## Features

- **GLSL → HLSL → UE4 Nodes** — Paste Shadertoy GLSL code, auto-detect and convert to UE4 material nodes
- **HLSL → UE4 Nodes** — Convert Custom HLSL code into native MaterialExpression nodes
- **UE4 Nodes → HLSL** — Reverse conversion, turn a material node graph back into HLSL code
- **Auto Input Nodes** — Automatically detect external variables and create ScalarParameter / VectorParameter / TextureSample nodes with wiring
- **One-Click T3D Copy** — Generated T3D contains complete nodes and connections, Ctrl+V to paste into UE4
- **Web UI** — Real-time code input, node graph preview, and T3D copy in the browser
- **Auto Code Detection** — Paste any code and the tool auto-detects whether it's GLSL / HLSL / T3D format

## Workflow

```
Copy GLSL code from Shadertoy
         ↓
Paste into Web UI (auto-detected as GLSL)
         ↓
Auto convert: GLSL → HLSL → Material Node Graph + Input Parameter Nodes
         ↓
One-click copy T3D → Ctrl+V in UE4.27 Material Editor
         ↓
Complete node graph + all connections, ready to use
```

## Prerequisites

- **Python 3.6+** (pure standard library, no third-party dependencies)
- **Unreal Engine 4.27** (T3D format verified on 4.27 only)

## Quick Start

### Web UI (Recommended)

```bash
# Clone the repository
git clone https://github.com/misketnfeos/hlsl2material.git
cd hlsl2material

# Start the web server
python web_server.py --port 8080

# Open http://127.0.0.1:8080 in your browser
```

Web UI supports:
- **T3D Node Paste** — Copy nodes from UE4 → Visualize → Reverse convert to HLSL → Copy back to UE4
- **HLSL / GLSL Mode** — Paste any code → Auto-detect type → Convert to node graph → Copy T3D

### Command Line

```bash
# HLSL conversion
python hlsl2material.py my_shader.hlsl
python hlsl2material.py --code "float3 c = lerp(a, b, uv.x); return c;"

# Use built-in examples
python hlsl2material.py --example fresnel
python hlsl2material.py --example dissolve -n M_Dissolve

# Auto-create input parameter nodes
python hlsl2material.py --example fresnel --auto-input

# GLSL / Shadertoy conversion
python hlsl2material.py --glsl my_shader.glsl
python hlsl2material.py --glsl-code "void mainImage(out vec4 o, in vec2 u){o=vec4(u/iResolution.xy,0,1);}"

# Reverse conversion: Material Node Graph → HLSL
python hlsl2material.py --reverse --example fresnel

# List all built-in examples
python hlsl2material.py --list-examples
```

## Architecture

```
hlsl2material/
├── hlsl2material.py            # Main entry point / CLI tool
├── web_server.py               # Web UI (recommended usage)
├── hlsl_parser.py              # HLSL lexer & parser → AST
├── hlsl_preprocessor.py        # HLSL preprocessor (macro expansion, etc.)
├── node_mapper.py              # AST → UE4 MaterialExpression node graph
├── shadertoy_converter.py      # Shadertoy GLSL → HLSL conversion
├── reverse_converter.py        # Reverse conversion: MaterialGraph → HLSL
├── auto_input_generator.py     # Auto-detect input variables & create parameter nodes
├── t3d_generator.py            # MaterialGraph → T3D clipboard format
├── t3d_parser.py               # T3D format parser
├── graph_visualizer.py         # Node graph → interactive HTML visualization
├── ue4_codegen.py              # Node graph → UE4 Editor Python script
├── custom_converter.py         # Custom HLSL node converter
└── material_expressions.json   # UE4 material expression mapping data
```

## Supported HLSL Subset

| Category | Supported |
|----------|-----------|
| **Types** | `float`, `float2`, `float3`, `float4`, `half`, `half3`, `half4`, `int`, `bool` |
| **Operators** | `+` `-` `*` `/` `%` `>` `<` `>=` `<=` `==` `!=` `&&` `\|\|` `!` `? :` |
| **Built-in Functions** | `lerp`, `saturate`, `clamp`, `dot`, `cross`, `normalize`, `pow`, `sin`, `cos`, `tan`, `abs`, `sign`, `floor`, `ceil`, `round`, `frac`, `sqrt`, `min`, `max`, `step`, `smoothstep`, `length`, `distance`, `tex2D`, `mul`, etc. |
| **Syntax** | Variable declaration & assignment, Swizzle (`.xyz`, `.rg`), Constructors (`float3(...)`), `return` statements, Ternary operator |

> **Note:** `for`/`while` loops, user-defined functions, `struct`, and preprocessor directives are not yet supported — these will be preserved as Custom Expression nodes.

## GLSL → HLSL Conversion Rules

| GLSL | HLSL |
|------|------|
| `vec2` / `vec3` / `vec4` | `float2` / `float3` / `float4` |
| `mat2` / `mat3` / `mat4` | `float2x2` / `float3x3` / `float4x4` |
| `mix(a, b, t)` | `lerp(a, b, t)` |
| `fract(x)` | `frac(x)` |
| `mod(a, b)` | `fmod(a, b)` |
| `texture()` / `texture2D()` | `tex2D()` |
| `vec3(1.0)` | `float3(1.0, 1.0, 1.0)` |
| `iTime` | Time node |
| `iResolution` | ViewSize |
| `fragCoord / iResolution` | UV (0~1) |

## Built-in Examples

| Example | Effect |
|---------|--------|
| `fresnel` | Fresnel effect |
| `dissolve` | Dissolve effect |
| `simple_blend` | Simple color blending |
| `rim_light` | Rim lighting effect |
| `uv_distortion` | UV distortion effect |

## Contributing

Issues and Pull Requests are welcome!

## License

This project is licensed under the [MIT License](LICENSE).
