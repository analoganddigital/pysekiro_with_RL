import os

import cv2
import matplotlib.pyplot as plt
import numpy as np
np.warnings.filterwarnings('ignore', category=np.VisibleDeprecationWarning)
import pandas as pd

from pysekiro.actions import act
from pysekiro.get_vertices import roi
from pysekiro.model import MODEL

# ---*---

ROI_WIDTH   = 50
ROI_HEIGHT  = 50
FRAME_COUNT = 3

x   = 140
x_w = 340
y   = 30
y_h = 230

n_action = 5

# ---*---

# 约束状态值的上下限，防止异常值和特殊值的影响。
def limit(value, lm1, lm2):

    if value < lm1:
        return lm1
    elif value > lm2:
        return lm2
    else:
        return value

# one_hot = lambda x:[(1 if i == x else 0) for i in range(n_action)]

# ---*---

class RewardSystem:
    def __init__(self):
        self.past_status = [152, 0, 100, 0]

        # 记录积累reward过程
        self.current_cumulative_reward = 0
        self.reward_history = list()

    def get_reward(self, status):
        if sum(status) != 0:

            self.status = status

            # 每个状态的计算方法：(现在的状态 - 过去的状态) * 正负强化权重，然后约束上下限
            s1 = limit((self.status[0] - self.past_status[0]) *  1, -152, +76)    # 自身生命
            s2 = limit((self.status[1] - self.past_status[1]) * -1, -10, +10)    # 自身架势
            t1 = limit((self.status[2] - self.past_status[2]) * -1, -20,   0)    # 目标生命
            t2 = limit((self.status[3] - self.past_status[3]) *  1, -10, +10)    # 目标架势

            reward = 0.2 * (s1 + t1) + 0.8 * (s2 + t2)
            # print(f'  s1:{s1:>4}, s2:{s2:>4}, t1:{t1:>4}, t2:{t2:>4}, reward:{reward:>4}')

            self.past_status = self.status

            self.current_cumulative_reward += reward
            self.reward_history.append(self.current_cumulative_reward)
        else:
            reward = 0

        return reward

    def save_reward_curve(self, save_path='reward.png'):
        plt.rcParams['figure.figsize'] = 150, 15
        plt.plot(np.arange(len(self.reward_history)), self.reward_history)
        plt.ylabel('reward')
        plt.xlabel('training steps')
        plt.savefig(save_path)

# ---*---

class DQNReplayer:
    def __init__(self, capacity):
        self.memory = pd.DataFrame(
            index=range(capacity),
            columns=['screen', 'action', 'reward', 'next_screen']
        )
        self.i = 0
        self.count = 0
        self.capacity = capacity

    def store(self, *args):
        self.memory.loc[self.i] = args
        self.i = (self.i + 1) % self.capacity
        self.count = min(self.count + 1, self.capacity)

    def sample(self, size):
        indices = np.random.choice(self.count, size=size)
        return (np.stack(self.memory.loc[indices, field]) for field in self.memory.columns)

# ---*---

class Sekiro_Agent:
    def __init__(
        self,
        n_action = n_action, 
        gamma = 0.99,
        batch_size = 128,
        replay_memory_size = 20000,
        epsilon = 1.0,
        epsilon_decrease_rate = 0.999,
        update_freq = 100,
        target_network_update_freq = 300,
        model_weights = None,
        save_path = None
    ):
        self.n_action = n_action    # 动作数量
        
        self.gamma = gamma    # 奖励衰减

        self.batch_size = batch_size                    # 样本抽取数量
        self.replay_memory_size = replay_memory_size    # 记忆容量

        self.epsilon = epsilon                                # 探索参数
        self.epsilon_decrease_rate = epsilon_decrease_rate    # 探索衰减率

        self.update_freq = update_freq    # 训练评估网络的频率
        self.target_network_update_freq = target_network_update_freq    # 更新目标网络的频率

        self.model_weights = model_weights    # 指定读取的模型参数的路径
        self.save_path = save_path            # 指定模型权重保存的路径
        if not self.save_path:
            self.save_path = 'tmp_weights.h5'
        
        self.evaluate_net = self.build_network()    # 评估网络
        self.target_net = self.build_network()      # 目标网络
        self.reward_system = RewardSystem()                     # 奖惩系统
        self.replayer = DQNReplayer(self.replay_memory_size)    # 经验回放

        self.step = 0    # 计步

    # 评估网络和目标网络的构建方法
    def build_network(self):
        model = MODEL(ROI_WIDTH, ROI_HEIGHT, FRAME_COUNT,
            outputs = self.n_action,
            model_weights = self.model_weights
        )
        return model

    # 行为选择与执行方法
    def choose_action(self, screen, train):
        if train:
            r = np.random.rand()
        else:
            r = 1.01    # 永远大于 self.epsilon
        
        # train = True 开启探索模式
        if r < self.epsilon:
            self.epsilon *= self.epsilon_decrease_rate    # 逐渐减小探索参数, 降低行为的随机性
            action = np.random.randint(self.n_action)
        
        # train = False 直接进入这里
        else:
            screen = cv2.resize(roi(screen, x, x_w, y, y_h), (ROI_WIDTH, ROI_HEIGHT)).reshape(-1, ROI_WIDTH, ROI_HEIGHT, FRAME_COUNT)
            q_values = self.evaluate_net.predict(screen)[0]
            action = np.argmax(q_values)
        
        # 执行动作
        act(action)
        
        return action

    # 学习方法
    def learn(self):

        if self.step >= self.batch_size and self.step % self.update_freq == 0:    # 更新评估网络

            if self.step % self.target_network_update_freq == 0:    # 更新目标网络
                print(f'\n step:{self.step:>4}, current_cumulative_reward:{self.reward_system.current_cumulative_reward:>5.3f}, memory:{self.replayer.count:7>} \n')
                self.update_target_network() 

            # 经验回放
            screens, actions, rewards, next_screens = self.replayer.sample(self.batch_size)

            screens = screens.reshape(-1, ROI_WIDTH, ROI_HEIGHT, FRAME_COUNT)
            next_screens = next_screens.reshape(-1, ROI_WIDTH, ROI_HEIGHT, FRAME_COUNT)

            # 计算回报的估计值
            q_next = self.target_net.predict(next_screens)
            q_target = self.evaluate_net.predict(screens)
            q_target[range(self.batch_size), actions] = rewards + self.gamma * q_next.max(axis=-1)

            self.evaluate_net.fit(screens, q_target, verbose=0)

    # 更新目标网络权重方法
    def update_target_network(self):
        self.target_net.set_weights(self.evaluate_net.get_weights())

    # 保存评估网络权重方法
    def save_evaluate_network(self):
        self.evaluate_net.save_weights(self.save_path)