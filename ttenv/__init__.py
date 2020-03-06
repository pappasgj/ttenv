from gym import wrappers
from ttenv import target_tracking, target_imtracking, target_seq_tracking

def make(env_name, render=False, figID=0, record=False, ros=False, directory='',
                                        T_steps=None, num_targets=1, **kwargs):
    """
    env_name : str
        name of an environment. (e.g. 'Cartpole-v0')
    type : str
        type of an environment. One of ['atari', 'classic_control',
        'classic_mdp','target_tracking']
    """
    if T_steps is None:
        if num_targets > 1:
            T_steps = 150
        else:
            T_steps = 100
    local_view = 0
    if env_name == 'TargetTracking-v0':
        env0 = target_tracking.TargetTrackingEnv0(num_targets=num_targets, **kwargs)
    elif env_name == 'TargetTracking-v1':
        env0 = target_tracking.TargetTrackingEnv1(num_targets=num_targets, **kwargs)
    elif env_name == 'TargetTracking-v2':
        env0 = target_tracking.TargetTrackingEnv2(num_targets=num_targets, **kwargs)
    elif env_name == 'TargetTracking-v3':
        env0 = target_tracking.TargetTrackingEnv3(num_targets=num_targets, **kwargs)
    elif env_name == 'TargetTracking-v4':
        env0 = target_tracking.TargetTrackingEnv4(num_targets=num_targets, **kwargs)
    elif env_name == 'TargetTracking-v5':
        local_view = 1
        env0 = target_imtracking.TargetTrackingEnv5(num_targets=num_targets, **kwargs)
    elif env_name == 'TargetTracking-v6':
        local_view = 1
        env0 = target_imtracking.TargetTrackingEnv6(num_targets=num_targets, **kwargs)
    elif env_name == 'TargetTracking-v7':
        local_view = 5
        env0 = target_imtracking.TargetTrackingEnv7(num_targets=num_targets, **kwargs)
    elif env_name == 'TargetTracking-v8':
        local_view = 5
        env0 = target_imtracking.TargetTrackingEnv8(num_targets=num_targets, **kwargs)
    elif env_name == 'TargetTracking-v9':
        local_view = 5
        env0 = target_imtracking.TargetTrackingEnv9(num_targets=num_targets, **kwargs)
    elif env_name == 'TargetTracking-v1_SEQ':
        env0 = target_seq_tracking.TargetTrackingEnv1_SEQ(num_targets=num_targets, **kwargs)
    elif env_name == 'TargetTracking-v5_SEQ':
        local_view = 1
        env0 = target_seq_tracking.TargetTrackingEnv5_SEQ(num_targets=num_targets, **kwargs)
    elif env_name == 'TargetTracking-v7_SEQ':
        local_view = 5
        env0 = target_seq_tracking.TargetTrackingEnv7_SEQ(num_targets=num_targets, **kwargs)
    elif env_name == 'TargetTracking-v8_SEQ':
        local_view = 5
        env0 = target_seq_tracking.TargetTrackingEnv8_SEQ(num_targets=num_targets, **kwargs)
    elif env_name == 'TargetTracking-info1':
        from ttenv.infoplanner_python.target_tracking_infoplanner import TargetTrackingInfoPlanner1
        env0 = TargetTrackingInfoPlanner1(num_targets=num_targets, **kwargs)
    else:
        raise ValueError('No such environment exists.')

    env = wrappers.TimeLimit(env0, max_episode_steps=T_steps)
    if ros:
        from ttenv.ros_wrapper import Ros
        env = Ros(env)
    if render:
        from ttenv.display_wrapper import Display2D
        env = Display2D(env, figID=figID, local_view=local_view)
    if record:
        from ttenv.display_wrapper import Video2D
        env = Video2D(env, dirname = directory, local_view=local_view)

    return env
