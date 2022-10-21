import sys
import os
import bpy
import re
from pathlib import Path
import yaml
from argparse import ArgumentParser
from omegaconf import DictConfig, OmegaConf
from config import MainConfig
import blender.blender_utils as butils
from blender.blender_cam_utils import get_blender_camera_from_3x3_P
import json
import numpy as np
import debugpy
sys.path.append(os.path.dirname(__file__))  # So blender's python can find this folder

idx = 0
try:
    idx = sys.argv.index("--")
    cli_arguments = True
except ValueError:
    cli_arguments = False
arg_string = sys.argv[idx + 1:] if cli_arguments else ""

gui_enabled = False
try:
    gui_enabled = bool(sys.argv.index('-b'))
except ValueError:
    pass

prefs = bpy.context.preferences.addons['cycles'].preferences
prefs.compute_device_type = 'CUDA' if (sys.platform != 'darwin') else 'METAL'
for dev in prefs.devices:
    if dev.type == "CPU":
        dev.use = True

if __name__ == '__main__':
    parser = ArgumentParser()
    parser.add_argument('--config', default='config/config.yaml', type=str, help='path to config file')
    parser.add_argument('--debug', action='store_true', help='Will start the remote debugging on port 5678')
    parser.add_argument('--sample', action='store_true', help='run the code using a single random model')
    parser.add_argument('--render', action='store_true', help='perform rendering')
    parser.add_argument('--gpu', type=int, default=-1, help='specify gpu to use. defaults to all available')
    args = parser.parse_args(arg_string)
    gpu_num = 0
    for dev in prefs.devices:
        if dev.type != "CPU":
            if args.gpu == -1:
                dev.use = True
            else:
                dev.use = gpu_num == args.gpu
            gpu_num += 1

    cfg = yaml.safe_load(open(args.config, 'r'))
    config: MainConfig = OmegaConf.structured(cfg, DictConfig(MainConfig))

    if args.debug:
        debugpy.listen(5678)
        print("Waiting for debugger to attach... ", end='', flush=True)
        debugpy.wait_for_client()
        print("done!")

    scene = butils.init_blender(config.blender)
    scene.frame_end = config.samples_per_model
    stl_files = [f for f in Path(config.models_dir).rglob('*') if re.search(config.bladder_model_regex, str(f))]



    cam_matrix = np.asarray(json.load(open(config.camera_intrinsics, 'r'))['IntrinsicMatrix']).T
    camera, cam_data = get_blender_camera_from_3x3_P(cam_matrix, clip_limits=[0.001, 0.5])
    scene.camera = camera

    particle_nodes = butils.add_tumor_particle_nodegroup(**config.tumor_particles)

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
        stl_files = [stl_files[0]]

    # set paths for rendering outputs
    depth_node, image_node = butils.add_render_output_nodes(scene)
    depth_node.base_path = os.path.join(config.output_folder, 'depth')
    image_node.base_path = os.path.join(config.output_folder, 'color')

    # create a blender object that will put the camera to random positions using a shrinkwrap constraint
    random_position = bpy.data.objects.new('random_pos', None)
    endo_collection.objects.link(random_position)
    camera.parent = random_position
    shrinkwrap_constraint = butils.add_shrinkwrap_constraint(random_position, config.shrinkwrap)

    for stl_file in stl_files:
        stl_obj = butils.import_stl(str(stl_file), center=True, collection=bladder_collection)
        butils.scale_mesh_volume(stl_obj, config.bladder_volume)
        shrinkwrap_constraint.target = stl_obj  # attach the constraint to the new stl model
        # add node modifier and introduce the tumor particles
        particles = stl_obj.modifiers.new('Particles', 'NODES')
        particles = particle_nodes

        # get random values for all things to vary
        start_directions = np.random.uniform(0, 360, (config.samples_per_model, 3))
        distances = np.random.uniform(*config.distance_range, config.samples_per_model)
        viewing_angles = np.random.uniform(0, 1, (config.samples_per_model, 3)) \
                         * np.expand_dims(np.asarray(config.view_angle_max), axis=0)
        emissions = np.random.uniform(*config.emission_range, config.samples_per_model)

        # set the name of the stl as part of the file name. index is automatically appended
        depth_node.file_slots[0].path = stl_obj.name
        image_node.file_slots[0].path = stl_obj.name

        # record setups for rendering
        for i in range(config.samples_per_model):
            random_position.rotation_euler = start_directions[i]
            camera.rotation_euler = viewing_angles[i]
            shrinkwrap_constraint.distance = distances[i]
            emission_node.inputs[1].default_value = emissions[i]
            random_position.keyframe_insert(frame=i + 1, data_path="rotation_euler")
            camera.keyframe_insert(frame=i + 1, data_path="rotation_euler")
            shrinkwrap_constraint.keyframe_insert(frame=i + 1, data_path="distance")
            emission_node.inputs[1].keyframe_insert(frame=i + 1, data_path="default_value")

        if args.render:
            bpy.ops.render.render(animation=True, scene=scene.name)

        if not args.sample and not gui_enabled:
            bpy.data.objects.remove(stl_obj, do_unlink=True)
