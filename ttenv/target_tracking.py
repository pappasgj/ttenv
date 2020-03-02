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

TargetTrackingEnv0 : Static Target model + noise - No Velocity Estimate
    RL state: [d, alpha, logdet(Sigma), observed] * nb_targets , [o_d, o_alpha]
    Target: Static [x,y] + noise
    Belief Target: KF, Estimate only x and y

TargetTrackingEnv1 : Double Integrator Target model with KF belief tracker
    RL state: [d, alpha, ddot, alphadot, logdet(Sigma), observed] * nb_targets, [o_d, o_alpha]
    Target : Double Integrator model, [x,y,xdot,ydot]
    Belief Target : KF, Double Integrator model

TargetTrackingEnv2 : Predefined target paths with KF belief tracker
    RL state: [d, alpha, ddot, alphadot, logdet(Sigma), observed] * nb_targets, [o_d, o_alpha]
    Target : Pre-defined target paths - input files required
    Belief Target : KF, Double Integrator model

TargetTrackingEnv3 : SE2 Target model with UKF belief tracker
    RL state: [d, alpha, logdet(Sigma), observed] * nb_targets, [o_d, o_alpha]
    Target : SE2 model [x,y,theta] + a control policy u=[v,w]
    Belief Target : UKF for SE2 model [x,y,theta]

TargetTrackingEnv4 : SE2 Target model with UKF belief tracker [x,y,theta,v,w]
    RL state: [d, alpha, ddot, alphadot, logdet(Sigma), observed] * nb_targets, [o_d, o_alpha]
    Target : SE2 model [x,y,theta] + a control policy u=[v,w]
    Belief Target : UKF for SE2Vel model [x,y,theta,v,w]
"""
import gym
from gym import spaces, logger
from gym.utils import seeding

import numpy as np
from numpy import linalg as LA
import os, copy

from ttenv.maps import map_utils
from ttenv.agent_models import *
from ttenv.policies import *
from ttenv.belief_tracker import KFbelief, UKFbelief
from ttenv.metadata import METADATA
import ttenv.util as util

class TargetTrackingEnv0(gym.Env):
    def __init__(self, num_targets=1, map_name='empty',
                    is_training=True, known_noise=True, **kwargs):
        gym.Env.__init__(self)
        self.seed()
        self.id = 'TargetTracking-v0'
        self.state = None
        self.action_space = spaces.Discrete(len(METADATA['action_v']) * \
                                                    len(METADATA['action_w']))
        self.action_map = {}
        for (i,v) in enumerate(METADATA['action_v']):
            for (j,w) in enumerate(METADATA['action_w']):
                self.action_map[len(METADATA['action_w'])*i+j] = (v,w)
        assert(len(self.action_map.keys())==self.action_space.n)

        self.target_dim = 2
        self.num_targets = num_targets
        self.viewer = None
        self.is_training = is_training

        self.sampling_period = 0.5 # sec
        self.sensor_r_sd = METADATA['sensor_r_sd']
        self.sensor_b_sd = METADATA['sensor_b_sd']
        self.sensor_r = METADATA['sensor_r']
        self.fov = METADATA['fov']
        map_dir_path = '/'.join(map_utils.__file__.split('/')[:-1])
        self.MAP = map_utils.GridMap(
            map_path=os.path.join(map_dir_path, map_name),
            margin2wall = METADATA['margin2wall'])
        # LIMITS
        self.limit = {} # 0: low, 1:high
        self.limit['agent'] = [np.concatenate((self.MAP.mapmin,[-np.pi])), np.concatenate((self.MAP.mapmax, [np.pi]))]
        self.limit['target'] = [self.MAP.mapmin, self.MAP.mapmax]
        self.limit['state'] = [np.concatenate(([0.0, -np.pi, -50.0, 0.0]*num_targets, [0.0, -np.pi ])),
                               np.concatenate(([600.0, np.pi, 50.0, 2.0]*num_targets, [self.sensor_r, np.pi]))]
        self.observation_space = spaces.Box(self.limit['state'][0], self.limit['state'][1], dtype=np.float32)

        self.agent_init_pos =  np.array([self.MAP.origin[0], self.MAP.origin[1], 0.0])
        self.target_init_pos = np.array(self.MAP.origin)
        self.target_init_cov = METADATA['target_init_cov']
        self.target_noise_cov = METADATA['const_q'] * self.sampling_period**3 / 3 * np.eye(self.target_dim)
        if known_noise:
            self.target_true_noise_sd = self.target_noise_cov
        else:
            self.target_true_noise_sd = METADATA['const_q_true'] * np.eye(2)
        self.targetA = np.eye(self.target_dim)
        # Build a robot
        self.agent = AgentSE2(dim=3, sampling_period=self.sampling_period, limit=self.limit['agent'],
                            collision_func=lambda x: self.MAP.is_collision(x))
        # Build a target
        self.targets = [AgentDoubleInt2D(dim=self.target_dim, sampling_period=self.sampling_period,
                            limit=self.limit['target'],
                            collision_func=lambda x: self.MAP.is_collision(x),
                            A=self.targetA, W=self.target_true_noise_sd) for _ in range(num_targets)]
        self.belief_targets = [KFbelief(dim=self.target_dim, limit=self.limit['target'], A=self.targetA,
                            W=self.target_noise_cov, obs_noise_func=self.observation_noise,
                            collision_func=lambda x: self.MAP.is_collision(x))
                                for _ in range(num_targets)]
        self.reset_num = 0

    def get_init_pose(self, init_pose_list=[], target_path=[], **kwargs):
        """Generates initial positions for the agent, targets, and target beliefs.
        Parameters
        ---------
        init_pose_list : a list of dictionaries with pre-defined initial positions.
        lin_dist_range : a tuple of the minimum and maximum distance of a target
                        and a belief target from the agent.
        ang_dist_range_target : a tuple of the minimum and maximum angular
                            distance (counter clockwise) of a target from the
                            agent. -pi <= x <= pi
        ang_dist_range_belief : a tuple of the minimum and maximum angular
                            distance (counter clockwise) of a belief from the
                            agent. -pi <= x <= pi
        blocked : True if there is an obstacle between a target and the agent.
        """
        if init_pose_list != []:
            if target_path != []:
                self.set_target_path(target_path[self.reset_num])
            self.reset_num += 1
            return init_pose_list[self.reset_num-1]
        else:
            return self.get_init_pose_random(**kwargs)

    def gen_rand_pose(self, frame_xy, frame_theta, min_lin_dist, max_lin_dist,
            min_ang_dist, max_ang_dist, additional_frame=None):
        """Genertes random position and yaw.
        Parameters
        --------
        frame_xy, frame_theta : xy and theta coordinate of the frame you want to compute a distance from.
        min_lin_dist : the minimum linear distance from o_xy to a sample point.
        max_lin_dist : the maximum linear distance from o_xy to a sample point.
        min_ang_dist : the minimum angular distance (counter clockwise direction) from c_theta to a sample point.
        max_ang_dist : the maximum angular distance (counter clockwise direction) from c_theta to a sample point.
        """
        if max_ang_dist < min_ang_dist:
            max_ang_dist += 2*np.pi
        rand_ang = util.wrap_around(np.random.rand() * \
                        (max_ang_dist - min_ang_dist) + min_ang_dist)

        rand_r = np.random.rand() * (max_lin_dist - min_lin_dist) + min_lin_dist
        rand_xy = np.array([rand_r*np.cos(rand_ang), rand_r*np.sin(rand_ang)])
        rand_xy_global = util.transform_2d_inv(rand_xy, frame_theta, np.array(frame_xy))
        if additional_frame:
            rand_xy_global = util.transform_2d_inv(rand_xy_global, additional_frame[2], np.array(additional_frame[:2]))
        is_valid = not(self.MAP.is_collision(rand_xy_global))
        return is_valid, [rand_xy_global[0], rand_xy_global[1], rand_ang + frame_theta]

    def get_init_pose_random(self,
                            lin_dist_range_a2b=METADATA['lin_dist_range_a2b'],
                            ang_dist_range_a2b=METADATA['ang_dist_range_a2b'],
                            lin_dist_range_b2t=METADATA['lin_dist_range_b2t'],
                            ang_dist_range_b2t=METADATA['ang_dist_range_b2t'],
                            blocked=None,
                            **kwargs):
        if blocked is None and self.MAP.map is not None:
            if np.random.rand() < 0.5:
                blocked = True
            else:
                blocked = False

        is_agent_valid = False
        while(not is_agent_valid):
            init_pose = {}
            if self.MAP.map is None:
                blocked = False
                a_init = self.agent_init_pos[:2]
                is_agent_valid = True
            else:
                while(not is_agent_valid):
                    a_init = np.random.random((2,)) * (self.MAP.mapmax-self.MAP.mapmin) + self.MAP.mapmin
                    is_agent_valid = not(self.MAP.is_collision(a_init))

            init_pose['agent'] = [a_init[0], a_init[1], np.random.random() * 2 * np.pi - np.pi]
            init_pose['targets'], init_pose['belief_targets'] = [], []
            for i in range(self.num_targets):
                count, is_belief_valid = 0, False
                while(not is_belief_valid):
                    is_belief_valid, init_pose_belief = self.gen_rand_pose(
                        init_pose['agent'][:2], init_pose['agent'][2],
                        lin_dist_range_a2b[0], lin_dist_range_a2b[1],
                        ang_dist_range_a2b[0], ang_dist_range_a2b[1])
                    is_blocked = self.MAP.is_blocked(init_pose['agent'][:2], init_pose_belief[:2])
                    if is_belief_valid:
                        is_belief_valid = (blocked == is_blocked)
                    count += 1
                    if count > 100:
                        is_agent_valid = False
                        break
                init_pose['belief_targets'].append(init_pose_belief)

                count, is_target_valid, init_pose_target = 0, False, np.zeros((2,))
                while((not is_target_valid) and is_belief_valid):
                    is_target_valid, init_pose_target = self.gen_rand_pose(
                        init_pose['belief_targets'][i][:2],
                        init_pose['belief_targets'][i][2],
                        lin_dist_range_b2t[0], lin_dist_range_b2t[1],
                        ang_dist_range_b2t[0], ang_dist_range_b2t[1])
                    is_blocked = self.MAP.is_blocked(init_pose['agent'][:2], init_pose_target[:2])
                    if is_target_valid:
                        is_target_valid = (blocked == is_blocked)
                    count += 1
                    if count > 100:
                        is_agent_valid = False
                        break
                init_pose['targets'].append(init_pose_target)
        return init_pose

    def add_history_to_state(self, state, num_target_dep_vars, num_target_indep_vars, logdetcov_idx):
        """
        Replacing the current logetcov value to a sequence of the recent few
        logdetcov values for each target.
        It uses fixed values for :
            1) the number of target dependent variables
            2) current logdetcov index at each target dependent vector
            3) the number of target independent variables
        """
        new_state = []
        for i in range(self.num_targets):
            self.logdetcov_history[i].add(state[num_target_dep_vars*i+logdetcov_idx])
            new_state = np.concatenate((new_state, state[num_target_dep_vars*i: num_target_dep_vars*i+logdetcov_idx]))
            new_state = np.concatenate((new_state, self.logdetcov_history[i].get_values()))
            new_state = np.concatenate((new_state, state[num_target_dep_vars*i+logdetcov_idx+1:num_target_dep_vars*(i+1)]))
        new_state = np.concatenate((new_state, state[-num_target_indep_vars:]))
        return new_state

    def set_target_path(self, target_path):
        targets = [Agent2DFixedPath(dim=self.target_dim, sampling_period=self.sampling_period,
                                limit=self.limit['target'],
                                collision_func=lambda x: self.MAP.is_collision(x),
                                path=target_path[i]) for i in range(self.num_targets)]
        self.targets = targets

    def reset(self, **kwargs):
        self.state = []
        init_pose = self.get_init_pose(**kwargs)
        self.agent.reset(init_pose['agent'])
        for i in range(self.num_targets):
            self.belief_targets[i].reset(
                        init_state=init_pose['belief_targets'][i][:self.target_dim],
                        init_cov=self.target_init_cov)
            self.targets[i].reset(np.array(init_pose['targets'][i][:self.target_dim]))
            r, alpha = util.relative_distance_polar(self.belief_targets[i].state[:2],
                                                xy_base=self.agent.state[:2],
                                                theta_base=self.agent.state[2])
            logdetcov = np.log(LA.det(self.belief_targets[i].cov))
            self.state.extend([r, alpha, logdetcov, 0.0])

        self.state.extend([self.sensor_r, np.pi])
        self.state = np.array(self.state)
        return self.state

    def observation(self, target):
        r, alpha = util.relative_distance_polar(target.state[:2],
                                            xy_base=self.agent.state[:2],
                                            theta_base=self.agent.state[2])
        observed = (r <= self.sensor_r) \
                    & (abs(alpha) <= self.fov/2/180*np.pi) \
                    & (not(self.MAP.is_blocked(self.agent.state, target.state)))
        z = None
        if observed:
            z = np.array([r, alpha])
            z += np.random.multivariate_normal(np.zeros(2,), self.observation_noise(z))
        return observed, z

    def observation_noise(self, z):
        obs_noise_cov = np.array([[self.sensor_r_sd * self.sensor_r_sd, 0.0],
                                [0.0, self.sensor_b_sd * self.sensor_b_sd]])
        return obs_noise_cov

    def get_reward(self, is_training=True, **kwargs):
        return reward_fun_1(self.belief_targets, is_training=is_training, **kwargs)

    def step(self, action):
        action_vw = self.action_map[action]
        is_col = self.agent.update(action_vw, [t.state[:2] for t in self.targets])
        obstacles_pt = self.MAP.get_closest_obstacle(self.agent.state)
        observed = []
        for i in range(self.num_targets):
            self.targets[i].update(self.agent.state[:2])
            # Observe
            obs = self.observation(self.targets[i])
            observed.append(obs[0])
            self.belief_targets[i].predict() # Belief state at t+1
            if obs[0]: # if observed, update the target belief.
                self.belief_targets[i].update(obs[1], self.agent.state)

        reward, done, mean_nlogdetcov = self.get_reward(self.is_training,
                                                                is_col=is_col)
        self.state = []
        if obstacles_pt is None:
            obstacles_pt = (self.sensor_r, np.pi)
        for i in range(self.num_targets):
            r_b, alpha_b = util.relative_distance_polar(self.belief_targets[i].state[:2],
                                                xy_base=self.agent.state[:2],
                                                theta_base=self.agent.state[2])
            self.state.extend([r_b, alpha_b,
                                    np.log(LA.det(self.belief_targets[i].cov)), float(observed[i])])
        self.state.extend([obstacles_pt[0], obstacles_pt[1]])
        self.state = np.array(self.state)
        return self.state, reward, done, {'mean_nlogdetcov': mean_nlogdetcov}

class TargetTrackingEnv1(TargetTrackingEnv0):
    def __init__(self, num_targets=1, map_name='empty', is_training=True, known_noise=True, **kwargs):
        TargetTrackingEnv0.__init__(self, num_targets=num_targets, map_name=map_name,
            is_training=is_training, known_noise=known_noise, **kwargs)
        self.id = 'TargetTracking-v1'
        self.target_dim = 4
        self.target_init_vel = np.array(METADATA['target_init_vel'])

        # LIMIT
        self.limit = {} # 0: low, 1:highs
        self.limit['agent'] = [np.concatenate((self.MAP.mapmin,[-np.pi])), np.concatenate((self.MAP.mapmax, [np.pi]))]
        self.limit['target'] = [np.concatenate((self.MAP.mapmin,[-METADATA['target_speed_limit'], -METADATA['target_speed_limit']])),
                                np.concatenate((self.MAP.mapmax, [METADATA['target_speed_limit'], METADATA['target_speed_limit']]))]
        rel_speed_limit = METADATA['target_speed_limit'] + METADATA['action_v'][0] # Maximum relative speed
        self.limit['state'] = [np.concatenate(([0.0, -np.pi, -rel_speed_limit, -10*np.pi, -50.0, 0.0]*num_targets, [0.0, -np.pi])),
                               np.concatenate(([600.0, np.pi, rel_speed_limit, 10*np.pi,  50.0, 2.0]*num_targets, [self.sensor_r, np.pi]))]
        self.observation_space = spaces.Box(self.limit['state'][0], self.limit['state'][1], dtype=np.float32)
        self.targetA = np.concatenate((np.concatenate((np.eye(2), self.sampling_period*np.eye(2)), axis=1),
                                        [[0,0,1,0],[0,0,0,1]]))
        self.target_noise_cov = METADATA['const_q'] * np.concatenate((
                            np.concatenate((self.sampling_period**3/3*np.eye(2), self.sampling_period**2/2*np.eye(2)), axis=1),
                        np.concatenate((self.sampling_period**2/2*np.eye(2), self.sampling_period*np.eye(2)),axis=1) ))
        if known_noise:
            self.target_true_noise_sd = self.target_noise_cov
        else:
            self.target_true_noise_sd = METADATA['const_q_true'] * np.concatenate((
                        np.concatenate((self.sampling_period**2/2*np.eye(2), self.sampling_period/2*np.eye(2)), axis=1),
                        np.concatenate((self.sampling_period/2*np.eye(2), self.sampling_period*np.eye(2)),axis=1) ))
        # Build a robot
        self.agent = AgentSE2(3, self.sampling_period, self.limit['agent'],
                            lambda x: self.MAP.is_collision(x))
        # Build a target
        self.targets = [AgentDoubleInt2D_Nonlinear(self.target_dim, self.sampling_period, self.limit['target'],
                            lambda x: self.MAP.is_collision(x),
                            W=self.target_true_noise_sd, A=self.targetA,
                            obs_check_func=lambda x: self.MAP.get_closest_obstacle(
                                x, fov=2*np.pi, r_max=10e2, update_visit_freq=False)) for _ in range(num_targets)]
        self.belief_targets = [KFbelief(dim=self.target_dim, limit=self.limit['target'], A=self.targetA,
                            W=self.target_noise_cov, obs_noise_func=self.observation_noise,
                            collision_func=lambda x: self.MAP.is_collision(x))
                            for _ in range(num_targets)]

    def reset(self, **kwargs):
        if 'const_q' in kwargs and 'target_speed_limit' in kwargs:
            self.set_targets(target_speed_limit=kwargs['target_speed_limit'], const_q=kwargs['const_q'])
        self.has_discovered = [0] * self.num_targets
        self.state = []
        self.num_collisions = 0

        # Reset the agent, targets, and beliefs with sampled initial positions.
        init_pose = self.get_init_pose(**kwargs)
        self.agent.reset(init_pose['agent'])
        for i in range(self.num_targets):
            self.belief_targets[i].reset(
                        init_state=np.concatenate((init_pose['belief_targets'][i][:2], np.zeros(2))),
                        init_cov=self.target_init_cov)
            self.targets[i].reset(np.concatenate((init_pose['targets'][i][:2], self.target_init_vel)))

        # The targets are observed by the agent (z_0) and the beliefs are updated (b_0).
        observed = self.observe_and_update_belief()

        # Predict the target for the next step, b_1|0.
        self.belief_targets[i].predict()

        # Compute the RL state.
        self.state_func([0.0, 0.0], observed)

        return self.state

    def step(self, action):
        # The agent performs an action (t -> t+1)
        action_vw = self.action_map[action]
        is_col = self.agent.update(action_vw, [t.state[:2] for t in self.targets])
        self.num_collisions += int(is_col)

        # The targets move (t -> t+1)
        for i in range(self.num_targets):
            if self.has_discovered[i]:
                self.targets[i].update(self.agent.state[:2])

        # The targets are observed by the agent (z_t+1) and the beliefs are updated.
        observed = self.observe_and_update_belief()

        # Compute a reward from b_t+1|t+1 or b_t+1|t.
        reward, done, mean_nlogdetcov = self.get_reward(self.is_training,
                                                                is_col=is_col)
        # Predict the target for the next step, b_t+2|t+1
        self.belief_targets[i].predict()

        # Compute the RL state.
        self.state_func(action_vw, observed)

        return self.state, reward, done, {'mean_nlogdetcov': mean_nlogdetcov}

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
            self.state.extend([r_b, alpha_b, r_dot_b, alpha_dot_b,
                                    np.log(LA.det(self.belief_targets[i].cov)),
                                    float(observed[i])])
        self.state.extend([obstacles_pt[0], obstacles_pt[1]])
        self.state = np.array(self.state)

    def observe_and_update_belief(self):
        observed = []
        for i in range(self.num_targets):
            observation = self.observation(self.targets[i])
            observed.append(observation[0])
            if observation[0]: # if observed, update the target belief.
                self.belief_targets[i].update(observation[1], self.agent.state)
                if not(self.has_discovered[i]):
                    self.has_discovered[i] = 1
        return observed

    def set_targets(self, target_speed_limit=None, const_q=None, known_noise=True, **kwargs):
        if target_speed_limit is None:
            self.target_speed_limit = np.random.choice([1.0, 3.0])
        else:
            self.target_speed_limit = target_speed_limit

        if const_q is None:
            self.const_q = np.random.choice([0.001, 0.1, 1.0])
        else:
            self.const_q = const_q

        self.limit['target'] = [np.concatenate((self.MAP.mapmin,[-self.target_speed_limit, -self.target_speed_limit])),
                                np.concatenate((self.MAP.mapmax, [self.target_speed_limit, self.target_speed_limit]))]
        rel_speed_limit = self.target_speed_limit + METADATA['action_v'][0] # Maximum relative speed
        self.limit['state'] = [np.concatenate(([0.0, -np.pi, -rel_speed_limit, -10*np.pi, -50.0, 0.0]*self.num_targets, [0.0, -np.pi])),
                               np.concatenate(([600.0, np.pi, rel_speed_limit, 10*np.pi,  50.0, 2.0]*self.num_targets, [self.sensor_r, np.pi]))]
        # Build targets
        self.targetA = np.concatenate((np.concatenate((np.eye(2), self.sampling_period*np.eye(2)), axis=1),
                                        [[0,0,1,0],[0,0,0,1]]))
        self.target_noise_cov = self.const_q * np.concatenate((
                            np.concatenate((self.sampling_period**3/3*np.eye(2), self.sampling_period**2/2*np.eye(2)), axis=1),
                        np.concatenate((self.sampling_period**2/2*np.eye(2), self.sampling_period*np.eye(2)),axis=1) ))
        if known_noise:
            self.target_true_noise_sd = self.target_noise_cov
        else:
            self.target_true_noise_sd = self.const_q_true * np.concatenate((
                        np.concatenate((self.sampling_period**2/2*np.eye(2), self.sampling_period/2*np.eye(2)), axis=1),
                        np.concatenate((self.sampling_period/2*np.eye(2), self.sampling_period*np.eye(2)),axis=1) ))

        self.targets = [AgentDoubleInt2D_Nonlinear(self.target_dim, self.sampling_period, self.limit['target'],
                            lambda x: self.MAP.is_collision(x),
                            W=self.target_true_noise_sd, A=self.targetA,
                            obs_check_func=lambda x: self.MAP.get_closest_obstacle(
                                x, fov=2*np.pi, r_max=10e2, update_visit_freq=False)) for _ in range(self.num_targets)]
        self.belief_targets = [KFbelief(dim=self.target_dim, limit=self.limit['target'], A=self.targetA,
                            W=self.target_noise_cov, obs_noise_func=self.observation_noise,
                            collision_func=lambda x: self.MAP.is_collision(x))
                            for _ in range(self.num_targets)]

class TargetTrackingEnv2(TargetTrackingEnv1):
    def __init__(self, num_targets=1, map_name='empty', is_training=True,
                known_noise=True, target_path_dir=None, **kwargs):
        """
        A predefined path for each target must be provided under the target_path_dir.
        Each path_i file for i=target_num is a T by 4 matrix where T is the
        number of time steps in a trajectory (or per episode). Each row consists
        of (x, y, xdot, ydot).
        """
        if target_path_dir is None:
            raise ValueError('No path directory for targets is provided.')
        TargetTrackingEnv1.__init__(self, num_targets=num_targets,
            map_name=map_name, is_training=is_training, known_noise=known_noise, **kwargs)
        self.id = 'TargetTracking-v2'
        self.targets = [Agent2DFixedPath(dim=self.target_dim, sampling_period=self.sampling_period,
                                limit=self.limit['target'],
                                collision_func=lambda x: self.MAP.is_collision(x),
                                path=np.load(os.path.join(target_path_dir, "path_%d.npy"%(i+1)))) for i in range(self.num_targets)]
    def reset(self, **kwargs):
        self.state = []
        if self.MAP.map is None:
            a_init = self.agent_init_pos[:2]
            self.agent.reset(self.agent_init_pos)
        else:
            isvalid = False
            while(not isvalid):
                a_init = np.random.random((2,)) * (self.MAP.mapmax-self.MAP.mapmin) + self.MAP.mapmin
                isvalid = not(self.MAP.is_collision(a_init))
            self.agent.reset([a_init[0], a_init[1], np.random.random()*2*np.pi-np.pi])
        for i in range(self.num_targets):
            t_init = np.load("path_sh_%d.npy"%(i+1))[0][:2]
            self.belief_targets[i].reset(init_state=np.concatenate((t_init + METADATA['init_distance_belief'] * (np.random.rand(2)-0.5), np.zeros(2))), init_cov=self.target_init_cov)
            self.targets[i].reset(np.concatenate((t_init, self.target_init_vel)))
            r, alpha = util.relative_distance_polar(self.belief_targets[i].state[:2],
                                                xy_base=self.agent.state[:2],
                                                theta_base=self.agent.state[2])
            logdetcov = np.log(LA.det(self.belief_targets[i].cov))
            self.state.extend([r, alpha, 0.0, 0.0, logdetcov, 0.0])
        self.state.extend([self.sensor_r, np.pi])
        self.state = np.array(self.state)
        return self.state


class TargetTrackingEnv3(TargetTrackingEnv0):
    def __init__(self, num_targets=1, map_name='empty', is_training=True, known_noise=True, **kwargs):
        TargetTrackingEnv0.__init__(self, num_targets=num_targets,
            map_name=map_name, is_training=is_training, known_noise=known_noise, **kwargs)
        self.id = 'TargetTracking-v3'
        self.target_dim = 3

        # LIMIT
        self.limit = {} # 0: low, 1:highs
        self.limit['agent'] = [np.concatenate((self.MAP.mapmin,[-np.pi])), np.concatenate((self.MAP.mapmax, [np.pi]))]
        self.limit['target'] = [np.concatenate((self.MAP.mapmin, [-np.pi])), np.concatenate((self.MAP.mapmax, [np.pi]))]
        self.limit['state'] = [np.concatenate(([0.0, -np.pi, -50.0, 0.0]*num_targets, [0.0, -np.pi ])),
                               np.concatenate(([600.0, np.pi, 50.0, 2.0]*num_targets, [self.sensor_r, np.pi]))]
        self.observation_space = spaces.Box(self.limit['state'][0], self.limit['state'][1], dtype=np.float32)
        self.target_noise_cov = METADATA['const_q'] * self.sampling_period * np.eye(self.target_dim)
        if known_noise:
            self.target_true_noise_sd = self.target_noise_cov
        else:
            self.target_true_noise_sd = METADATA['const_q_true'] * \
                                self.sampling_period * np.eye(self.target_dim)
        # Build a robot
        self.agent = AgentSE2(3, self.sampling_period, self.limit['agent'],
                            lambda x: self.MAP.is_collision(x))
        # Build a target
        self.targets = [AgentSE2(self.target_dim, self.sampling_period, self.limit['target'],
                            lambda x: self.MAP.is_collision(x),
                            policy=SinePolicy(0.1, 0.5, 5.0, self.sampling_period)) for _ in range(num_targets)]
        # SinePolicy(0.5, 0.5, 2.0, self.sampling_period)
        # CirclePolicy(self.sampling_period, self.MAP.origin, 3.0)
        # RandomPolicy()

        self.belief_targets = [UKFbelief(dim=self.target_dim, limit=self.limit['target'], fx=SE2Dynamics,
                            W=self.target_noise_cov, obs_noise_func=self.observation_noise,
                            collision_func=lambda x: self.MAP.is_collision(x))
                            for _ in range(num_targets)]

    def step(self, action):
        action_vw = self.action_map[action]
        is_col = self.agent.update(action_vw, [t.state[:2] for t in self.targets])
        obstacles_pt = self.MAP.get_closest_obstacle(self.agent.state)
        observed = []
        for i in range(self.num_targets):
            self.targets[i].update()

            # Observe
            obs = self.observation(self.targets[i])
            observed.append(obs[0])
            # Update the belief of the agent on the target using UKF
            self.belief_targets[i].update(obs[0], obs[1], self.agent.state,
                                        np.array([np.random.random(),
                                        np.pi*np.random.random()-0.5*np.pi]))

        reward, done, mean_nlogdetcov = self.get_reward(self.is_training, is_col=is_col)
        self.state = []
        if obstacles_pt is None:
            obstacles_pt = (self.sensor_r, np.pi)
        for i in range(self.num_targets):
            r_b, alpha_b = util.relative_distance_polar(self.belief_targets[i].state[:2],
                                                xy_base=self.agent.state[:2],
                                                theta_base=self.agent.state[2])
            self.state.extend([r_b, alpha_b,
                                np.log(LA.det(self.belief_targets[i].cov)), float(observed[i])])
        self.state.extend([obstacles_pt[0], obstacles_pt[1]])
        self.state = np.array(self.state)
        return self.state, reward, done, {'mean_nlogdetcov': mean_nlogdetcov}

class TargetTrackingEnv4(TargetTrackingEnv0):
    def __init__(self, num_targets=1, map_name='empty', is_training=True, known_noise=True, **kwargs):
        TargetTrackingEnv0.__init__(self, num_targets=num_targets,
            map_name=map_name, is_training=is_training, known_noise=known_noise, **kwargs)
        self.id = 'TargetTracking-v4'
        self.target_dim = 5
        self.target_init_vel = np.array(METADATA['target_init_vel'])

        # LIMIT
        self.limit = {} # 0: low, 1:highs
        rel_speed_limit = METADATA['target_speed_limit'] + METADATA['action_v'][0] # Maximum relative speed
        self.limit['agent'] = [np.concatenate((self.MAP.mapmin,[-np.pi])), np.concatenate((self.MAP.mapmax, [np.pi]))]
        self.limit['target'] = [np.concatenate((self.MAP.mapmin, [-np.pi, -METADATA['target_speed_limit'], -np.pi])),
                                            np.concatenate((self.MAP.mapmax, [np.pi, METADATA['target_speed_limit'], np.pi]))]
        self.limit['state'] = [np.concatenate(([0.0, -np.pi, -rel_speed_limit, -10*np.pi, -50.0, 0.0]*num_targets, [0.0, -np.pi ])),
                               np.concatenate(([600.0, np.pi, rel_speed_limit, 10*np.pi, 50.0, 2.0]*num_targets, [self.sensor_r, np.pi]))]
        self.observation_space = spaces.Box(self.limit['state'][0], self.limit['state'][1], dtype=np.float32)
        self.target_noise_cov = np.zeros((self.target_dim, self.target_dim))
        for i in range(3):
            self.target_noise_cov[i,i] = METADATA['const_q'] * self.sampling_period**3/3
        self.target_noise_cov[3:, 3:] = METADATA['const_q'] * \
                    np.array([[self.sampling_period, self.sampling_period**2/2],
                             [self.sampling_period**2/2, self.sampling_period]])
        if known_noise:
            self.target_true_noise_sd = self.target_noise_cov
        else:
            self.target_true_noise_sd = METADATA['const_q_true'] * \
                                  self.sampling_period * np.eye(self.target_dim)
        # Build a robot
        self.agent = AgentSE2(3, self.sampling_period, self.limit['agent'],
                            lambda x: self.MAP.is_collision(x))
        # Build a target
        self.targets = [AgentSE2(self.target_dim, self.sampling_period, self.limit['target'],
                            lambda x: self.MAP.is_collision(x),
                            policy=ConstantPolicy(self.target_noise_cov[3:, 3:])) for _ in range(num_targets)]
        # SinePolicy(0.5, 0.5, 2.0, self.sampling_period)
        # CirclePolicy(self.sampling_period, self.MAP.origin, 3.0)
        # RandomPolicy()

        self.belief_targets = [UKFbelief(dim=self.target_dim, limit=self.limit['target'], fx=SE2DynamicsVel,
                            W=self.target_noise_cov, obs_noise_func=self.observation_noise,
                            collision_func=lambda x: self.MAP.is_collision(x))
                            for _ in range(num_targets)]

    def reset(self, **kwargs):
        self.state = []
        init_pose = self.get_init_pose(**kwargs)
        self.agent.reset(init_pose['agent'])
        for i in range(self.num_targets):
            self.belief_targets[i].reset(
                        init_state=np.concatenate((init_pose['belief_targets'][i], np.zeros(2))),
                        init_cov=self.target_init_cov)
            t_init = np.concatenate((init_pose['targets'][i], [self.target_init_vel[0], 0.0]))
            self.targets[i].reset(t_init)
            self.targets[i].policy.reset(t_init)
            r, alpha = util.relative_distance_polar(self.belief_targets[i].state[:2],
                                                xy_base=self.agent.state[:2],
                                                theta_base=self.agent.state[2])
            logdetcov = np.log(LA.det(self.belief_targets[i].cov))
            self.state.extend([r, alpha, 0.0, 0.0, logdetcov, 0.0])
        self.state.extend([self.sensor_r, np.pi])
        self.state = np.array(self.state)
        return self.state

    def step(self, action):
        action_vw = self.action_map[action]
        is_col = self.agent.update(action_vw, [t.state[:2] for t in self.targets])
        obstacles_pt = self.MAP.get_closest_obstacle(self.agent.state)
        observed = []
        for i in range(self.num_targets):
            self.targets[i].update()
            # Observe
            obs = self.observation(self.targets[i])
            observed.append(obs[0])
            # Update the belief of the agent on the target using UKF
            self.belief_targets[i].update(obs[0], obs[1], self.agent.state,
             np.array([np.random.random(), np.pi*np.random.random()-0.5*np.pi]))

        reward, done, mean_nlogdetcov = self.get_reward(self.is_training, is_col=is_col)
        self.state = []
        if obstacles_pt is None:
            obstacles_pt = (self.sensor_r, np.pi)
        for i in range(self.num_targets):
            r_b, alpha_b = util.relative_distance_polar(self.belief_targets[i].state[:2],
                                                xy_base=self.agent.state[:2],
                                                theta_base=self.agent.state[2])
            r_dot_b, alpha_dot_b = util.relative_velocity_polar_se2(
                                    self.belief_targets[i].state[:3],
                                    self.belief_targets[i].state[3:],
                                    self.agent.state, action_vw)
            self.state.extend([r_b, alpha_b, r_dot_b, alpha_dot_b,
                                    np.log(LA.det(self.belief_targets[i].cov)), float(observed[i])])
        self.state.extend([obstacles_pt[0], obstacles_pt[1]])
        self.state = np.array(self.state)
        return self.state, reward, done, {'mean_nlogdetcov': mean_nlogdetcov}

def reward_fun_0(belief_targets, obstacles_pt, observed, is_training=True,
        c_mean=0.1, c_std=0.1, c_observed=0.1, c_penalty=1.0):

    # Penalty when it is closed to an obstacle.
    if obstacles_pt is None:
        penalty = 0.0
    else:
        penalty =  METADATA['margin2wall']**2 * \
                        1./max(METADATA['margin2wall']**2, obstacles_pt[0]**2)

    detcov = [LA.det(b_target.cov) for b_target in belief_targets]
    r_detcov_mean = - np.mean(np.log(detcov))
    r_detcov_std = - np.std(np.log(detcov))
    r_observed = np.mean(observed)
    # reward = - c_penalty * penalty + c_mean * r_detcov_mean + \
    #              c_std * r_detcov_std + c_observed * r_observed
    if sum(observed) == 0:
        reward = - c_penalty * penalty + c_mean * r_detcov_mean + \
                     c_std * r_detcov_std
    else:
        reward = - c_penalty * penalty + c_mean * r_detcov_mean + \
                     c_std * r_detcov_std
        reward = max(0.0, reward) + c_observed * r_observed

    mean_nlogdetcov = None
    if not(is_training):
        logdetcov = [np.log(LA.det(b_target.cov)) for b_target in belief_targets]
        mean_nlogdetcov = -np.mean(logdetcov)
    return reward, False, mean_nlogdetcov

def reward_fun(belief_targets, obstacles_pt, is_training=True, c_mean=0.1):

    detcov = [LA.det(b_target.cov) for b_target in belief_targets]
    r_detcov_mean = - np.mean(np.log(detcov))
    reward = c_mean * r_detcov_mean

    mean_nlogdetcov = None
    if not(is_training):
        logdetcov = [np.log(LA.det(b_target.cov)) for b_target in belief_targets]
        mean_nlogdetcov = -np.mean(logdetcov)
    return reward, False, mean_nlogdetcov

def reward_fun_1(belief_targets, is_col, is_training=True, c_mean=0.1, c_penalty=1.0):
    detcov = [LA.det(b_target.cov) for b_target in belief_targets]
    r_detcov_mean = - np.mean(np.log(detcov))
    reward = c_mean * r_detcov_mean
    if is_col :
        reward = min(0.0, reward) - c_penalty * 1.0

    mean_nlogdetcov = None
    if not(is_training):
        logdetcov = [np.log(LA.det(b_target.cov)) for b_target in belief_targets]
        mean_nlogdetcov = -np.mean(logdetcov)
    return reward, False, mean_nlogdetcov
