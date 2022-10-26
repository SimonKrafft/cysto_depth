import sys
import os
sys.path.append(os.path.dirname(__file__))  # So blender's python can find this folder
import shutil
import bpy
import re
from pathlib import Path
from argparse import ArgumentParser
from omegaconf import OmegaConf, DictConfig
from config import MainConfig
import blender.blender_utils as butils
from blender.blender_cam_utils import get_blender_camera_from_3x3_P
import json
import numpy as np
import debugpy


def start_debugger():
    debugpy.listen(5678)
    print("Waiting for debugger to attach... ", end='', flush=True)
    debugpy.wait_for_client()
    print("done!")


if __name__ == '__main__':
    arguments, headless = butils.extract_system_arguments()
    parser = ArgumentParser()
    parser.add_argument('--config', default='config/config.yaml', type=str, help='path to config file')
    parser.add_argument('--debug', action='store_true', help='Will start the remote debugging on port 5678')
    parser.add_argument('--sample', action='store_true', help='run the code using a single random model')
    parser.add_argument('--render', action='store_true', help='perform rendering')
    parser.add_argument('--gpu', type=int, default=-1, help='specify gpu to use. defaults to all available')
    args, unknown_args = parser.parse_known_args(arguments)
    cli_conf = OmegaConf.from_cli(unknown_args)  # assume any additional args are config overrides
    butils.set_gpu_rendering_preferences(args.gpu)
    cfg = DictConfig(OmegaConf.load(args.config))
    config: MainConfig = OmegaConf.merge(OmegaConf.structured(MainConfig()), cfg, cli_conf)

    if args.debug:
        start_debugger()
    if config.clear_output_folder:
        if os.path.exists(config.output_folder):
            shutil.rmtree(config.output_folder)

    scene = butils.init_blender(config.blender)
    scene.frame_end = config.samples_per_model
    stl_files = [f for f in Path(config.models_dir).rglob('*') if re.search(config.bladder_model_regex, str(f))]

    cam_matrix = np.asarray(json.load(open(config.camera_intrinsics, 'r'))['IntrinsicMatrix']).T
    camera, cam_data = get_blender_camera_from_3x3_P(cam_matrix, scene=scene, clip_limits=[0.001, 0.5],
                                                     scale=config.blender.render.resolution_percentage / 100)
    scene.camera = camera

    particle_nodes = butils.add_tumor_particle_nodegroup(**config.tumor_particles)
    diverticulum_nodes = butils.add_diverticulum_nodegroup(**config.diverticulum)

    endo_collection = bpy.data.collections.new("Endoscope")
    bladder_collection = bpy.data.collections.new("Bladder")
    scene.collection.children.link(endo_collection)
    scene.collection.children.link(bladder_collection)

    endo_collection.objects.link(camera)
    light, emission_node = butils.add_surface_lighting(**config.endo_light,
                                                       collection=endo_collection,
                                                       parent_object=camera)

    bpy.data.worlds["World"].node_tree.nodes["Background"].inputs[1].default_value = 0
    if args.sample:
        stl_files = [stl_files[np.random.randint(0, len(stl_files)-1)]]

    # set paths for rendering outputs
    output_nodes = butils.add_render_output_nodes(scene, normals=config.render_normals)
    output_nodes[0].base_path = os.path.join(config.output_folder, 'color')
    output_nodes[1].base_path = os.path.join(config.output_folder, 'depth')
    if config.render_normals:
        output_nodes[2].base_path = os.path.join(config.output_folder, 'normal')

    # create a blender object that will put the camera to random positions using a shrinkwrap constraint
    random_position = bpy.data.objects.new('random_pos', None)
    endo_collection.objects.link(random_position)
    camera.parent = random_position
    shrinkwrap_constraint = butils.add_shrinkwrap_constraint(random_position, config.shrinkwrap)

    for stl_file in stl_files:
        stl_obj = butils.import_stl(str(stl_file), center=True, collection=bladder_collection)
        butils.scale_mesh_volume(stl_obj, config.bladder_volume)
        shrinkwrap_constraint.target = stl_obj  # attach the constraint to the new stl model
        # add node modifier and introduce the tumor particles and the diverticulum
        diverticulum = stl_obj.modifiers.new('Diverticulum', 'NODES')
        diverticulum.node_group = diverticulum_nodes
        # add node modifier and introduce the tumor particles
        particles = stl_obj.modifiers.new('Particles', 'NODES')
        particles.node_group = particle_nodes
        butils.add_subdivision_modifier(stl_obj, config.subdivision_mod)

        # set the name of the stl as part of the file name. index is automatically appended
        [setattr(n.file_slots[0], 'path', f'{stl_obj.name}_#####') for n in output_nodes if n is not None]

        # set random scenes and render
        for i in range(1, config.samples_per_model + 1):
            random_position.rotation_euler = (np.random.uniform(0, np.radians(360), size=3))
            camera.rotation_euler = np.random.uniform(0, 1, size=3) * np.radians(np.asarray(config.view_angle_max))
            shrinkwrap_constraint.distance = np.random.uniform(*config.distance_range, 1)
            emission_node.inputs[1].default_value = np.random.uniform(*config.emission_range, 1)
            random_position.keyframe_insert(frame=i, data_path="rotation_euler")
            camera.keyframe_insert(frame=i, data_path="rotation_euler")
            shrinkwrap_constraint.keyframe_insert(frame=i, data_path="distance")
            emission_node.inputs[1].keyframe_insert(frame=i, data_path="default_value")

            if args.render:
                # render per frame so any in-between processing (i.e. normals transformation) can be done.
                scene.frame_set(i)
                bpy.ops.render.render(write_still=True, scene=scene.name)
                if config.render_normals:
                    norms_file = os.path.join(output_nodes[2].base_path, f'{stl_obj.name}_{i:05d}.exr')
                    butils.convert_norm_exr_2_cam(norms_file, camera)

        if not args.sample:
            bpy.data.objects.remove(stl_obj, do_unlink=True)
            butils.clear_all_keyframes()
