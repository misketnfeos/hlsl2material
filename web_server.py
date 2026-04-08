"""
============================================================
 web_server.py
 HLSL → UE4 Material Node 交互式 Web 应用
============================================================

启动一个本地 Web 服务器，提供：
  - 交互式 HLSL 代码输入界面
  - 实时解析并显示节点图
  - 生成 UE4 Editor Python 脚本（可下载）

用法：
  python web_server.py [--port 8080]

然后在浏览器中打开 http://127.0.0.1:8080
============================================================
"""

import os
import sys
import json
import traceback
import subprocess
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

# Windows 控制台编码修复
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

# 确保模块可导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hlsl_parser import parse_hlsl, dump_ast
from node_mapper import hlsl_to_material_graph, MaterialGraph, MaterialNode
from graph_visualizer import compute_layout, NODE_COLORS, DEFAULT_COLOR
from ue4_codegen import generate_ue4_script
from ue4_executor import UE4Executor, load_config, save_config
from t3d_parser import parse_t3d_clipboard, parse_t3d_to_result, T3DParser
from t3d_generator import generate_t3d_from_parse_result, generate_t3d_from_material_graph
from custom_converter import convert_custom_nodes
from shadertoy_converter import ShadertoyConverter, convert_shadertoy
from auto_input_generator import AutoInputGenerator
from reverse_converter import ReverseConverter, material_graph_to_hlsl


# ═══════════════════════════════════════════════════════════
# 全局 UE4 执行器实例 & 异步执行状态
# ═══════════════════════════════════════════════════════════

_ue4_executor = None
_exec_lock = threading.Lock()
_exec_status = {
    'running': False,
    'result': None,
    'progress': '',
}


def get_executor() -> UE4Executor:
    """获取或创建 UE4 执行器单例"""
    global _ue4_executor
    if _ue4_executor is None:
        _ue4_executor = UE4Executor(load_config())
    return _ue4_executor


# ═══════════════════════════════════════════════════════════
# 节点图数据序列化（给前端用）
# ═══════════════════════════════════════════════════════════

def graph_to_json(graph: MaterialGraph) -> dict:
    """将 MaterialGraph 转为 JSON 可序列化的 dict"""
    positions = compute_layout(graph)
    node_by_id = {n.id: n for n in graph.nodes}

    nodes_data = []
    for node in graph.nodes:
        x, y = positions.get(node.id, (50, 50))
        color = NODE_COLORS.get(node.ue_class, DEFAULT_COLOR)
        input_list = []
        for iname, inode in node.inputs.items():
            if inode:
                input_list.append({
                    'name': iname,
                    'source_id': inode.id,
                })

        props_str = ''
        if node.properties:
            props_str = json.dumps(node.properties, indent=2, ensure_ascii=False)

        nodes_data.append({
            'id': node.id,
            'display_name': node.display_name,
            'ue_class': node.ue_class,
            'x': x, 'y': y,
            'color': color,
            'inputs': input_list,
            'input_names': list(node.inputs.keys()) if node.inputs else node.input_names,
            'properties': props_str,
            'is_output': (graph.output_node and node.id == graph.output_node.id),
            'is_input': node.ue_class in ('MaterialExpressionFunctionInput',
                                           'MaterialExpressionScalarParameter',
                                           'MaterialExpressionVectorParameter'),
            'is_builtin': node.ue_class.startswith('MaterialExpressionCamera') or
                          node.ue_class.startswith('MaterialExpressionWorld') or
                          node.ue_class.startswith('MaterialExpressionPixelNormal') or
                          node.ue_class.startswith('MaterialExpressionVertexNormal') or
                          node.ue_class.startswith('MaterialExpressionTexture') and 'Coordinate' in node.ue_class or
                          node.ue_class == 'MaterialExpressionTime' or
                          node.ue_class == 'MaterialExpressionScreenPosition' or
                          node.ue_class == 'MaterialExpressionVertexColor' or
                          node.ue_class.startswith('MaterialExpressionReflectionVector') or
                          node.ue_class.startswith('MaterialExpressionObject') or
                          node.ue_class.startswith('MaterialExpressionActor') or
                          node.ue_class == 'MaterialExpressionPixelDepth' or
                          node.ue_class == 'MaterialExpressionSceneDepth',
            'source_line': node.source_line,
        })

    connections = []
    for node in graph.nodes:
        for i, (iname, inode) in enumerate(node.inputs.items()):
            if inode and inode.id in node_by_id:
                connections.append({
                    'from_id': inode.id,
                    'to_id': node.id,
                    'to_pin_index': i,
                    'label': iname,
                })

    return {
        'nodes': nodes_data,
        'connections': connections,
        'warnings': graph.warnings,
        'custom_expressions': graph.custom_expressions,
        'stats': {
            'node_count': len(graph.nodes),
            'input_count': len(graph.input_nodes),
            'connection_count': len(connections),
        }
    }


# ═══════════════════════════════════════════════════════════
# 内置示例
# ═══════════════════════════════════════════════════════════

EXAMPLES = {
    'fresnel': {
        'name': '菲涅尔效果',
        'code': '''// 菲涅尔效果
float3 viewDir = normalize(CameraPosition - WorldPosition);
float fresnel = pow(1.0 - saturate(dot(Normal, viewDir)), FresnelPower);
float3 result = lerp(BaseColor, RimColor, fresnel);
return result;'''
    },
    'dissolve': {
        'name': '溶解效果',
        'code': '''// 溶解效果
float noise = tex2D(NoiseTex, UV).r;
float edge = smoothstep(DissolveAmount - EdgeWidth, DissolveAmount, noise);
float edgeMask = smoothstep(DissolveAmount, DissolveAmount + EdgeWidth, noise);
float3 edgeColor = EdgeColor * (edge - edgeMask);
float3 result = BaseColor * edgeMask + edgeColor;
return result;'''
    },
    'rim_light': {
        'name': '边缘光',
        'code': '''// 边缘光
float NdotV = dot(Normal, ViewDir);
float rim = 1.0 - saturate(NdotV);
rim = pow(rim, RimPower);
float3 rimColor = RimColor * rim * RimIntensity;
float3 result = BaseColor + rimColor;
return result;'''
    },
    'uv_distortion': {
        'name': 'UV 扭曲',
        'code': '''// UV 扭曲
float2 distortion = tex2D(DistortionTex, UV + Time * 0.1).rg;
distortion = distortion * 2.0 - 1.0;
float2 distortedUV = UV + distortion * DistortionStrength;
float3 result = tex2D(MainTex, distortedUV).rgb;
return result;'''
    },
    'simple_blend': {
        'name': '简单混合',
        'code': '''// 简单颜色混合
float3 color1 = float3(1.0, 0.0, 0.0);
float3 color2 = float3(0.0, 0.0, 1.0);
float t = saturate(UV.x);
float3 result = lerp(color1, color2, t);
return result;'''
    },
}


# ═══════════════════════════════════════════════════════════
# HTML 页面
# ═══════════════════════════════════════════════════════════

def get_index_html():
    """生成交互式前端页面"""

    # 构建示例选项 HTML
    example_options = ''
    for key, ex in EXAMPLES.items():
        example_options += f'<option value="{key}">{ex["name"]}</option>\n'

    # 构建示例代码 JSON
    examples_json = json.dumps({k: v['code'] for k, v in EXAMPLES.items()}, ensure_ascii=False)

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>HLSL → UE4 Material Nodes</title>
<style>
/* ── 全局 ── */
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
:root {{
    --bg-dark: #0d1117;
    --bg-panel: #161b22;
    --bg-input: #0d1117;
    --border: #30363d;
    --text: #c9d1d9;
    --text-dim: #8b949e;
    --accent: #58a6ff;
    --accent-hover: #79c0ff;
    --red: #f85149;
    --green: #3fb950;
    --orange: #d29922;
    --purple: #bc8cff;
}}
body {{
    background: var(--bg-dark);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Microsoft YaHei', sans-serif;
    height: 100vh;
    overflow: hidden;
    display: flex;
    flex-direction: column;
}}

/* ── 顶栏 ── */
.topbar {{
    background: var(--bg-panel);
    border-bottom: 1px solid var(--border);
    padding: 8px 16px;
    display: flex;
    align-items: center;
    gap: 16px;
    flex-shrink: 0;
    z-index: 100;
}}
.topbar .logo {{
    font-size: 15px;
    font-weight: 700;
    color: var(--accent);
    white-space: nowrap;
    display: flex;
    align-items: center;
    gap: 6px;
}}
.topbar .logo span {{
    background: linear-gradient(135deg, #e94560, #ff6b81);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
}}
.topbar .stats {{
    display: flex;
    gap: 12px;
    font-size: 12px;
    color: var(--text-dim);
}}
.topbar .stats b {{
    color: var(--text);
}}
.topbar .actions {{
    margin-left: auto;
    display: flex;
    gap: 8px;
    align-items: center;
}}
.mode-tabs {{
    display: flex;
    gap: 2px;
    background: var(--bg-dark);
    border-radius: 8px;
    padding: 2px;
    border: 1px solid var(--border);
}}
.mode-tab {{
    padding: 5px 14px;
    border: none;
    background: transparent;
    color: var(--text-dim);
    font-size: 12px;
    cursor: pointer;
    border-radius: 6px;
    transition: all 0.15s;
    white-space: nowrap;
}}
.mode-tab:hover {{
    color: var(--text);
    background: rgba(255,255,255,0.05);
}}
.mode-tab.active {{
    background: var(--accent);
    color: #fff;
    font-weight: 600;
}}
.t3d-actions, .hlsl-actions {{
    display: flex;
    gap: 8px;
    align-items: center;
}}
.btn-convert {{
    background: linear-gradient(135deg, #e67e22, #d35400) !important;
    color: #fff !important;
    font-weight: 600;
}}
.btn-convert:hover {{
    background: linear-gradient(135deg, #f39c12, #e67e22) !important;
    box-shadow: 0 0 12px rgba(230, 126, 34, 0.4);
}}

/* ── 按钮 ── */
.btn {{
    padding: 5px 14px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--bg-panel);
    color: var(--text);
    font-size: 12px;
    cursor: pointer;
    transition: all 0.15s;
    white-space: nowrap;
    display: flex;
    align-items: center;
    gap: 4px;
}}
.btn:hover {{
    background: #21262d;
    border-color: var(--text-dim);
}}
.btn-primary {{
    background: #238636;
    border-color: #2ea043;
    color: #fff;
}}
.btn-primary:hover {{
    background: #2ea043;
}}
.btn-accent {{
    background: #1f6feb;
    border-color: #388bfd;
    color: #fff;
}}
.btn-accent:hover {{
    background: #388bfd;
}}
.btn-danger {{
    background: transparent;
    border-color: var(--red);
    color: var(--red);
}}
.btn-danger:hover {{
    background: #f851491a;
}}
.btn-execute {{
    background: linear-gradient(135deg, #e94560, #c53678);
    border-color: #e94560;
    color: #fff;
    font-weight: 600;
}}
.btn-execute:hover {{
    background: linear-gradient(135deg, #f05670, #d54788);
    box-shadow: 0 0 12px rgba(233,69,96,0.4);
}}
.btn-execute.running {{
    animation: pulse-btn 1.5s infinite;
    pointer-events: none;
    opacity: 0.7;
}}
@keyframes pulse-btn {{
    0%, 100% {{ box-shadow: 0 0 0 0 rgba(233,69,96,0.5); }}
    50% {{ box-shadow: 0 0 16px 4px rgba(233,69,96,0.3); }}
}}

/* ── 执行结果面板 ── */
.exec-panel {{
    display: none;
    position: fixed;
    top: 50px;
    right: 10px;
    width: 360px;
    max-height: 70vh;
    background: var(--bg-panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    z-index: 200;
    box-shadow: 0 8px 30px rgba(0,0,0,0.5);
    overflow: hidden;
}}
.exec-panel.show {{
    display: flex;
    flex-direction: column;
}}
.exec-panel-header {{
    padding: 10px 14px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-weight: 600;
    font-size: 13px;
}}
.exec-panel-body {{
    flex: 1;
    overflow-y: auto;
    padding: 12px 14px;
    font-size: 12px;
}}
.exec-status {{
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 10px;
    padding: 8px 12px;
    border-radius: 6px;
    font-weight: 600;
}}
.exec-status.pass {{ background: #3fb95020; color: var(--green); border: 1px solid #3fb95040; }}
.exec-status.fail {{ background: #f8514920; color: var(--red); border: 1px solid #f8514940; }}
.exec-status.warn {{ background: #d2992220; color: var(--orange); border: 1px solid #d2992240; }}
.exec-status.running {{ background: #58a6ff20; color: var(--accent); border: 1px solid #58a6ff40; }}
.exec-status.error {{ background: #f8514920; color: var(--red); border: 1px solid #f8514940; }}
.exec-detail-row {{
    display: flex;
    justify-content: space-between;
    padding: 3px 0;
    color: var(--text-dim);
}}
.exec-detail-row b {{
    color: var(--text);
}}
.exec-error-list {{
    margin-top: 8px;
    padding: 8px;
    background: #f8514910;
    border-radius: 4px;
    border: 1px solid #f8514930;
    font-size: 11px;
    color: var(--red);
    max-height: 150px;
    overflow-y: auto;
}}
.exec-error-list div {{
    margin: 2px 0;
    word-break: break-all;
}}

/* ── 选择器 ── */
select {{
    padding: 5px 10px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--bg-input);
    color: var(--text);
    font-size: 12px;
    cursor: pointer;
    outline: none;
}}
select:hover {{
    border-color: var(--text-dim);
}}

/* ── 主体布局 ── */
.main {{
    flex: 1;
    display: flex;
    overflow: hidden;
}}

/* ── 左侧：代码编辑区 ── */
.editor-panel {{
    width: 380px;
    min-width: 280px;
    max-width: 600px;
    background: var(--bg-panel);
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    flex-shrink: 0;
}}
.editor-header {{
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 12px;
    color: var(--text-dim);
}}
.editor-header .title {{
    font-weight: 600;
    color: var(--text);
}}
.code-area {{
    flex: 1;
    position: relative;
}}
.code-area textarea {{
    width: 100%;
    height: 100%;
    background: var(--bg-input);
    color: #e6edf3;
    border: none;
    padding: 12px 14px;
    font-family: 'Cascadia Code', 'JetBrains Mono', 'Fira Code', 'Consolas', monospace;
    font-size: 13px;
    line-height: 1.6;
    resize: none;
    outline: none;
    tab-size: 4;
}}
.code-area textarea::placeholder {{
    color: #484f58;
}}

/* ── 信息面板 ── */
.info-panel {{
    border-top: 1px solid var(--border);
    max-height: 180px;
    overflow-y: auto;
    font-size: 11px;
    padding: 8px 12px;
}}
.info-panel.has-warnings {{
    border-top-color: var(--orange);
}}
.info-panel .info-title {{
    font-weight: 600;
    margin-bottom: 4px;
    color: var(--text-dim);
}}
.info-panel .warning-item {{
    color: var(--orange);
    margin: 2px 0;
    padding-left: 12px;
    position: relative;
}}
.info-panel .warning-item::before {{
    content: '⚠';
    position: absolute;
    left: 0;
}}
.info-panel .error-item {{
    color: var(--red);
    margin: 2px 0;
    padding-left: 12px;
    position: relative;
}}
.info-panel .error-item::before {{
    content: '✗';
    position: absolute;
    left: 0;
}}
.info-panel .success-item {{
    color: var(--green);
    margin: 2px 0;
}}

/* ── 拖拽分隔条 ── */
.resize-handle {{
    width: 4px;
    cursor: col-resize;
    background: transparent;
    transition: background 0.2s;
    flex-shrink: 0;
}}
.resize-handle:hover,
.resize-handle.active {{
    background: var(--accent);
}}

/* ── 右侧：节点图 ── */
.graph-panel {{
    flex: 1;
    position: relative;
    overflow: hidden;
    cursor: grab;
    background:
        radial-gradient(circle at 1px 1px, #1b2332 1px, transparent 0);
    background-size: 24px 24px;
}}
.graph-panel:active {{
    cursor: grabbing;
}}
#graph-canvas {{
    position: absolute;
    transform-origin: 0 0;
}}
#graph-svg {{
    position: absolute;
    top: 0;
    left: 0;
    pointer-events: none;
}}
#graph-svg path {{
    fill: none;
    stroke-width: 2;
    opacity: 0.5;
    transition: opacity 0.15s;
}}
#graph-svg path:hover {{
    opacity: 1;
    stroke-width: 3;
}}

/* ── 节点样式 ── */
.mat-node {{
    position: absolute;
    width: 220px;
    border-radius: 6px;
    overflow: hidden;
    box-shadow: 0 2px 12px rgba(0,0,0,0.5);
    cursor: default;
    transition: box-shadow 0.15s;
    border: 1px solid rgba(255,255,255,0.06);
}}
.mat-node:hover {{
    box-shadow: 0 4px 20px rgba(88,166,255,0.3);
    z-index: 10;
}}
.mat-node.output-node {{
    border: 2px solid var(--red);
}}
.mat-node.input-node {{
    border: 2px solid var(--orange);
}}
.mat-node-header {{
    padding: 6px 10px;
    font-size: 11px;
    font-weight: 600;
    color: #fff;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    text-align: center;
}}
.mat-node-body {{
    background: #1c2333;
    padding: 6px 0;
    font-size: 11px;
    color: #ccc;
    border-top: 1px solid rgba(255,255,255,0.05);
}}
.pin-row {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 2px 10px;
    min-height: 20px;
}}
.pin-left {{
    display: flex;
    align-items: center;
    gap: 5px;
}}
.pin-right {{
    display: flex;
    align-items: center;
    gap: 5px;
}}
.pin-dot {{
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #484f58;
    flex-shrink: 0;
    border: 1px solid #666;
}}
.pin-dot.connected {{
    background: var(--accent);
    border-color: var(--accent);
}}
.pin-dot.output-dot {{
    background: #aaa;
    border-color: #aaa;
}}
.pin-name {{
    font-size: 10px;
    color: #bbb;
}}
.mat-node-class {{
    font-size: 9px;
    color: #484f58;
    padding: 2px 10px 0;
    word-break: break-all;
}}
/* ── 缩放指示 ── */
.zoom-badge {{
    position: absolute;
    bottom: 12px;
    left: 50%;
    transform: translateX(-50%);
    background: var(--bg-panel);
    border: 1px solid var(--border);
    padding: 3px 10px;
    border-radius: 10px;
    font-size: 11px;
    color: var(--text-dim);
    z-index: 50;
    pointer-events: none;
}}

/* ── 图例 ── */
.legend {{
    position: absolute;
    top: 10px;
    right: 10px;
    background: var(--bg-panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 8px 10px;
    font-size: 10px;
    z-index: 50;
    opacity: 0.85;
}}
.legend:hover {{
    opacity: 1;
}}
.legend-row {{
    display: flex;
    align-items: center;
    gap: 5px;
    margin: 2px 0;
}}
.legend-dot {{
    width: 10px;
    height: 10px;
    border-radius: 2px;
    flex-shrink: 0;
}}

/* ── 空状态 ── */
.empty-state {{
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    text-align: center;
    color: #30363d;
    pointer-events: none;
    z-index: 5;
}}
.empty-state .icon {{
    font-size: 48px;
    margin-bottom: 12px;
}}
.empty-state .hint {{
    font-size: 14px;
    color: #484f58;
}}

/* ── 加载动画 ── */
.loading-overlay {{
    display: none;
    position: absolute;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(13,17,23,0.7);
    z-index: 200;
    justify-content: center;
    align-items: center;
}}
.loading-overlay.show {{
    display: flex;
}}
.spinner {{
    width: 32px;
    height: 32px;
    border: 3px solid var(--border);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.6s linear infinite;
}}
@keyframes spin {{
    to {{ transform: rotate(360deg); }}
}}

/* ── Toast 提示 ── */
.toast {{
    position: fixed;
    top: 60px;
    right: 20px;
    background: var(--bg-panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 16px;
    font-size: 13px;
    z-index: 300;
    box-shadow: 0 4px 20px rgba(0,0,0,0.4);
    transform: translateX(120%);
    transition: transform 0.3s ease;
}}
.toast.show {{
    transform: translateX(0);
}}
.toast.success {{
    border-color: var(--green);
    color: var(--green);
}}
.toast.error {{
    border-color: var(--red);
    color: var(--red);
}}

/* ── UE4 脚本弹窗 ── */
.modal-overlay {{
    display: none;
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.6);
    z-index: 500;
    justify-content: center;
    align-items: center;
}}
.modal-overlay.show {{
    display: flex;
}}
.modal {{
    background: var(--bg-panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    width: 700px;
    max-width: 90vw;
    max-height: 80vh;
    display: flex;
    flex-direction: column;
    box-shadow: 0 8px 30px rgba(0,0,0,0.5);
}}
.modal-header {{
    padding: 14px 18px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-weight: 600;
    font-size: 14px;
}}
.modal-body {{
    flex: 1;
    overflow-y: auto;
    padding: 14px 18px;
}}
.modal-body pre {{
    background: var(--bg-input);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 12px;
    font-family: 'Cascadia Code', 'Consolas', monospace;
    font-size: 12px;
    line-height: 1.5;
    overflow-x: auto;
    color: #e6edf3;
    max-height: 50vh;
    white-space: pre;
}}
.modal-footer {{
    padding: 10px 18px;
    border-top: 1px solid var(--border);
    display: flex;
    gap: 8px;
    justify-content: flex-end;
}}
</style>
</head>
<body>

<!-- ── 顶栏 ── -->
<div class="topbar">
    <div class="logo">
        <span>🎮 Material Node Converter</span>
    </div>
    <div class="mode-tabs">
        <button class="mode-tab active" data-mode="t3d" id="tab-t3d">📋 T3D 节点粘贴</button>
        <button class="mode-tab" data-mode="hlsl" id="tab-hlsl">📝 HLSL / GLSL</button>
    </div>
    <div class="stats" id="stats">
        <span>节点: <b id="stat-nodes">0</b></span>
        <span>输入: <b id="stat-inputs">0</b></span>
        <span>连线: <b id="stat-conns">0</b></span>
    </div>
    <div class="actions">
        <!-- HLSL 模式: 加载示例 -->
        <div class="hlsl-actions" id="hlsl-actions" style="display:none;">
            <select id="example-select" title="加载示例">
                <option value="">📦 加载示例...</option>
                {example_options}
            </select>
        </div>
        <!-- T3D 模式: 解析按钮 -->
        <div class="t3d-actions" id="t3d-actions">
            <button class="btn btn-secondary" id="btn-parse-t3d" title="解析 T3D 文本并显示节点图">
                ▶ 解析
            </button>
        </div>
        <!-- 统一按钮 (两种模式共用) -->
        <button class="btn btn-primary" id="btn-to-nodes" title="转换为原生材质节点">
            ▶ 转节点
        </button>
        <button class="btn btn-convert" id="btn-to-custom" title="转换为 Custom Node">
            ▶ 转 Custom Node
        </button>
        <button class="btn btn-accent" id="btn-copy" title="复制 T3D 到剪贴板，在 UE4 材质编辑器 Ctrl+V 粘贴">
            📋 复制 UE4 节点
        </button>
    </div>
</div>

<!-- ── 主体 ── -->
<div class="main">
    <!-- 左侧编辑器 -->
    <div class="editor-panel" id="editor-panel">
        <!-- T3D 模式 header -->
        <div class="editor-header" id="editor-header-t3d">
            <span class="title">📋 UE4 节点 T3D 文本</span>
            <span id="char-count-t3d">0 字符</span>
        </div>
        <!-- HLSL 模式 header -->
        <div class="editor-header" id="editor-header-hlsl" style="display:none;">
            <span class="title">📝 HLSL / GLSL 代码</span>
            <span id="char-count">0 字符</span>
        </div>
        <div class="code-area">
            <!-- T3D 模式文本框 -->
            <textarea id="t3d-input" spellcheck="false"
                placeholder="在 UE4 材质编辑器中选中节点 → Ctrl+C 复制&#10;然后在这里 Ctrl+V 粘贴 T3D 文本&#10;&#10;示例格式：&#10;Begin Object Class=/Script/UnrealEd.MaterialGraphNode Name=&quot;MaterialGraphNode_0&quot;&#10;   Begin Object Class=/Script/Engine.MaterialExpressionAdd Name=&quot;MaterialExpressionAdd_0&quot;&#10;   End Object&#10;   ...&#10;End Object&#10;&#10;粘贴后点击 &quot;解析节点&quot; 或按 Ctrl+Enter"></textarea>
            <!-- HLSL 模式文本框 -->
            <textarea id="hlsl-input" spellcheck="false" style="display:none;"
                placeholder="// 支持自动识别 HLSL 和 GLSL/Shadertoy 代码&#10;// &#10;// HLSL 示例：&#10;// float3 c = lerp(a, b, uv.x); return c;&#10;//&#10;// Shadertoy/GLSL 示例：&#10;// void mainImage(out vec4 fragColor, in vec2 fragCoord) {{&#10;//     vec2 uv = fragCoord / iResolution.xy;&#10;//     fragColor = vec4(uv, 0.5, 1.0);&#10;// }}&#10;//&#10;// 按 Ctrl+Enter 转换"></textarea>
            <div id="code-type-indicator" style="position:absolute;bottom:8px;right:12px;font-size:11px;color:#888;pointer-events:none;"></div>
        </div>
        <div class="info-panel" id="info-panel">
            <div class="info-title">输出信息</div>
            <div class="success-item">就绪 - 从 UE4 材质编辑器复制节点后粘贴到左侧，点击"解析节点"</div>
        </div>
    </div>

    <!-- 拖拽分隔条 -->
    <div class="resize-handle" id="resize-handle"></div>

    <!-- 右侧节点图 -->
    <div class="graph-panel" id="graph-panel">
        <div class="empty-state" id="empty-state">
            <div class="icon">🔲</div>
            <div class="hint">从 UE4 复制节点粘贴到左侧<br>或输入 HLSL 代码<br>节点图将在这里显示</div>
        </div>

        <div id="graph-canvas">
            <svg id="graph-svg" width="4000" height="4000"></svg>
        </div>

        <div class="legend">
            <div class="legend-row"><div class="legend-dot" style="background:#4a6741"></div>常量</div>
            <div class="legend-row"><div class="legend-dot" style="background:#3d5a80"></div>数学运算</div>
            <div class="legend-row"><div class="legend-dot" style="background:#6b4c8a"></div>插值/钳制</div>
            <div class="legend-row"><div class="legend-dot" style="background:#4a7a8a"></div>三角函数</div>
            <div class="legend-row"><div class="legend-dot" style="background:#7a5a3d"></div>向量操作</div>
            <div class="legend-row"><div class="legend-dot" style="background:#8a3d3d"></div>纹理采样</div>
            <div class="legend-row"><div class="legend-dot" style="background:#8a5a3d"></div>纹理参数</div>
            <div class="legend-row"><div class="legend-dot" style="background:#8a7a3d"></div>输入参数</div>
            <div class="legend-row"><div class="legend-dot" style="background:#2d6a4f"></div>引擎内置</div>
            <div class="legend-row"><div class="legend-dot" style="background:#8a4a6a"></div>条件判断</div>
            <div class="legend-row"><div class="legend-dot" style="background:#4a7a5a"></div>MaterialFunction</div>
            <div class="legend-row"><div class="legend-dot" style="background:#5a5a5a"></div>Custom</div>
        </div>

        <div class="zoom-badge" id="zoom-badge">100%</div>

        <div class="loading-overlay" id="loading">
            <div class="spinner"></div>
        </div>
    </div>
</div>

<!-- ── Toast ── -->
<div class="toast" id="toast"></div>

<!-- ── UE4 自动执行结果面板 ── -->
<div class="exec-panel" id="exec-panel">
    <div class="exec-panel-header">
        <span>🚀 UE4 自动执行</span>
        <button class="btn" id="btn-exec-close" style="padding:2px 8px;font-size:11px;">✕</button>
    </div>
    <div class="exec-panel-body" id="exec-panel-body">
        <div class="exec-status running" id="exec-status">
            <div class="spinner" style="width:16px;height:16px;border-width:2px;"></div>
            <span>等待执行...</span>
        </div>
        <div id="exec-details"></div>
    </div>
</div>



<script>
// ═══════════════════════════════════════════════════════════
// 状态
// ═══════════════════════════════════════════════════════════
const EXAMPLES = {examples_json};

let currentMode = 't3d';  // 't3d' 或 'hlsl'
let currentGraphData = null;
let currentUE4Script = '';
let currentT3DText = '';   // 保存原始 T3D 文本（用于复制）
let scale = 1;
let panX = 0, panY = 0;
let isDragging = false;
let dragStartX = 0, dragStartY = 0;

// ═══════════════════════════════════════════════════════════
// DOM 引用
// ═══════════════════════════════════════════════════════════
const $input = document.getElementById('hlsl-input');
const $t3dInput = document.getElementById('t3d-input');
const $canvas = document.getElementById('graph-canvas');
const $svg = document.getElementById('graph-svg');
const $graphPanel = document.getElementById('graph-panel');
const $emptyState = document.getElementById('empty-state');
const $loading = document.getElementById('loading');
const $infoPanel = document.getElementById('info-panel');
const $charCount = document.getElementById('char-count');
const $charCountT3d = document.getElementById('char-count-t3d');
const $zoomBadge = document.getElementById('zoom-badge');
const $toast = document.getElementById('toast');
// const $modal removed
// const $scriptContent removed
const $editorPanel = document.getElementById('editor-panel');
const $resizeHandle = document.getElementById('resize-handle');

// ═══════════════════════════════════════════════════════════
// 模式切换
// ═══════════════════════════════════════════════════════════

function switchMode(mode) {{
    currentMode = mode;
    
    // 更新标签
    document.querySelectorAll('.mode-tab').forEach(tab => {{
        tab.classList.toggle('active', tab.dataset.mode === mode);
    }});
    
    // 切换显示
    const isT3D = mode === 't3d';
    document.getElementById('hlsl-actions').style.display = isT3D ? 'none' : 'flex';
    document.getElementById('t3d-actions').style.display = isT3D ? 'flex' : 'none';
    document.getElementById('editor-header-t3d').style.display = isT3D ? 'flex' : 'none';
    document.getElementById('editor-header-hlsl').style.display = isT3D ? 'none' : 'flex';
    $t3dInput.style.display = isT3D ? '' : 'none';
    $input.style.display = isT3D ? 'none' : '';
}}

document.getElementById('tab-t3d').addEventListener('click', () => switchMode('t3d'));
document.getElementById('tab-hlsl').addEventListener('click', () => switchMode('hlsl'));

// ═══════════════════════════════════════════════════════════
// 转换功能
// ═══════════════════════════════════════════════════════════

// ═══════════════════════════════════════════════════════════
// T3D 解析功能
// ═══════════════════════════════════════════════════════════

async function parseT3D() {{
    const text = $t3dInput.value.trim();
    if (!text) {{
        showToast('请粘贴 UE4 节点 T3D 文本', 'error');
        return;
    }}

    $loading.classList.add('show');
    $emptyState.style.display = 'none';

    try {{
        const resp = await fetch('api/parse-t3d', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ t3d_text: text }}),
        }});

        const data = await resp.json();

        if (data.error) {{
            showInfo([{{ type: 'error', text: data.error }}]);
            showToast('T3D 解析失败', 'error');
            $loading.classList.remove('show');
            return;
        }}

        currentGraphData = data.graph;
        currentT3DText = text;  // 保存原始 T3D 文本用于复制
        renderGraph(currentGraphData);

        // 更新统计
        document.getElementById('stat-nodes').textContent = data.graph.stats.node_count;
        document.getElementById('stat-inputs').textContent = data.graph.stats.input_count;
        document.getElementById('stat-conns').textContent = data.graph.stats.connection_count;

        // 更新信息面板
        const msgs = [];
        msgs.push({{ type: 'success', text: `✓ T3D 解析完成 — ${{data.graph.stats.node_count}} 个节点, ${{data.graph.stats.connection_count}} 条连线` }});
        if (data.graph.warnings && data.graph.warnings.length > 0) {{
            data.graph.warnings.forEach(w => msgs.push({{ type: 'warning', text: w }}));
        }}
        showInfo(msgs);
        showToast('T3D 解析成功！', 'success');

    }} catch (err) {{
        showInfo([{{ type: 'error', text: '请求失败: ' + err.message }}]);
        showToast('请求失败', 'error');
    }}

    $loading.classList.remove('show');
}}

async function copyT3DToClipboard() {{
    // 复制当前的 T3D 文本到剪贴板（不强制重新转换，直接复制上次转换结果）
    let textToCopy = '';
    
    if (currentMode === 't3d') {{
        textToCopy = currentT3DText || $t3dInput.value.trim();
    }} else {{
        textToCopy = currentT3DText;
    }}
    
    if (!textToCopy) {{
        showToast('请先进行转换（转节点 或 转 Custom Node）', 'error');
        return;
    }}
    
    try {{
        await navigator.clipboard.writeText(textToCopy);
        showToast('✅ 已复制 UE4 节点到剪贴板！在材质编辑器 Ctrl+V 粘贴', 'success');
    }} catch (e) {{
        // 降级方案
        const ta = document.createElement('textarea');
        ta.value = textToCopy;
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        showToast('✅ 已复制 UE4 节点到剪贴板！在材质编辑器 Ctrl+V 粘贴', 'success');
    }}
}}

// ═══════════════════════════════════════════════════════════
// Custom 节点转换功能
// ═══════════════════════════════════════════════════════════

async function convertCustomNodes() {{
    const text = $t3dInput.value.trim();
    if (!text) {{
        showToast('请先粘贴 UE4 节点 T3D 文本', 'error');
        return;
    }}

    $loading.classList.add('show');
    $emptyState.style.display = 'none';

    try {{
        const resp = await fetch('api/convert-custom', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ t3d_text: text }}),
        }});

        const data = await resp.json();

        if (data.error) {{
            showInfo([{{ type: 'error', text: data.error }}]);
            showToast('转换失败', 'error');
            $loading.classList.remove('show');
            return;
        }}

        // 更新图显示
        if (data.graph) {{
            currentGraphData = data.graph;
            renderGraph(currentGraphData);
            document.getElementById('stat-nodes').textContent = data.graph.stats.node_count;
            document.getElementById('stat-inputs').textContent = data.graph.stats.input_count;
            document.getElementById('stat-conns').textContent = data.graph.stats.connection_count;
        }}

        // 保存转换后的 T3D 文本（用于复制）
        if (data.t3d_output) {{
            currentT3DText = data.t3d_output;
        }}

        // 更新信息面板
        const msgs = [];
        if (data.custom_count === 0) {{
            msgs.push({{ type: 'warning', text: '未找到 Custom 节点，无需转换' }});
        }} else {{
            msgs.push({{ type: 'success', text: `✓ 找到 ${{data.custom_count}} 个 Custom 节点，成功转换 ${{data.converted_count}} 个 → ${{data.total_new_nodes}} 个原生节点` }});
        }}
        if (data.warnings && data.warnings.length > 0) {{
            data.warnings.forEach(w => {{
                if (w.startsWith('✓')) {{
                    msgs.push({{ type: 'success', text: w }});
                }} else if (w.startsWith('✗')) {{
                    msgs.push({{ type: 'error', text: w }});
                }} else {{
                    msgs.push({{ type: 'warning', text: w }});
                }}
            }});
        }}
        showInfo(msgs);

        if (data.converted_count > 0) {{
            showToast(`🔄 已转换 ${{data.converted_count}} 个 Custom 节点 → ${{data.total_new_nodes}} 个原生节点！`, 'success');
        }} else {{
            showToast('没有可转换的 Custom 节点', 'error');
        }}

    }} catch (err) {{
        showInfo([{{ type: 'error', text: '请求失败: ' + err.message }}]);
        showToast('请求失败', 'error');
    }}

    $loading.classList.remove('show');
}}

// ═══════════════════════════════════════════════════════════
// HLSL 转换功能
// ═══════════════════════════════════════════════════════════

async function convertHLSL() {{
    const code = $input.value.trim();
    if (!code) {{
        showToast('请输入 HLSL 或 GLSL 代码', 'error');
        return;
    }}

    $loading.classList.add('show');
    $emptyState.style.display = 'none';

    // 前端预检测类型，显示提示
    const preDetect = detectCodeTypeLocal(code);
    if (preDetect === 'glsl') {{
        showInfo([{{ type: 'success', text: '检测到 Shadertoy/GLSL 代码，自动转换为 HLSL...' }}]);
    }}

    try {{
        const resp = await fetch('api/convert', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ code: code, name: 'M_Generated' }}),
        }});

        const data = await resp.json();

        if (data.error) {{
            showInfo([{{ type: 'error', text: data.error }}]);
            showToast('解析失败', 'error');
            $loading.classList.remove('show');
            return;
        }}

        currentGraphData = data.graph;
        currentUE4Script = data.ue4_script || '';
        currentT3DText = data.t3d_output || '';
        renderGraph(currentGraphData);

        // 更新统计
        document.getElementById('stat-nodes').textContent = data.graph.stats.node_count;
        document.getElementById('stat-inputs').textContent = data.graph.stats.input_count;
        document.getElementById('stat-conns').textContent = data.graph.stats.connection_count;

        // 更新信息面板
        const msgs = [];
        if (data.detected_type === 'glsl' && data.hlsl_code) {{
            msgs.push({{ type: 'success', text: '✓ GLSL → HLSL 转换完成' }});
            msgs.push({{ type: 'success', text: '生成的 HLSL 代码：' }});
            msgs.push({{ type: 'success', text: data.hlsl_code.substring(0, 500) + (data.hlsl_code.length > 500 ? '...' : '') }});
        }}
        msgs.push({{ type: 'success', text: `✓ 转换完成 — ${{data.graph.stats.node_count}} 个节点, ${{data.graph.stats.connection_count}} 条连线` }});
        if (data.graph.warnings && data.graph.warnings.length > 0) {{
            data.graph.warnings.forEach(w => msgs.push({{ type: 'warning', text: w }}));
        }}
        showInfo(msgs);
        showToast('转换成功！', 'success');

    }} catch (err) {{
        showInfo([{{ type: 'error', text: '请求失败: ' + err.message }}]);
        showToast('请求失败', 'error');
    }}

    $loading.classList.remove('show');
}}

// ═══════════════════════════════════════════════════════════
// 渲染节点图
// ═══════════════════════════════════════════════════════════

function renderGraph(graphData) {{
    // 清空旧内容
    const oldNodes = $canvas.querySelectorAll('.mat-node');
    oldNodes.forEach(n => n.remove());
    $svg.innerHTML = '';

    if (!graphData || !graphData.nodes || graphData.nodes.length === 0) {{
        $emptyState.style.display = '';
        return;
    }}

    $emptyState.style.display = 'none';

    // 调整 SVG 大小
    let maxX = 0, maxY = 0;
    graphData.nodes.forEach(n => {{
        if (n.x + 300 > maxX) maxX = n.x + 300;
        if (n.y + 200 > maxY) maxY = n.y + 200;
    }});
    $svg.setAttribute('width', maxX + 200);
    $svg.setAttribute('height', maxY + 200);

    // ── 节点元素映射 ──
    const nodeEls = {{}};
    const inputDots = {{}};
    const outputDots = {{}};

    // 创建节点
    graphData.nodes.forEach(n => {{
        const div = document.createElement('div');
        div.className = 'mat-node' + (n.is_output ? ' output-node' : '') + (n.is_input ? ' input-node' : '');
        div.style.left = n.x + 'px';
        div.style.top = n.y + 'px';
        div.setAttribute('data-node-id', n.id);

        const inputNames = (n.input_names && n.input_names.length > 0) ? n.input_names : [];
        const maxRows = Math.max(inputNames.length, 1);
        let pinsHtml = '';

        for (let i = 0; i < maxRows; i++) {{
            let leftHtml = '';
            let rightHtml = '';

            if (i < inputNames.length) {{
                const iname = inputNames[i];
                const connected = n.inputs.some(inp => inp.name === iname);
                leftHtml = `<div class="pin-left"><div class="pin-dot ${{connected ? 'connected' : ''}}" data-input-dot="${{n.id}}-${{i}}"></div><span class="pin-name">${{iname}}</span></div>`;
            }} else {{
                leftHtml = '<div class="pin-left"></div>';
            }}

            if (i === 0) {{
                rightHtml = `<div class="pin-right"><div class="pin-dot output-dot" data-output-dot="${{n.id}}"></div></div>`;
            }} else {{
                rightHtml = '<div class="pin-right"></div>';
            }}

            pinsHtml += `<div class="pin-row">${{leftHtml}}${{rightHtml}}</div>`;
        }}

        const icon = n.is_output ? '📤 ' : (n.is_input ? '📥 ' : (n.is_builtin ? '🔧 ' : ''));

        div.innerHTML = `
            <div class="mat-node-header" style="background:${{n.color}}">${{icon}}${{n.display_name}}</div>
            <div class="mat-node-body">
                ${{pinsHtml}}
                <div class="mat-node-class">${{n.ue_class}}</div>
            </div>
        `;

        if (n.properties) {{
            div.title = n.properties;
        }}

        $canvas.appendChild(div);
        nodeEls[n.id] = div;
    }});

    // 收集 pin dot 元素
    $canvas.querySelectorAll('[data-input-dot]').forEach(el => {{
        inputDots[el.getAttribute('data-input-dot')] = el;
    }});
    $canvas.querySelectorAll('[data-output-dot]').forEach(el => {{
        outputDots[el.getAttribute('data-output-dot')] = el;
    }});

    // 精确获取 pin dot 中心坐标
    function getDotCenter(dotEl) {{
        const nodeEl = dotEl.closest('.mat-node');
        const nodeX = parseFloat(nodeEl.style.left);
        const nodeY = parseFloat(nodeEl.style.top);
        const dotRect = dotEl.getBoundingClientRect();
        const nodeRect = nodeEl.getBoundingClientRect();
        const nodeScale = nodeRect.width / nodeEl.offsetWidth;
        const dx = (dotRect.left - nodeRect.left) / nodeScale + dotRect.width / nodeScale / 2;
        const dy = (dotRect.top - nodeRect.top) / nodeScale + dotRect.height / nodeScale / 2;
        return {{ x: nodeX + dx, y: nodeY + dy }};
    }}

    // 延迟绘制连线（等待 DOM 布局完成）
    setTimeout(() => {{
        graphData.connections.forEach(c => {{
            const outDot = outputDots[c.from_id];
            const inDot = inputDots[c.to_id + '-' + c.to_pin_index];
            if (!outDot || !inDot) return;

            const srcNode = graphData.nodes.find(n => n.id === c.from_id);
            const color = srcNode ? srcNode.color : '#555';
            const from = getDotCenter(outDot);
            const to = getDotCenter(inDot);

            const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
            const dx = Math.abs(from.x - to.x) * 0.5;
            path.setAttribute('d', `M${{from.x}},${{from.y}} C${{from.x + dx}},${{from.y}} ${{to.x - dx}},${{to.y}} ${{to.x}},${{to.y}}`);
            path.setAttribute('stroke', color);
            path.style.pointerEvents = 'stroke';
            $svg.appendChild(path);
        }});
    }}, 50);

    // 自动居中
    autoFitView(maxX, maxY);
}}

function autoFitView(contentW, contentH) {{
    const panelRect = $graphPanel.getBoundingClientRect();
    const pw = panelRect.width;
    const ph = panelRect.height;

    if (contentW <= 0 || contentH <= 0) return;

    const fitScale = Math.min(pw / (contentW + 100), ph / (contentH + 100), 1.2);
    scale = Math.max(0.15, Math.min(fitScale, 1.5));

    panX = (pw - contentW * scale) / 2;
    panY = (ph - contentH * scale) / 2;

    updateTransform();
}}

// ═══════════════════════════════════════════════════════════
// 平移 & 缩放
// ═══════════════════════════════════════════════════════════

function updateTransform() {{
    $canvas.style.transform = `translate(${{panX}}px, ${{panY}}px) scale(${{scale}})`;
    $zoomBadge.textContent = Math.round(scale * 100) + '%';
}}

$graphPanel.addEventListener('mousedown', e => {{
    if (e.target === $graphPanel || e.target === $canvas || e.target === $svg || e.target.id === 'empty-state' || e.target.classList.contains('hint') || e.target.classList.contains('icon')) {{
        isDragging = true;
        dragStartX = e.clientX - panX;
        dragStartY = e.clientY - panY;
    }}
}});
document.addEventListener('mousemove', e => {{
    if (isDragging) {{
        panX = e.clientX - dragStartX;
        panY = e.clientY - dragStartY;
        updateTransform();
    }}
}});
document.addEventListener('mouseup', () => isDragging = false);

$graphPanel.addEventListener('wheel', e => {{
    e.preventDefault();
    const delta = e.deltaY > 0 ? 0.9 : 1.1;
    const newScale = Math.max(0.1, Math.min(3, scale * delta));
    const rect = $graphPanel.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    panX = mx - (mx - panX) * (newScale / scale);
    panY = my - (my - panY) * (newScale / scale);
    scale = newScale;
    updateTransform();
}}, {{ passive: false }});

// ═══════════════════════════════════════════════════════════
// 拖拽分隔条
// ═══════════════════════════════════════════════════════════

let isResizing = false;
$resizeHandle.addEventListener('mousedown', e => {{
    isResizing = true;
    $resizeHandle.classList.add('active');
    e.preventDefault();
}});
document.addEventListener('mousemove', e => {{
    if (isResizing) {{
        const newWidth = Math.max(280, Math.min(600, e.clientX));
        $editorPanel.style.width = newWidth + 'px';
    }}
}});
document.addEventListener('mouseup', () => {{
    isResizing = false;
    $resizeHandle.classList.remove('active');
}});

// ═══════════════════════════════════════════════════════════
// 按钮事件
// ═══════════════════════════════════════════════════════════

// 转 Custom Node 功能
// ═══════════════════════════════════════════════════════════

async function reverseToCustomNode() {{
    // T3D 模式: 从输入框取 T3D 文本
    // HLSL 模式: 如果已经有节点图，用当前 T3D 数据
    let t3dText = '';
    if (currentMode === 't3d') {{
        t3dText = $t3dInput.value.trim();
    }} else {{
        t3dText = currentT3DText;
    }}
    
    if (!t3dText) {{
        showToast('请先转换为节点或粘贴 T3D 文本', 'error');
        return;
    }}

    $loading.classList.add('show');
    try {{
        const resp = await fetch('api/reverse-convert', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ t3d_text: t3dText }}),
        }});
        const data = await resp.json();
        if (data.error) {{
            showToast('反向转换失败: ' + data.error, 'error');
            $loading.classList.remove('show');
            return;
        }}
        if (data.t3d_output) {{
            currentT3DText = data.t3d_output;
            // 重新解析 T3D 并渲染节点图
            try {{
                const parseResp = await fetch('api/parse-t3d', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ t3d_text: data.t3d_output }}),
                }});
                const parseData = await parseResp.json();
                if (parseData.graph) {{
                    currentGraphData = parseData.graph;
                    renderGraph(currentGraphData);
                    document.getElementById('stat-nodes').textContent = parseData.graph.stats.node_count;
                    document.getElementById('stat-inputs').textContent = parseData.graph.stats.input_count;
                    document.getElementById('stat-conns').textContent = parseData.graph.stats.connection_count;
                }}
            }} catch (e) {{ console.warn('Re-parse failed:', e); }}
        }}
        showToast('已转换为 Custom Node', 'success');
    }} catch (err) {{
        showToast('请求失败: ' + err.message, 'error');
    }}
    $loading.classList.remove('show');
}}

async function hlslToCustomNode() {{
    const code = $input.value.trim();
    if (!code) {{
        showToast('请输入 HLSL / GLSL 代码', 'error');
        return;
    }}

    $loading.classList.add('show');
    try {{
        const resp = await fetch('api/hlsl-to-custom-node', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ code: code }}),
        }});
        const data = await resp.json();
        if (data.error) {{
            showToast('转换失败: ' + data.error, 'error');
            $loading.classList.remove('show');
            return;
        }}
        if (data.t3d_output) {{
            currentT3DText = data.t3d_output;
            // 重新解析 T3D 并渲染节点图
            try {{
                const parseResp = await fetch('api/parse-t3d', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ t3d_text: data.t3d_output }}),
                }});
                const parseData = await parseResp.json();
                if (parseData.graph) {{
                    currentGraphData = parseData.graph;
                    renderGraph(currentGraphData);
                    document.getElementById('stat-nodes').textContent = parseData.graph.stats.node_count;
                    document.getElementById('stat-inputs').textContent = parseData.graph.stats.input_count;
                    document.getElementById('stat-conns').textContent = parseData.graph.stats.connection_count;
                }}
            }} catch (e) {{ console.warn('Re-parse failed:', e); }}
        }}
        const inputs = (data.input_names || []).join(', ');
        showToast('已转换为 Custom Node' + (inputs ? ' (输入: ' + inputs + ')' : ''), 'success');
    }} catch (err) {{
        showToast('请求失败: ' + err.message, 'error');
    }}
    $loading.classList.remove('show');
}}

// 统一按钮事件
document.getElementById('btn-to-nodes').addEventListener('click', () => {{
    if (currentMode === 't3d') {{
        convertCustomNodes();
    }} else {{
        convertHLSL();
    }}
}});

document.getElementById('btn-to-custom').addEventListener('click', () => {{
    if (currentMode === 't3d') {{
        // T3D 模式: 反向转换为 Custom Node
        reverseToCustomNode();
    }} else {{
        // HLSL 模式: 直接包装为 Custom Node
        hlslToCustomNode();
    }}
}});

document.getElementById('btn-copy').addEventListener('click', copyT3DToClipboard);

document.getElementById('btn-parse-t3d').addEventListener('click', () => {{
    parseT3D();
}});

document.getElementById('example-select').addEventListener('change', function() {{
    if (this.value && EXAMPLES[this.value]) {{
        $input.value = EXAMPLES[this.value];
        $charCount.textContent = $input.value.length + ' 字符';
        this.value = '';
        // 自动触发转换
        convertHLSL();
    }}
}});

// btn-ue4 handler removed

// 弹窗相关事件已移除

// ═══════════════════════════════════════════════════════════
// UE4 自动执行
// ═══════════════════════════════════════════════════════════

// const $execPanel removed
// const $execStatus removed
// const $execDetails removed
// const $btnExecute removed

// 检查 UE4 环境
async function checkUE4Env() {{
    try {{
        const resp = await fetch('api/check-env');
        const data = await resp.json();
        return data;
    }} catch (e) {{
        return {{ ready: false, errors: [e.message] }};
    }}
}}

// 自动执行
async function executeInUE4() {{
    const code = $input.value.trim();
    if (!code) {{
        showToast('请先输入 HLSL 代码', 'error');
        return;
    }}

    // 检查环境
    const env = await checkUE4Env();
    if (!env.ready) {{
        showToast('UE4 环境未就绪: ' + (env.errors || []).join(', '), 'error');
        return;
    }}

    // 显示执行面板
    $execPanel.classList.add('show');
    $btnExecute.classList.add('running');
    $btnExecute.innerHTML = '⏳ 执行中...';

    $execStatus.className = 'exec-status running';
    $execStatus.innerHTML = '<div class="spinner" style="width:16px;height:16px;border-width:2px;"></div><span>正在启动 UE4 引擎...</span>';
    $execDetails.innerHTML = '<div class="exec-detail-row"><span>引擎初始化中，预计 30-60 秒...</span></div>';

    try {{
        const resp = await fetch('api/execute', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ code: code, name: 'M_Generated' }}),
        }});

        const data = await resp.json();
        displayExecResult(data);

    }} catch (err) {{
        $execStatus.className = 'exec-status error';
        $execStatus.innerHTML = '💥 <span>请求失败</span>';
        $execDetails.innerHTML = `<div class="exec-error-list"><div>${{err.message}}</div></div>`;
    }}

    $btnExecute.classList.remove('running');
    $btnExecute.innerHTML = '🚀 自动执行';
}}

function displayExecResult(data) {{
    const status = data.status || 'UNKNOWN';
    const statusMap = {{
        'PASS': {{ cls: 'pass', icon: '✅', text: '验证通过' }},
        'PASS_NO_COUNT': {{ cls: 'pass', icon: '✅', text: '执行成功（无法验证节点数）' }},
        'WARN': {{ cls: 'warn', icon: '⚠️', text: '有警告' }},
        'FAIL': {{ cls: 'fail', icon: '❌', text: '验证失败' }},
        'ERROR': {{ cls: 'error', icon: '💥', text: '执行异常' }},
        'TIMEOUT': {{ cls: 'error', icon: '⏰', text: '执行超时' }},
        'ENV_ERROR': {{ cls: 'error', icon: '🔧', text: '环境错误' }},
        'PARSE_ERROR': {{ cls: 'error', icon: '📝', text: '解析错误' }},
        'ENGINE_ERROR': {{ cls: 'error', icon: '🔥', text: '引擎错误' }},
        'COMPLETED_NO_VALIDATION': {{ cls: 'warn', icon: '🔶', text: '执行完成（无验证数据）' }},
    }};

    const s = statusMap[status] || {{ cls: 'warn', icon: '❓', text: status }};
    $execStatus.className = `exec-status ${{s.cls}}`;
    $execStatus.innerHTML = `${{s.icon}} <span>${{s.text}}</span>`;

    let detailsHtml = '';
    detailsHtml += `<div class="exec-detail-row"><span>材质名称</span><b>${{data.material_name || 'N/A'}}</b></div>`;
    detailsHtml += `<div class="exec-detail-row"><span>期望节点数</span><b>${{data.node_count || 0}}</b></div>`;
    detailsHtml += `<div class="exec-detail-row"><span>连线数</span><b>${{data.connection_count || 0}}</b></div>`;
    detailsHtml += `<div class="exec-detail-row"><span>执行耗时</span><b>${{data.execution_time || 0}}s</b></div>`;

    if (data.validation) {{
        const v = data.validation;
        if (v.actual_nodes !== undefined) {{
            detailsHtml += `<div class="exec-detail-row"><span>实际节点数</span><b>${{v.actual_nodes}}</b></div>`;
        }}
        if (v.compile_success !== undefined) {{
            detailsHtml += `<div class="exec-detail-row"><span>编译结果</span><b>${{v.compile_success ? '✅ 成功' : '❌ 失败'}}</b></div>`;
        }}
        if (v.warnings && v.warnings.length > 0) {{
            detailsHtml += '<div class="exec-error-list" style="background:#d2992210;border-color:#d2992230;color:#d29922;">';
            v.warnings.forEach(w => {{ detailsHtml += `<div>⚠ ${{w}}</div>`; }});
            detailsHtml += '</div>';
        }}
    }}

    if (data.engine_errors && data.engine_errors.length > 0) {{
        detailsHtml += '<div class="exec-error-list">';
        data.engine_errors.forEach(e => {{ detailsHtml += `<div>✗ ${{e}}</div>`; }});
        detailsHtml += '</div>';
    }}

    $execDetails.innerHTML = detailsHtml;

    if (s.cls === 'pass') {{
        showToast('🎉 UE4 自动执行成功！', 'success');
    }} else if (s.cls === 'fail' || s.cls === 'error') {{
        showToast('UE4 执行失败', 'error');
    }}
}}

// btn-execute handler removed
document.getElementById('btn-exec-close').addEventListener('click', () => {{
    $execPanel.classList.remove('show');
}});

// Ctrl+Enter 快捷键 (HLSL)
$input.addEventListener('keydown', e => {{
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {{
        e.preventDefault();
        convertHLSL();
    }}
    // Tab 输入
    if (e.key === 'Tab') {{
        e.preventDefault();
        const start = $input.selectionStart;
        const end = $input.selectionEnd;
        $input.value = $input.value.substring(0, start) + '    ' + $input.value.substring(end);
        $input.selectionStart = $input.selectionEnd = start + 4;
    }}
}});

// Ctrl+Enter 快捷键 (T3D)
$t3dInput.addEventListener('keydown', e => {{
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {{
        e.preventDefault();
        parseT3D();
    }}
    // Tab 输入
    if (e.key === 'Tab') {{
        e.preventDefault();
        const start = $t3dInput.selectionStart;
        const end = $t3dInput.selectionEnd;
        $t3dInput.value = $t3dInput.value.substring(0, start) + '    ' + $t3dInput.value.substring(end);
        $t3dInput.selectionStart = $t3dInput.selectionEnd = start + 4;
    }}
}});

// 字符计数 (T3D)
$t3dInput.addEventListener('input', () => {{
    $charCountT3d.textContent = $t3dInput.value.length + ' 字符';
}});

// 字符计数
$input.addEventListener('input', () => {{
    $charCount.textContent = $input.value.length + ' 字符';
    // 延迟检测代码类型
    clearTimeout($input._detectTimer);
    $input._detectTimer = setTimeout(() => {{
        const code = $input.value.trim();
        const indicator = document.getElementById('code-type-indicator');
        if (!code) {{
            indicator.textContent = '';
            return;
        }}
        const codeType = detectCodeTypeLocal(code);
        if (codeType === 'glsl') {{
            indicator.textContent = 'GLSL/Shadertoy';
            indicator.style.color = '#bc8cff';
        }} else if (codeType === 't3d') {{
            indicator.textContent = 'T3D';
            indicator.style.color = '#d29922';
        }} else {{
            indicator.textContent = 'HLSL';
            indicator.style.color = '#3fb950';
        }}
    }}, 300);
}});

// 前端代码类型检测函数
function detectCodeTypeLocal(code) {{
    const stripped = code.trim();
    if (stripped.startsWith('Begin Object') || stripped.includes('Class=/Script/')) {{
        return 't3d';
    }}
    const glslIndicators = ['void mainImage', 'vec2 ', 'vec3 ', 'vec4 ', 'mat2 ', 'mat3 ', 'mat4 ',
                            'iTime', 'iResolution', 'iMouse', 'iChannel', 'fragCoord', 'fragColor',
                            'mix(', 'fract(', 'texture(', 'texture2D(', 'gl_Frag'];
    let score = 0;
    for (const ind of glslIndicators) {{
        if (code.includes(ind)) score++;
    }}
    if (score >= 2) return 'glsl';
    return 'hlsl';
}}

// 反向转换按钮事件
// btn-reverse handler removed (functionality moved to btn-to-custom)

// ═══════════════════════════════════════════════════════════
// 工具函数
// ═══════════════════════════════════════════════════════════

function showInfo(messages) {{
    let html = '<div class="info-title">输出信息</div>';
    let hasWarning = false;
    messages.forEach(m => {{
        if (m.type === 'error') {{
            html += `<div class="error-item">${{m.text}}</div>`;
            hasWarning = true;
        }} else if (m.type === 'warning') {{
            html += `<div class="warning-item">${{m.text}}</div>`;
            hasWarning = true;
        }} else {{
            html += `<div class="success-item">${{m.text}}</div>`;
        }}
    }});
    $infoPanel.innerHTML = html;
    $infoPanel.className = 'info-panel' + (hasWarning ? ' has-warnings' : '');
}}

function showToast(msg, type) {{
    $toast.textContent = msg;
    $toast.className = 'toast ' + type;
    requestAnimationFrame(() => {{
        $toast.classList.add('show');
    }});
    setTimeout(() => {{
        $toast.classList.remove('show');
    }}, 2500);
}}

// 初始化
updateTransform();
</script>
</body>
</html>'''


# ═══════════════════════════════════════════════════════════
# HTTP 请求处理器
# ═══════════════════════════════════════════════════════════

def detect_code_type(code: str) -> str:
    """自动检测代码类型: 'glsl', 'hlsl', 或 't3d'"""
    code_stripped = code.strip()
    # T3D 格式检测
    if code_stripped.startswith('Begin Object') or 'Class=/Script/' in code_stripped:
        return 't3d'
    # GLSL/Shadertoy 特征检测
    glsl_indicators = ['void mainImage', 'vec2 ', 'vec3 ', 'vec4 ', 'mat2 ', 'mat3 ', 'mat4 ',
                       'iTime', 'iResolution', 'iMouse', 'iChannel', 'fragCoord', 'fragColor',
                       'mix(', 'fract(', 'texture(', 'texture2D(', 'gl_Frag']
    glsl_score = sum(1 for indicator in glsl_indicators if indicator in code)
    if glsl_score >= 2:
        return 'glsl'
    return 'hlsl'


class HLSLHandler(BaseHTTPRequestHandler):
    """处理 HTTP 请求"""

    def log_message(self, format, *args):
        """覆写日志，使输出更简洁"""
        sys.stdout.write(f"  {args[0]}\n")

    def do_GET(self):
        """处理 GET 请求"""
        parsed = urlparse(self.path)

        if parsed.path == '/' or parsed.path == '/index.html':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(get_index_html().encode('utf-8'))
        elif parsed.path == '/api/check-env':
            self._handle_check_env()
        elif parsed.path == '/api/exec-status':
            self._handle_exec_status()
        else:
            self.send_response(404)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'Not Found')

    def do_POST(self):
        """处理 POST 请求"""
        parsed = urlparse(self.path)

        if parsed.path == '/api/convert':
            self._handle_convert()
        elif parsed.path == '/api/ue4script':
            self._handle_ue4script()
        elif parsed.path == '/api/execute':
            self._handle_execute()
        elif parsed.path == '/api/config':
            self._handle_update_config()
        elif parsed.path == '/api/parse-t3d':
            self._handle_parse_t3d()
        elif parsed.path == '/api/generate-t3d':
            self._handle_generate_t3d()
        elif parsed.path == '/api/hlsl-to-t3d':
            self._handle_hlsl_to_t3d()
        elif parsed.path == '/api/convert-custom':
            self._handle_convert_custom()
        elif parsed.path == '/api/reverse-convert':
            self._handle_reverse_convert()
        elif parsed.path == '/api/detect-type':
            self._handle_detect_type()
        elif parsed.path == '/api/hlsl-to-custom-node':
            self._handle_hlsl_to_custom_node()
        else:
            self.send_response(404)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': 'Not Found'}).encode('utf-8'))

    def _handle_convert(self):
        """处理 HLSL/GLSL 转换请求（自动检测代码类型）"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body)

            code = data.get('code', '')
            name = data.get('name', 'M_Generated')

            if not code.strip():
                self._send_json({'error': '代码为空'})
                return

            # 自动检测代码类型
            detected_type = detect_code_type(code)
            hlsl_code = code
            glsl_converted = False

            # 如果是 GLSL，先转换为 HLSL
            if detected_type == 'glsl':
                try:
                    converter = ShadertoyConverter()
                    hlsl_code = converter.convert(code)
                    glsl_converted = True
                except Exception as e:
                    self._send_json({'error': f'GLSL → HLSL 转换失败: {str(e)}'})
                    return

            # 解析并转换
            graph = hlsl_to_material_graph(hlsl_code)
            graph_data = graph_to_json(graph)

            # 生成 T3D（供复制到 UE4）
            from t3d_generator import generate_t3d_from_material_graph
            t3d_output = generate_t3d_from_material_graph(graph)

            # 生成 UE4 脚本
            ue4_script = generate_ue4_script(graph, name, '/Game/Materials')

            result = {
                'graph': graph_data,
                'ue4_script': ue4_script,
                't3d_output': t3d_output,
                'detected_type': detected_type,
            }

            # 如果是从 GLSL 转换的，附带生成的 HLSL 代码
            if glsl_converted:
                result['hlsl_code'] = hlsl_code

            self._send_json(result)

        except SyntaxError as e:
            self._send_json({'error': f'HLSL 解析错误: {str(e)}'})
        except Exception as e:
            traceback.print_exc()
            self._send_json({'error': f'内部错误: {str(e)}'})

    def _handle_ue4script(self):
        """单独生成 UE4 脚本"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body)

            code = data.get('code', '')
            name = data.get('name', 'M_Generated')
            path = data.get('path', '/Game/Materials')

            graph = hlsl_to_material_graph(code)
            script = generate_ue4_script(graph, name, path)

            self._send_json({'script': script})

        except Exception as e:
            self._send_json({'error': str(e)})

    def _handle_check_env(self):
        """检查 UE4 环境就绪状态"""
        try:
            executor = get_executor()
            env = executor.check_environment()
            config = load_config()
            env['config'] = {
                'ue4_editor_cmd': config['ue4_editor_cmd'],
                'project_path': config['project_path'],
                'timeout': config.get('timeout', 120),
            }
            self._send_json(env)
        except Exception as e:
            self._send_json({'ready': False, 'errors': [str(e)]})

    def _handle_execute(self):
        """执行 HLSL → UE4 自动闭环"""
        global _exec_status
        
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body)

            code = data.get('code', '')
            name = data.get('name', 'M_Generated')

            if not code.strip():
                self._send_json({'status': 'ERROR', 'engine_errors': ['代码为空']})
                return

            if _exec_status['running']:
                self._send_json({'status': 'BUSY', 'engine_errors': ['已有执行任务在运行中']})
                return

            # 同步执行（HTTP 请求等待完成）
            # 对于长时间执行，前端会 fetch 并等待
            _exec_status['running'] = True
            _exec_status['progress'] = '启动中...'

            try:
                executor = get_executor()
                result = executor.execute_hlsl(code, name)
                _exec_status['result'] = result
                self._send_json(result)
            finally:
                _exec_status['running'] = False

        except Exception as e:
            _exec_status['running'] = False
            traceback.print_exc()
            self._send_json({
                'status': 'ERROR',
                'engine_errors': [f'服务器异常: {str(e)}'],
            })

    def _handle_exec_status(self):
        """获取当前执行状态"""
        self._send_json({
            'running': _exec_status['running'],
            'progress': _exec_status.get('progress', ''),
            'result': _exec_status.get('result'),
        })

    def _handle_update_config(self):
        """更新配置"""
        global _ue4_executor
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body)

            config = load_config()
            if 'ue4_editor_cmd' in data:
                config['ue4_editor_cmd'] = data['ue4_editor_cmd']
            if 'project_path' in data:
                config['project_path'] = data['project_path']
            if 'timeout' in data:
                config['timeout'] = int(data['timeout'])
            
            save_config(config)
            _ue4_executor = None  # 重置执行器
            
            self._send_json({'success': True, 'config': config})
        except Exception as e:
            self._send_json({'error': str(e)})

    def _handle_parse_t3d(self):
        """解析 UE4 T3D 剪贴板文本 → 节点图数据"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body)
            
            t3d_text = data.get('t3d_text', '')
            if not t3d_text.strip():
                self._send_json({'error': 'T3D 文本为空'})
                return
            
            result = parse_t3d_clipboard(t3d_text)
            
            if 'error' in result:
                self._send_json({'error': result['error'], 'warnings': result.get('warnings', [])})
                return
            
            self._send_json(result)
            
        except Exception as e:
            traceback.print_exc()
            self._send_json({'error': f'T3D 解析错误: {str(e)}'})

    def _handle_generate_t3d(self):
        """从 T3D 解析结果重新生成 T3D 剪贴板文本（原样导出或带偏移）"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body)
            
            t3d_text = data.get('t3d_text', '')
            offset_x = data.get('offset_x', 0)
            offset_y = data.get('offset_y', 0)
            
            if not t3d_text.strip():
                self._send_json({'error': '原始 T3D 文本为空'})
                return
            
            # 重新解析原始文本
            parse_result = parse_t3d_to_result(t3d_text)
            if parse_result.errors:
                self._send_json({'error': '\n'.join(parse_result.errors)})
                return
            
            # 生成新的 T3D 文本
            output = generate_t3d_from_parse_result(parse_result, offset_x, offset_y)
            
            self._send_json({
                't3d_output': output,
                'node_count': len(parse_result.nodes),
            })
            
        except Exception as e:
            traceback.print_exc()
            self._send_json({'error': f'T3D 生成错误: {str(e)}'})

    def _handle_hlsl_to_t3d(self):
        """HLSL/GLSL 代码 → 材质节点 → T3D 剪贴板文本（自动检测 + 自动输入节点）"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body)
            
            code = data.get('code', '')
            start_x = data.get('start_x', 0)
            start_y = data.get('start_y', 0)
            
            if not code.strip():
                self._send_json({'error': '代码为空'})
                return
            
            # 自动检测代码类型
            detected_type = detect_code_type(code)
            hlsl_code = code
            glsl_converted = False

            # 如果是 GLSL，先转换为 HLSL
            if detected_type == 'glsl':
                try:
                    converter = ShadertoyConverter()
                    hlsl_code = converter.convert(code)
                    glsl_converted = True
                except Exception as e:
                    self._send_json({'error': f'GLSL → HLSL 转换失败: {str(e)}'})
                    return

            # HLSL → MaterialGraph
            graph = hlsl_to_material_graph(hlsl_code)
            
            # 自动创建输入节点
            try:
                graph.auto_create_inputs(hlsl_code)
            except Exception:
                pass  # 输入节点生成失败不影响主流程
            
            # MaterialGraph → T3D
            t3d_text = generate_t3d_from_material_graph(graph, start_x, start_y)
            
            # 同时生成图数据供前端显示
            graph_data = graph_to_json(graph)
            
            result = {
                't3d_output': t3d_text,
                'graph': graph_data,
                'node_count': len(graph.nodes),
                'detected_type': detected_type,
            }

            if glsl_converted:
                result['hlsl_code'] = hlsl_code

            self._send_json(result)
            
        except SyntaxError as e:
            self._send_json({'error': f'HLSL 解析错误: {str(e)}'})
        except Exception as e:
            traceback.print_exc()
            self._send_json({'error': f'转换错误: {str(e)}'})

    def _handle_convert_custom(self):
        """将 T3D 中的 Custom 节点转换为原生材质节点"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body)
            
            t3d_text = data.get('t3d_text', '')
            if not t3d_text.strip():
                self._send_json({'error': 'T3D 文本为空'})
                return
            
            result = convert_custom_nodes(t3d_text)
            self._send_json(result)
            
        except Exception as e:
            traceback.print_exc()
            self._send_json({'error': f'Custom 节点转换错误: {str(e)}'})

    def _handle_reverse_convert(self):
        """反向转换: T3D 原生节点 → Custom Node T3D"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body)
            
            t3d_text = data.get('t3d_text', '')
            if not t3d_text.strip():
                self._send_json({'error': 'T3D 文本为空'})
                return
            
            # 使用新的反向转换函数
            from reverse_converter import reverse_to_custom_node_t3d
            result = reverse_to_custom_node_t3d(t3d_text)
            self._send_json(result)
            
        except Exception as e:
            traceback.print_exc()
            self._send_json({'error': f'反向转换错误: {str(e)}'})

    def _handle_detect_type(self):
        """检测代码类型"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body)
            
            code = data.get('code', '')
            if not code.strip():
                self._send_json({'type': 'unknown'})
                return
            
            detected = detect_code_type(code)
            self._send_json({'type': detected})
            
        except Exception as e:
            self._send_json({'error': str(e)})

    def _handle_hlsl_to_custom_node(self):
        """HLSL 代码直接转换为 Custom Node T3D（不经过节点解析）"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body)
            
            code = data.get('code', '')
            if not code.strip():
                self._send_json({'error': '代码为空'})
                return
            
            # 自动检测代码类型
            detected_type = detect_code_type(code)
            hlsl_code = code
            
            # 如果是 GLSL，先转换为 HLSL
            if detected_type == 'glsl':
                try:
                    converter = ShadertoyConverter()
                    hlsl_code = converter.convert_to_hlsl(code)
                except Exception as e:
                    self._send_json({'error': f'GLSL 转换失败: {str(e)}'})
                    return
            
            # 使用 AutoInputGenerator 提取输入参数
            from auto_input_generator import AutoInputGenerator
            input_gen = AutoInputGenerator()
            input_vars = input_gen.extract_inputs(hlsl_code=hlsl_code)
            input_names = [v.name for v in input_vars]
            
            # 推断输出类型（简单启发式）
            output_type = 'CMOT_Float3'  # 默认 float3
            
            # 生成 Custom Node T3D
            from t3d_generator import generate_t3d_from_custom_hlsl
            t3d_output = generate_t3d_from_custom_hlsl(
                hlsl_code,
                input_names=input_names,
                output_type=output_type,
                description='Generated from HLSL'
            )
            
            self._send_json({
                't3d_output': t3d_output,
                'input_names': input_names,
                'output_type': output_type,
                'detected_type': detected_type,
            })
            
        except Exception as e:
            traceback.print_exc()
            self._send_json({'error': f'HLSL 转 Custom Node 错误: {str(e)}'})

    def _send_json(self, data: dict):
        """发送 JSON 响应"""
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

    def do_OPTIONS(self):
        """处理 CORS 预检"""
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()


# ═══════════════════════════════════════════════════════════
# 启动服务器
# ═══════════════════════════════════════════════════════════

def kill_port(port):
    """强制关闭占用指定端口的进程（Windows）"""
    try:
        # 使用 netstat 查找占用端口的 PID
        result = subprocess.run(
            ['netstat', '-ano'],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            # 匹配 LISTENING 状态的目标端口
            if f':{port}' in line and 'LISTENING' in line:
                parts = line.split()
                pid = int(parts[-1])
                if pid > 0:
                    print(f"  ⚠ 发现端口 {port} 被 PID {pid} 占用，正在强制关闭...")
                    try:
                        subprocess.run(
                            ['taskkill', '/F', '/PID', str(pid)],
                            capture_output=True, timeout=5
                        )
                        print(f"  ✓ 已关闭 PID {pid}")
                    except Exception as e:
                        print(f"  ✗ 关闭 PID {pid} 失败: {e}")
    except Exception as e:
        print(f"  ⚠ 检查端口占用时出错: {e}")

    # 等待端口释放
    import time
    time.sleep(0.5)


def main():
    PORT = 8080
    HOST = '0.0.0.0'

    # 启动前强制关闭占用 8080 端口的旧进程
    kill_port(PORT)

    # 清除 __pycache__ 确保加载最新代码
    pycache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '__pycache__')
    if os.path.isdir(pycache_dir):
        import shutil
        shutil.rmtree(pycache_dir, ignore_errors=True)
        print("  ✓ 已清除 __pycache__")

    server = HTTPServer((HOST, PORT), HLSLHandler)

    print(f"""
{'═' * 56}
  🎮 UE4 Material Node Converter
  交互式 Web 工具已启动！
{'═' * 56}

  🌐 打开浏览器访问: http://{HOST}:{PORT}

  功能：
    • 📋 T3D 模式：从 UE4 复制节点 → Web 可视化 → 复制回 UE4
    • 📝 HLSL 模式：输入 HLSL 代码 → 转换节点图 → 复制到 UE4
    • 零引擎改动，无需 C++ 插件，DF 版本可用

  快捷键：
    • Ctrl+Enter  解析/转换
    • 鼠标滚轮    缩放节点图
    • 鼠标拖拽    平移视图

  按 Ctrl+C 停止服务器
{'─' * 56}
""")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\n  服务器已停止。")
        server.server_close()


if __name__ == '__main__':
    main()
