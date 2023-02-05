import sys
import os

sys.path.append(os.path.dirname(__file__))  # So blender's python can find this folder
import shutil
import bpy
import re
from pathlib import Path
from argparse import ArgumentParser
from omegaconf import OmegaConf, DictConfig
from config.blender_config import MainConfig
import blender.blender_utils as butils
from blender.blender_cam_utils import get_blender_camera_from_3x3_P
import json
import numpy as np
import debugpy
from mathutils import Vector, Matrix


def start_debugger():
    debugpy.listen(5678)
    print("Waiting for debugger to attach... ", end='', flush=True)
    debugpy.wait_for_client()
    print("done!")


def blender_rendering():
    arguments, headless = butils.extract_system_arguments()
    parser = ArgumentParser()
    parser.add_argument('--config', default='config/config.yaml', type=str, help='path to config file')
    parser.add_argument('--debug', action='store_true', help='Will start the remote debugging on port 5678')
    parser.add_argument('--sample', action='store_true', help='run the code using a single random model')
    parser.add_argument('--render', action='store_true', help='perform rendering')
    parser.add_argument('--gpu', type=int, default=-1, help='specify gpu to use. defaults to all available')
    parser.add_argument('--id_offset', type=int, default=0, help='value to offset the frame id by.')
    args, unknown_args = parser.parse_known_args(arguments)
    cli_conf = OmegaConf.from_cli(unknown_args)  # assume any additional args are config overrides
    cfg = DictConfig(OmegaConf.load(args.config))
    config: MainConfig = OmegaConf.merge(OmegaConf.structured(MainConfig()), cfg, cli_conf)
    butils.set_gpu_rendering_preferences(args.gpu, device_type=config.blender.cycles.device_type)
    frame_offset = args.id_offset

    if args.debug:
        start_debugger()
    if config.clear_output_folder:
        if os.path.exists(config.output_folder):
            shutil.rmtree(config.output_folder)

    for material_file in config.materials_files:
        with bpy.data.libraries.load(material_file) as (data_from, data_to):
            data_to.materials = data_from.materials

    scene, view_layer = butils.init_blender(config.blender)
    scene.frame_end = config.samples_per_model
    stl_files = [f for f in Path(config.models_dir).rglob('*') if re.search(config.bladder_model_regex, str(f))]

    cam_matrix = np.asarray(json.load(open(config.camera_intrinsics, 'r'))['IntrinsicMatrix']).T
    camera, cam_data = get_blender_camera_from_3x3_P(cam_matrix, scene=scene, clip_limits=[0.001, 0.5],
                                                     scale=config.blender.render.resolution_percentage/100)
    butils.apply_transformations(camera)
    scene.camera = camera

    # setup collection hierarchy
    endo_collection = bpy.data.collections.new("Endoscope")
    bladder_collection = bpy.data.collections.new("Bladder")
    scene.collection.children.link(endo_collection)
    scene.collection.children.link(bladder_collection)
    endo_collection.objects.link(camera)
    endo_tip = bpy.data.objects.new('endo_tip', None)
    endo_collection.objects.link(endo_tip)
    camera.parent = endo_tip

    particle_nodes, tumor = butils.add_tumor_particle_nodegroup(collection=bladder_collection, **config.tumor_particles)
    tumor.data.materials.append(None)
    tumor.material_slots[0].link = 'OBJECT'
    diverticulum_nodes = butils.add_diverticulum_nodegroup(**config.diverticulum)

    if config.with_tool:
        # add resection loop
        loop_angle_offset = bpy.data.objects.new('endo_angle', None)
        resection_loop, wire, insulation, loop_direction_marker, loop_no_clip_markers = \
            butils.add_resection_loop(config.resection_loop, collection=endo_collection, parent=loop_angle_offset)
        loop_angle_offset.parent = endo_tip
        loop_angle_offset.rotation_euler = Vector(np.radians([-config.endoscope_angle, 0, 0]))
        loop_angle_offset.location = Vector([0.0, 0.0, -2.0 * config.resection_loop.scaling_factor])
        trafo = butils.apply_transformations(loop_angle_offset)
        wire_shrinkwrap_constraint = butils.add_shrinkwrap_constraint(wire, config.shrinkwrap_wire)
        # update no-clip-markers and loop_direction_marker
        loop_direction_marker = np.array(trafo) @ loop_direction_marker
        loop_no_clip_markers = np.array(trafo) @ loop_no_clip_markers
        endo_collection.objects.link(loop_angle_offset)
        insulation.data.materials.append(None)
        insulation.material_slots[0].link = 'OBJECT'
        insulation.material_slots[0].material = bpy.data.materials['insulation']

    # add light surface
    light, emission_node = butils.add_surface_lighting(**config.endo_light,
                                                       collection=endo_collection,
                                                       parent_object=endo_tip)

    bpy.data.worlds["World"].node_tree.nodes["Background"].inputs[1].default_value = 0  # turn off global lighting
    if args.sample:
        stl_files = [stl_files[np.random.randint(0, len(stl_files) - 1)]]

    scene.node_tree.nodes.clear()
    default_material = bpy.data.materials['Material']

    # set paths for rendering outputs
    output_nodes = butils.add_render_output_nodes(scene, normals=config.render_normals)

    # create a blender object that will put the camera to random positions using a shrinkwrap constraint
    random_position = bpy.data.objects.new('random_pos', None)
    endo_collection.objects.link(random_position)
    endo_tip.parent = random_position
    rand_pos_shrinkwrap_constraint = butils.add_shrinkwrap_constraint(random_position, config.shrinkwrap_tool)

    for stl_file in stl_files:
        stl_obj = butils.import_stl(str(stl_file), center=True, collection=bladder_collection, flip_normals=False)
        butils.scale_mesh_volume(stl_obj, config.bladder_volume)
        rand_pos_shrinkwrap_constraint.target = stl_obj  # attach the constraint to the new shrink target
        if config.with_tool:
            wire_shrinkwrap_constraint.target = stl_obj
        # add node modifier and introduce the tumor particles and the diverticulum
        diverticulum = stl_obj.modifiers.new('Diverticulum', 'NODES')
        diverticulum.node_group = diverticulum_nodes
        # add node modifier and introduce the tumor particles
        particles = stl_obj.modifiers.new('Particles', 'NODES')
        particles.node_group = particle_nodes
        butils.add_subdivision_modifier(stl_obj, config.subdivision_mod)
        stl_obj.data.materials.append(None)
        stl_obj.material_slots[0].link = 'OBJECT'  # so that the divercula are seen by the material properly

        # apply materials to bladder wall and tumors
        stl_obj.material_slots[0].material = default_material
        tumor.material_slots[0].material = default_material

        # set the name of the stl as part of the file name. index is automatically appended
        [setattr(n.file_slots[0], 'path', f'{stl_obj.name}_#####') for n in output_nodes if n is not None]
        camera.rotation_euler = Vector(np.radians([0, 0, 180]))

        for material_name in config.bladder_materials:
            butils.update_bladder_material(config.bladder_material_config, material_name)

            # set folder name to render to
            [setattr(output_nodes[i], 'base_path', os.path.join(config.output_folder, lbl, material_name))
             for i, lbl in enumerate(['color', 'depth', 'normals']) if output_nodes[i]]
            # set random scenes and render
            for i in range(1, config.samples_per_model + 1):
                frame_number = i + frame_offset
                random_position.rotation_euler = (np.random.uniform(0, np.radians(360), size=3))
                endo_tip.rotation_euler = np.random.uniform(0, 1, size=3) * np.radians(
                    np.asarray(config.view_angle_max))
                emission_node.inputs[1].default_value = np.random.uniform(*config.emission_range, 1)
                if config.with_tool:
                    # retract resection loop insulation
                    wire_retraction = np.random.uniform(-1 * config.resection_loop.scaling_factor,
                                                        config.resection_loop.max_retraction * \
                                                        config.resection_loop.scaling_factor)
                    wire.location = Vector((0, 0, -1)) * wire_retraction
                    insulation_retraction = np.random.uniform(-1 * config.resection_loop.scaling_factor,
                                                              wire_retraction)
                    insulation.location = Vector((0, 0, -1)) * insulation_retraction

                bpy.context.view_layer.update()
                camera_euler = random_position.matrix_world.to_euler()
                camera_direction = np.array([0, 0, 1]) @ camera_euler.to_matrix()
                camera_hit, camera_hit_location, _, _ = stl_obj.ray_cast(random_position.matrix_world.to_translation(),
                                                                         camera_direction)
                if not camera_hit:
                    continue
                camera_ray_length = Vector(random_position.matrix_world.to_translation() - camera_hit_location).length
                random_position.location = random_position.matrix_world.to_translation() + Vector(
                    camera_direction * np.random.uniform(0, camera_ray_length * 0.9))
                if config.with_tool:
                    insulation.keyframe_insert(frame=frame_number, data_path='location')
                    wire.keyframe_insert(frame=frame_number, data_path='location')
                    loop_angle_offset.keyframe_insert(frame=frame_number, data_path='location')
                    loop_angle_offset.keyframe_insert(frame=frame_number, data_path='rotation_euler')
                random_position.keyframe_insert(frame=frame_number, data_path="rotation_euler")
                random_position.keyframe_insert(frame=frame_number, data_path="location")
                endo_tip.keyframe_insert(frame=frame_number, data_path="rotation_euler")
                camera.keyframe_insert(frame=frame_number, data_path='rotation_euler')
                # shrinkwrap_constraint.keyframe_insert(frame=i, data_path="distance")
                emission_node.inputs[1].keyframe_insert(frame=frame_number, data_path="default_value")

                if args.render:
                    scene.frame_set(frame_number)
                    stl_obj.material_slots[0].material = bpy.data.materials[material_name]
                    bpy.ops.render.render(write_still=True, scene=scene.name)

                    # switch to basic material and renderer for rendering normals and depth
                    scene.render.engine = 'BLENDER_EEVEE'
                    stl_obj.material_slots[0].material = bpy.data.materials['Material']
                    [setattr(n, 'mute', not n.mute) for n in output_nodes if n is not None]
                    bpy.ops.render.render(write_still=True, scene=scene.name)

                    # put everything back
                    [setattr(n, 'mute', not n.mute) for n in output_nodes if n is not None]
                    scene.render.engine = config.blender.render.engine

                if config.with_tool:
                    loop_angle_offset.location = (0, 0, 0)

        if not args.sample:
            bpy.data.objects.remove(stl_obj, do_unlink=True)
            butils.clear_all_keyframes()


if __name__ == '__main__':
    blender_rendering()
