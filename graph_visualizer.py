"""
============================================================
 graph_visualizer.py
 材质节点图 → HTML 可视化预览
============================================================

将 MaterialGraph 渲染为一个交互式 HTML 文件，
可以在浏览器中查看节点图布局和连线关系。

功能：
  - 自动布局（从右到左/从输出到输入的拓扑排列）
  - 节点颜色编码（不同类型不同颜色）
  - 连线（贝塞尔曲线，带箭头）
  - 悬停显示属性详情
  - 缩放和拖拽
============================================================
"""

import json
import html as html_escape
from typing import List, Dict, Set, Optional, Tuple
from node_mapper import MaterialNode, MaterialGraph


# ═══════════════════════════════════════════════════════════
# 节点颜色方案（参考 UE4 材质编辑器风格）
# ═══════════════════════════════════════════════════════════

NODE_COLORS = {
    # 常量
    'MaterialExpressionConstant':          '#4a6741',
    'MaterialExpressionConstant2Vector':   '#4a6741',
    'MaterialExpressionConstant3Vector':   '#4a6741',
    'MaterialExpressionConstant4Vector':   '#4a6741',
    # 数学运算
    'MaterialExpressionAdd':               '#3d5a80',
    'MaterialExpressionSubtract':          '#3d5a80',
    'MaterialExpressionMultiply':          '#3d5a80',
    'MaterialExpressionDivide':            '#3d5a80',
    'MaterialExpressionFmod':              '#3d5a80',
    'MaterialExpressionPower':             '#3d5a80',
    'MaterialExpressionSquareRoot':        '#3d5a80',
    'MaterialExpressionAbs':               '#3d5a80',
    'MaterialExpressionSign':              '#3d5a80',
    'MaterialExpressionFloor':             '#3d5a80',
    'MaterialExpressionCeil':              '#3d5a80',
    'MaterialExpressionRound':             '#3d5a80',
    'MaterialExpressionFrac':              '#3d5a80',
    'MaterialExpressionMin':               '#3d5a80',
    'MaterialExpressionMax':               '#3d5a80',
    'MaterialExpressionOneMinus':          '#3d5a80',
    # 插值
    'MaterialExpressionLinearInterpolate': '#6b4c8a',
    'MaterialExpressionSmoothStep':        '#6b4c8a',
    'MaterialExpressionStep':              '#6b4c8a',
    'MaterialExpressionClamp':             '#6b4c8a',
    'MaterialExpressionSaturate':          '#6b4c8a',
    # 三角函数
    'MaterialExpressionSine':              '#4a7a8a',
    'MaterialExpressionCosine':            '#4a7a8a',
    'MaterialExpressionTangent':           '#4a7a8a',
    'MaterialExpressionArcsine':           '#4a7a8a',
    'MaterialExpressionArccosine':         '#4a7a8a',
    'MaterialExpressionArctangent':        '#4a7a8a',
    'MaterialExpressionArctangent2':       '#4a7a8a',
    # 向量操作
    'MaterialExpressionDotProduct':        '#7a5a3d',
    'MaterialExpressionCrossProduct':      '#7a5a3d',
    'MaterialExpressionNormalize':         '#7a5a3d',
    'MaterialExpressionComponentMask':     '#7a5a3d',
    'MaterialExpressionAppendVector':      '#7a5a3d',
    'MaterialExpressionVectorLength':      '#7a5a3d',
    'MaterialExpressionDistance':          '#7a5a3d',
    # 纹理
    'MaterialExpressionTextureSample':     '#8a3d3d',
    'MaterialExpressionTextureObjectParameter': '#8a5a3d',
    # 输入参数
    'MaterialExpressionFunctionInput':     '#8a7a3d',
    # 条件
    'MaterialExpressionIf':                '#8a4a6a',
    # Custom
    'MaterialExpressionCustom':            '#5a5a5a',
    # MaterialFunction（引擎内置函数如 SmoothStep）
    'MaterialExpressionMaterialFunctionCall': '#4a7a5a',
    # 其它
    'MaterialExpressionExponential':       '#3d5a80',
    'MaterialExpressionExponential2':      '#3d5a80',
    'MaterialExpressionLogarithm10':       '#3d5a80',
    'MaterialExpressionLogarithm2':        '#3d5a80',
    'MaterialExpressionDDX':               '#3d5a80',
    'MaterialExpressionDDY':               '#3d5a80',
    'MaterialExpressionReflectionVector':  '#7a5a3d',
    # 参数节点
    'MaterialExpressionScalarParameter':   '#8a7a3d',
    'MaterialExpressionVectorParameter':   '#8a7a3d',
    # UE4 引擎内置变量节点（深蓝绿色，表示"来自引擎"）
    'MaterialExpressionCameraPositionWS':  '#2d6a4f',
    'MaterialExpressionWorldPosition':     '#2d6a4f',
    'MaterialExpressionPixelNormalWS':     '#2d6a4f',
    'MaterialExpressionVertexNormalWS':    '#2d6a4f',
    'MaterialExpressionCameraVectorWS':    '#2d6a4f',
    'MaterialExpressionTextureCoordinate': '#2d6a4f',
    'MaterialExpressionTime':              '#2d6a4f',
    'MaterialExpressionScreenPosition':    '#2d6a4f',
    'MaterialExpressionVertexColor':       '#2d6a4f',
    'MaterialExpressionReflectionVectorWS':'#2d6a4f',
    'MaterialExpressionObjectPositionWS':  '#2d6a4f',
    'MaterialExpressionActorPositionWS':   '#2d6a4f',
    'MaterialExpressionPixelDepth':        '#2d6a4f',
    'MaterialExpressionSceneDepth':        '#2d6a4f',
}

DEFAULT_COLOR = '#555555'


# ═══════════════════════════════════════════════════════════
# 布局算法（Sugiyama 分层布局，多遍 Barycenter + 碰撞检测）
# ═══════════════════════════════════════════════════════════

def _node_height(node: MaterialNode, target: str = 'html') -> int:
    """根据节点的 pin 数量动态计算高度"""
    if target == 'ue4':
        HEADER_H = 50
        PIN_ROW_H = 30
        CLASS_H = 30
    else:
        HEADER_H = 30
        PIN_ROW_H = 20
        CLASS_H = 18
    num_pins = max(len(node.inputs) if node.inputs else len(node.input_names), 1)
    return HEADER_H + num_pins * PIN_ROW_H + CLASS_H


def _count_crossings(layers, level_a, level_b, dependents, dependencies, y_order):
    """
    计算相邻两层 (level_a, level_b) 之间的连线交叉数。
    level_a 在左（上游），level_b 在右（下游）。
    交叉 = 两条边 (u1→v1) 和 (u2→v2) 中 u1<u2 但 v1>v2（或反之）的对数。
    """
    if level_a not in layers or level_b not in layers:
        return 0
    
    # 收集所有跨层连线：(u_order, v_order)
    edges = []
    nids_a = layers[level_a]
    for idx_u, uid in enumerate(nids_a):
        for consumer_id in dependents.get(uid, set()):
            if consumer_id in y_order:
                # consumer 在 level_b 层
                v_ord = y_order[consumer_id]
                edges.append((idx_u, v_ord))
    
    # 暴力计算交叉数（对于小规模图足够快）
    crossings = 0
    for i in range(len(edges)):
        for j in range(i + 1, len(edges)):
            u1, v1 = edges[i]
            u2, v2 = edges[j]
            if (u1 - u2) * (v1 - v2) < 0:
                crossings += 1
    return crossings


def _total_crossings(layers, max_level, dependents, dependencies, y_order):
    """计算整个图的总连线交叉数"""
    total = 0
    for level in range(1, max_level + 1):
        total += _count_crossings(layers, level, level - 1, dependents, dependencies, y_order)
    return total


def compute_layout(graph: MaterialGraph, node_width: int = 220, node_height: int = 90,
                   h_gap: int = 100, v_gap: int = 40,
                   target: str = 'html') -> Dict[int, Tuple[int, int]]:
    """
    计算节点位置：Sugiyama 分层布局算法。
    
    核心改进（相比旧版）:
    1. 6 遍 Barycenter 交替扫描（旧版仅 2 遍），大幅减少连线交叉
    2. 每遍排序后统计交叉数，只在改善时采纳（防止越排越差）
    3. 紧凑间距参数（UE4 模式下节点宽 250、水平间距 80、垂直间距 25）
    4. 多扇出居中后进行碰撞检测 + 推开，防止节点重叠
    5. 单链路 Y 对齐：只有一个下游的节点对齐到下游 Y（横平竖直连线）

    参数:
        target: 'html' — 用于 HTML 可视化（紧凑布局）
                'ue4'  — 用于 UE4 材质编辑器（紧凑布局，适配编辑器节点大小）
    返回: {node_id: (x, y)}
    """
    # ── DEBUG: 版本标记，用于确认代码是否被正确加载 ──
    _VERSION_TAG = "LAYOUT_V2_20260318"
    try:
        import unreal as _ue
        _ue.log(f"[HLSL2Mat][compute_layout] === VERSION: {_VERSION_TAG} === target={target}, nodes={len(graph.nodes)}")
    except ImportError:
        print(f"[compute_layout] VERSION: {_VERSION_TAG}, target={target}, nodes={len(graph.nodes)}")
    
    if not graph.nodes:
        return {}

    # ── 紧凑间距参数 ──
    if target == 'ue4':
        node_width = 250
        node_height = 160
        h_gap = 80       # 旧值 150 → 80，层间更紧凑
        v_gap = 25        # 旧值 60 → 25，同层节点更紧凑
    
    MIN_V_GAP = v_gap     # 碰撞检测用的最小垂直间距

    # 构建依赖图
    node_by_id: Dict[int, MaterialNode] = {n.id: n for n in graph.nodes}
    dependents: Dict[int, Set[int]] = {n.id: set() for n in graph.nodes}  # 谁依赖这个节点（下游）
    dependencies: Dict[int, Set[int]] = {n.id: set() for n in graph.nodes}  # 这个节点依赖谁（上游）

    for node in graph.nodes:
        for input_name, input_node in node.inputs.items():
            if input_node and input_node.id in node_by_id:
                dependencies[node.id].add(input_node.id)
                dependents[input_node.id].add(node.id)

    # ── 确定层级（BFS，每个节点取最远层级） ──
    levels: Dict[int, int] = {}
    if graph.output_node:
        queue = [(graph.output_node.id, 0)]
        visited = {graph.output_node.id}
        while queue:
            nid, level = queue.pop(0)
            levels[nid] = max(levels.get(nid, 0), level)
            for dep_id in dependencies.get(nid, []):
                if dep_id not in visited or levels.get(dep_id, 0) < level + 1:
                    visited.add(dep_id)
                    queue.append((dep_id, level + 1))
                    levels[dep_id] = max(levels.get(dep_id, 0), level + 1)

    # 没有被遍历到的节点放在最左边
    max_level = max(levels.values()) if levels else 0
    for node in graph.nodes:
        if node.id not in levels:
            max_level += 1
            levels[node.id] = max_level
    max_level = max(levels.values()) if levels else 0

    # ── 按层级分组 ──
    layers: Dict[int, List[int]] = {}
    for nid, level in levels.items():
        layers.setdefault(level, []).append(nid)

    # ══════════════════════════════════════════════════════════
    # Barycenter 排序 — 6 遍交替扫描，带交叉数守卫
    # ══════════════════════════════════════════════════════════
    
    # 初始排序（按 ID 稳定性）
    for level in sorted(layers.keys()):
        layers[level].sort()

    # 初始 y_order
    y_order: Dict[int, float] = {}
    for level in sorted(layers.keys()):
        for idx, nid in enumerate(layers[level]):
            y_order[nid] = idx

    BARYCENTER_ITERATIONS = 6  # 旧版仅 2 遍 → 提升到 6 遍

    best_layers = {l: list(nids) for l, nids in layers.items()}
    best_y_order = dict(y_order)
    best_crossings = _total_crossings(layers, max_level, dependents, dependencies, y_order)

    for iteration in range(BARYCENTER_ITERATIONS):
        if iteration % 2 == 0:
            # ── 正向扫描：从右(level 0)到左(max_level) ──
            # 用下游消费者的 y_order 来排当前层
            for level in range(0, max_level + 1):
                if level not in layers or len(layers[level]) <= 1:
                    continue
                nids = layers[level]
                barycenters = {}
                for nid in nids:
                    neighbor_ys = []
                    # 查看下游（level 更小的层）
                    for consumer_id in dependents.get(nid, set()):
                        if consumer_id in y_order:
                            neighbor_ys.append(y_order[consumer_id])
                    # 也查看上游（level 更大的层）作为辅助参考
                    for dep_id in dependencies.get(nid, set()):
                        if dep_id in y_order:
                            neighbor_ys.append(y_order[dep_id])
                    if neighbor_ys:
                        barycenters[nid] = sum(neighbor_ys) / len(neighbor_ys)
                    else:
                        barycenters[nid] = y_order.get(nid, float(nid))
                
                nids.sort(key=lambda n: barycenters.get(n, float(n)))
                layers[level] = nids
                for idx, nid in enumerate(nids):
                    y_order[nid] = idx
        else:
            # ── 反向扫描：从左(max_level)到右(level 0) ──
            # 用上游提供者的 y_order 来排当前层
            for level in range(max_level, -1, -1):
                if level not in layers or len(layers[level]) <= 1:
                    continue
                nids = layers[level]
                barycenters = {}
                for nid in nids:
                    neighbor_ys = []
                    for dep_id in dependencies.get(nid, set()):
                        if dep_id in y_order:
                            neighbor_ys.append(y_order[dep_id])
                    for consumer_id in dependents.get(nid, set()):
                        if consumer_id in y_order:
                            neighbor_ys.append(y_order[consumer_id])
                    if neighbor_ys:
                        barycenters[nid] = sum(neighbor_ys) / len(neighbor_ys)
                    else:
                        barycenters[nid] = y_order.get(nid, float(nid))
                
                nids.sort(key=lambda n: barycenters.get(n, float(n)))
                layers[level] = nids
                for idx, nid in enumerate(nids):
                    y_order[nid] = idx

        # ── 交叉数守卫：只在改善时采纳 ──
        cur_crossings = _total_crossings(layers, max_level, dependents, dependencies, y_order)
        if cur_crossings < best_crossings:
            best_crossings = cur_crossings
            best_layers = {l: list(nids) for l, nids in layers.items()}
            best_y_order = dict(y_order)
        else:
            # 回滚到最佳状态，继续尝试
            layers = {l: list(nids) for l, nids in best_layers.items()}
            y_order = dict(best_y_order)
        
        # 如果已经 0 交叉，提前终止
        if best_crossings == 0:
            break

    # 使用最佳排序结果
    layers = best_layers
    y_order = best_y_order

    # ══════════════════════════════════════════════════════════
    # 计算实际像素坐标
    # ══════════════════════════════════════════════════════════
    positions: Dict[int, Tuple[int, int]] = {}

    # 先计算每个节点的实际高度缓存
    node_heights: Dict[int, int] = {}
    for nid in node_by_id:
        node_heights[nid] = _node_height(node_by_id[nid], target)

    for level, nids in layers.items():
        x = (max_level - level) * (node_width + h_gap) + 50
        current_y = 50
        for nid in nids:
            positions[nid] = (x, current_y)
            nh = node_heights.get(nid, node_height)
            current_y += nh + MIN_V_GAP

    # ══════════════════════════════════════════════════════════
    # 单链路 Y 对齐（横平竖直连线优化）
    # ══════════════════════════════════════════════════════════
    # 从右到左：如果一个节点只有 1 个下游消费者，且该消费者也只有 1 个上游依赖（即纯链条），
    # 则将当前节点的 Y 对齐到下游节点的 Y，使连线水平。
    for level in range(1, max_level + 1):
        if level not in layers:
            continue
        for nid in layers[level]:
            consumers = [cid for cid in dependents.get(nid, set()) if cid in positions]
            if len(consumers) == 1:
                consumer_id = consumers[0]
                # 检查下游节点是否只依赖当前节点（纯链条）
                consumer_deps = [did for did in dependencies.get(consumer_id, set()) if did in positions]
                if len(consumer_deps) == 1:
                    old_x, _old_y = positions[nid]
                    _cx, consumer_y = positions[consumer_id]
                    positions[nid] = (old_x, consumer_y)

    # ══════════════════════════════════════════════════════════
    # 多扇出节点居中优化
    # ══════════════════════════════════════════════════════════
    # 如果一个节点被多个下游引用，将其 Y 调整为所有消费者 Y 的中心
    for level in range(1, max_level + 1):
        if level not in layers:
            continue
        for nid in layers[level]:
            consumers = [cid for cid in dependents.get(nid, set()) if cid in positions]
            if len(consumers) >= 2:
                consumer_ys = [positions[cid][1] for cid in consumers]
                center_y = (min(consumer_ys) + max(consumer_ys)) / 2
                old_x, _old_y = positions[nid]
                positions[nid] = (old_x, int(center_y))

    # ══════════════════════════════════════════════════════════
    # 碰撞检测 + 推开（防止节点重叠）
    # ══════════════════════════════════════════════════════════
    # 多扇出居中和单链路对齐可能导致同层节点重叠，这里进行修正。
    # 按层逐层处理，同层内按 Y 排序，确保相邻节点不重叠。
    for level in sorted(layers.keys()):
        nids = layers[level]
        if len(nids) <= 1:
            continue
        
        # 按当前 Y 坐标排序
        nids_sorted = sorted(nids, key=lambda n: positions[n][1])
        
        # 从上到下检查碰撞，如果重叠则将下方节点推开
        for i in range(1, len(nids_sorted)):
            prev_nid = nids_sorted[i - 1]
            cur_nid = nids_sorted[i]
            prev_x, prev_y = positions[prev_nid]
            cur_x, cur_y = positions[cur_nid]
            prev_h = node_heights.get(prev_nid, node_height)
            
            # 最小间隔 = 上一个节点的底部 + MIN_V_GAP
            min_y = prev_y + prev_h + MIN_V_GAP
            if cur_y < min_y:
                positions[cur_nid] = (cur_x, min_y)

    return positions


# ═══════════════════════════════════════════════════════════
# HTML 生成
# ═══════════════════════════════════════════════════════════

def generate_html(graph: MaterialGraph, title: str = "HLSL → UE4 Material Node Graph") -> str:
    """生成交互式 HTML 可视化页面"""

    positions = compute_layout(graph)
    node_by_id = {n.id: n for n in graph.nodes}

    # 计算画布大小
    if positions:
        max_x = max(x for x, y in positions.values()) + 300
        max_y = max(y for x, y in positions.values()) + 200
    else:
        max_x, max_y = 800, 600

    # 构建节点数据
    nodes_json = []
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

        nodes_json.append({
            'id': node.id,
            'display_name': node.display_name,
            'ue_class': node.ue_class,
            'x': x, 'y': y,
            'color': color,
            'inputs': input_list,
            'input_names': list(node.inputs.keys()) if node.inputs else node.input_names,
            'properties': props_str,
            'is_output': (graph.output_node and node.id == graph.output_node.id),
            'is_input': node.ue_class == 'MaterialExpressionFunctionInput',
            'source_line': node.source_line,
        })

    # 连线数据改为只传逻辑关系，由 JS 在 DOM 渲染后动态计算精确坐标
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

    warnings_html = ''
    if graph.warnings:
        warnings_html = '<div class="warnings"><h3>⚠️ 转换警告</h3><ul>'
        for w in graph.warnings:
            warnings_html += f'<li>{html_escape.escape(w)}</li>'
        warnings_html += '</ul></div>'

    custom_html = ''
    if graph.custom_expressions:
        custom_html = '<div class="custom-code"><h3>🔧 需保留在 CustomExpression 中的代码</h3><pre>'
        for c in graph.custom_expressions:
            custom_html += html_escape.escape(c) + '\n'
        custom_html += '</pre></div>'

    stats_html = f"""
    <div class="stats">
        <span>📊 节点总数: <b>{len(graph.nodes)}</b></span>
        <span>📥 输入参数: <b>{len(graph.input_nodes)}</b></span>
        <span>🔗 连线数: <b>{len(connections)}</b></span>
    </div>
    """

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{html_escape.escape(title)}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    background: #1a1a2e;
    color: #e0e0e0;
    font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif;
    overflow: hidden;
}}
.toolbar {{
    position: fixed; top: 0; left: 0; right: 0;
    background: #16213e; padding: 10px 20px;
    display: flex; align-items: center; gap: 20px;
    z-index: 100; box-shadow: 0 2px 10px rgba(0,0,0,0.5);
    border-bottom: 1px solid #0f3460;
}}
.toolbar h1 {{
    font-size: 16px; color: #e94560;
    white-space: nowrap;
}}
.stats {{
    display: flex; gap: 16px; font-size: 13px;
}}
.stats span {{ color: #a0a0c0; }}
.stats b {{ color: #e0e0ff; }}
.warnings {{
    position: fixed; bottom: 10px; left: 10px;
    background: #2a1a00; border: 1px solid #e94560;
    border-radius: 8px; padding: 12px 16px;
    max-width: 400px; max-height: 200px; overflow-y: auto;
    z-index: 100; font-size: 12px;
}}
.warnings h3 {{ font-size: 13px; color: #e94560; margin-bottom: 6px; }}
.warnings li {{ margin-left: 16px; margin-bottom: 4px; color: #ffaa66; }}
.custom-code {{
    position: fixed; bottom: 10px; right: 10px;
    background: #1a1a2e; border: 1px solid #555;
    border-radius: 8px; padding: 12px 16px;
    max-width: 400px; max-height: 200px; overflow-y: auto;
    z-index: 100; font-size: 12px;
}}
.custom-code h3 {{ font-size: 13px; color: #66aaff; margin-bottom: 6px; }}
.custom-code pre {{ color: #aaa; font-size: 11px; white-space: pre-wrap; }}
#canvas-container {{
    position: fixed; top: 45px; left: 0; right: 0; bottom: 0;
    overflow: hidden; cursor: grab;
}}
#canvas-container:active {{ cursor: grabbing; }}
#canvas {{
    position: absolute; transform-origin: 0 0;
}}
svg {{
    position: absolute; top: 0; left: 0;
    pointer-events: none;
}}
svg path {{
    stroke-width: 2; fill: none;
    opacity: 0.6;
}}
svg path:hover {{ opacity: 1; stroke-width: 3; }}
.node {{
    position: absolute;
    width: 220px;
    border-radius: 6px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.4);
    overflow: hidden;
    cursor: pointer;
    transition: box-shadow 0.2s;
}}
.node:hover {{
    box-shadow: 0 4px 16px rgba(233,69,96,0.4);
    z-index: 10;
}}
.node-header {{
    padding: 6px 10px;
    font-size: 12px;
    font-weight: bold;
    color: #fff;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    text-align: center;
}}
.node-body {{
    background: #2a2a3e;
    padding: 6px 0;
    font-size: 11px;
    color: #ccc;
    border-top: 1px solid rgba(255,255,255,0.1);
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
    width: 8px; height: 8px;
    border-radius: 50%;
    background: #555;
    flex-shrink: 0;
    border: 1px solid #888;
}}
.pin-dot.connected {{ background: #4ae; border-color: #4ae; }}
.pin-dot.output-dot {{
    background: #aaa;
    border-color: #aaa;
}}
.pin-name {{
    font-size: 10px;
    color: #bbb;
}}
.node.output-node {{
    border: 2px solid #e94560;
}}
.node.input-node {{
    border: 2px solid #e9a945;
}}
.node-class {{
    font-size: 9px;
    color: #555;
    padding: 2px 10px 0;
    word-break: break-all;
}}
.zoom-info {{
    position: fixed; bottom: 10px; left: 50%;
    transform: translateX(-50%);
    background: #16213e; padding: 4px 12px;
    border-radius: 12px; font-size: 11px; color: #888;
    z-index: 100;
}}
.legend {{
    position: fixed; top: 55px; right: 10px;
    background: #16213e; border: 1px solid #333;
    border-radius: 8px; padding: 10px;
    font-size: 11px; z-index: 100;
}}
.legend-item {{
    display: flex; align-items: center; gap: 6px;
    margin: 3px 0;
}}
.legend-color {{
    width: 14px; height: 14px; border-radius: 3px;
}}
</style>
</head>
<body>
<div class="toolbar">
    <h1>🎮 HLSL → UE4 Material Nodes</h1>
    {stats_html}
</div>

<div class="legend">
    <div class="legend-item"><div class="legend-color" style="background:#4a6741"></div>常量</div>
    <div class="legend-item"><div class="legend-color" style="background:#3d5a80"></div>数学运算</div>
    <div class="legend-item"><div class="legend-color" style="background:#6b4c8a"></div>插值/钳制</div>
    <div class="legend-item"><div class="legend-color" style="background:#4a7a8a"></div>三角函数</div>
    <div class="legend-item"><div class="legend-color" style="background:#7a5a3d"></div>向量操作</div>
    <div class="legend-item"><div class="legend-color" style="background:#8a3d3d"></div>纹理采样</div>
    <div class="legend-item"><div class="legend-color" style="background:#8a5a3d"></div>纹理参数</div>
    <div class="legend-item"><div class="legend-color" style="background:#8a7a3d"></div>输入参数</div>
    <div class="legend-item"><div class="legend-color" style="background:#8a4a6a"></div>条件判断</div>
    <div class="legend-item"><div class="legend-color" style="background:#4a7a5a"></div>MaterialFunction</div>
    <div class="legend-item"><div class="legend-color" style="background:#5a5a5a"></div>Custom</div>
</div>

{warnings_html}
{custom_html}

<div id="canvas-container">
    <div id="canvas">
        <svg id="connections" width="{max_x + 100}" height="{max_y + 200}"></svg>
    </div>
</div>

<div class="zoom-info" id="zoom-info">100%</div>

<script>
const nodesData = {json.dumps(nodes_json, ensure_ascii=False)};
const connectionsData = {json.dumps(connections, ensure_ascii=False)};

const canvas = document.getElementById('canvas');
const svg = document.getElementById('connections');
const container = document.getElementById('canvas-container');
const zoomInfo = document.getElementById('zoom-info');

let scale = 1;
let panX = 0, panY = 0;
let isDragging = false;
let dragStartX, dragStartY;

// ── 节点元素映射 ──
const nodeElements = {{}};  // id → DOM element
const inputDots = {{}};     // "nodeId-pinIndex" → DOM element (输入 pin dot)
const outputDots = {{}};    // nodeId → DOM element (输出 pin dot)

// 创建节点 DOM
nodesData.forEach(n => {{
    const div = document.createElement('div');
    div.className = 'node' + (n.is_output ? ' output-node' : '') + (n.is_input ? ' input-node' : '');
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

    const headerIcon = n.is_output ? '📤 ' : (n.is_input ? '📥 ' : '');

    div.innerHTML = `
        <div class="node-header" style="background:${{n.color}}">${{headerIcon}}${{n.display_name}}</div>
        <div class="node-body">
            ${{pinsHtml}}
            <div class="node-class">${{n.ue_class}}</div>
        </div>
    `;

    if (n.properties) {{
        div.title = n.properties;
    }}

    canvas.appendChild(div);
    nodeElements[n.id] = div;
}});

// 收集 DOM 中的 pin dot 元素
document.querySelectorAll('[data-input-dot]').forEach(el => {{
    inputDots[el.getAttribute('data-input-dot')] = el;
}});
document.querySelectorAll('[data-output-dot]').forEach(el => {{
    outputDots[el.getAttribute('data-output-dot')] = el;
}});

// ── 精确获取 pin dot 中心坐标（相对于 canvas）──
function getDotCenter(dotEl) {{
    // dot 相对于它所在的 node 的位置
    const nodeEl = dotEl.closest('.node');
    const nodeX = parseFloat(nodeEl.style.left);
    const nodeY = parseFloat(nodeEl.style.top);

    // dot 在 node 内的偏移
    const dotRect = dotEl.getBoundingClientRect();
    const nodeRect = nodeEl.getBoundingClientRect();
    const nodeScale = nodeRect.width / nodeEl.offsetWidth;  // 处理 transform scale
    const dx = (dotRect.left - nodeRect.left) / nodeScale + dotRect.width / nodeScale / 2;
    const dy = (dotRect.top - nodeRect.top) / nodeScale + dotRect.height / nodeScale / 2;

    return {{ x: nodeX + dx, y: nodeY + dy }};
}}

// ── 绘制连线（DOM 渲染完成后从实际 pin dot 位置获取坐标）──
setTimeout(() => {{
    connectionsData.forEach(c => {{
        const outDot = outputDots[c.from_id];
        const inDot = inputDots[c.to_id + '-' + c.to_pin_index];
        if (!outDot || !inDot) return;

        const srcNode = nodesData.find(n => n.id === c.from_id);
        const color = srcNode ? srcNode.color : '#666';
        const from = getDotCenter(outDot);
        const to = getDotCenter(inDot);

        const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        const dx = Math.abs(from.x - to.x) * 0.5;
        path.setAttribute('d', `M${{from.x}},${{from.y}} C${{from.x + dx}},${{from.y}} ${{to.x - dx}},${{to.y}} ${{to.x}},${{to.y}}`);
        path.setAttribute('stroke', color);
        path.style.pointerEvents = 'stroke';
        svg.appendChild(path);
    }});
}}, 50);  // 等 DOM 布局完成

// 平移和缩放
function updateTransform() {{
    canvas.style.transform = `translate(${{panX}}px, ${{panY}}px) scale(${{scale}})`;
    zoomInfo.textContent = Math.round(scale * 100) + '%';
}}

container.addEventListener('mousedown', e => {{
    if (e.target === container || e.target === canvas || e.target === svg) {{
        isDragging = true;
        dragStartX = e.clientX - panX;
        dragStartY = e.clientY - panY;
    }}
}});

container.addEventListener('mousemove', e => {{
    if (isDragging) {{
        panX = e.clientX - dragStartX;
        panY = e.clientY - dragStartY;
        updateTransform();
    }}
}});

container.addEventListener('mouseup', () => isDragging = false);
container.addEventListener('mouseleave', () => isDragging = false);

container.addEventListener('wheel', e => {{
    e.preventDefault();
    const delta = e.deltaY > 0 ? 0.9 : 1.1;
    const newScale = Math.max(0.1, Math.min(3, scale * delta));

    const rect = container.getBoundingClientRect();
    const mouseX = e.clientX - rect.left;
    const mouseY = e.clientY - rect.top;

    panX = mouseX - (mouseX - panX) * (newScale / scale);
    panY = mouseY - (mouseY - panY) * (newScale / scale);
    scale = newScale;
    updateTransform();
}});

// 初始视图
updateTransform();
</script>
</body>
</html>"""

    return html


def save_html(graph: MaterialGraph, output_path: str, title: str = "HLSL → UE4 Material Nodes"):
    """保存 HTML 可视化文件"""
    html_content = generate_html(graph, title)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html_content)
    return output_path
