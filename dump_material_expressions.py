# -*- coding: utf-8 -*-
# Dump MaterialExpression classes and their input ports via connection probing
# Run in UE4 Editor Python console:
#   exec(open(r'<path-to-hlsl2material>/dump_material_expressions.py').read())

import unreal
import json

def dump_material_expressions():
    results = {}

    asset_tools = unreal.AssetToolsHelpers.get_asset_tools()
    mat_factory = unreal.MaterialFactoryNew()

    temp_path = '/Game/_Temp_MatExprDump2'
    temp_mat = asset_tools.create_asset(
        'TempMat2', temp_path, unreal.Material, mat_factory
    )

    if not temp_mat:
        unreal.log_error('Cannot create temp material!')
        return

    mel = unreal.MaterialEditingLibrary

    known_classes = [
        'MaterialExpressionAdd',
        'MaterialExpressionSubtract',
        'MaterialExpressionMultiply',
        'MaterialExpressionDivide',
        'MaterialExpressionFmod',
        'MaterialExpressionPower',
        'MaterialExpressionSquareRoot',
        'MaterialExpressionAbs',
        'MaterialExpressionSign',
        'MaterialExpressionFloor',
        'MaterialExpressionCeil',
        'MaterialExpressionRound',
        'MaterialExpressionFrac',
        'MaterialExpressionMin',
        'MaterialExpressionMax',
        'MaterialExpressionOneMinus',
        'MaterialExpressionNegate',
        'MaterialExpressionTruncate',
        'MaterialExpressionLinearInterpolate',
        'MaterialExpressionSmoothStep',
        'MaterialExpressionStep',
        'MaterialExpressionClamp',
        'MaterialExpressionSaturate',
        'MaterialExpressionSine',
        'MaterialExpressionCosine',
        'MaterialExpressionTangent',
        'MaterialExpressionArcsine',
        'MaterialExpressionArccosine',
        'MaterialExpressionArctangent',
        'MaterialExpressionArctangent2',
        'MaterialExpressionDotProduct',
        'MaterialExpressionCrossProduct',
        'MaterialExpressionNormalize',
        'MaterialExpressionComponentMask',
        'MaterialExpressionAppendVector',
        'MaterialExpressionVectorLength',
        'MaterialExpressionDistance',
        'MaterialExpressionTransform',
        'MaterialExpressionTransformPosition',
        'MaterialExpressionTextureSample',
        'MaterialExpressionTextureObject',
        'MaterialExpressionTextureSampleParameter2D',
        'MaterialExpressionTextureCoordinate',
        'MaterialExpressionScalarParameter',
        'MaterialExpressionVectorParameter',
        'MaterialExpressionStaticBoolParameter',
        'MaterialExpressionStaticSwitchParameter',
        'MaterialExpressionStaticComponentMaskParameter',
        'MaterialExpressionConstant',
        'MaterialExpressionConstant2Vector',
        'MaterialExpressionConstant3Vector',
        'MaterialExpressionConstant4Vector',
        'MaterialExpressionConstantBiasScale',
        'MaterialExpressionIf',
        'MaterialExpressionStaticSwitch',
        'MaterialExpressionWorldPosition',
        'MaterialExpressionCameraPositionWS',
        'MaterialExpressionCameraVectorWS',
        'MaterialExpressionPixelNormalWS',
        'MaterialExpressionVertexNormalWS',
        'MaterialExpressionReflectionVectorWS',
        'MaterialExpressionPixelDepth',
        'MaterialExpressionSceneDepth',
        'MaterialExpressionScreenPosition',
        'MaterialExpressionViewSize',
        'MaterialExpressionObjectPositionWS',
        'MaterialExpressionActorPositionWS',
        'MaterialExpressionObjectRadius',
        'MaterialExpressionObjectBounds',
        'MaterialExpressionVertexColor',
        'MaterialExpressionTime',
        'MaterialExpressionTwoSidedSign',
        'MaterialExpressionVertexTangentWS',
        'MaterialExpressionLightVector',
        'MaterialExpressionPreSkinnedPosition',
        'MaterialExpressionPreSkinnedNormal',
        'MaterialExpressionExponential',
        'MaterialExpressionExponential2',
        'MaterialExpressionLogarithm2',
        'MaterialExpressionLogarithm10',
        'MaterialExpressionDDX',
        'MaterialExpressionDDY',
        'MaterialExpressionFresnel',
        'MaterialExpressionDesaturation',
        'MaterialExpressionBlackBody',
        'MaterialExpressionNoise',
        'MaterialExpressionDepthFade',
        'MaterialExpressionSceneColor',
        'MaterialExpressionSceneTexture',
        'MaterialExpressionParticleColor',
        'MaterialExpressionParticlePositionWS',
        'MaterialExpressionParticleRadius',
        'MaterialExpressionParticleRelativeTime',
        'MaterialExpressionParticleDirection',
        'MaterialExpressionParticleSpeed',
        'MaterialExpressionParticleSize',
        'MaterialExpressionDynamicParameter',
        'MaterialExpressionSphereMask',
        'MaterialExpressionAntialiasedTextureMask',
        'MaterialExpressionDistanceFieldGradient',
        'MaterialExpressionDistanceToNearestSurface',
        'MaterialExpressionPanner',
        'MaterialExpressionRotator',
        'MaterialExpressionFunctionInput',
        'MaterialExpressionFunctionOutput',
        'MaterialExpressionCustom',
        'MaterialExpressionChannelMaskParameter',
        'MaterialExpressionFeatureLevelSwitch',
        'MaterialExpressionQualitySwitch',
        'MaterialExpressionShadingPathSwitch',
        'MaterialExpressionMakeMaterialAttributes',
        'MaterialExpressionBreakMaterialAttributes',
        'MaterialExpressionBumpOffset',
        'MaterialExpressionPerInstanceFadeAmount',
        'MaterialExpressionPerInstanceRandom',
        'MaterialExpressionCollectionParameter',
        'MaterialExpressionEyeAdaptation',
        'MaterialExpressionAtmosphericFogColor',
        'MaterialExpressionGIReplace',
        'MaterialExpressionLightmassReplace',
    ]

    # All possible input port names to probe
    candidate_inputs = [
        # Common math ops
        'A', 'B', 'C', 'D', 'E',
        # Lerp
        'Alpha',
        # General
        'Input', 'Input0', 'Input1', 'Input2', 'Input3',
        # Texture
        'Coordinates', 'UVs', 'TextureObject', 'Tex', 'MipValue', 'MipValueMode',
        'AutomaticViewMipBias', 'CoordinatesDX', 'CoordinatesDY',
        # If node
        'AGreaterThanB', 'AEqualsB', 'ALessThanB',
        # Clamp
        'Min', 'Max',
        # SmoothStep / Step
        'Value',
        # Fresnel
        'ExponentIn', 'BaseReflectFractionIn', 'Power', 'Normal', 'CameraVector',
        # Desaturation
        'Fraction',
        # DepthFade
        'InOpacity', 'FadeDistance',
        # Noise
        'Position', 'FilterWidth',
        # SphereMask
        'Radius', 'Hardness',
        # Panner
        'Coordinate', 'Time', 'Speed',
        # Rotator
        'Center', 'RotationAngle',
        # BumpOffset
        'Height', 'HeightRatioInput',
        # SceneColor / SceneDepth
        'OffsetFraction', 'ConstInput',
        # Transform
        'TransformSource', 'TransformType',
        # MakeMaterialAttributes
        'BaseColor', 'Metallic', 'Specular', 'Roughness',
        'EmissiveColor', 'Opacity', 'OpacityMask',
        'WorldPositionOffset', 'WorldDisplacement',
        'TessellationMultiplier', 'SubsurfaceColor',
        'ClearCoat', 'ClearCoatRoughness', 'AmbientOcclusion',
        'Refraction', 'CustomizedUVs0', 'CustomizedUVs1',
        'CustomizedUVs2', 'CustomizedUVs3', 'PixelDepthOffset',
        # ConstantBiasScale
        'Bias', 'Scale',
        # ComponentMask
        'R', 'G',
        # GIReplace / LightmassReplace
        'Default', 'StaticIndirect', 'DynamicIndirect',
        'Realtime', 'Lightmass',
        # StaticSwitch
        'True', 'False',
        # FunctionInput / Output
        'Preview',
        # FeatureLevelSwitch / QualitySwitch / ShadingPathSwitch
        'ES2', 'ES3_1', 'SM4', 'SM5',
        'Low', 'High', 'Medium',
        # BreakMaterialAttributes
        'MaterialAttributes',
        # DynamicParameter
        'DefaultValue',
        # Custom expression
        'Code',
        # Extra candidates
        'X', 'Y', 'Z', 'W',
        'Color', 'Texture', 'Mask',
        'VectorInput', 'ScalarInput',
        'Distance', 'Exponent',
        'Ratio', 'Source', 'Target',
        'InA', 'InB',
        'Base', 'Blend',
        'ConstA', 'ConstB',
        'WorldPosition',
    ]

    # Create a single source expression for probing connections
    source_expr = mel.create_material_expression(
        temp_mat, unreal.MaterialExpressionConstant, 0, 0
    )
    if not source_expr:
        unreal.log_error('Cannot create source expression for probing!')
        return

    unreal.log('=' * 60)
    unreal.log('Start probing MaterialExpression input ports...')
    unreal.log('Total classes to probe: %d' % len(known_classes))
    unreal.log('Candidate input names: %d' % len(candidate_inputs))
    unreal.log('=' * 60)

    success_count = 0
    fail_count = 0

    for class_name in known_classes:
        try:
            ue_class = getattr(unreal, class_name, None)
            if ue_class is None:
                fail_count += 1
                unreal.log('  [SKIP] %s: class not found' % class_name)
                continue

            target_expr = mel.create_material_expression(
                temp_mat, ue_class, 0, 0
            )
            if target_expr is None:
                fail_count += 1
                unreal.log('  [SKIP] %s: create failed' % class_name)
                continue

            node_info = {
                'class_name': class_name,
                'inputs': [],
                'properties': {},
            }

            # Method 1: Probe connections to discover input ports
            found_inputs = []
            for inp_name in candidate_inputs:
                try:
                    ok = mel.connect_material_expressions(
                        source_expr, '', target_expr, inp_name
                    )
                    if ok:
                        found_inputs.append(inp_name)
                except Exception:
                    pass

            node_info['inputs'] = found_inputs

            # Method 2: Get editable properties via get_editor_property
            for prop_name in dir(target_expr):
                if prop_name.startswith('_'):
                    continue
                if prop_name in ('call_method', 'cast', 'get_class',
                                 'get_editor_property', 'set_editor_property',
                                 'get_fname', 'get_full_name', 'get_name',
                                 'get_outer', 'get_outermost', 'get_path_name',
                                 'get_typed_outer', 'get_world', 'modify',
                                 'rename', 'static_class', 'get_default_object'):
                    continue
                try:
                    prop_val = target_expr.get_editor_property(prop_name)
                    prop_type = type(prop_val).__name__
                    if prop_type in ('float', 'int', 'bool', 'str'):
                        node_info['properties'][prop_name] = {
                            'type': prop_type,
                            'value': str(prop_val),
                        }
                    elif prop_type == 'LinearColor':
                        node_info['properties'][prop_name] = {
                            'type': 'LinearColor',
                            'value': str(prop_val),
                        }
                    elif prop_type == 'Name':
                        node_info['properties'][prop_name] = {
                            'type': 'Name',
                            'value': str(prop_val),
                        }
                except Exception:
                    pass

            results[class_name] = node_info
            success_count += 1
            unreal.log('  [OK] %s: inputs=%s' % (class_name, str(found_inputs)))

        except Exception as e:
            fail_count += 1
            unreal.log('  [FAIL] %s: %s' % (class_name, str(e)))

    # Cleanup
    try:
        mel.delete_all_material_expressions(temp_mat)
    except Exception:
        pass
    try:
        unreal.SystemLibrary.collect_garbage()
    except Exception:
        pass
    try:
        unreal.EditorAssetLibrary.delete_asset(temp_path + '/TempMat2')
    except Exception:
        pass
    try:
        unreal.EditorAssetLibrary.delete_directory(temp_path)
    except Exception:
        unreal.log_warning('Cleanup note: manually delete /Game/_Temp_MatExprDump2 if needed')

    # Save JSON to the same directory as this script
    import os
    script_dir = os.path.dirname(os.path.abspath(__file__))
    output_path = os.path.join(script_dir, 'material_expressions.json')

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    unreal.log('=' * 60)
    unreal.log('Done! Success: %d, Failed: %d' % (success_count, fail_count))
    unreal.log('JSON saved to: %s' % output_path)
    unreal.log('=' * 60)

    return results

dump_material_expressions()
