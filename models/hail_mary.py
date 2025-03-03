from matplotlib import pyplot as plt
import torch
from torch import Tensor
from models.base_model import BaseModel
from models.adaptive_encoder import AdaptiveEncoder
from models.discriminator import Discriminator
from models.depth_model import DepthEstimationModel
from models.depth_norm_model import DepthNormModel
from data.data_transforms import ImageNetNormalization
from utils.image_utils import generate_heatmap_fig, freeze_batchnorm, generate_img_fig
from config.training_config import SyntheticTrainingConfig, GANTrainingConfig, DiscriminatorConfig, \
    DepthNorm2ImageConfig
from argparse import Namespace
from utils.rendering import PhongRender, depth_to_normals
from utils.loss import GANDiscriminatorLoss, GANGeneratorLoss
from typing import *

opts = {'adam': torch.optim.Adam, 'radam': torch.optim.RAdam, 'rmsprop': torch.optim.RMSprop}


class DiscriminatorCriticInputs(TypedDict):
    color: Tensor
    encoder_outs: List[Tensor]
    depth: Tensor
    normals: Tensor
    phong: Tensor
    calculated_phong: Tensor


class HailMary(BaseModel):
    def __init__(self,
                 synth_config: SyntheticTrainingConfig,
                 gan_config: GANTrainingConfig,
                 depth_norm_config: DepthNorm2ImageConfig):
        super().__init__()
        self.automatic_optimization = False
        ckpt = None
        if gan_config.resume_from_checkpoint:
            ckpt = torch.load(gan_config.resume_from_checkpoint, map_location=self.device)
            hparams = ckpt['hyper_parameters']
            hparams['resume_from_checkpoint'] = gan_config.resume_from_checkpoint
            [setattr(gan_config, key, val) for key, val in hparams.items() if key in gan_config]

        self.save_hyperparameters(Namespace(**gan_config))
        self.depth_model = DepthEstimationModel(synth_config)
        gan_config.encoder.backbone = self.depth_model.config.encoder.backbone
        self.config = gan_config
        self.generator = AdaptiveEncoder(gan_config.encoder)
        self.generator.load_state_dict(self.depth_model.encoder.state_dict(), strict=False)
        self.texture_generator = DepthNormModel(depth_norm_config)
        self.texture_generator.log = self.log
        self.depth_model.requires_grad = False
        self.imagenet_denorm = ImageNetNormalization(inverse=True)
        self.phong_renderer: PhongRender = None
        self.discriminator_losses: Dict[str, Union[float, Tensor]] = {}
        self.generator_losses = {'g_loss': 0.0}
        self.critic_losses: Dict[str, Union[float, Tensor]] = {}
        self.discriminators: torch.nn.ModuleDict = torch.nn.ModuleDict()
        self.critics: torch.nn.ModuleDict = torch.nn.ModuleDict()
        self.generated_source_id: int = len(self.texture_generator.sources)
        self.critic_opt_idx = 0
        self.discriminators_opt_idx = 0
        self._unwrapped_optimizers = []
        self.setup_generator_optimizer()
        if self.config.use_critic:
            self.setup_critics()
        if self.config.use_discriminator:
            self.setup_discriminators()
        self.setup_texture_generator()
        self.generator_losses.update(self.texture_generator.generator_losses)
        self.discriminator_losses.update(self.texture_generator.discriminator_losses)
        self.texture_generator.generator_losses = self.generator_losses
        self.texture_generator.discriminator_losses = self.discriminator_losses
        self.texture_generator_opt_idx = len(self._unwrapped_optimizers)
        self.texture_critic_opt_idx = self.texture_generator_opt_idx + 1 if \
            self.texture_generator.config.use_critic else self.texture_generator_opt_idx

        self._unwrapped_optimizers.extend(self.texture_generator.configure_optimizers())
        self.depth_model.requires_grad = True
        self.generator.requires_grad = True
        self.texture_generator.requires_grad = True
        self.texture_discriminator_opt_idx = self.texture_critic_opt_idx + 1
        self.validation_epoch = 0
        self.generator_global_step = -1
        self.critic_global_step = 0
        self.total_train_step_count = -1
        self.batches_accumulated = 0
        self._full_batch = False
        self._generator_training = False
        self.unadapted_images_for_plotting = None
        self.validation_data = None
        self.discriminator_loss = GANDiscriminatorLoss[gan_config.discriminator_loss]
        self.critic_loss = GANDiscriminatorLoss[gan_config.critic_loss]
        self.generator_critic_loss = GANGeneratorLoss[gan_config.critic_loss]
        self.generator_discriminator_loss = GANGeneratorLoss[gan_config.discriminator_loss]
        self._discriminator_critic_count = len(self.discriminators) + len(self.critics) + \
                                           len(self.texture_generator.discriminators) + \
                                           len(self.texture_generator.critics)
        if gan_config.resume_from_checkpoint:
            self._resume_from_checkpoint(ckpt)

    def _resume_from_checkpoint(self, ckpt: dict):
        with torch.no_grad():
            # run some data through the network to initial dense layers in discriminators if needed
            encoder_outs, encoder_mare_outs, decoder_outs, normals = self(
                torch.ones(1, 3, self.config.image_size, self.config.image_size, device=self.device))
            feat_outs = encoder_outs[::-1][:len(self.discriminators['features'])]

            if self.config.use_discriminator:
                if self.config.use_feature_level:
                    for idx, feature_out in enumerate(feat_outs):
                        self.discriminators['features'][idx](feature_out)
                self.discriminators['depth_image'](decoder_outs[-1])
                if self.config.predict_normals:
                    self.discriminators['phong'](normals)
                    self.discriminators['depth_phong'](normals)
                    self.discriminators['normals'](normals)

            if self.config.use_critic:
                if self.config.use_feature_level:
                    for idx, feature_out in enumerate(feat_outs):
                        self.critics['features'][idx](feature_out)
                self.critics['depth_image'](decoder_outs[-1])
                if self.config.predict_normals:
                    self.critics['phong'](normals)
                    self.critics['depth_phong'](normals)
                    self.critics['normals'](normals)

        self.load_state_dict(ckpt['state_dict'], strict=False)

    def setup_generator_optimizer(self):
        opt = opts[self.config.generator_optimizer.lower()]
        self._unwrapped_optimizers.append(opt(filter(lambda p: p.requires_grad, self.generator.parameters()),
                                              lr=self.config.generator_lr))

    def setup_discriminators(self):
        self.discriminator_losses['d_discriminators_loss'] = 0.0

        if self.config.use_feature_level:
            d_in_shapes = self.generator.feature_levels[::-1]
            d_feat_list = []
            for d_in_shape in d_in_shapes[:-1]:
                d_config: DiscriminatorConfig = self.config.feature_level_discriminator.copy()
                d_config.in_channels = d_in_shape
                d = Discriminator(d_config)
                d_feat_list.append(d)
            self.discriminators['features'] = torch.nn.ModuleList(modules=d_feat_list)
            self.discriminator_losses.update({f'd_loss_discriminator_feature_{i}': 0.0 for i in range(len(d_feat_list))})
            self.discriminator_losses.update(
                {f'd_loss_reg_discriminator_feature_{i}': 0.0 for i in range(len(d_feat_list))})
            self.generator_losses.update({f'g_loss_discriminator_feature_{i}': 0.0 for i in range(len(d_feat_list))})
        if self.config.predict_normals:
            self.discriminators['phong'] = Discriminator(self.config.phong_discriminator)
            self.discriminators['depth_phong'] = Discriminator(self.config.phong_discriminator)
            self.discriminators['normals'] = Discriminator(self.config.normals_discriminator)
            self.discriminator_losses.update({'d_loss_discriminator_phong': 0.0,
                                              'd_loss_discriminator_depth_phong': 0.0,
                                              'd_loss_discriminator_normals': 0.0})
            self.discriminator_losses.update({'d_loss_reg_discriminator_phong': 0.0,
                                              'd_loss_reg_discriminator_depth_phong': 0.0,
                                              'd_loss_reg_discriminator_normals': 0.0})
            self.generator_losses.update({'g_loss_discriminator_phong': 0.0, 'g_loss_discriminator_depth_phong': 0.0,
                                          'g_loss_discriminator_normals': 0.0})
        self.discriminators['depth_image'] = Discriminator(self.config.depth_discriminator)
        self.discriminator_losses.update({'d_loss_discriminator_depth_img': 0.0,
                                          'd_loss_reg_discriminator_depth_img': 0.0})
        self.generator_losses.update({'g_loss_discriminator_depth_img': 0.0})
        opt = opts[self.config.discriminator_optimizer.lower()]
        self._unwrapped_optimizers.append(opt(filter(lambda p: p.requires_grad, self.discriminators.parameters()),
                                              lr=self.config.discriminator_lr))
        self.discriminators_opt_idx = self.critic_opt_idx + 1

    def setup_texture_generator(self):
        if self.texture_generator.config.use_discriminator:
            self.texture_generator.discriminators[str(self.generated_source_id)] = \
                Discriminator(self.texture_generator.config.discriminator_config)
            self.texture_generator.generator_losses[f'g_discriminator_loss-{self.generated_source_id}'] = 0.0
            self.texture_generator.discriminator_losses[f'd_discriminator_loss-{self.generated_source_id}'] = 0.0
            self.texture_generator.discriminator_losses[f'd_discriminator_reg_loss-{self.generated_source_id}'] = 0.0
        if self.texture_generator.config.use_critic:
            self.texture_generator.critics[str(self.generated_source_id)] = \
                Discriminator(self.texture_generator.config.critic_config)
            self.texture_generator.generator_losses[f'g_critic_loss-{self.generated_source_id}'] = 0.0
            self.texture_generator.critic_losses[f'd_critic_loss-{self.generated_source_id}'] = 0.0
            self.texture_generator.critic_losses[f'd_critic_gp-{self.generated_source_id}'] = 0.0

    def setup_critics(self):
        self.critic_losses['d_critics_loss'] = 0.0
        if self.config.use_feature_level:
            d_in_shapes = self.generator.feature_levels[::-1]
            d_feat_list = []
            for d_in_shape in d_in_shapes[:-1]:
                d_config: DiscriminatorConfig = self.config.feature_level_critic.copy()
                d_config.in_channels = d_in_shape
                d = Discriminator(d_config)
                d_feat_list.append(d)
            self.critics['features'] = torch.nn.ModuleList(modules=d_feat_list)
            self.critic_losses.update({f'd_loss_critic_feature_{i}': 0.0 for i in range(len(d_feat_list))})
            self.generator_losses.update({f'g_loss_critic_feature_{i}': 0.0 for i in range(len(d_feat_list))})
            self.critic_losses.update({f'd_loss_critic_gp_feature_{i}': 0.0 for i in range(len(d_feat_list))})
        if self.config.predict_normals:
            self.critics['phong'] = Discriminator(self.config.phong_critic)
            self.critics['depth_phong'] = Discriminator(self.config.phong_critic)
            self.critics['normals'] = Discriminator(self.config.normals_critic)
            self.critic_losses.update({'d_loss_critic_phong': 0.0,
                                       'd_loss_critic_depth_phong': 0.0,
                                       'd_loss_critic_normals': 0.0,
                                       'd_loss_critic_gp_phong': 0.0,
                                       'd_loss_critic_gp_depth_phong': 0.0,
                                       'd_loss_critic_gp_normals': 0.0})
            self.generator_losses.update({'g_loss_critic_phong': 0.0, 'g_loss_critic_depth_phong': 0.0,
                                          'g_loss_critic_normals': 0.0})
        self.critics['depth_image'] = Discriminator(self.config.depth_critic)
        self.critic_losses.update({'d_loss_critic_depth_img': 0.0, 'd_loss_critic_gp_depth_img': 0.0})
        self.generator_losses.update({'g_loss_critic_depth_img': 0.0})
        opt = opts[self.config.critic_optimizer.lower()]
        self._unwrapped_optimizers.append(opt(filter(lambda p: p.requires_grad, self.critics.parameters()),
                                              lr=self.config.critic_lr))
        self.critic_opt_idx += 1

    @staticmethod
    def reset_log_dict(log_dict: dict):
        log_dict.update({k: 0.0 for k in log_dict.keys()})

    def forward(self, x, generator: bool = True) -> Tuple[Union[torch.Tensor, List[torch.Tensor]], ...]:
        return self.get_predictions(x, generator=generator)

    def __call__(self, *args, **kwargs) -> Tuple[Union[torch.Tensor, List[torch.Tensor]], ...]:
        return super(HailMary, self).__call__(*args, **kwargs)

    def on_validation_epoch_start(self) -> None:
        if self.config.predict_normals:
            self.phong_renderer = PhongRender(config=self.config.phong_config,
                                              image_size=self.config.image_size,
                                              device=self.device)

    def get_predictions(self, x: torch.Tensor, generator: bool) -> Tuple[Union[torch.Tensor, List[torch.Tensor]], ...]:
        """ helper function to clean up the training_step function.

        :param x: batch of images
        :param generator: whether to use the generator encoder
        :return: encoder_outs, encoder_mare_outs, decoder_outs, normals
                Note: everything is a list of tensors for each Unet level except normals.
        """
        if generator:
            encoder_outs, encoder_mare_outs = self.generator(x)
        else:
            encoder_outs, encoder_mare_outs = self.depth_model.encoder(x)
        if self.depth_model.config.merged_decoder:
            output = self.depth_model.decoder(encoder_outs)
            decoder_outs = [level[:, 0, ...].unsqueeze(1) for level in output]
            normals = output[-1][:, 1:, ...]
        else:
            normals = self.depth_model.normals_decoder(encoder_outs)
            decoder_outs = self.depth_model.decoder(encoder_outs)
        normals = torch.where(decoder_outs[-1] > self.depth_model.config.min_depth,
                              normals, torch.zeros([1], device=self.device))

        return encoder_outs, encoder_mare_outs, decoder_outs, normals

    def training_step(self, batch, batch_idx):
        # start by freezing all batchnorm layers throughout the networks that shouldn't update statistics
        self.train()
        self.depth_model.apply(freeze_batchnorm)
        if self.config.freeze_batch_norm:
            self.generator.apply(freeze_batchnorm)

        self.total_train_step_count += 1
        self.batches_accumulated += 1
        if self.batches_accumulated == self.config.accumulate_grad_batches:
            self._full_batch = True
            self.batches_accumulated = 0
        if self._generator_training:
            # print('generator')
            self.generator_train_step(batch, batch_idx)
        else:
            self.discriminator_critic_train_step(batch, batch_idx)
        if self._full_batch:
            self.zero_grad()
        self._full_batch = False

    def generator_train_step(self, batch: Dict[int, List[Tensor]], batch_idx) -> None:
        z = batch[self.generated_source_id][0]  # real images
        # set discriminators to eval so that any normalization statistics don't get updated
        self.discriminators.eval()
        self.critics.eval()
        if self.config.encoder.residual_learning:
            self.generator.set_residuals_train()

        # output of encoder when evaluating a real image
        encoder_outs_generated, encoder_mare_outs_generated, decoder_outs_generated, normals_generated = self(z,
                                                                                                              generator=True)
        depth_out = decoder_outs_generated[-1]

        g_loss: Tensor = 0.0

        original_phong_rendering = self.phong_renderer((depth_out, normals_generated))
        calculated_norms = depth_to_normals(depth_out, self.phong_renderer.camera_intrinsics[None],
                                            self.phong_renderer.resized_pixel_locations)
        depth_phong = self.phong_renderer((depth_out, calculated_norms))
        if self.config.use_discriminator:
            if self.config.use_feature_level:
                feat_outs = encoder_outs_generated[::-1][:len(self.discriminators['features'])]
                for idx, feature_out in enumerate(feat_outs):
                    g_loss += self._apply_generator_discriminator_loss(feature_out, self.discriminators['features'][idx],
                                                                       f'discriminator_feature_{idx}') \
                              * self.config.feature_discriminator_factor
            g_loss += self._apply_generator_discriminator_loss(depth_out, self.discriminators['depth_image'],
                                                               'discriminator_depth_img') \
                      * self.config.img_discriminator_factor
            g_loss += self._apply_generator_discriminator_loss(original_phong_rendering, self.discriminators['phong'],
                                                               'discriminator_phong') \
                      * self.config.phong_discriminator_factor
            g_loss += self._apply_generator_discriminator_loss(depth_phong, self.discriminators['depth_phong'],
                                                               'discriminator_depth_phong') * self.config.phong_discriminator_factor

        if self.config.use_critic:
            if self.config.use_feature_level:
                feat_outs = encoder_outs_generated[::-1][:len(self.critics['features'])]
                for idx, feature_out in enumerate(feat_outs):
                    generated_predicted = self.critics['features'][idx](feature_out).type_as(feature_out)
                    g_loss += self._apply_generator_critic_loss(generated_predicted, f'critic_feature_{idx}') \
                              * self.config.feature_discriminator_factor
            valid_predicted_depth = self.critics['depth_image'](depth_out)
            g_loss += self._apply_generator_critic_loss(valid_predicted_depth, 'critic_depth_img') \
                      * self.config.img_discriminator_factor

            phong_discrimination = self.critics['phong'](original_phong_rendering)
            g_loss += self._apply_generator_critic_loss(phong_discrimination, 'critic_phong') \
                      * self.config.phong_discriminator_factor
            g_loss += self._apply_generator_critic_loss(self.critics['depth_phong'](depth_phong),
                                                        'critic_depth_phong') \
                      * self.config.phong_discriminator_factor

        batch[self.generated_source_id].extend([depth_out, normals_generated])
        texture_generator_loss = self.texture_generator.calculate_generator_loss(batch)
        g_loss += texture_generator_loss

        self.manual_backward(g_loss)
        self.generator_losses['g_loss'] += g_loss.detach()
        if self._full_batch:
            self.generator_global_step += 1
            self._generator_training = False
            if (self.generator_global_step + 1) % len(self.discriminators) != 0:
                return
            optimizers = self.optimizers(True)
            optimizers = [optimizers[i] for i in [0, self.texture_generator_opt_idx]]
            [o.step() for o in optimizers]
            [o.zero_grad() for o in optimizers]
            self.generator_losses.update({k: self.generator_losses[k] / self.config.accumulate_grad_batches
                                          for k in self.generator_losses.keys()})
            self.log_dict(self.generator_losses)
            self.reset_log_dict(self.generator_losses)

    def discriminator_critic_train_step(self, batch: Dict[int, List[Tensor]], batch_idx) -> None:
        self.generator.eval()
        self.critics.train()
        self.discriminators.train()
        optimizers = self.optimizers(True)

        predictions = self.get_discriminator_critic_inputs(batch, batch_idx)
        first_of_mini_batches = self.critic_global_step % self.config.wasserstein_critic_updates == 0
        last_mini_batch = (self.critic_global_step + 1) % self.config.wasserstein_critic_updates == 0 \
            if self.config.use_critic else True

        if self.config.use_discriminator and first_of_mini_batches:
            # print('discriminator')
            discriminator_loss = self._discriminators(predictions)
            self.manual_backward(discriminator_loss)
            self.discriminator_losses['d_discriminators_loss'] += discriminator_loss.detach()
            if self._full_batch:
                # print('discriminator step')
                discriminator_opt = optimizers[self.discriminators_opt_idx]
                discriminator_opt.step()
                discriminator_opt.zero_grad()

        if self.config.use_critic:
            # print('critic')
            critic_loss = self._critics(predictions)
            self.manual_backward(critic_loss)
            self.discriminator_losses['d_critics_loss'] += critic_loss.detach()
            if self._full_batch:
                # print('critic step')
                o = optimizers[self.critic_opt_idx]
                o.step()
                o.zero_grad()
                self.critic_global_step += 1

        if len(batch[self.generated_source_id]) != 3:
            batch[self.generated_source_id].extend([predictions[self.generated_source_id]['depth'],
                                                    predictions[self.generated_source_id]['normals']])

        if self.texture_generator.config.use_critic:
            critic_loss = self.texture_generator.calculate_critic_loss(batch)
            self.manual_backward(critic_loss)
            if self._full_batch:
                o = optimizers[self.texture_critic_opt_idx]
                o.step()
                o.zero_grad()
                if not self.config.use_critic:
                    self.critic_global_step += 1

        if self.texture_generator.config.use_discriminator and first_of_mini_batches:
            discriminator_loss = self.texture_generator.calculate_discriminator_loss(batch)
            self.manual_backward(discriminator_loss)
            if self._full_batch:
                o = optimizers[self.texture_discriminator_opt_idx]
                o.step()
                o.zero_grad()

        if self._full_batch:
            if self.texture_generator.config.use_critic or self.config.use_critic:
                self.critic_losses.update(
                    {k: self.texture_generator.critic_losses[k] / self.config.accumulate_grad_batches
                     for k in self.texture_generator.critic_losses.keys()})
                self.log_dict(self.critic_losses)
                self.reset_log_dict(self.critic_losses)

            if first_of_mini_batches:
                if self.texture_generator.config.use_discriminator or self.config.use_discriminator:
                    self.discriminator_losses.update({k: self.discriminator_losses[k] / self.config.accumulate_grad_batches
                                                      for k in self.discriminator_losses.keys()})
                    self.log_dict(self.discriminator_losses)
                    self.reset_log_dict(self.discriminator_losses)
            if last_mini_batch:
                self._generator_training = True

    def get_discriminator_critic_inputs(self, batch, batch_idx) -> Dict[int, DiscriminatorCriticInputs]:
        """

        :param batch:
        :param batch_idx:
        :return: dict
        """
        with torch.no_grad():
            results = {}
            for source_id in batch:
                results[source_id] = {}
                z = batch[source_id][0]
                encoder_outs, encoder_mare_outs, decoder_outs, normals = self(z,
                                                                              generator=source_id == self.generated_source_id)
                depth = decoder_outs[-1]
                results[source_id]['color'] = z
                results[source_id]['encoder_outs'] = [e.detach() for e in encoder_outs]
                results[source_id]['depth'] = depth.detach()
                results[source_id]['normals'] = normals.detach()
                phong = self.phong_renderer((depth, normals))
                calculated_phong = self.phong_renderer(
                    (depth, depth_to_normals(depth, self.phong_renderer.camera_intrinsics[None],
                                             self.phong_renderer.resized_pixel_locations)))
                results[source_id]['phong'] = phong.detach()
                results[source_id]['calculated_phong'] = calculated_phong.detach()
        return results

    def _discriminators(self, predictions: Dict[int, DiscriminatorCriticInputs]) -> Tensor:
        depth_generated = predictions[self.generated_source_id]['depth']
        encoder_outs_generated = predictions[self.generated_source_id]['encoder_outs']
        phong_generated = predictions[self.generated_source_id]['phong']
        calculated_phong_generated = predictions[self.generated_source_id]['calculated_phong']

        depth_original = []
        encoder_outs_original = []
        phong_original = []
        calculated_phong_original = []
        for source_id in predictions:
            if source_id == self.generated_source_id:
                break
            depth_original.append(predictions[source_id]['depth'])
            encoder_outs_original.append(predictions[source_id]['encoder_outs'])
            phong_original.append(predictions[source_id]['phong'])
            calculated_phong_original.append(predictions[source_id]['calculated_phong'])

        depth_original = torch.cat(depth_original, dim=0)
        encoder_outs_original = [torch.cat([s[i] for s in encoder_outs_original], dim=0) for i in
                                 range(len(encoder_outs_original[0]))]
        phong_original = torch.cat(phong_original, dim=0)
        calculated_phong_original = torch.cat(calculated_phong_original, dim=0)

        loss: Tensor = 0.0
        loss += self._apply_discriminator_loss(depth_generated,
                                               depth_original,
                                               self.discriminators['depth_image'],
                                               'depth_img')
        if self.config.use_feature_level:
            feat_outs = zip(encoder_outs_generated[::-1], encoder_outs_original[::-1])
            for idx, d_feat in enumerate(self.discriminators['features']):
                feature_out_r, feature_out_s = next(feat_outs)
                loss += self._apply_discriminator_loss(feature_out_r,
                                                       feature_out_s,
                                                       d_feat,
                                                       f'feature_{idx}')

        loss += self._apply_discriminator_loss(phong_generated,
                                               phong_original,
                                               self.discriminators['phong'],
                                               'phong')
        loss += self._apply_discriminator_loss(calculated_phong_generated,
                                               calculated_phong_original,
                                               self.discriminators['depth_phong'],
                                               'depth_phong')
        return loss

    def _critics(self, predictions: Dict[int, DiscriminatorCriticInputs]) -> Tensor:
        depth_generated = predictions[self.generated_source_id]['depth']
        encoder_outs_generated = predictions[self.generated_source_id]['encoder_outs']
        phong_generated = predictions[self.generated_source_id]['phong']
        calculated_phong_generated = predictions[self.generated_source_id]['calculated_phong']

        depth_original = []
        encoder_outs_original = []
        phong_original = []
        calculated_phong_original = []
        for source_id in predictions:
            if source_id == self.generated_source_id:
                break
            depth_original.append(predictions[source_id]['depth'])
            encoder_outs_original.append(predictions[source_id]['encoder_outs'])
            phong_original.append(predictions[source_id]['phong'])
            calculated_phong_original.append(predictions[source_id]['calculated_phong'])

        depth_original = torch.cat(depth_original, dim=0)
        encoder_outs_original = [torch.cat([s[i] for s in encoder_outs_original], dim=0) for i in
                                 range(len(encoder_outs_original[0]))]
        phong_original = torch.cat(phong_original, dim=0)
        calculated_phong_original = torch.cat(calculated_phong_original, dim=0)
        loss: Tensor = 0.0
        loss += self._apply_critic_loss(depth_generated, depth_original, self.critics['depth_image'],
                                        self.config.wasserstein_lambda, 'depth_img')

        if self.config.use_feature_level:
            feat_outs = zip(encoder_outs_generated[::-1], encoder_outs_original[::-1])
            for idx, feature_critic in enumerate(self.critics['features']):
                feature_out_r, feature_out_s = next(feat_outs)
                loss += self._apply_critic_loss(feature_out_r, feature_out_s, feature_critic,
                                                self.config.wasserstein_lambda, f'feature_{idx}')

        loss += self._apply_critic_loss(phong_generated, phong_original, self.critics['phong'],
                                        self.config.wasserstein_lambda, 'phong')
        loss += self._apply_critic_loss(calculated_phong_generated, calculated_phong_original,
                                        self.critics['depth_phong'],
                                        self.config.wasserstein_lambda, 'depth_phong')
        return loss

    def _apply_generator_discriminator_loss(self, discriminator_in: Tensor, discriminator: torch.nn.Module, name: str,
                                            label: float = 1.0) -> Tensor:
        loss, penalty = self.generator_discriminator_loss(discriminator_in, label, discriminator)
        self.generator_losses[f'g_loss_{name}'] += loss.detach()
        return loss

    def _apply_generator_critic_loss(self, discriminator_out: Tensor, name: str, ) -> Tensor:
        loss, penalty = self.generator_critic_loss(discriminator_out)
        self.generator_losses[f'g_loss_critic_{name}'] += loss.detach()
        self.generator_losses[f'g_loss_critic_gp_{name}'] += penalty.detach()
        return loss + penalty

    def _apply_discriminator_loss(self, generated: Tensor, original: Tensor, discriminator: torch.nn.Module,
                                  name: str) -> Tensor:
        loss_generated, gen_penalty = self.discriminator_loss(generated, 0.0, discriminator)
        loss_original, org_penalty = self.discriminator_loss(original, 1.0, discriminator)
        combined_loss = loss_original + loss_generated
        combined_penalty = gen_penalty + org_penalty
        self.discriminator_losses[f'd_loss_discriminator_{name}'] += combined_loss.detach()
        self.discriminator_losses[f'd_loss_reg_discriminator_{name}'] += combined_penalty.detach()
        return combined_loss + combined_penalty

    def _apply_critic_loss(self, generated: Tensor, original: Tensor, critic: torch.nn.Module,
                           wasserstein_lambda: float, name: str):
        critic_loss, penalty = self.critic_loss(generated, original, critic, wasserstein_lambda)
        self.critic_losses[f'd_loss_critic_{name}'] += critic_loss.detach()
        self.critic_losses[f'd_loss_critic_gp_{name}'] += penalty.detach()
        return critic_loss + penalty

    def validation_step(self, batch, batch_idx):
        """
        TODO: This function only does plotting... We need some sort of metric
        :param batch:
        :param batch_idx:
        :return:
        """
        self.eval()
        if self.validation_data is None:
            with torch.no_grad():
                self.validation_data = {}
                for source_id in batch:
                    if source_id < self.generated_source_id:
                        self.validation_data[source_id] = [x[:2].detach() for x in batch[source_id]]
                        self.validation_data[source_id][0] = self.imagenet_denorm(
                            self.validation_data[source_id][0]).detach()
                    else:
                        self.validation_data[source_id] = [batch[source_id][0][:2].detach()]
        with torch.no_grad():
            z = batch[self.generated_source_id]
            _, _, decoder_outs, normals = self(z[0], generator=True)
            batch[self.generated_source_id].extend([decoder_outs[-1], normals])
            self.texture_generator.validation_step(batch, batch_idx)

    def configure_optimizers(self):
        return self._unwrapped_optimizers

    def on_validation_epoch_end(self) -> None:
        self.validation_epoch += 1
        self.plot()
        self.log_gate_coefficients(step=self.global_step)
        self.texture_generator.on_validation_epoch_end()

    def log_gate_coefficients(self, step=None):
        if step is None:
            step = self.current_epoch
        # iterating through all parameters
        if self.config.encoder.adaptive_gating and self.config.encoder.residual_learning:
            for name, params in self.generator.named_parameters():
                if 'gate_coefficients' in name:
                    scalars = {str(i): params[i] for i in range(len(params))}
                    self.logger.experiment.add_scalars(name, scalars, step)

    def plot(self):
        with torch.no_grad():
            z = self.validation_data[self.generated_source_id][0]
            _, _, decoder_outs_adapted, normals_adapted = self(z, generator=True)
            depth_adapted = decoder_outs_adapted[-1].detach()
            denormed_images = self.imagenet_denorm(z).detach()
            self.validation_data[self.generated_source_id] = [denormed_images, depth_adapted, normals_adapted.detach()]
            self.texture_generator.validation_data = self.validation_data
            self.texture_generator.val_denorm_color_images = torch.cat(
                [self.validation_data[i][0].detach().cpu() for i in self.validation_data], dim=0)
            self.texture_generator.plot(self.global_step)
            self.validation_data[self.generated_source_id][0] = z

            if self.unadapted_images_for_plotting is None:
                _, _, decoder_outs_unadapted, normals_unadapted = self(z, generator=False)
                depth_unadapted = decoder_outs_unadapted[-1].detach()
                phong_unadapted = self.phong_renderer((depth_unadapted, normals_unadapted)).detach().cpu()

                self.unadapted_images_for_plotting = (
                depth_unadapted.detach(), normals_unadapted.detach().cpu(), phong_unadapted)

            depth_unadapted, normals_unadapted, phong_unadapted = self.unadapted_images_for_plotting
            denormed_images = denormed_images.cpu()
            plot_tensors = [denormed_images]
            labels = ["Input Image", "Predicted Adapted", "Predicted Unadapted", "Diff"]
            centers = [None, None, None, 0]
            minmax = []
            plot_tensors.append(depth_adapted.cpu())
            plot_tensors.append(depth_unadapted.cpu())
            plot_tensors.append((depth_adapted - depth_unadapted).cpu())

            for idx, imgs in enumerate(zip(*plot_tensors)):
                fig = generate_heatmap_fig(imgs, labels=labels, centers=centers, minmax=minmax,
                                           align_scales=True)
                self.logger.experiment.add_figure(f"GAN Prediction Result-{idx}", fig, self.global_step)
                plt.close(fig)
            phong_adapted = self.phong_renderer((depth_adapted, normals_adapted)).detach().cpu()

            labels = ["Input Image", "Predicted Adapted", "Predicted Unadapted"]
            for idx, img_set in enumerate(zip(denormed_images, phong_adapted, phong_unadapted)):
                fig = generate_img_fig(img_set, labels)
                self.logger.experiment.add_figure(f'GAN-phong-{idx}', fig, self.global_step)
                plt.close(fig)
