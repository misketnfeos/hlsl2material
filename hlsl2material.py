"""
============================================================
 hlsl2material - HLSL Custom Node → UE4 Material Node 转换工具
============================================================

主入口文件。将 Custom HLSL 代码转换为：
  1. 交互式 HTML 节点图预览
  2. UE4 Editor Python 脚本（可在引擎中自动创建材质）

支持反向转换（--reverse）：
  3. 将材质节点图（MaterialGraph JSON）反向转换为 HLSL 代码

用法：
  python hlsl2material.py <hlsl_file_or_code> [选项]

选项：
  --name, -n        材质名称 (默认: M_Generated)
  --path, -p        UE4 中的材质路径 (默认: /Game/Materials)
  --output, -o      输出目录 (默认: 当前目录)
  --no-html         不生成 HTML 预览
  --no-script       不生成 UE4 Python 脚本
  --ast             输出 AST 调试信息
  --reverse         反向转换模式：MaterialGraph → HLSL

示例：
  # 从文件转换
  python hlsl2material.py my_effect.hlsl

  # 从剪贴板/内联代码
  python hlsl2material.py --code "float3 c = lerp(a, b, uv.x); return c;"

  # 指定材质名
  python hlsl2material.py my_effect.hlsl -n M_MyEffect -p /Game/Materials/Effects

  # 反向转换：HLSL → Graph → HLSL（双向验证）
  python hlsl2material.py --reverse --code "float3 c = lerp(a, b, 0.5); return c;"
  python hlsl2material.py --reverse my_graph.json
============================================================
"""

import os
import sys
import argparse

# Windows 控制台编码修复
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

# 确保模块可导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hlsl_parser import parse_hlsl, dump_ast
from node_mapper import hlsl_to_material_graph
from graph_visualizer import save_html, generate_html
from ue4_codegen import generate_ue4_script


# ═══════════════════════════════════════════════════════════
# 示例 HLSL 代码（用于测试/演示）
# ═══════════════════════════════════════════════════════════

EXAMPLE_HLSL = {
    'fresnel': """
// 菲涅尔效果
float3 viewDir = normalize(CameraPosition - WorldPosition);
float fresnel = pow(1.0 - saturate(dot(Normal, viewDir)), FresnelPower);
float3 result = lerp(BaseColor, RimColor, fresnel);
return result;
""",

    'dissolve': """
// 溶解效果
float noise = tex2D(NoiseTex, UV).r;
float edge = smoothstep(DissolveAmount - EdgeWidth, DissolveAmount, noise);
float edgeMask = smoothstep(DissolveAmount, DissolveAmount + EdgeWidth, noise);
float3 edgeColor = EdgeColor * (edge - edgeMask);
float3 result = BaseColor * edgeMask + edgeColor;
return result;
""",

    'simple_blend': """
// 简单颜色混合
float3 color1 = float3(1.0, 0.0, 0.0);
float3 color2 = float3(0.0, 0.0, 1.0);
float t = saturate(UV.x);
float3 result = lerp(color1, color2, t);
return result;
""",

    'rim_light': """
// 边缘光
float NdotV = dot(Normal, ViewDir);
float rim = 1.0 - saturate(NdotV);
rim = pow(rim, RimPower);
float3 rimColor = RimColor * rim * RimIntensity;
float3 result = BaseColor + rimColor;
return result;
""",

    'uv_distortion': """
// UV 扭曲
float2 distortion = tex2D(DistortionTex, UV + Time * 0.1).rg;
distortion = distortion * 2.0 - 1.0;
float2 distortedUV = UV + distortion * DistortionStrength;
float3 result = tex2D(MainTex, distortedUV).rgb;
return result;
""",
}


def main():
    parser = argparse.ArgumentParser(
        description='HLSL Custom Node → UE4 Material Node 转换工具',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例用法:
  python hlsl2material.py my_shader.hlsl
  python hlsl2material.py --code "return lerp(a, b, 0.5);"
  python hlsl2material.py --example fresnel
  python hlsl2material.py --example dissolve -n M_Dissolve

GLSL/Shadertoy 转换:
  python hlsl2material.py --glsl my_shader.glsl
  python hlsl2material.py --glsl-code "void mainImage(out vec4 o, in vec2 u){o=vec4(u/iResolution.xy,0,1);}"

可用的内置示例:
  fresnel       - 菲涅尔效果
  dissolve      - 溶解效果
  simple_blend  - 简单颜色混合
  rim_light     - 边缘光
  uv_distortion - UV 扭曲
        """
    )

    input_group = parser.add_mutually_exclusive_group()
    input_group.add_argument('hlsl_file', nargs='?', help='HLSL 源文件路径')
    input_group.add_argument('--code', '-c', help='直接输入 HLSL 代码')
    input_group.add_argument('--example', '-e', choices=list(EXAMPLE_HLSL.keys()),
                             help='使用内置示例')
    input_group.add_argument('--glsl', metavar='FILE',
                             help='从 GLSL/Shadertoy 文件转换')
    input_group.add_argument('--glsl-code', metavar='CODE',
                             help='从 GLSL/Shadertoy 代码字符串直接转换')

    parser.add_argument('--name', '-n', default='M_Generated', help='材质名称')
    parser.add_argument('--path', '-p', default='/Game/Materials', help='UE4 材质路径')
    parser.add_argument('--output', '-o', default='.', help='输出目录')
    parser.add_argument('--no-html', action='store_true', help='不生成 HTML 预览')
    parser.add_argument('--no-script', action='store_true', help='不生成 UE4 脚本')
    parser.add_argument('--ast', action='store_true', help='输出 AST 调试信息')
    parser.add_argument('--auto-input', action='store_true',
                        help='自动创建输入变量节点并连线（分析 HLSL 代码中的外部变量）')
    parser.add_argument('--list-examples', action='store_true', help='列出所有内置示例')
    parser.add_argument('--reverse', '-r', action='store_true',
                        help='反向转换：将 HLSL 代码或 MaterialGraph JSON 转换回 HLSL')

    args = parser.parse_args()

    # 列出示例
    if args.list_examples:
        print("可用的内置示例：")
        for name, code in EXAMPLE_HLSL.items():
            first_line = code.strip().split('\n')[0]
            print(f"  {name:20s} {first_line}")
        return

    # 反向转换模式
    if args.reverse:
        _do_reverse_conversion(args)
        return

    # GLSL/Shadertoy 转换模式
    if args.glsl or args.glsl_code:
        from shadertoy_converter import (
            convert_shadertoy, convert_shadertoy_file,
            print_result,
        )

        if args.glsl:
            result = convert_shadertoy_file(args.glsl)
        else:
            result = convert_shadertoy(args.glsl_code)

        print_result(result)

        if result.success:
            os.makedirs(args.output, exist_ok=True)
            out_name = args.name if args.name != 'M_Generated' else 'M_Shadertoy'
            hlsl_path = os.path.join(args.output, f'{out_name}_custom_node.hlsl')
            with open(hlsl_path, 'w', encoding='utf-8') as f:
                f.write(result.custom_node_code)
            print(f'\nHLSL 代码已保存到: {hlsl_path}')
        return

    # 获取 HLSL 源码
    hlsl_source = None
    source_name = args.name

    if args.example:
        hlsl_source = EXAMPLE_HLSL[args.example]
        source_name = f'M_{args.example.title()}'
        print(f"使用内置示例: {args.example}")
    elif args.code:
        hlsl_source = args.code
    elif args.hlsl_file:
        if not os.path.exists(args.hlsl_file):
            print(f"错误：文件不存在 - {args.hlsl_file}")
            sys.exit(1)
        with open(args.hlsl_file, 'r', encoding='utf-8') as f:
            hlsl_source = f.read()
        source_name = os.path.splitext(os.path.basename(args.hlsl_file))[0]
        source_name = f'M_{source_name}'
    else:
        # 无输入，使用默认示例
        print("未指定输入，使用默认示例 (fresnel)")
        print("使用 --help 查看所有选项")
        print()
        hlsl_source = EXAMPLE_HLSL['fresnel']
        source_name = 'M_Fresnel'

    # 如果指定了 --name，使用指定的名称
    if args.name != 'M_Generated':
        source_name = args.name

    print(f"\n{'═' * 60}")
    print(f"  HLSL → UE4 Material Node Converter")
    print(f"{'═' * 60}")
    print(f"\n输入 HLSL:\n")
    for i, line in enumerate(hlsl_source.strip().split('\n'), 1):
        print(f"  {i:3d} │ {line}")

    # ── Step 1: 解析 ──
    print(f"\n{'─' * 60}")
    print("Step 1: 解析 HLSL...")
    try:
        program = parse_hlsl(hlsl_source)
    except SyntaxError as e:
        print(f"\n❌ 解析失败: {e}")
        sys.exit(1)

    print(f"  ✓ 识别到 {len(program.statements)} 条语句")
    print(f"  ✓ 输入参数: {program.inputs or '(无)'}")

    if args.ast:
        print(f"\nAST:\n{dump_ast(program)}")

    # ── Step 2: 转换为节点图 ──
    print(f"\n{'─' * 60}")
    print("Step 2: 转换为材质节点图...")
    graph = hlsl_to_material_graph(hlsl_source)

    print(f"  ✓ 生成 {len(graph.nodes)} 个节点")
    print(f"  ✓ 输入参数节点: {len(graph.input_nodes)}")
    if graph.output_node:
        print(f"  ✓ 输出节点: {graph.output_node.display_name}")
    if graph.warnings:
        print(f"  ⚠ {len(graph.warnings)} 条警告:")
        for w in graph.warnings:
            print(f"    - {w}")

    # ── Step 2.5: 自动创建输入节点 ──
    if args.auto_input:
        print(f"\n{'─' * 60}")
        print("Step 2.5: 自动创建输入变量节点...")
        from auto_input_generator import AutoInputGenerator, create_input_nodes_for_custom
        gen = AutoInputGenerator()
        input_vars = gen.extract_inputs(hlsl_code=hlsl_source)
        print(f"  ✓ 发现 {len(input_vars)} 个外部输入变量:")
        for iv in input_vars:
            type_str = iv.var_type
            if iv.is_builtin:
                type_str = f'builtin ({iv.display_name})'
            elif iv.is_texture:
                type_str = 'texture'
            print(f"    - {iv.name}: {type_str} [{iv.param_type}]")

        # 自动添加输入节点到 graph
        graph.auto_create_inputs(hlsl_source)

    # 确保输出目录存在
    os.makedirs(args.output, exist_ok=True)

    # ── Step 3: 生成 HTML 预览 ──
    if not args.no_html:
        print(f"\n{'─' * 60}")
        print("Step 3: 生成 HTML 节点图预览...")
        html_path = os.path.join(args.output, f'{source_name}_nodes.html')
        save_html(graph, html_path, f'{source_name} - Material Node Graph')
        print(f"  ✓ 已保存: {html_path}")

    # ── Step 4: 生成 UE4 脚本 ──
    if not args.no_script:
        print(f"\n{'─' * 60}")
        print("Step 4: 生成 UE4 Editor Python 脚本...")
        script_path = os.path.join(args.output, f'{source_name}_ue4_script.py')
        script = generate_ue4_script(graph, source_name, args.path, script_path)
        print(f"  ✓ 已保存: {script_path}")
        print(f"  ✓ 材质名称: {source_name}")
        print(f"  ✓ 材质路径: {args.path}/{source_name}")

    # ── 完成 ──
    print(f"\n{'═' * 60}")
    print("  ✅ 转换完成！")
    print(f"{'═' * 60}")

    if not args.no_html:
        print(f"\n📊 预览节点图: {html_path}")
    if not args.no_script:
        print(f"\n🎮 在 UE4 中使用:")
        print(f"   1. 打开 UE4 编辑器")
        print(f"   2. Window → Developer Tools → Output Log")
        print(f"   3. 底部下拉选择 'Python'")
        print(f'   4. 输入: exec(open(r"{os.path.abspath(script_path)}").read())')
    print()

    return html_path if not args.no_html else None


def _do_reverse_conversion(args):
    """执行反向转换：MaterialGraph → HLSL"""
    import json
    from reverse_converter import material_graph_to_hlsl, reverse_from_hlsl

    print(f"\n{'═' * 60}")
    print(f"  Material Graph → HLSL Reverse Converter")
    print(f"{'═' * 60}")

    # 获取输入源
    input_source = None
    input_type = None  # 'hlsl' or 'json'

    if args.example:
        input_source = EXAMPLE_HLSL[args.example]
        input_type = 'hlsl'
        print(f"\n使用内置示例: {args.example}")
    elif args.code:
        input_source = args.code
        input_type = 'hlsl'
    elif args.hlsl_file:
        if not os.path.exists(args.hlsl_file):
            print(f"错误：文件不存在 - {args.hlsl_file}")
            sys.exit(1)
        with open(args.hlsl_file, 'r', encoding='utf-8') as f:
            input_source = f.read()
        # 检测文件类型
        if args.hlsl_file.endswith('.json'):
            input_type = 'json'
        else:
            input_type = 'hlsl'
    else:
        print("未指定输入，使用默认示例 (fresnel)")
        input_source = EXAMPLE_HLSL['fresnel']
        input_type = 'hlsl'

    if input_type == 'json':
        # 从 MaterialGraph JSON 加载
        print(f"\n从 MaterialGraph JSON 反向转换...")
        try:
            graph_data = json.loads(input_source)
            # 重建 MaterialGraph
            from node_mapper import MaterialGraph, MaterialNode
            graph = _rebuild_graph_from_json(graph_data)
            hlsl_result = material_graph_to_hlsl(graph)
        except (json.JSONDecodeError, KeyError) as e:
            print(f"\n错误：无法解析 JSON - {e}")
            sys.exit(1)
    else:
        # 从 HLSL 代码双向转换
        print(f"\n原始 HLSL:")
        for i, line in enumerate(input_source.strip().split('\n'), 1):
            print(f"  {i:3d} | {line}")

        print(f"\n{'─' * 60}")
        print("Step 1: HLSL → MaterialGraph...")
        graph = hlsl_to_material_graph(input_source)
        print(f"  生成 {len(graph.nodes)} 个节点")

        print(f"\n{'─' * 60}")
        print("Step 2: MaterialGraph → HLSL...")
        hlsl_result = material_graph_to_hlsl(graph)

    # 输出结果
    print(f"\n{'─' * 60}")
    print("反向转换结果:\n")
    for i, line in enumerate(hlsl_result.strip().split('\n'), 1):
        print(f"  {i:3d} | {line}")

    # 保存到文件
    os.makedirs(args.output, exist_ok=True)
    source_name = args.name if args.name != 'M_Generated' else 'reversed'
    out_path = os.path.join(args.output, f'{source_name}_reversed.hlsl')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(hlsl_result)

    print(f"\n{'═' * 60}")
    print(f"  HLSL 代码已保存: {out_path}")
    print(f"{'═' * 60}\n")


def _rebuild_graph_from_json(data: dict):
    """从 JSON 数据重建 MaterialGraph"""
    from node_mapper import MaterialGraph, MaterialNode

    graph = MaterialGraph()
    node_map = {}  # id → MaterialNode

    # 创建所有节点
    for ndata in data.get('nodes', []):
        node = MaterialNode(
            id=ndata.get('id', 0),
            ue_class=ndata.get('ue_class', ''),
            display_name=ndata.get('display_name', ''),
            properties=ndata.get('properties', {}),
            input_names=ndata.get('input_names', []),
        )
        graph.nodes.append(node)
        node_map[node.id] = node

    # 恢复连接
    for ndata in data.get('nodes', []):
        node = node_map[ndata['id']]
        for iname, target_id in ndata.get('inputs', {}).items():
            if target_id is not None and target_id in node_map:
                node.inputs[iname] = node_map[target_id]

    # 设置输出节点
    output_id = data.get('output_node_id')
    if output_id is not None and output_id in node_map:
        graph.output_node = node_map[output_id]

    return graph


if __name__ == '__main__':
    main()
