import gc
import time
import copy
import ray
import torch
import torch.nn.functional as F
import numpy as np
import numpy.random as npr


from bulletarm_baselines.vtt.vtt.agent import Agent
from bulletarm_baselines.vtt.vtt.data_generator import DataGenerator, EvalDataGenerator
from bulletarm_baselines.vtt.vtt.models.sac import TwinnedQNetwork, GaussianPolicy
from bulletarm_baselines.vtt.vtt.models.latent import LatentModel
from bulletarm_baselines.vtt.vtt.utils import create_feature_actions
from bulletarm_baselines.vtt.vtt import torch_utils

@ray.remote
class Trainer(object):
  '''
  Ray worker that cordinates training of our model.

  Args:
    initial_checkpoint (dict): Checkpoint to initalize training with.
    config (dict): Task config.
  '''
  def __init__(self, initial_checkpoint, config):
    self.config = config
    self.device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')

    self.alpha = self.config.init_temp
    self.target_entropy = -self.config.action_dim
    self.log_alpha = torch.tensor(np.log(self.alpha), requires_grad=True, device=self.device)
    self.alpha_optimizer = torch.optim.Adam([self.log_alpha], lr=self.config.actor_lr_init)

    # Initialize actor, critic, and latent models
    self.latent = LatentModel([4, self.config.vision_size, self.config.vision_size], [self.config.action_dim])
    self.latent.train()
    self.latent.to(self.device)

    self.actor = GaussianPolicy([self.config.action_dim], self.config.seq_len, 288)
    self.actor.train()
    self.actor.to(self.device)

    self.critic = TwinnedQNetwork([self.config.action_dim], 32, 256)
    self.critic.train()
    self.critic.to(self.device)

    self.critic_target = TwinnedQNetwork([self.config.action_dim], 32, 256)
    self.critic_target.train()
    self.critic_target.to(self.device)
    for param in self.critic_target.parameters():
      param.requires_grad = False

    self.pre_training_step = initial_checkpoint['pre_training_step']
    self.training_step = initial_checkpoint['training_step']

    # Initialize optimizer
    self.latent_optimizer = torch.optim.Adam(self.latent.parameters(),
                                             lr=self.config.latent_lr_init)
    self.latent_scheduler = torch.optim.lr_scheduler.ExponentialLR(self.latent_optimizer,
                                                                   self.config.lr_decay)
    self.actor_optimizer = torch.optim.Adam(self.actor.parameters(),
                                            lr=self.config.actor_lr_init)
    self.actor_scheduler = torch.optim.lr_scheduler.ExponentialLR(self.actor_optimizer,
                                                                  self.config.lr_decay)
    self.critic_optimizer = torch.optim.Adam(self.critic.parameters(),
                                             lr=self.config.critic_lr_init)
    self.critic_scheduler = torch.optim.lr_scheduler.ExponentialLR(self.critic_optimizer,
                                                                   self.config.lr_decay)

    if initial_checkpoint['optimizer_state'] is not None:
      self.actor_optimizer.load_state_dict(
        copy.deepcopy(initial_checkpoint['optimizer_state'][0])
      )
      self.critic_optimizer.load_state_dict(
        copy.deepcopy(initial_checkpoint['optimizer_state'][1])
      )

    # Initialize data generator
    self.agent = Agent(self.config, self.device, latent=self.latent, actor=self.actor, critic=self.critic)
    self.data_generator = DataGenerator(self.agent, self.config, self.config.seed)

    # Set random number generator seed
    if self.config.seed:
      npr.seed(self.config.seed)
      torch.manual_seed(self.config.seed)

  def generateExpertData(self, replay_buffer, shared_storage, logger):
    '''
    Generate the amount of expert data defined in the task config.

    Args:
      replay_buffer (ray.worker): Replay buffer worker containing data samples.
      shared_storage (ray.worker): Shared storage worker, shares data across workers.
      logger (ray.worker): Logger worker, logs training data across workers.
    '''
    num_expert_eps = 0
    self.data_generator.resetEnvs(is_expert=True)
    while num_expert_eps < self.config.num_expert_episodes:
      self.data_generator.stepEnvsAsync(shared_storage, replay_buffer, logger, expert=True)
      complete_eps = self.data_generator.stepEnvsWait(shared_storage, replay_buffer, logger, expert=True)
      num_expert_eps += complete_eps

  def generateData(self, num_eps, replay_buffer, shared_storage, logger):
    '''

    Args:
      replay_buffer (ray.worker): Replay buffer worker containing data samples.
      shared_storage (ray.worker): Shared storage worker, shares data across workers.
      logger (ray.worker): Logger worker, logs training data across workers.
    '''
    current_eps = 0
    self.data_generator.resetEnvs(is_expert=False)
    while current_eps < num_eps:
      self.data_generator.stepEnvsAsync(shared_storage, replay_buffer, logger)
      complete_eps = self.data_generator.stepEnvsWait(shared_storage, replay_buffer, logger)
      current_eps += complete_eps

  def continuousUpdateWeights(self, replay_buffer, shared_storage, logger):
    '''
    Continuously sample batches from the replay buffer and perform weight updates.
    This continues until the desired number of training steps has been reached.
    Pre training also happens here.

    Args:
      replay_buffer (ray.worker): Replay buffer worker containing data samples.
      shared_storage (ray.worker): Shared storage worker, shares data across workers.
      logger (ray.worker): Logger worker, logs training data across workers.
    '''
    self.data_generator.resetEnvs(is_expert=False)

    # Pretrain latent model
    next_batch = replay_buffer.sampleLatent.remote(shared_storage)
    while self.pre_training_step < self.config.pretraining_steps and \
      not ray.get(shared_storage.getInfo.remote('terminate')):
      batch = ray.get(next_batch)[1]

      next_batch = replay_buffer.sampleLatent.remote(shared_storage)

      latent_loss = self.updateLatent(batch, logger)
      self.updateLatentAlign(batch)

       # Logger/Shared storage updates
      shared_storage.setInfo.remote(
        {
          'pretraining_step' : self.pre_training_step,
        }
      )

      logger.updateScalars.remote(
        {
          '3.Loss/4.Latent_lr' : self.latent_optimizer.param_groups[0]['lr'],
        }
      )

      self.pre_training_step += 1

    # Train policy
    next_batch = replay_buffer.sample.remote(shared_storage)
    while self.training_step < self.config.training_steps and \
          not ray.get(shared_storage.getInfo.remote('terminate')):

      # Pause training if we need to wait for eval interval to end
      if ray.get(shared_storage.getInfo.remote('pause_training')):
        time.sleep(0.5)
        continue

      self.data_generator.stepEnvsAsync(shared_storage, replay_buffer, logger)

      batch = ray.get(next_batch)[1]
      next_batch = replay_buffer.sample.remote(shared_storage)

      latent_loss = self.updateLatent(batch, logger)
      self.updateLatentAlign(batch)
      _, loss = self.updateSLAC(batch)
      # replay_buffer.updatePriorities.remote(priorities.cpu(), idx_batch)
      self.training_step += 1

      self.data_generator.stepEnvsWait(shared_storage, replay_buffer, logger)

      # Update target critic towards current critic
      self.softTargetUpdate()

      # Update LRs
      if self.training_step > 0 and self.training_step % self.config.lr_decay_interval == 0:
        self.actor_scheduler.step()
        self.critic_scheduler.step()

      # Save to shared storage
      if self.training_step % self.config.checkpoint_interval == 0:
        latent_weights = torch_utils.dictToCpu(self.latent.state_dict())
        actor_weights = torch_utils.dictToCpu(self.actor.state_dict())
        critic_weights = torch_utils.dictToCpu(self.critic.state_dict())
        latent_optimizer_state = torch_utils.dictToCpu(self.latent_optimizer.state_dict())
        actor_optimizer_state = torch_utils.dictToCpu(self.actor_optimizer.state_dict())
        critic_optimizer_state = torch_utils.dictToCpu(self.critic_optimizer.state_dict())

        shared_storage.setInfo.remote(
          {
            'weights' : copy.deepcopy((latent_weights, actor_weights, critic_weights)),
            'optimizer_state' : (copy.deepcopy(latent_optimizer_state),
                                 copy.deepcopy(actor_optimizer_state),
                                 copy.deepcopy(critic_optimizer_state))
          }
        )

        if self.config.save_model:
          #shared_storage.saveReplayBuffer.remote(replay_buffer.getBuffer.remote())
          shared_storage.saveCheckpoint.remote()

      # Logger/Shared storage updates
      shared_storage.setInfo.remote(
        {
          'training_step' : self.training_step,
          'run_eval_interval' : self.training_step > 0 and self.training_step % self.config.eval_interval == 0
        }
      )
      logger.updateScalars.remote(
        {
          '3.Loss/4.Latent_lr' : self.latent_optimizer.param_groups[0]['lr'],
          '3.Loss/5.Actor_lr' : self.actor_optimizer.param_groups[0]['lr'],
          '3.Loss/6.Critic_lr' : self.critic_optimizer.param_groups[0]['lr'],
          '3.Loss/7.Entropy' : loss[3]
        }
      )
      logger.logTrainingStep.remote(
        {
          'Latent' : latent_loss,
          'Actor' : loss[0],
          'Critic' : loss[1],
          'Alpha' : loss[2],
        }
      )

  def updateLatent(self, batch, logger):
    obs_batch, action_batch, reward_batch, done_batch = self.processBatch(batch)

    loss_kld, loss_image, loss_reward, align_loss, contact_loss = self.latent.calculate_loss(obs_batch[0], obs_batch[1], action_batch,
                                                                                             reward_batch, done_batch, self.config.max_force)

    self.latent_optimizer.zero_grad()
    (loss_kld + loss_image + loss_reward + align_loss + contact_loss).backward()
    self.latent_optimizer.step()

    logger.logTrainingStep.remote(
        {
          'KLD Loss' : loss_kld.item(),
          'Reconstruction Loss' : loss_image.item(),
          'Action conditioned Loss' : loss_reward.item(),
          'Latent Alignment Loss' : align_loss.item(),
          'Contact Loss' : contact_loss.item(),
        }
      )

    return loss_kld.item() + loss_reward.item() + loss_image.item() + align_loss.item() + contact_loss.item()

  def updateLatentAlign(self, batch):
    obs_batch, action_batch, reward_batch, done_batch = self.processBatch(batch)

    align_loss = self.latent.calculate_alignment_loss(obs_batch[0], obs_batch[1])

    self.latent_optimizer.zero_grad()
    align_loss.backward()
    self.latent_optimizer.step()

    return align_loss.item()

  def updateSLAC(self, batch):
    '''
    Perform one training step.

    Args:
      () : The current batch.

    Returns:
      (numpy.array, double) : (Priorities, Batch Loss)
    '''
    obs_batch, action_batch, reward_batch, done_batch = self.processBatch(batch)

    # Calculate latent representation
    with torch.no_grad():
      feature_, _, _ = self.latent.encoder(obs_batch[0], obs_batch[1])
      z_ = torch.cat(self.latent.sample_posterior(feature_, action_batch)[2:], dim=-1)
    z, next_z = z_[:, -2], z_[:, -1]
    feature_action, next_feature_action = create_feature_actions(feature_, action_batch)

    # Critic Update
    with torch.no_grad():
      next_action, next_log_pi = self.actor.sample(next_feature_action)
      next_q1, next_q2 = self.critic_target(next_z, next_action)
      next_log_pi, next_q1, next_q2 = next_log_pi.squeeze(), next_q1.squeeze(), next_q2.squeeze()

      next_q = torch.min(next_q1, next_q2) - self.alpha * next_log_pi
      target_q = reward_batch[:, -1] + (1 - done_batch[:, -1]) * self.config.discount * next_q

    # curr_q1, curr_q2 = self.critic(z, action_batch)
    curr_q1, curr_q2 = self.critic(z, action_batch[:, -1])
    curr_q1, curr_q2 = curr_q1.squeeze(), curr_q2.squeeze()

    critic_loss = F.mse_loss(curr_q1, target_q) + F.mse_loss(curr_q2, target_q)

    with torch.no_grad():
      td_error = 0.5 * (torch.abs(curr_q1 - target_q) + torch.abs(curr_q2 - target_q))

    self.critic_optimizer.zero_grad()
    critic_loss.backward(retain_graph=False)
    self.critic_optimizer.step()

    # Actor update
    action, log_pi = self.actor.sample(feature_action)
    q1, q2 = self.critic(z, action)

    actor_loss = -torch.mean(torch.min(q1, q2) - self.alpha * log_pi)

    self.actor_optimizer.zero_grad()
    actor_loss.backward(retain_graph=False)
    self.actor_optimizer.step()

    # Alpha update
    alpha_loss = -(self.log_alpha * (log_pi + self.target_entropy).detach()).mean()

    self.alpha_optimizer.zero_grad()
    alpha_loss.backward(retain_graph=False)
    self.alpha_optimizer.step()

    with torch.no_grad():
      entropy = -log_pi.detach().mean()
      self.alpha = self.log_alpha.exp()

    return td_error, (actor_loss.item(), critic_loss.item(), alpha_loss.item(), entropy.item())

  def processBatch(self, batch):
    obs_batch, action_batch, reward_batch, done_batch, _ = batch

    obs_batch = (obs_batch[0].to(self.device), obs_batch[1].to(self.device))
    action_batch = action_batch.to(self.device)
    reward_batch = reward_batch.to(self.device)
    done_batch = done_batch.to(self.device)

    return obs_batch, action_batch, reward_batch, done_batch

  def softTargetUpdate(self):
    '''
    Update the target critic model to the current critic model.
    '''
    for t_param, l_param in zip(self.critic_target.parameters(), self.critic.parameters()):
      t_param.data.copy_(self.config.tau * l_param.data + (1.0 - self.config.tau) * t_param.data)