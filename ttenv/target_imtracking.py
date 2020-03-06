"""Target Tracking Environments for Reinforcement Learning. OpenAI gym format
[Vairables]
d: radial coordinate of a belief target in the learner frame
alpha : angular coordinate of a belief target in the learner frame
ddot : radial velocity of a belief target in the learner frame
alphadot : angular velocity of a belief target in the learner frame
Sigma : Covariance of a belief target
o_d : linear distance to the closet obstacle point
o_alpha : angular distance to the closet obstacle point
[Environment Descriptions]
TargetTrackingEnv5 : Local Image-based Double Integrator Target model with KF belief tracker
    RL state: [local_map_image, [d, alpha, ddot, alphadot, logdet(Sigma), observed] * nb_targets, [o_d, o_alpha]]
    Target : Double Integrator model, [x,y,xdot,ydot]
    Belief Target : KF, Double Integrator model

TargetTrackingEnv6 : Local visit frequency map is given to the agent as well as all the inputs of V5.
    The covered area is same as the area presented in the local map.

TargetTrackingEnv7 : Local map & Local visit frequency maps of outside the front range - left, right, front, back are given.
    Therefore, the image input fed to the convolutional neural network has five depth.
    This intend to use a smaller image size.
"""
import gym
from gym import spaces, logger

import numpy as np
from numpy import linalg as LA
import os, copy

from ttenv.agent_models import *
from ttenv.policies import *
from ttenv.belief_tracker import KFbelief
import ttenv.util as util
from ttenv.target_tracking import TargetTrackingEnv1

class TargetTrackingEnv5(TargetTrackingEnv1):
    def __init__(self, num_targets=1, map_name='empty', is_training=True,
                                        known_noise=True, im_size=28, **kwargs):
        TargetTrackingEnv1.__init__(self, num_targets=num_targets,
            map_name=map_name, is_training=is_training, known_noise=known_noise, **kwargs)
        self.id = 'TargetTracking-v5'
        self.im_size = im_size
        self.local_mapmin_g = None
        self.observation_space = spaces.Box(
            np.concatenate((-np.ones(im_size*im_size,), self.limit['state'][0])),
            np.concatenate((np.ones(im_size*im_size,), self.limit['state'][1])),
            dtype=np.float32)

    def reset(self, **kwargs):
        _ = super().reset(**kwargs)

        # Get the local maps.
        map_state = self.map_state_func()
        return np.concatenate((map_state, self.state))

    def step(self, action):
        _, reward, done, info = super().step(action)

        # Get the local maps.
        map_state = self.map_state_func()
        return np.concatenate((map_state, self.state)), reward, done, info

    def map_state_func(self):
        self.local_map, self.local_mapmin_g, _ = self.MAP.local_map(
                                                    self.im_size, self.agent.state)
        # normalize the maps
        self.local_map = [(self.local_map - 0.5) * 2]
        self.local_mapmin_g = [self.local_mapmin_g]
        return self.local_map[0].flatten()

class TargetTrackingEnv6(TargetTrackingEnv5):
    def __init__(self, num_targets=1, map_name='empty', is_training=True,
                                        known_noise=True, im_size=50, **kwargs):
        TargetTrackingEnv5.__init__(self, num_targets=num_targets,
            map_name=map_name, is_training=is_training, known_noise=known_noise, im_size=im_size, **kwargs)
        self.id = 'TargetTracking-v6'
        self.observation_space = spaces.Box(np.concatenate((
            -np.ones(2*im_size*im_size,), self.limit['state'][0])),
            np.concatenate((np.ones(2*im_size*im_size,), self.limit['state'][1])),
            dtype=np.float32)

    def reset(self, **kwargs):
        self.state = []
        self.num_collisions = 0
        init_pose = self.get_init_pose(**kwargs)
        self.agent.reset(init_pose['agent'])
        for i in range(self.num_targets):
            self.belief_targets[i].reset(
                        init_state=np.concatenate((init_pose['belief_targets'][i][:2], np.zeros(2))),
                        init_cov=self.target_init_cov)
            self.targets[i].reset(np.concatenate((init_pose['targets'][i][:2], self.target_init_vel)))
            r, alpha = util.relative_distance_polar(self.belief_targets[i].state[:2],
                                 self.agent.state[:2], self.agent.state[2])
            logdetcov = np.log(LA.det(self.belief_targets[i].cov))
            self.state.extend([r, alpha, 0.0, 0.0, logdetcov, 0.0])

        self.state.extend([self.sensor_r, np.pi])
        self.state = np.array(self.state)
        self.MAP.reset_visit_freq_map()
        obstacles_pt = self.MAP.get_closest_obstacle(self.agent.state)
        self.local_map, self.local_mapmin_g, self.local_visit_freq_map = self.MAP.local_map(
                            self.im_size, self.agent.state, get_visit_freq=True)
        return np.concatenate((self.local_map.flatten(), self.local_visit_freq_map.flatten() - 1.0, self.state))

    def step(self, action):
        action_vw = self.action_map[action]
        is_col = self.agent.update(action_vw, [t.state[:2] for t in self.targets])
        self.num_collisions += int(is_col)
        observed = []
        for i in range(self.num_targets):
            self.targets[i].update(self.agent.state[:2])
            # Observe
            obs = self.observation(self.targets[i])
            observed.append(obs[0])
            self.belief_targets[i].predict() # Belief state at t+1
            if obs[0]: # if observed, update the target belief.
                self.belief_targets[i].update(obs[1], self.agent.state)

        obstacles_pt = self.MAP.get_closest_obstacle(self.agent.state) # visit freq map is updated as well.
        reward, done, mean_nlogdetcov = self.get_reward(self.is_training, is_col=is_col)
        self.state = []
        if obstacles_pt is None:
            obstacles_pt = (self.sensor_r, np.pi)
        for i in range(self.num_targets):
            r_b, alpha_b = util.relative_distance_polar(self.belief_targets[i].state[:2],
                                 self.agent.state[:2], self.agent.state[2])
            r_dot_b, alpha_dot_b = util.relative_velocity_polar(
                                    self.belief_targets[i].state[:2],
                                    self.belief_targets[i].state[2:],
                                    self.agent.state[:2], self.agent.state[-1],
                                    action_vw[0], action_vw[1])
            self.state.extend([r_b, alpha_b, r_dot_b, alpha_dot_b,
                np.log(LA.det(self.belief_targets[i].cov)), float(observed[i])])
        self.state.extend([obstacles_pt[0], obstacles_pt[1]])
        self.state = np.array(self.state)
        self.local_map, self.local_mapmin_g, self.local_visit_freq_map = self.MAP.local_map(
                            self.im_size, self.agent.state, get_visit_freq=True)
        return np.concatenate((self.local_map.flatten(), self.local_visit_freq_map.flatten() - 1.0, self.state)), reward, done, {'mean_nlogdetcov': mean_nlogdetcov}

class TargetTrackingEnv7(TargetTrackingEnv5):
    def __init__(self, num_targets=1, map_name='empty', is_training=True,
                                        known_noise=True, im_size=28, **kwargs):
        TargetTrackingEnv5.__init__(self, num_targets=num_targets,
            map_name=map_name, is_training=is_training, known_noise=known_noise,
            im_size=im_size, **kwargs)
        self.id = 'TargetTracking-v7'

        new_state_limit_low, new_state_limit_high = [], []
        for i in range(num_targets):
            new_state_limit_low.extend(np.append(self.limit['state'][0][i*6:(i+1)*6], 0.0))
            new_state_limit_high.extend(np.append(self.limit['state'][1][i*6:(i+1)*6], 2.0))
        new_state_limit_low = np.concatenate((new_state_limit_low, [0.0, -np.pi]))
        new_state_limit_high = np.concatenate((new_state_limit_high, [self.sensor_r, np.pi]))
        self.limit['state'] = [new_state_limit_low, new_state_limit_high]
        self.observation_space = spaces.Box(
            np.concatenate((-np.ones(5*im_size*im_size,), self.limit['state'][0])),
            np.concatenate((np.ones(5*im_size*im_size,), self.limit['state'][1])),
            dtype=np.float32)

    def reset(self, **kwargs):
        self.MAP.reset_visit_freq_map()
        return super().reset(**kwargs)

    def state_func(self, action_vw, observed):
        # Find the closest obstacle coordinate.
        obstacles_pt = self.MAP.get_closest_obstacle(self.agent.state)
        if obstacles_pt is None:
            obstacles_pt = (self.sensor_r, np.pi)

        self.state = []
        for i in range(self.num_targets):
            r_b, alpha_b = util.relative_distance_polar(self.belief_targets[i].state[:2],
                                                xy_base=self.agent.state[:2],
                                                theta_base=self.agent.state[2])
            r_dot_b, alpha_dot_b = util.relative_velocity_polar(
                                    self.belief_targets[i].state[:2],
                                    self.belief_targets[i].state[2:],
                                    self.agent.state[:2], self.agent.state[2],
                                    action_vw[0], action_vw[1])
            is_belief_blocked = self.MAP.is_blocked(self.agent.state[:2], self.belief_targets[i].state[:2])
            self.state.extend([r_b, alpha_b, r_dot_b, alpha_dot_b,
                                np.log(LA.det(self.belief_targets[i].cov)),
                                float(observed[i]), float(is_belief_blocked)])
        self.state.extend([obstacles_pt[0], obstacles_pt[1]])
        self.state = np.array(self.state)

        # Update the visit frequency map.
        b_speed = np.mean([np.sqrt(np.sum(self.belief_targets[i].state[2:]**2)) for i in range(self.num_targets)])
        decay_factor = np.exp(self.sampling_period*b_speed/self.sensor_r*np.log(0.7))
        self.MAP.update_visit_freq_map(self.agent.state, decay_factor, observed=bool(np.mean(observed)))

    def map_state_func(self):
        self.local_map, self.local_mapmin_g, _ = self.MAP.local_map(
                                                self.im_size, self.agent.state)
        _, local_mapmin_gs, local_visit_maps = self.MAP.local_visit_map_surroundings(
                                                self.im_size, self.agent.state)
        # normalize the maps
        self.local_map = [(self.local_map - 0.5) * 2]
        for i in range(4):
            self.local_map.append(local_visit_maps[i] - 1.0)

        self.local_mapmin_g = [self.local_mapmin_g]
        self.local_mapmin_g.extend(local_mapmin_gs)

        return np.array(self.local_map).T.flatten()

class TargetTrackingEnv8(TargetTrackingEnv5):
    def __init__(self, num_targets=1, map_name='empty', is_training=True,
                                        known_noise=True, im_size=28, **kwargs):
        TargetTrackingEnv5.__init__(self, num_targets=num_targets,
            map_name=map_name, is_training=is_training, known_noise=known_noise, im_size=im_size, **kwargs)
        self.id = 'TargetTracking-v8'
        self.observation_space = spaces.Box(np.concatenate((
            -np.ones(5*im_size*im_size,), self.limit['state'][0])),
            np.concatenate((np.ones(5*im_size*im_size,), self.limit['state'][1])),
            dtype=np.float32)

    def reset(self, **kwargs):
        self.MAP.reset_visit_freq_map()
        return super().reset(**kwargs)

    def map_state_func(self):
        # Update the visit frequency map.
        b_speed = np.mean([np.sqrt(np.sum(self.belief_targets[i].state[2:]**2)) for i in range(self.num_targets)])
        decay_factor = np.exp(self.sampling_period*b_speed/self.sensor_r*np.log(0.7))
        self.MAP.update_visit_freq_map(self.agent.state, decay_factor)

        self.local_map, self.local_mapmin_g, _ = self.MAP.local_map(
                                                self.im_size, self.agent.state)
        _, local_mapmin_gs, local_visit_maps = self.MAP.local_visit_map_surroundings(
                                                self.im_size, self.agent.state)
        # normalize the maps
        self.local_map = [(self.local_map - 0.5) * 2]
        for i in range(4):
            self.local_map.append(local_visit_maps[i] - 1.0)

        self.local_mapmin_g = [self.local_mapmin_g]
        self.local_mapmin_g.extend(local_mapmin_gs)

        return np.array(self.local_map).T.flatten()

class TargetTrackingEnv9(TargetTrackingEnv7):
    def __init__(self, num_targets=1, map_name='empty', is_training=True,
                                        known_noise=True, im_size=28, **kwargs):
        TargetTrackingEnv7.__init__(self, num_targets=num_targets,
            map_name=map_name, is_training=is_training, known_noise=known_noise,
            im_size=im_size, **kwargs)
        self.id = 'TargetTracking-v9'

        new_state_limit_low = np.append(self.limit['state'][0], 0.0)
        new_state_limit_high = np.append(self.limit['state'][1], self.sensor_r)
        self.limit['state'] = [new_state_limit_low, new_state_limit_high]
        self.observation_space = spaces.Box(
            np.concatenate((-np.ones(5*im_size*im_size,), self.limit['state'][0])),
            np.concatenate((np.ones(5*im_size*im_size,), self.limit['state'][1])),
            dtype=np.float32)

    def state_func(self, action_vw, observed):
        # Find the closest obstacle coordinate.
        obstacles_pt, front_obstacle_r = self.MAP.get_closest_obstacle_v2(self.agent.state)
        if obstacles_pt is None:
            obstacles_pt = (self.sensor_r, np.pi)
        if front_obstacle_r is None:
            front_obstacle_r = self.sensor_r

        self.state = []
        for i in range(self.num_targets):
            r_b, alpha_b = util.relative_distance_polar(self.belief_targets[i].state[:2],
                                                xy_base=self.agent.state[:2],
                                                theta_base=self.agent.state[2])
            r_dot_b, alpha_dot_b = util.relative_velocity_polar(
                                    self.belief_targets[i].state[:2],
                                    self.belief_targets[i].state[2:],
                                    self.agent.state[:2], self.agent.state[2],
                                    action_vw[0], action_vw[1])
            is_belief_blocked = self.MAP.is_blocked(self.agent.state[:2], self.belief_targets[i].state[:2])
            self.state.extend([r_b, alpha_b, r_dot_b, alpha_dot_b,
                                np.log(LA.det(self.belief_targets[i].cov)),
                                float(observed[i]), float(is_belief_blocked)])
        self.state.extend([obstacles_pt[0], obstacles_pt[1], front_obstacle_r])
        self.state = np.array(self.state)
        print(self.state[-3:])

        # Update the visit frequency map.
        b_speed = np.mean([np.sqrt(np.sum(self.belief_targets[i].state[2:]**2)) for i in range(self.num_targets)])
        decay_factor = np.exp(self.sampling_period*b_speed/self.sensor_r*np.log(0.7))
        self.MAP.update_visit_freq_map(self.agent.state, decay_factor, observed=bool(np.mean(observed)))
