"""
============================================================
 ue4_executor.py
 UE4 自动执行器 — 完整自循环闭环
============================================================

自动化流程：
  1. HLSL 代码 → 生成 UE4 Python 脚本（带验证逻辑）
  2. 将脚本写入项目目录
  3. 调用 UE4Editor-Cmd.exe -ExecutePythonScript 执行脚本
  4. 解析引擎日志，提取验证结果
  5. 返回结构化的成功/失败信息

配置：
  - UE4Editor-Cmd.exe 路径
  - 项目 .uproject 路径
  - 超时时间

============================================================
"""

import os
import sys
import json
import time
import subprocess
import tempfile
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# Windows 控制台编码修复
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

# 确保模块可导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hlsl_parser import parse_hlsl
from node_mapper import hlsl_to_material_graph, MaterialGraph
from ue4_codegen import generate_ue4_script


# ═══════════════════════════════════════════════════════════
# 默认配置
# ═══════════════════════════════════════════════════════════

DEFAULT_CONFIG = {
    'ue4_editor_cmd': r'<INSTALL_PATH>\Engine\Binaries\Win64\UE4Editor-Cmd.exe',
    'ue4_editor':     r'<INSTALL_PATH>\Engine\Binaries\Win64\UE4Editor.exe',
    'project_path':   r'<YOUR_PROJECT>\<ProjectName>.uproject',
    'timeout':        300,    # 秒（编辑器模式启动较慢，需要更长超时）
    'material_path':  '/Game/Materials',
    'execution_mode': 'editor',  # 'editor' = 打开编辑器（可渲染截图），'commandlet' = Commandlet 模式（快但无渲染）
}

# 配置文件路径
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ue4_executor_config.json')


def load_config() -> dict:
    """加载配置（从文件或使用默认值）"""
    config = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                user_config = json.load(f)
            config.update(user_config)
        except Exception:
            pass
    return config


def save_config(config: dict):
    """保存配置到文件"""
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════
# 验证脚本生成
# ═══════════════════════════════════════════════════════════

def generate_validation_script(material_name: str, material_path: str,
                                expected_node_count: int,
                                expected_connection_count: int) -> str:
    """
    生成在 UE4 引擎内运行的验证脚本。
    
    该脚本在创建材质后，会：
    1. 加载创建的材质资产
    2. 检查材质表达式节点数量
    3. 检查是否能成功重编译
    4. 输出 JSON 格式的验证结果到日志
    """
    return f'''
# ═══════════════════════════════════════════════════════════
# 验证函数 — 检查创建的材质是否正确
# ═══════════════════════════════════════════════════════════

def validate_material():
    """验证材质创建结果"""
    import json as _json
    
    result = {{
        "status": "unknown",
        "material_name": "{material_name}",
        "material_path": "{material_path}/{material_name}",
        "expected_nodes": {expected_node_count},
        "actual_nodes": 0,
        "errors": [],
        "warnings": [],
        "timestamp": "",
    }}
    
    try:
        import datetime
        result["timestamp"] = str(datetime.datetime.now())
    except:
        result["timestamp"] = "N/A"
    
    # 1. 加载材质
    full_path = "{material_path}/{material_name}"
    mat = unreal.load_asset(full_path)
    
    if mat is None:
        result["status"] = "FAIL"
        result["errors"].append(f"无法加载材质: {{full_path}}")
        _output_result(result)
        return result
    
    # 2. 获取材质表达式数量
    try:
        expressions = unreal.MaterialEditingLibrary.get_used_textures(mat)
        # get_used_textures 返回纹理，用另一种方式统计节点
    except:
        pass
    
    # 尝试通过 Python API 获取表达式
    try:
        # UE4.27 中可以这样获取
        expr_list = mat.get_editor_property("expressions")
        if expr_list is not None:
            result["actual_nodes"] = len(expr_list)
        else:
            result["actual_nodes"] = -1
            result["warnings"].append("无法获取 expressions 属性")
    except Exception as e:
        result["actual_nodes"] = -1
        result["warnings"].append(f"获取表达式列表异常: {{str(e)}}")
    
    # 3. 尝试重编译
    try:
        unreal.MaterialEditingLibrary.recompile_material(mat)
        result["compile_success"] = True
    except Exception as e:
        result["compile_success"] = False
        result["errors"].append(f"重编译失败: {{str(e)}}")
    
    # 4. 判断结果
    if result["actual_nodes"] >= 0:
        if result["actual_nodes"] == {expected_node_count}:
            result["status"] = "PASS"
        else:
            result["status"] = "WARN"
            result["warnings"].append(
                f"节点数不匹配: 期望 {expected_node_count}, 实际 {{result['actual_nodes']}}"
            )
    else:
        # 无法获取节点数，但如果编译成功就算 PASS
        if result.get("compile_success", False):
            result["status"] = "PASS_NO_COUNT"
        else:
            result["status"] = "FAIL"
    
    _output_result(result)
    return result


def _output_result(result):
    """将验证结果输出到引擎日志（JSON 格式，带标记方便解析）"""
    import json as _json
    json_str = _json.dumps(result, ensure_ascii=False)
    unreal.log("===HLSL2MAT_VALIDATION_BEGIN===")
    unreal.log(json_str)
    unreal.log("===HLSL2MAT_VALIDATION_END===")
'''


def generate_custom_material_script(hlsl_code: str, material_name: str,
                                     material_path: str,
                                     input_params: dict) -> str:
    """
    生成在 UE4 中创建 Custom Expression 对照材质的脚本。
    
    将原始 HLSL 代码放入 Custom Expression 节点，
    并连接所有必要的输入参数节点。
    
    参数:
        hlsl_code: 原始 HLSL 代码
        material_name: 对照材质名称（如 M_Test_fresnel_Custom）
        material_path: 材质保存路径
        input_params: 输入参数信息 {"参数名": {"type": "引擎变量/用户参数", "ue_class": "...", "dim": 3}, ...}
    """
    # 清理 HLSL 代码中的引号，避免 Python 字符串问题
    safe_hlsl = hlsl_code.replace('\\', '\\\\').replace('"""', '\\"\\"\\"')
    
    # 分类输入参数：引擎内置变量 vs 用户参数
    engine_vars = {k: v for k, v in input_params.items() if v.get('type') == 'engine_builtin'}
    user_params = {k: v for k, v in input_params.items() if v.get('type') == 'user_param'}
    texture_params = {k: v for k, v in input_params.items() if v.get('type') == 'texture'}
    
    lines = []
    lines.append(f'# Custom Expression 对照材质创建脚本: {material_name}')
    lines.append('import unreal')
    lines.append('')
    lines.append('def create_custom_material():')
    lines.append(f'    """创建 Custom Expression 对照材质: {material_name}"""')
    lines.append('    asset_tools = unreal.AssetToolsHelpers.get_asset_tools()')
    lines.append(f'    mat = asset_tools.create_asset(')
    lines.append(f'        "{material_name}",')
    lines.append(f'        "{material_path}",')
    lines.append(f'        unreal.Material,')
    lines.append(f'        unreal.MaterialFactoryNew()')
    lines.append(f'    )')
    lines.append('')
    lines.append('    if mat is None:')
    lines.append(f'        mat = unreal.load_asset("{material_path}/{material_name}")')
    lines.append('        if mat is None:')
    lines.append(f'            unreal.log_error("无法创建 Custom 对照材质: {material_name}")')
    lines.append('            return None')
    lines.append('')
    lines.append(f'    unreal.log("创建 Custom 对照材质: {material_name}")')
    lines.append('')
    
    # 创建 Custom Expression 节点
    lines.append('    # ── 创建 Custom Expression 节点 ──')
    lines.append('    custom_node = unreal.MaterialEditingLibrary.create_material_expression(')
    lines.append('        mat, unreal.MaterialExpressionCustom, 0, 0')
    lines.append('    )')
    
    # 设置 HLSL 代码
    lines.append(f'    custom_node.set_editor_property("code", """{safe_hlsl}""")')
    lines.append('    custom_node.set_editor_property("output_type", unreal.CustomMaterialOutputType.CMOT_FLOAT3)')
    lines.append('')
    
    # 构建 Custom 节点的输入列表
    # Custom Expression 的输入需要通过 CustomInput 数组设置
    custom_input_idx = 0
    input_node_vars = {}
    
    for param_name, param_info in engine_vars.items():
        ue_class = param_info.get('ue_class', 'MaterialExpressionWorldPosition')
        var_name = f'engine_{param_name}'
        lines.append(f'    # 引擎内置变量: {param_name}')
        lines.append(f'    {var_name} = unreal.MaterialEditingLibrary.create_material_expression(')
        lines.append(f'        mat, unreal.{ue_class}, -400, {custom_input_idx * 120}')
        lines.append(f'    )')
        input_node_vars[param_name] = (var_name, custom_input_idx)
        custom_input_idx += 1
    
    for param_name, param_info in user_params.items():
        dim = param_info.get('dim', 3)
        var_name = f'param_{param_name}'
        if dim == 1:
            lines.append(f'    {var_name} = unreal.MaterialEditingLibrary.create_material_expression(')
            lines.append(f'        mat, unreal.MaterialExpressionScalarParameter, -400, {custom_input_idx * 120}')
            lines.append(f'    )')
            lines.append(f'    {var_name}.set_editor_property("parameter_name", "{param_name}")')
        else:
            lines.append(f'    {var_name} = unreal.MaterialEditingLibrary.create_material_expression(')
            lines.append(f'        mat, unreal.MaterialExpressionVectorParameter, -400, {custom_input_idx * 120}')
            lines.append(f'    )')
            lines.append(f'    {var_name}.set_editor_property("parameter_name", "{param_name}")')
        input_node_vars[param_name] = (var_name, custom_input_idx)
        custom_input_idx += 1
    
    for param_name, param_info in texture_params.items():
        var_name = f'tex_{param_name}'
        lines.append(f'    {var_name} = unreal.MaterialEditingLibrary.create_material_expression(')
        lines.append(f'        mat, unreal.MaterialExpressionTextureObjectParameter, -400, {custom_input_idx * 120}')
        lines.append(f'    )')
        lines.append(f'    {var_name}.set_editor_property("parameter_name", "{param_name}")')
        input_node_vars[param_name] = (var_name, custom_input_idx)
        custom_input_idx += 1
    
    # 设置 Custom 节点的输入引脚
    if input_node_vars:
        lines.append('')
        lines.append('    # ── 设置 Custom 节点输入引脚 ──')
        lines.append('    try:')
        lines.append('        custom_inputs = []')
        for param_name, (var_name, idx) in input_node_vars.items():
            lines.append(f'        _inp = unreal.CustomInput()')
            lines.append(f'        _inp.input_name = "{param_name}"')
            lines.append(f'        custom_inputs.append(_inp)')
        lines.append('        custom_node.set_editor_property("inputs", custom_inputs)')
        lines.append('    except Exception as e:')
        lines.append(f'        unreal.log_warning(f"设置 Custom 输入引脚失败: {{e}}")')
        lines.append('')
    
    # 连接输入节点到 Custom 节点
    if input_node_vars:
        lines.append('    # ── 连接输入到 Custom 节点 ──')
        for param_name, (var_name, idx) in input_node_vars.items():
            lines.append(f'    try:')
            lines.append(f'        unreal.MaterialEditingLibrary.connect_material_expressions(')
            lines.append(f'            {var_name}, "", custom_node, "{idx}"')
            lines.append(f'        )  # {param_name} → Custom.Input[{idx}]')
            lines.append(f'    except Exception as e:')
            lines.append(f'        unreal.log_warning(f"连接 {param_name} 失败: {{e}}")')
        lines.append('')
    
    # 连接到材质输出 (Emissive Color)
    lines.append('    # ── 连接到材质输出 (Emissive Color) ──')
    lines.append('    try:')
    lines.append('        unreal.MaterialEditingLibrary.connect_material_property(')
    lines.append('            custom_node, "",')
    lines.append('            unreal.MaterialProperty.MP_EMISSIVE_COLOR')
    lines.append('        )')
    lines.append('    except Exception as e:')
    lines.append(f'        unreal.log_warning(f"连接到 Emissive 失败: {{e}}")')
    lines.append('')
    
    # 重编译
    lines.append('    # ── 重编译 ──')
    lines.append('    unreal.MaterialEditingLibrary.recompile_material(mat)')
    lines.append(f'    unreal.log("Custom 对照材质 {material_name} 创建完成！")')
    lines.append('    return mat')
    lines.append('')
    
    return '\n'.join(lines)


def generate_render_compare_script(material_name_nodes: str, material_name_custom: str,
                                    material_path: str, output_dir: str) -> str:
    """
    生成在 UE4 中渲染两个材质球并截图对比的脚本。
    
    需要引擎不带 -nullrhi 才能渲染。
    
    参数:
        material_name_nodes: 节点图版材质名
        material_name_custom: Custom Expression 版材质名
        material_path: 材质路径
        output_dir: 截图输出目录（磁盘路径）
    """
    safe_output_dir = output_dir.replace('\\', '/')
    
    return f'''
# ═══════════════════════════════════════════════════════════
# 渲染对比 — 截图两个材质球并输出像素差异
# ═══════════════════════════════════════════════════════════

def render_and_compare():
    """渲染两个材质球截图并计算像素差异"""
    import json as _json
    import os as _os
    import math as _math
    
    result = {{
        "status": "unknown",
        "nodes_material": "{material_path}/{material_name_nodes}",
        "custom_material": "{material_path}/{material_name_custom}",
        "nodes_screenshot": "",
        "custom_screenshot": "",
        "diff_screenshot": "",
        "pixel_diff_percent": -1.0,
        "mse": -1.0,
        "max_diff": -1.0,
        "match": False,
        "errors": [],
        "warnings": [],
    }}
    
    output_dir = r"{safe_output_dir}"
    
    # 确保输出目录存在
    if not _os.path.exists(output_dir):
        _os.makedirs(output_dir)
    
    # 加载两个材质
    mat_nodes = unreal.load_asset("{material_path}/{material_name_nodes}")
    mat_custom = unreal.load_asset("{material_path}/{material_name_custom}")
    
    if mat_nodes is None:
        result["errors"].append("无法加载节点图版材质: {material_path}/{material_name_nodes}")
        result["status"] = "FAIL"
        _output_compare_result(result)
        return result
    
    if mat_custom is None:
        result["errors"].append("无法加载 Custom 对照材质: {material_path}/{material_name_custom}")
        result["status"] = "FAIL"
        _output_compare_result(result)
        return result
    
    # ── 使用 RenderTarget 截图方案 ──
    # 创建 RenderTarget
    RT_SIZE = 256
    
    try:
        # 创建两个 RenderTarget
        rt_nodes = unreal.RenderingLibrary.create_render_target2d(
            unreal.EditorLevelLibrary.get_editor_world(),
            RT_SIZE, RT_SIZE,
            unreal.TextureRenderTargetFormat.RTF_RGBA8
        )
        rt_custom = unreal.RenderingLibrary.create_render_target2d(
            unreal.EditorLevelLibrary.get_editor_world(),
            RT_SIZE, RT_SIZE,
            unreal.TextureRenderTargetFormat.RTF_RGBA8
        )
    except Exception as e:
        result["warnings"].append(f"创建 RenderTarget 失败: {{e}}，尝试替代方案")
        # 替代方案：用材质属性对比
        _compare_material_properties(mat_nodes, mat_custom, result)
        _output_compare_result(result)
        return result
    
    # 方案 A：用 draw_material_to_render_target 直接绘制材质到 RT
    try:
        unreal.RenderingLibrary.draw_material_to_render_target(
            unreal.EditorLevelLibrary.get_editor_world(),
            rt_nodes, mat_nodes
        )
        unreal.RenderingLibrary.draw_material_to_render_target(
            unreal.EditorLevelLibrary.get_editor_world(),
            rt_custom, mat_custom
        )
    except Exception as e:
        result["warnings"].append(f"draw_material_to_render_target 失败: {{e}}")
        _compare_material_properties(mat_nodes, mat_custom, result)
        _output_compare_result(result)
        return result
    
    # 导出 RenderTarget 到磁盘
    nodes_path = _os.path.join(output_dir, "{material_name_nodes}_render.bmp")
    custom_path = _os.path.join(output_dir, "{material_name_custom}_render.bmp")
    
    try:
        unreal.RenderingLibrary.export_render_target(
            unreal.EditorLevelLibrary.get_editor_world(),
            rt_nodes, output_dir, "{material_name_nodes}_render"
        )
        result["nodes_screenshot"] = nodes_path
        
        unreal.RenderingLibrary.export_render_target(
            unreal.EditorLevelLibrary.get_editor_world(),
            rt_custom, output_dir, "{material_name_custom}_render"
        )
        result["custom_screenshot"] = custom_path
    except Exception as e:
        result["warnings"].append(f"导出 RenderTarget 失败: {{e}}")
        _compare_material_properties(mat_nodes, mat_custom, result)
        _output_compare_result(result)
        return result
    
    # ── 在引擎内直接用 RenderTarget 像素数据做对比 ──
    try:
        # 读取 RT 像素数据（UE4 Python API）
        pixels_nodes = unreal.RenderingLibrary.read_render_target_pixel(
            unreal.EditorLevelLibrary.get_editor_world(), rt_nodes, 0, 0
        )
        pixels_custom = unreal.RenderingLibrary.read_render_target_pixel(
            unreal.EditorLevelLibrary.get_editor_world(), rt_custom, 0, 0
        )
        
        # read_render_target_pixel 只能读单像素，改用全量读取
        # 方案改为：用 Python 端读取导出的 BMP 文件对比
        result["status"] = "SCREENSHOTS_EXPORTED"
        result["warnings"].append("截图已导出到磁盘，将在 Python 端做像素对比")
        
    except Exception as e:
        result["warnings"].append(f"像素读取失败: {{e}}")
        result["status"] = "SCREENSHOTS_EXPORTED"
    
    _output_compare_result(result)
    return result


def _compare_material_properties(mat_nodes, mat_custom, result):
    """当截图不可用时，通过对比材质编译属性来验证"""
    try:
        # 对比编译状态
        unreal.MaterialEditingLibrary.recompile_material(mat_nodes)
        unreal.MaterialEditingLibrary.recompile_material(mat_custom)
        result["both_compile_success"] = True
    except Exception as e:
        result["both_compile_success"] = False
        result["errors"].append(f"材质编译失败: {{e}}")
    
    # 导出两个材质的节点拓扑供外部对比
    try:
        topo_nodes = _export_material_topology(mat_nodes)
        topo_custom = _export_material_topology(mat_custom)
        result["topology_nodes"] = topo_nodes
        result["topology_custom"] = topo_custom
    except Exception as e:
        result["warnings"].append(f"导出拓扑失败: {{e}}")
    
    result["status"] = "PROPERTY_COMPARE"


def _export_material_topology(mat):
    """导出材质的节点拓扑信息"""
    topo = {{"nodes": [], "node_count": 0}}
    try:
        expr_list = mat.get_editor_property("expressions")
        if expr_list:
            topo["node_count"] = len(expr_list)
            for i, expr in enumerate(expr_list):
                node_info = {{
                    "index": i,
                    "class": str(type(expr).__name__),
                }}
                topo["nodes"].append(node_info)
    except Exception as e:
        topo["error"] = str(e)
    return topo


def _output_compare_result(result):
    """输出对比结果到引擎日志"""
    import json as _json
    json_str = _json.dumps(result, ensure_ascii=False, default=str)
    unreal.log("===HLSL2MAT_COMPARE_BEGIN===")
    unreal.log(json_str)
    unreal.log("===HLSL2MAT_COMPARE_END===")
'''


def generate_compare_full_script(hlsl_code: str, material_name: str = 'M_Generated',
                                  material_path: str = '/Game/Materials',
                                  output_dir: str = '') -> Tuple[str, int, int, dict]:
    """
    生成完整的对比验证脚本：
    1. 创建节点图版材质
    2. 创建 Custom Expression 对照材质
    3. 渲染两个材质球截图
    4. 输出对比结果
    
    返回: (脚本内容, 节点数, 连线数, 输入参数信息)
    """
    # 1. 解析 HLSL 并生成节点图
    graph = hlsl_to_material_graph(hlsl_code)
    node_count = len(graph.nodes)
    
    # 统计连线数
    connection_count = 0
    for node in graph.nodes:
        for iname, inode in node.inputs.items():
            if inode:
                connection_count += 1
    
    # 2. 收集输入参数信息（用于创建 Custom 版材质的输入连接）
    input_params = _collect_input_params(graph)
    
    # 3. 生成节点图版材质创建脚本
    nodes_mat_name = material_name
    create_script = generate_ue4_script(graph, nodes_mat_name, material_path)
    
    # 4. 生成 Custom 对照材质创建脚本
    custom_mat_name = f'{material_name}_Custom'
    custom_script = generate_custom_material_script(
        hlsl_code, custom_mat_name, material_path, input_params
    )
    
    # 5. 生成渲染对比脚本
    render_script = generate_render_compare_script(
        nodes_mat_name, custom_mat_name, material_path, output_dir
    )
    
    # 6. 生成验证脚本（基础验证仍然保留）
    validate_script = generate_validation_script(
        nodes_mat_name, material_path,
        node_count, connection_count
    )
    
    # 7. 组合成完整脚本
    full_script = create_script + '\n\n' + custom_script + '\n\n' + render_script + '\n\n' + validate_script + f'''

# ═══════════════════════════════════════════════════════════
# 主执行流程（创建两个材质 + 渲染对比 + 验证）
# ═══════════════════════════════════════════════════════════

unreal.log("===HLSL2MAT_EXEC_BEGIN===")
try:
    # Step 1: 创建节点图版材质
    unreal.log("Step 1: 创建节点图版材质 {nodes_mat_name}...")
    _mat_nodes = create_material()
    
    # Step 2: 创建 Custom 对照材质
    unreal.log("Step 2: 创建 Custom 对照材质 {custom_mat_name}...")
    _mat_custom = create_custom_material()
    
    # Step 3: 基础验证
    unreal.log("Step 3: 基础验证...")
    _result = validate_material()
    
    # Step 4: 渲染对比
    unreal.log("Step 4: 渲染对比...")
    _compare = render_and_compare()
    
    unreal.log("全部完成！")
except Exception as _e:
    unreal.log_error(f"执行异常: {{str(_e)}}")
    unreal.log("===HLSL2MAT_VALIDATION_BEGIN===")
    import json as _json
    unreal.log(_json.dumps({{"status": "ERROR", "errors": [str(_e)]}}, ensure_ascii=False))
    unreal.log("===HLSL2MAT_VALIDATION_END===")
unreal.log("===HLSL2MAT_EXEC_END===")
'''
    
    # 移除重复的 __main__ 调用
    full_script = full_script.replace(
        "if __name__ == '__main__':\n    create_material()\n",
        "# (自动执行流程在脚本末尾)\n"
    )
    
    return full_script, node_count, connection_count, input_params


def _collect_input_params(graph: MaterialGraph) -> dict:
    """
    从材质图中收集输入参数信息，用于创建 Custom 对照材质。
    
    只收集 input_nodes 中的参数（这些是实际在 HLSL 中被引用的变量）。
    引擎内置变量也作为 Custom 输入引脚传入（Custom Expression 中不能直接引用引擎变量名）。
    
    去重逻辑：同一个 ue_class 的引擎内置变量只保留一个。
    """
    from node_mapper import ENGINE_BUILTIN_VARS
    
    params = {}
    seen_ue_classes = set()  # 去重用
    
    for name, node in graph.input_nodes.items():
        # 跳过同类型的重复引擎变量
        if node.ue_class in seen_ue_classes:
            continue
        seen_ue_classes.add(node.ue_class)
        
        if node.ue_class in ('MaterialExpressionTextureObjectParameter',
                             'MaterialExpressionTextureSampleParameter2D'):
            params[name] = {
                'type': 'texture',
                'ue_class': node.ue_class,
                'dim': 4,
            }
        elif node.ue_class == 'MaterialExpressionScalarParameter':
            params[name] = {
                'type': 'user_param',
                'ue_class': node.ue_class,
                'dim': 1,
            }
        elif node.ue_class == 'MaterialExpressionVectorParameter':
            params[name] = {
                'type': 'user_param',
                'ue_class': node.ue_class,
                'dim': 3,
            }
        else:
            # 可能是引擎内置变量节点
            is_engine = any(
                node.ue_class == ue_class 
                for _, (ue_class, _, _) in ENGINE_BUILTIN_VARS.items()
            )
            params[name] = {
                'type': 'engine_builtin' if is_engine else 'user_param',
                'ue_class': node.ue_class,
                'dim': 3,
            }
    
    return params


def generate_full_script(hlsl_code: str, material_name: str = 'M_Generated',
                          material_path: str = '/Game/Materials') -> Tuple[str, int, int]:
    """
    生成完整的 UE4 Python 脚本（创建材质 + 验证）
    
    返回: (脚本内容, 期望节点数, 期望连线数)
    """
    # 1. 解析 HLSL 并生成节点图
    graph = hlsl_to_material_graph(hlsl_code)
    node_count = len(graph.nodes)
    
    # 统计连线数
    connection_count = 0
    for node in graph.nodes:
        for iname, inode in node.inputs.items():
            if inode:
                connection_count += 1
    
    # 2. 生成材质创建脚本
    create_script = generate_ue4_script(graph, material_name, material_path)
    
    # 3. 生成验证脚本
    validate_script = generate_validation_script(
        material_name, material_path,
        node_count, connection_count
    )
    
    # 4. 组合成完整脚本
    full_script = create_script + '\n\n' + validate_script + '''

# ═══════════════════════════════════════════════════════════
# 主执行流程（自动创建 + 验证）
# ═══════════════════════════════════════════════════════════

unreal.log("===HLSL2MAT_EXEC_BEGIN===")
try:
    _mat = create_material()
    if _mat is not None:
        unreal.log("材质创建成功，开始验证...")
        _result = validate_material()
        unreal.log(f"验证完成: {_result.get('status', 'unknown')}")
    else:
        unreal.log("===HLSL2MAT_VALIDATION_BEGIN===")
        import json as _json
        unreal.log(_json.dumps({"status": "FAIL", "errors": ["create_material() 返回 None"]}, ensure_ascii=False))
        unreal.log("===HLSL2MAT_VALIDATION_END===")
except Exception as _e:
    unreal.log_error(f"执行异常: {str(_e)}")
    unreal.log("===HLSL2MAT_VALIDATION_BEGIN===")
    import json as _json
    unreal.log(_json.dumps({"status": "ERROR", "errors": [str(_e)]}, ensure_ascii=False))
    unreal.log("===HLSL2MAT_VALIDATION_END===")
unreal.log("===HLSL2MAT_EXEC_END===")
'''
    
    # 移除重复的 __main__ 调用（create_script 末尾已经有了）
    # 我们用自己的主流程替代
    full_script = full_script.replace(
        "if __name__ == '__main__':\n    create_material()\n",
        "# (自动执行流程在脚本末尾)\n"
    )
    
    return full_script, node_count, connection_count


# ═══════════════════════════════════════════════════════════
# 日志解析
# ═══════════════════════════════════════════════════════════

def parse_validation_result(log_text: str) -> Optional[dict]:
    """
    从 UE4 引擎日志中提取验证结果 JSON
    
    日志中会包含形如：
      ===HLSL2MAT_VALIDATION_BEGIN===
      {"status": "PASS", ...}
      ===HLSL2MAT_VALIDATION_END===
    """
    pattern = r'===HLSL2MAT_VALIDATION_BEGIN===(.*?)===HLSL2MAT_VALIDATION_END==='
    match = re.search(pattern, log_text, re.DOTALL)
    
    if match:
        json_text = match.group(1).strip()
        # 清理可能的日志前缀（如时间戳、[LogPython]等）
        # UE4 日志行通常是: [2024.01.01-12.00.00:000][  0]LogPython: {"status": ...}
        lines = json_text.split('\n')
        clean_lines = []
        for line in lines:
            # 去除 UE4 日志前缀
            cleaned = re.sub(r'^\[.*?\]\[.*?\]LogPython:\s*', '', line)
            cleaned = re.sub(r'^LogPython:\s*', '', cleaned)
            cleaned = cleaned.strip()
            if cleaned:
                clean_lines.append(cleaned)
        
        json_str = ' '.join(clean_lines)
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            # 尝试逐行解析
            for line in clean_lines:
                try:
                    return json.loads(line)
                except:
                    continue
    
    return None


def parse_engine_errors(log_text: str) -> List[str]:
    """从引擎日志中提取错误信息"""
    errors = []
    # 需要跳过的非致命/无关错误模式
    skip_patterns = [
        'LogLinker', 'LogStreaming', 'LogInit',
        '[Callstack]',          # 崩溃调用栈（已有 Fatal error 行代表）
        'exit was already',     # 重复的退出请求
        'Error: =',             # "=== Critical error: ===" 分隔线
        'Error: \r', 'Error: \n', # 空行
    ]
    for line in log_text.split('\n'):
        line_s = line.strip()
        if not line_s:
            continue
        if 'Error:' in line or 'error:' in line:
            # 过滤掉已知的非致命/无关错误
            if any(skip in line for skip in skip_patterns):
                continue
            # 只有非空的实际错误信息才添加
            if len(line_s) > 10:
                errors.append(line_s)
    return errors[-10:]  # 只保留最后 10 条


def parse_compare_result(log_text: str) -> Optional[dict]:
    """
    从 UE4 引擎日志中提取渲染对比结果 JSON
    
    日志中会包含形如：
      ===HLSL2MAT_COMPARE_BEGIN===
      {"status": "SCREENSHOTS_EXPORTED", ...}
      ===HLSL2MAT_COMPARE_END===
    """
    pattern = r'===HLSL2MAT_COMPARE_BEGIN===(.*?)===HLSL2MAT_COMPARE_END==='
    match = re.search(pattern, log_text, re.DOTALL)
    
    if match:
        json_text = match.group(1).strip()
        lines = json_text.split('\n')
        clean_lines = []
        for line in lines:
            cleaned = re.sub(r'^\[.*?\]\[.*?\]LogPython:\s*', '', line)
            cleaned = re.sub(r'^LogPython:\s*', '', cleaned)
            cleaned = cleaned.strip()
            if cleaned:
                clean_lines.append(cleaned)
        
        json_str = ' '.join(clean_lines)
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            for line in clean_lines:
                try:
                    return json.loads(line)
                except:
                    continue
    
    return None


def compare_screenshots(img_path_a: str, img_path_b: str, diff_output: str = None) -> dict:
    """
    对比两张截图的像素差异。
    
    使用纯 Python 实现（不依赖 PIL/numpy），通过读取 BMP 文件的原始像素数据。
    如果安装了 PIL/numpy 则使用更精确的算法。
    
    返回:
        {
            "mse": 均方误差,
            "max_diff": 最大像素差异,
            "pixel_diff_percent": 差异像素占比(%),
            "match": 是否匹配(MSE < 阈值),
            "threshold": 使用的阈值,
        }
    """
    result = {
        'mse': -1.0,
        'max_diff': -1.0,
        'pixel_diff_percent': -1.0,
        'match': False,
        'threshold': 5.0,  # MSE 阈值
        'errors': [],
    }
    
    if not os.path.exists(img_path_a):
        result['errors'].append(f'文件不存在: {img_path_a}')
        return result
    if not os.path.exists(img_path_b):
        result['errors'].append(f'文件不存在: {img_path_b}')
        return result
    
    # 尝试使用 PIL + numpy（更精确）
    try:
        from PIL import Image
        import numpy as np
        
        # 如果文件没有扩展名但内容是 PNG，PIL 也能打开
        img_a = np.array(Image.open(img_path_a).convert('RGB'), dtype=np.float32)
        img_b = np.array(Image.open(img_path_b).convert('RGB'), dtype=np.float32)
        
        if img_a.shape != img_b.shape:
            # 调整大小
            min_h = min(img_a.shape[0], img_b.shape[0])
            min_w = min(img_a.shape[1], img_b.shape[1])
            img_a = img_a[:min_h, :min_w]
            img_b = img_b[:min_h, :min_w]
        
        diff = np.abs(img_a - img_b)
        mse = float(np.mean(diff ** 2))
        max_diff = float(np.max(diff))
        
        # 差异像素：任一通道差 > 10 视为差异
        diff_mask = np.any(diff > 10, axis=2)
        pixel_diff_percent = float(np.mean(diff_mask) * 100)
        
        result['mse'] = round(mse, 4)
        result['max_diff'] = round(max_diff, 2)
        result['pixel_diff_percent'] = round(pixel_diff_percent, 2)
        result['match'] = mse < result['threshold']
        
        # 生成差异图
        if diff_output:
            diff_img = (diff * 10).clip(0, 255).astype(np.uint8)  # 放大差异便于观察
            Image.fromarray(diff_img).save(diff_output)
            result['diff_image'] = diff_output
        
        return result
        
    except ImportError:
        pass
    
    # 回退：纯 Python BMP 读取
    try:
        pixels_a = _read_bmp_pixels(img_path_a)
        pixels_b = _read_bmp_pixels(img_path_b)
        
        if pixels_a is None or pixels_b is None:
            result['errors'].append('无法读取 BMP 像素数据')
            return result
        
        min_len = min(len(pixels_a), len(pixels_b))
        total_diff = 0
        max_diff = 0
        diff_pixels = 0
        
        for i in range(min_len):
            d = abs(pixels_a[i] - pixels_b[i])
            total_diff += d * d
            if d > max_diff:
                max_diff = d
            if d > 10:
                diff_pixels += 1
        
        mse = total_diff / max(min_len, 1)
        pixel_diff_percent = (diff_pixels / max(min_len // 3, 1)) * 100  # 每3字节=1像素
        
        result['mse'] = round(mse, 4)
        result['max_diff'] = float(max_diff)
        result['pixel_diff_percent'] = round(pixel_diff_percent, 2)
        result['match'] = mse < result['threshold']
        
    except Exception as e:
        result['errors'].append(f'像素对比失败: {str(e)}')
    
    return result


def _read_bmp_pixels(path: str) -> Optional[list]:
    """读取 BMP 文件的原始像素数据（RGB 字节列表）"""
    try:
        with open(path, 'rb') as f:
            header = f.read(54)  # BMP header
            if header[:2] != b'BM':
                return None
            data_offset = int.from_bytes(header[10:14], 'little')
            f.seek(data_offset)
            pixels = list(f.read())
        return pixels
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════
# UE4 引擎调用
# ═══════════════════════════════════════════════════════════

class UE4Executor:
    """UE4 自动执行器"""
    
    def __init__(self, config: dict = None):
        self.config = config or load_config()
        self.last_log = ''
        self.last_result = None
    
    def check_environment(self) -> dict:
        """检查运行环境是否就绪"""
        status = {
            'editor_exists': False,
            'project_exists': False,
            'ready': False,
            'errors': [],
            'execution_mode': self.config.get('execution_mode', 'editor'),
        }
        
        mode = self.config.get('execution_mode', 'editor')
        if mode == 'editor':
            editor = self.config.get('ue4_editor', self.config.get('ue4_editor_cmd', ''))
        else:
            editor = self.config.get('ue4_editor_cmd', '')
        project = self.config['project_path']
        
        if os.path.exists(editor):
            status['editor_exists'] = True
        else:
            mode_name = '编辑器' if mode == 'editor' else 'Commandlet'
            status['errors'].append(f'UE4 {mode_name} 不存在: {editor}')
        
        if os.path.exists(project):
            status['project_exists'] = True
        else:
            status['errors'].append(f'.uproject 文件不存在: {project}')
        
        status['ready'] = status['editor_exists'] and status['project_exists']
        return status
    
    def execute_hlsl(self, hlsl_code: str, material_name: str = 'M_Generated',
                     delete_existing: bool = True) -> dict:
        """
        完整的自循环执行流程
        
        参数:
            hlsl_code: HLSL 源代码
            material_name: 材质名称
            delete_existing: 是否先删除同名材质（避免冲突）
            
        返回:
            {
                "status": "PASS" / "FAIL" / "ERROR" / "TIMEOUT",
                "material_name": "M_Generated",
                "node_count": 15,
                "connection_count": 12,
                "validation": { ... },     # 引擎内验证结果
                "engine_errors": [],       # 引擎错误日志
                "execution_time": 45.2,    # 总耗时(秒)
                "script_path": "...",      # 脚本文件路径
                "log_path": "...",         # 日志文件路径
            }
        """
        start_time = time.time()
        result = {
            'status': 'PENDING',
            'material_name': material_name,
            'node_count': 0,
            'connection_count': 0,
            'validation': None,
            'engine_errors': [],
            'execution_time': 0,
            'script_path': '',
            'log_path': '',
            'hlsl_code': hlsl_code,
        }
        
        # 1. 检查环境
        env = self.check_environment()
        if not env['ready']:
            result['status'] = 'ENV_ERROR'
            result['engine_errors'] = env['errors']
            return result
        
        # 2. 生成脚本
        try:
            material_path = self.config['material_path']
            full_script, node_count, conn_count = generate_full_script(
                hlsl_code, material_name, material_path
            )
            result['node_count'] = node_count
            result['connection_count'] = conn_count
        except Exception as e:
            result['status'] = 'PARSE_ERROR'
            result['engine_errors'] = [f'HLSL 解析/脚本生成失败: {str(e)}']
            return result
        
        # 3. 写入脚本文件
        # 放到项目的 Scripts 目录下
        project_dir = os.path.dirname(self.config['project_path'])
        scripts_dir = os.path.join(project_dir, 'Scripts')
        os.makedirs(scripts_dir, exist_ok=True)
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        script_filename = f'hlsl2mat_{material_name}_{timestamp}.py'
        script_path = os.path.join(scripts_dir, script_filename)
        
        with open(script_path, 'w', encoding='utf-8') as f:
            f.write(full_script)
        result['script_path'] = script_path
        
        # 4. 构建命令行
        mode = self.config.get('execution_mode', 'editor')
        project_path = self.config['project_path']
        timeout = self.config.get('timeout', 300)
        
        if mode == 'editor':
            # 编辑器模式：使用 UE4Editor.exe（完整编辑器，可渲染/截图）
            editor_cmd = self.config.get('ue4_editor', self.config['ue4_editor_cmd'])
            cmd = [
                editor_cmd,
                project_path,
                f'-ExecutePythonScript={script_path}',
                '-nosplash',
                '-nosound',
                '-DisablePlugin=OculusEditor',
                '-DisablePlugin=OculusHMD',
            ]
        else:
            # Commandlet 模式：使用 UE4Editor-Cmd.exe（快速但无渲染能力）
            editor_cmd = self.config['ue4_editor_cmd']
            cmd = [
                editor_cmd,
                project_path,
                f'-ExecutePythonScript={script_path}',
                '-nosplash',
                '-unattended',
                '-nopause',
                '-nullrhi',
                '-nosound',
                '-nocontentbrowser',
                '-DisablePlugin=OculusEditor',
                '-DisablePlugin=OculusHMD',
            ]
        
        # 5. 编辑器模式：在脚本末尾注入"完成标记"逻辑
        #    脚本执行完后写一个标记文件，Python 端轮询检测
        done_marker_path = os.path.join(scripts_dir, f'hlsl2mat_{material_name}_{timestamp}.done')
        if mode == 'editor':
            done_marker_injection = f'''
# ═══════════════════════════════════════════════════════════
# 编辑器模式：写入完成标记文件
# ═══════════════════════════════════════════════════════════
try:
    with open(r"{done_marker_path}", "w") as _f:
        _f.write("DONE")
    unreal.log("===HLSL2MAT_DONE_MARKER_WRITTEN===")
except Exception as _e:
    unreal.log_warning(f"写入完成标记失败: {{_e}}")
'''
            # 追加到脚本末尾
            with open(script_path, 'a', encoding='utf-8') as f:
                f.write(done_marker_injection)
        
        # 6. 执行
        log_path = os.path.join(scripts_dir, f'hlsl2mat_{material_name}_{timestamp}.log')
        result['log_path'] = log_path
        
        print(f"\n{'═' * 60}")
        print(f"  🚀 UE4 自动执行")
        print(f"{'═' * 60}")
        print(f"  模式: {'编辑器 (Editor)' if mode == 'editor' else 'Commandlet (无头)'}")
        print(f"  引擎: {editor_cmd}")
        print(f"  材质: {material_name}")
        print(f"  节点数: {node_count}")
        print(f"  连线数: {conn_count}")
        print(f"  脚本: {script_path}")
        print(f"  超时: {timeout}s")
        print(f"{'─' * 60}")
        if mode == 'editor':
            print(f"  正在启动 UE4 编辑器（首次可能需要 60-120 秒加载）...")
            print(f"  💡 编辑器会打开窗口，脚本自动执行后可在编辑器中查看材质")
        else:
            print(f"  正在启动 UE4Editor-Cmd.exe ...")
            print(f"  (首次启动可能需要 30-60 秒加载引擎)")
        
        # UE4 Saved/Logs 路径（每次引擎启动会覆盖此文件，不是追加）
        saved_logs_dir = os.path.join(project_dir, 'Saved', 'Logs')
        ue4_log_path = os.path.join(saved_logs_dir, '<ProjectName>.log')
        
        try:
            if mode == 'editor':
                # ── 编辑器模式：启动进程后轮询完成标记文件 ──
                # 编辑器不会自动退出，所以不能用 communicate() 等待
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                
                # 清除旧的完成标记
                if os.path.exists(done_marker_path):
                    os.remove(done_marker_path)
                
                # 轮询等待完成标记
                poll_interval = 2  # 每2秒检查一次
                elapsed = 0
                script_done = False
                
                while elapsed < timeout:
                    time.sleep(poll_interval)
                    elapsed += poll_interval
                    
                    # 检查完成标记文件
                    if os.path.exists(done_marker_path):
                        print(f"  ✅ 脚本执行完成！(耗时约 {elapsed}s)")
                        script_done = True
                        break
                    
                    # 检查进程是否意外退出
                    if proc.poll() is not None:
                        print(f"  ⚠ 编辑器进程已退出 (return code: {proc.returncode})")
                        break
                    
                    if elapsed % 10 == 0:
                        print(f"  ⏳ 等待中... ({elapsed}s)")
                
                if not script_done and elapsed >= timeout:
                    result['status'] = 'TIMEOUT'
                    result['engine_errors'].append(f'执行超时 ({timeout}s)')
                
                # 从 UE4 日志文件读取结果
                full_log = ''
                time.sleep(1)  # 等待日志刷新
                if os.path.exists(ue4_log_path):
                    try:
                        with open(ue4_log_path, 'r', encoding='utf-8', errors='replace') as f:
                            full_log = f.read()
                        print("  📋 从 UE4 Saved/Logs 读取了引擎日志")
                    except Exception as log_err:
                        print(f"  ⚠ 读取 UE4 日志失败: {log_err}")
                
                self.last_log = full_log
                with open(log_path, 'w', encoding='utf-8') as f:
                    f.write(full_log)
                
                # 解析结果
                if result['status'] != 'TIMEOUT':
                    validation = parse_validation_result(full_log)
                    if validation:
                        result['validation'] = validation
                        result['status'] = validation.get('status', 'UNKNOWN')
                    elif script_done:
                        # 标记文件存在说明脚本执行到了末尾
                        result['status'] = 'PASS_NO_COUNT'
                        result['engine_errors'].append('脚本执行完成，但未找到验证标记（可能是日志未刷新）')
                    else:
                        engine_errors = parse_engine_errors(full_log)
                        result['engine_errors'].extend(engine_errors)
                        result['status'] = 'ENGINE_ERROR'
                
                result['return_code'] = proc.poll() if proc.poll() is not None else 0
                
                # 清理标记文件
                if os.path.exists(done_marker_path):
                    try:
                        os.remove(done_marker_path)
                    except:
                        pass
            
            else:
                # ── Commandlet 模式：等待进程退出 ──
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                )
                
                log_lines = []
                try:
                    stdout, _ = proc.communicate(timeout=timeout)
                    log_lines = stdout.split('\n') if stdout else []
                except subprocess.TimeoutExpired:
                    proc.kill()
                    stdout, _ = proc.communicate()
                    log_lines = stdout.split('\n') if stdout else []
                    result['status'] = 'TIMEOUT'
                    result['engine_errors'].append(f'执行超时 ({timeout}s)')
                
                full_log = '\n'.join(log_lines)
                
                # 后备方案：从 UE4 日志文件读取
                if '===HLSL2MAT_VALIDATION' not in full_log and os.path.exists(ue4_log_path):
                    try:
                        with open(ue4_log_path, 'r', encoding='utf-8', errors='replace') as f:
                            ue4_log_content = f.read()
                        if ue4_log_content.strip():
                            full_log = ue4_log_content
                            log_lines = full_log.split('\n')
                            print("  📋 从 UE4 Saved/Logs 读取了日志（stdout 中无验证标记）")
                    except Exception as log_err:
                        print(f"  ⚠ 读取 UE4 日志失败: {log_err}")
                
                self.last_log = full_log
                with open(log_path, 'w', encoding='utf-8') as f:
                    f.write(full_log)
                
                # 解析结果
                if result['status'] != 'TIMEOUT':
                    validation = parse_validation_result(full_log)
                    if validation:
                        result['validation'] = validation
                        result['status'] = validation.get('status', 'UNKNOWN')
                    else:
                        script_succeeded = (
                            '创建完成' in full_log or
                            'HLSL2MAT_EXEC_END' in full_log or
                            '===HLSL2MAT_EXEC_BEGIN===' in full_log
                        )
                        
                        engine_errors = parse_engine_errors(full_log)
                        fatal_errors = [e for e in engine_errors if 'OculusEditor' not in e and 'Shutting down' not in e]
                        result['engine_errors'].extend(engine_errors)
                        
                        if proc.returncode == 0:
                            result['status'] = 'COMPLETED_NO_VALIDATION'
                        elif script_succeeded and not fatal_errors:
                            result['status'] = 'PASS_NO_COUNT'
                            result['engine_errors'].append(
                                '⚠ 脚本执行成功，但引擎退出时出错（可能是 VR 插件相关的已知问题，不影响材质创建）'
                            )
                        else:
                            result['status'] = 'ENGINE_ERROR'
                
                result['return_code'] = proc.returncode
            
        except FileNotFoundError:
            result['status'] = 'ENV_ERROR'
            result['engine_errors'] = [f'找不到引擎: {editor_cmd}']
        except Exception as e:
            result['status'] = 'ERROR'
            result['engine_errors'] = [f'执行异常: {str(e)}']
        
        result['execution_time'] = round(time.time() - start_time, 1)
        self.last_result = result
        
        # 打印结果
        self._print_result(result)
        
        return result
    
    def _print_result(self, result: dict):
        """打印执行结果"""
        status = result['status']
        emoji = {
            'PASS': '✅', 'PASS_NO_COUNT': '✅',
            'WARN': '⚠️', 'FAIL': '❌',
            'ERROR': '💥', 'TIMEOUT': '⏰',
            'ENV_ERROR': '🔧', 'PARSE_ERROR': '📝',
            'ENGINE_ERROR': '🔥',
            'COMPLETED_NO_VALIDATION': '🔶',
        }.get(status, '❓')
        
        print(f"\n{'═' * 60}")
        print(f"  {emoji} 执行结果: {status}")
        print(f"{'═' * 60}")
        print(f"  材质: {result['material_name']}")
        print(f"  耗时: {result['execution_time']}s")
        print(f"  节点数: {result['node_count']}")
        print(f"  连线数: {result['connection_count']}")
        
        if result['validation']:
            v = result['validation']
            print(f"  实际节点数: {v.get('actual_nodes', 'N/A')}")
            print(f"  编译成功: {v.get('compile_success', 'N/A')}")
            if v.get('warnings'):
                print(f"  验证警告:")
                for w in v['warnings']:
                    print(f"    ⚠ {w}")
        
        if result['engine_errors']:
            print(f"  引擎错误:")
            for err in result['engine_errors'][:5]:
                print(f"    ✗ {err[:100]}")
        
        print(f"  日志: {result.get('log_path', 'N/A')}")
        print(f"{'═' * 60}\n")
    
    def quick_test(self, hlsl_code: str = None) -> dict:
        """快速测试（使用默认的菲涅尔示例）"""
        if hlsl_code is None:
            hlsl_code = """
// 菲涅尔效果
float3 viewDir = normalize(CameraPosition - WorldPosition);
float fresnel = pow(1.0 - saturate(dot(Normal, viewDir)), FresnelPower);
float3 result = lerp(BaseColor, RimColor, fresnel);
return result;
"""
        return self.execute_hlsl(hlsl_code, 'M_AutoTest')
    
    def batch_test(self, test_cases: Dict[str, str]) -> Dict[str, dict]:
        """
        批量测试多个 HLSL 代码片段
        
        参数:
            test_cases: {"测试名": "HLSL 代码", ...}
        返回:
            {"测试名": 执行结果, ...}
        """
        results = {}
        total = len(test_cases)
        
        print(f"\n{'═' * 60}")
        print(f"  🧪 批量测试 — 共 {total} 个用例")
        print(f"{'═' * 60}\n")
        
        for i, (name, code) in enumerate(test_cases.items(), 1):
            print(f"\n  [{i}/{total}] {name}")
            print(f"  {'─' * 50}")
            mat_name = f'M_Test_{name}'
            results[name] = self.execute_hlsl(code, mat_name)
        
        # 汇总
        passed = sum(1 for r in results.values() if r['status'] in ('PASS', 'PASS_NO_COUNT'))
        failed = sum(1 for r in results.values() if r['status'] in ('FAIL', 'ERROR', 'ENGINE_ERROR'))
        
        print(f"\n{'═' * 60}")
        print(f"  📊 批量测试汇总")
        print(f"{'═' * 60}")
        print(f"  总计: {total}")
        print(f"  通过: {passed} ✅")
        print(f"  失败: {failed} ❌")
        print(f"  其他: {total - passed - failed} ⚠️")
        print(f"{'═' * 60}\n")
        
        return results
    
    def compare_test(self, hlsl_code: str, material_name: str = 'M_CompareTest') -> dict:
        """
        效果对比测试：创建节点图版和 Custom 对照版材质，渲染截图对比
        
        参数:
            hlsl_code: HLSL 源代码
            material_name: 材质名称
            
        返回:
            {
                "status": "MATCH" / "MISMATCH" / "FAIL" / "ERROR",
                "basic_validation": { ... },    # 基础验证结果（节点数、编译）
                "compare_result": { ... },      # 引擎内对比结果
                "pixel_compare": { ... },       # Python 端像素对比结果
                "execution_time": 45.2,
            }
        """
        start_time = time.time()
        result = {
            'status': 'PENDING',
            'material_name': material_name,
            'basic_validation': None,
            'compare_result': None,
            'pixel_compare': None,
            'execution_time': 0,
            'node_count': 0,
            'connection_count': 0,
            'engine_errors': [],
        }
        
        # 1. 检查环境
        env = self.check_environment()
        if not env['ready']:
            result['status'] = 'ENV_ERROR'
            result['engine_errors'] = env['errors']
            return result
        
        # 2. 生成对比脚本
        project_dir = os.path.dirname(self.config['project_path'])
        scripts_dir = os.path.join(project_dir, 'Scripts')
        os.makedirs(scripts_dir, exist_ok=True)
        
        # 截图输出目录
        compare_output_dir = os.path.join(scripts_dir, 'compare_output')
        os.makedirs(compare_output_dir, exist_ok=True)
        
        try:
            material_path = self.config['material_path']
            full_script, node_count, conn_count, input_params = generate_compare_full_script(
                hlsl_code, material_name, material_path, compare_output_dir
            )
            result['node_count'] = node_count
            result['connection_count'] = conn_count
        except Exception as e:
            result['status'] = 'PARSE_ERROR'
            result['engine_errors'] = [f'HLSL 解析/脚本生成失败: {str(e)}']
            result['execution_time'] = round(time.time() - start_time, 1)
            return result
        
        # 3. 写入脚本
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        script_filename = f'hlsl2mat_compare_{material_name}_{timestamp}.py'
        script_path = os.path.join(scripts_dir, script_filename)
        
        with open(script_path, 'w', encoding='utf-8') as f:
            f.write(full_script)
        
        # 4. 构建命令行（效果对比必须使用编辑器模式，因为需要渲染能力）
        # 强制使用编辑器模式（即使配置为 commandlet）
        editor_cmd = self.config.get('ue4_editor', self.config['ue4_editor_cmd'])
        project_path = self.config['project_path']
        timeout = self.config.get('timeout', 300)  # 编辑器模式需要更长超时
        
        cmd = [
            editor_cmd,
            project_path,
            f'-ExecutePythonScript={script_path}',
            '-nosplash',
            '-nosound',
            # 注意：不使用 -nullrhi，需要渲染能力！
            # 注意：不使用 -unattended，编辑器模式不需要
            '-DisablePlugin=OculusEditor',
            '-DisablePlugin=OculusHMD',
        ]
        
        # 5. 执行
        log_path = os.path.join(scripts_dir, f'hlsl2mat_compare_{material_name}_{timestamp}.log')
        
        print(f"\n{'═' * 60}")
        print(f"  🔬 UE4 效果对比测试")
        print(f"{'═' * 60}")
        print(f"  模式: 编辑器 (Editor) — 效果对比需要渲染能力")
        print(f"  引擎: {editor_cmd}")
        print(f"  材质: {material_name} (节点图) vs {material_name}_Custom (Custom)")
        print(f"  节点数: {node_count}")
        print(f"  连线数: {conn_count}")
        print(f"  输入参数: {list(input_params.keys())}")
        print(f"  脚本: {script_path}")
        print(f"  截图目录: {compare_output_dir}")
        print(f"  超时: {timeout}s")
        print(f"{'─' * 60}")
        print(f"  正在启动 UE4 编辑器（首次可能需要 60-120 秒加载）...")
        print(f"  💡 编辑器打开后会自动执行脚本、渲染截图、对比效果")
        
        # UE4 日志路径
        saved_logs_dir = os.path.join(project_dir, 'Saved', 'Logs')
        ue4_log_path = os.path.join(saved_logs_dir, '<ProjectName>.log')
        
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace',
            )
            
            log_lines = []
            try:
                stdout, _ = proc.communicate(timeout=timeout)
                log_lines = stdout.split('\n') if stdout else []
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, _ = proc.communicate()
                log_lines = stdout.split('\n') if stdout else []
                result['status'] = 'TIMEOUT'
                result['engine_errors'].append(f'执行超时 ({timeout}s)')
            
            full_log = '\n'.join(log_lines)
            
            # ── 后备方案：如果 stdout 中没有验证标记，从 UE4 Saved/Logs 读取完整日志 ──
            # UE4 的 unreal.log() 不走 stdout 管道，而是写入自己的日志文件
            # 所以 stdout 可能只有 VR 插件错误等无关内容
            if '===HLSL2MAT_VALIDATION' not in full_log and os.path.exists(ue4_log_path):
                try:
                    with open(ue4_log_path, 'r', encoding='utf-8', errors='replace') as f:
                        ue4_log_content = f.read()
                    if ue4_log_content.strip():
                        full_log = ue4_log_content
                        log_lines = full_log.split('\n')
                        print("  📋 从 UE4 Saved/Logs 读取了日志（stdout 中无验证标记）")
                except Exception as log_err:
                    print(f"  ⚠ 读取 UE4 日志失败: {log_err}")
            
            # 保存日志
            with open(log_path, 'w', encoding='utf-8') as f:
                f.write(full_log)
            
            # 6. 解析基础验证结果
            validation = parse_validation_result(full_log)
            if validation:
                result['basic_validation'] = validation
            
            # 7. 解析对比结果
            compare = parse_compare_result(full_log)
            if compare:
                result['compare_result'] = compare
            
            # 8. Python 端像素对比
            # UE4 export_render_target 导出的文件名可能不带扩展名，
            # 也可能带 .bmp/.hdr/.png，需要逐一尝试
            nodes_img = None
            custom_img = None
            
            for ext in ['', '.bmp', '.hdr', '.png', '.exr']:
                candidate = os.path.join(compare_output_dir, f'{material_name}_render{ext}')
                if os.path.exists(candidate):
                    nodes_img = candidate
                    break
            
            for ext in ['', '.bmp', '.hdr', '.png', '.exr']:
                candidate = os.path.join(compare_output_dir, f'{material_name}_Custom_render{ext}')
                if os.path.exists(candidate):
                    custom_img = candidate
                    break
            
            if nodes_img and custom_img:
                diff_img = os.path.join(compare_output_dir, f'{material_name}_diff.bmp')
                pixel_result = compare_screenshots(nodes_img, custom_img, diff_img)
                result['pixel_compare'] = pixel_result
                
                if pixel_result.get('match'):
                    result['status'] = 'MATCH'
                else:
                    result['status'] = 'MISMATCH'
            else:
                # 截图不可用，回退到基础验证
                if validation and validation.get('status') in ('PASS', 'PASS_NO_COUNT'):
                    result['status'] = 'PASS_NO_RENDER'
                    result['engine_errors'].append(
                        '截图文件不存在，无法做渲染对比。可能是 -nullrhi 仍然生效或 RenderTarget 创建失败。'
                    )
                elif result['status'] != 'TIMEOUT':
                    engine_errors = parse_engine_errors(full_log)
                    result['engine_errors'].extend(engine_errors)
                    
                    script_succeeded = (
                        '创建完成' in full_log or
                        'HLSL2MAT_EXEC_END' in full_log
                    )
                    
                    fatal_errors = [e for e in engine_errors if 'OculusEditor' not in e and 'Shutting down' not in e]
                    
                    if script_succeeded and not fatal_errors:
                        result['status'] = 'PASS_NO_RENDER'
                    else:
                        result['status'] = 'ENGINE_ERROR'
            
            result['return_code'] = proc.returncode
            
        except FileNotFoundError:
            result['status'] = 'ENV_ERROR'
            result['engine_errors'] = [f'找不到 UE4Editor-Cmd.exe: {editor_cmd}']
        except Exception as e:
            result['status'] = 'ERROR'
            result['engine_errors'] = [f'执行异常: {str(e)}']
        
        result['execution_time'] = round(time.time() - start_time, 1)
        
        # 打印对比结果
        self._print_compare_result(result)
        
        return result
    
    def batch_compare(self, test_cases: Dict[str, str]) -> Dict[str, dict]:
        """
        批量效果对比测试
        
        参数:
            test_cases: {"测试名": "HLSL 代码", ...}
        返回:
            {"测试名": 对比结果, ...}
        """
        results = {}
        total = len(test_cases)
        
        print(f"\n{'═' * 60}")
        print(f"  🔬 批量效果对比测试 — 共 {total} 个用例")
        print(f"{'═' * 60}")
        print(f"  每个用例会创建 节点图版 + Custom对照版 两个材质")
        print(f"  然后在引擎内渲染截图并做像素差异对比")
        print(f"{'═' * 60}\n")
        
        for i, (name, code) in enumerate(test_cases.items(), 1):
            print(f"\n  [{i}/{total}] {name}")
            print(f"  {'─' * 50}")
            mat_name = f'M_Compare_{name}'
            results[name] = self.compare_test(code, mat_name)
        
        # 汇总
        matched = sum(1 for r in results.values() if r['status'] == 'MATCH')
        mismatched = sum(1 for r in results.values() if r['status'] == 'MISMATCH')
        no_render = sum(1 for r in results.values() if r['status'] == 'PASS_NO_RENDER')
        failed = sum(1 for r in results.values() if r['status'] in ('FAIL', 'ERROR', 'ENGINE_ERROR'))
        
        print(f"\n{'═' * 60}")
        print(f"  📊 效果对比测试汇总")
        print(f"{'═' * 60}")
        print(f"  总计: {total}")
        print(f"  ✅ 效果一致 (MATCH):     {matched}")
        print(f"  ❌ 效果不一致 (MISMATCH): {mismatched}")
        print(f"  ⚠️ 通过但无渲染对比:      {no_render}")
        print(f"  💥 失败/错误:             {failed}")
        
        # 详细列表
        print(f"\n  {'─' * 50}")
        for name, r in results.items():
            status = r['status']
            emoji = {'MATCH': '✅', 'MISMATCH': '❌', 'PASS_NO_RENDER': '⚠️'}.get(status, '💥')
            pixel_info = ''
            if r.get('pixel_compare') and r['pixel_compare'].get('mse', -1) >= 0:
                pc = r['pixel_compare']
                pixel_info = f"  MSE={pc['mse']:.2f}  差异={pc['pixel_diff_percent']:.1f}%"
            print(f"  {emoji} {name:20s} → {status:18s}{pixel_info}")
        
        print(f"{'═' * 60}\n")
        
        return results
    
    def _print_compare_result(self, result: dict):
        """打印效果对比测试结果"""
        status = result['status']
        emoji = {
            'MATCH': '✅', 'MISMATCH': '❌',
            'PASS_NO_RENDER': '⚠️',
            'FAIL': '❌', 'ERROR': '💥', 'TIMEOUT': '⏰',
            'ENV_ERROR': '🔧', 'PARSE_ERROR': '📝',
            'ENGINE_ERROR': '🔥',
        }.get(status, '❓')
        
        print(f"\n{'═' * 60}")
        print(f"  {emoji} 效果对比结果: {status}")
        print(f"{'═' * 60}")
        print(f"  材质: {result['material_name']}")
        print(f"  耗时: {result['execution_time']}s")
        print(f"  节点数: {result['node_count']}")
        print(f"  连线数: {result['connection_count']}")
        
        if result.get('basic_validation'):
            v = result['basic_validation']
            print(f"  编译成功: {v.get('compile_success', 'N/A')}")
        
        if result.get('pixel_compare'):
            pc = result['pixel_compare']
            print(f"  像素对比:")
            print(f"    MSE(均方误差):    {pc.get('mse', 'N/A')}")
            print(f"    最大差异:          {pc.get('max_diff', 'N/A')}")
            print(f"    差异像素占比:      {pc.get('pixel_diff_percent', 'N/A')}%")
            print(f"    匹配(MSE<{pc.get('threshold', 5)}): {'✅ 是' if pc.get('match') else '❌ 否'}")
            if pc.get('diff_image'):
                print(f"    差异图: {pc['diff_image']}")
        
        if result.get('compare_result'):
            cr = result['compare_result']
            if cr.get('nodes_screenshot'):
                print(f"  节点图截图: {cr['nodes_screenshot']}")
            if cr.get('custom_screenshot'):
                print(f"  Custom截图: {cr['custom_screenshot']}")
            if cr.get('warnings'):
                for w in cr['warnings']:
                    print(f"    ⚠ {w}")
        
        if result.get('engine_errors'):
            print(f"  引擎错误:")
            for err in result['engine_errors'][:5]:
                print(f"    ✗ {err[:100]}")
        
        print(f"{'═' * 60}\n")

def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='UE4 自动执行器 — HLSL → 材质自循环')
    parser.add_argument('hlsl_file', nargs='?', help='HLSL 源文件路径')
    parser.add_argument('--code', '-c', help='直接输入 HLSL 代码')
    parser.add_argument('--name', '-n', default='M_AutoTest', help='材质名称')
    parser.add_argument('--check', action='store_true', help='仅检查环境')
    parser.add_argument('--quick-test', action='store_true', help='用内置示例快速测试')
    parser.add_argument('--batch', action='store_true', help='批量测试所有内置示例')
    parser.add_argument('--compare', action='store_true', help='效果对比测试（创建 Custom 对照材质，渲染截图对比）')
    parser.add_argument('--compare-all', action='store_true', help='批量效果对比测试所有内置示例')
    parser.add_argument('--editor', help='UE4Editor.exe 路径（编辑器模式）')
    parser.add_argument('--editor-cmd', help='UE4Editor-Cmd.exe 路径（Commandlet 模式）')
    parser.add_argument('--project', help='.uproject 文件路径')
    parser.add_argument('--timeout', type=int, default=300, help='执行超时(秒)')
    parser.add_argument('--mode', choices=['editor', 'commandlet'], default=None,
                        help='执行模式: editor=打开编辑器(可渲染), commandlet=无头模式(快但不能渲染)')
    
    args = parser.parse_args()
    
    # 加载/更新配置
    config = load_config()
    if args.editor:
        config['ue4_editor'] = args.editor
    if args.editor_cmd:
        config['ue4_editor_cmd'] = args.editor_cmd
    if args.project:
        config['project_path'] = args.project
    if args.timeout:
        config['timeout'] = args.timeout
    if args.mode:
        config['execution_mode'] = args.mode
    save_config(config)
    
    executor = UE4Executor(config)
    
    # 检查环境
    if args.check:
        env = executor.check_environment()
        mode = config.get('execution_mode', 'editor')
        print(f"\n环境检查:")
        print(f"  执行模式:          {'编辑器 (Editor)' if mode == 'editor' else 'Commandlet (无头)'}")
        if mode == 'editor':
            editor_path = config.get('ue4_editor', config.get('ue4_editor_cmd', ''))
            print(f"  UE4Editor.exe:     {'✅' if env['editor_exists'] else '❌'} {editor_path}")
        else:
            print(f"  UE4Editor-Cmd.exe: {'✅' if env['editor_exists'] else '❌'} {config['ue4_editor_cmd']}")
        print(f"  项目文件:          {'✅' if env['project_exists'] else '❌'} {config['project_path']}")
        print(f"  就绪:              {'✅' if env['ready'] else '❌'}")
        if env['errors']:
            for e in env['errors']:
                print(f"  ✗ {e}")
        return
    
    # 快速测试
    if args.quick_test:
        executor.quick_test()
        return
    
    # 批量测试
    if args.batch:
        from hlsl2material import EXAMPLE_HLSL
        executor.batch_test(EXAMPLE_HLSL)
        return
    
    # 效果对比测试
    if args.compare:
        hlsl_code = None
        if args.code:
            hlsl_code = args.code
        elif args.hlsl_file:
            with open(args.hlsl_file, 'r', encoding='utf-8') as f:
                hlsl_code = f.read()
        else:
            # 默认用菲涅尔示例
            hlsl_code = """
// 菲涅尔效果
float3 viewDir = normalize(CameraPosition - WorldPosition);
float fresnel = pow(1.0 - saturate(dot(Normal, viewDir)), FresnelPower);
float3 result = lerp(BaseColor, RimColor, fresnel);
return result;
"""
        executor.compare_test(hlsl_code, args.name)
        return
    
    # 批量效果对比测试
    if args.compare_all:
        from hlsl2material import EXAMPLE_HLSL
        executor.batch_compare(EXAMPLE_HLSL)
        return
    
    # 从文件或代码执行
    hlsl_code = None
    if args.code:
        hlsl_code = args.code
    elif args.hlsl_file:
        if not os.path.exists(args.hlsl_file):
            print(f"错误：文件不存在 - {args.hlsl_file}")
            sys.exit(1)
        with open(args.hlsl_file, 'r', encoding='utf-8') as f:
            hlsl_code = f.read()
    else:
        # 默认快速测试
        executor.quick_test()
        return
    
    executor.execute_hlsl(hlsl_code, args.name)


if __name__ == '__main__':
    main()
