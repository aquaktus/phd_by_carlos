import random
import re
import copy
import torch
from time import perf_counter
from itertools import compress as mask
import tqdm
from src.FasterMCTS import FasterMCTS
from torch.utils.data import Dataset, DataLoader
from pytorch_lightning import Trainer, Callback, seed_everything

class AlphaZero_Trainer():
    def __init__(self, model, env):
        self.model = model
        self.env = env
        
    def execute_episodes(self, problem_type='simple_addition', episodes=100, simulations=800, force_states=None, force_targets=None, temp=1, seed=None):
        current_states, target_strings = self.env.random_states(episodes, problem_type, seed=seed)
        if type(force_states)!=type(None):
            current_states, target_strings = force_states, force_targets
        len_unique = len(target_strings)
        print(f'For {episodes} episodes found {len_unique} unique.')
        
        examples = [[] for i in range(len(target_strings))]
        finished_episodes = []
        mcts = FasterMCTS(self.model, self.env)
        
        t1 = perf_counter()  
        while examples != []:
            for i in tqdm.tqdm(range(simulations), desc=f'Sims for {len(target_strings)} episodes'): 
                mcts.search(current_states, target_strings)
            pi_s = mcts.getActionProb(current_states, temp=temp)
            
            current_strings = self.env.to_hash(current_states)
            for i in range(current_states.shape[0]):
                print("Top_actions:", self.env.to_hash(pi_s[i].argsort(descending=True).unsqueeze(1)[:10]))
                examples[i].append({'current_state':current_states[i][current_states[i]!=self.env.PAD_id].clone(), 
                                    'current_string': current_strings[i],
                                     'target_string': target_strings[i],
                                     'top_pi_actions': self.env.to_hash(pi_s[i].argsort(descending=True)[:10].unsqueeze(1)),
                                     'top_pi_action_ids': pi_s[i].argsort(descending=True)[:10],
                                     'pi':pi_s[i], 
                                     'reward':None})              # rewards can not be determined yet 
            
            sampled_actions = pi_s.multinomial(1)    # sample action from improved policy
            print("chose:", self.env.to_hash(sampled_actions))
            next_states, rewards, is_terminal_mask = self.env.step(current_states, target_strings, sampled_actions)
            
            for i in range(current_states.shape[0]):
                if is_terminal_mask[i] == True:
                    episode_reward = rewards[i]
                    episode_steps = self.assignRewards(examples[i], episode_reward)
                    
                    final_len = episode_steps[-1]['current_state'].shape[0]
                    final_state = episode_steps[-1]['current_state']
                    episode_policies = torch.zeros((final_len, self.env.action_size))
                    episode_values = torch.full((final_len,), episode_reward)
                    eposode_grad_mask = torch.tensor([False]*final_len)
                    for step in episode_steps:
                        current_len = step['current_state'].shape[0]
                        episode_policies[current_len-1] = step['pi']
                        eposode_grad_mask[current_len-1] = True
                        
                    finished_episodes.append({
                        'state_string':episode_steps[-1]['current_string'],
                        'input_ids':final_state,
                        'target_policies':episode_policies,
                        'target_values':episode_values,
                        'not_auto_gen_mask':eposode_grad_mask
                    })
            
            examples = list(mask(examples,~is_terminal_mask))
                    
            current_states = next_states[~is_terminal_mask]
            target_strings = list(mask(target_strings,~is_terminal_mask))
            print(f'{is_terminal_mask.sum()} terminated, {current_states.shape[0]} left.')
            
        t2 = perf_counter()
        print(f'Performed {len_unique} episodes in {t2-t1:.2f}s.')
        return finished_episodes
    
    def assignRewards(self, examples, reward):
        for ex in examples:
            ex['reward'] = copy.deepcopy(reward)
        return examples
    
#     def decomposePositiveEpisodes(self, current_states, target_strings):
#         examples = []
#         for i in range(current_states.shape[0]):
#             examples += self.decomposePositiveEpisode(current_states[i][current_states[i] != self.env.PAD_id], target_strings[i])
#         return examples
    
#     def decomposePositiveEpisode(self, current_state, target_string):
#         examples = []
#         sp_mask = self.env.singleAutoGeneratedMask(current_state)
#         for i in range(current_state.shape[0]):
#             if sp_mask[i] == True:
#                 continue
#             one_hot_pi = torch.zeros(self.env.getActionSize(), dtype=torch.float)
#             one_hot_pi[current_state[i]] = 1
#             new_example = {'current_state':current_state[:i], 
#                            'target_string':target_string,
#                            'pi': one_hot_pi,
#                            'id': 'custom',
#                            'reward':1
#                           }
#             examples.append(new_example)
#         return examples
    
    def decomposeSupervisedEpisodes(self, positive_strings, prompt_strings=None, v=1):
        samples = []
        if not prompt_strings:
            prompt_strings = [None]*len(positive_strings)
            
        for positive_string, prompt_string in zip(positive_strings, prompt_strings):
            input_ids, target_policies, target_values, not_auto_gen_mask = self.decomposeSupervisedEpisode(positive_string, prompt_string, v=v)
            
            samples.append({
                'positive_string':positive_string,
                'input_ids':input_ids,
                'target_policies':target_policies,
                'target_values':target_values,
                'not_auto_gen_mask':not_auto_gen_mask
            })
        return samples
    
    def decomposeSupervisedEpisode(self, positive_string, prompt_string=None, v=1):
        positive_state = self.env.strings_to_state([positive_string])[0]
        is_auto_gen_mask = self.env.singleAutoGeneratedMask(positive_state)
        
        action_size = self.env.action_size
        seq_len = positive_state.shape[0]
        
        target_policies = torch.zeros((seq_len, action_size))
        target_policies[torch.arange(seq_len), positive_state] = 1
        
        if prompt_string:
            prompt_state = self.env.strings_to_state([prompt_string])[0]
            prompt_len = prompt_state.shape[0]
            assert torch.eq(positive_state[:prompt_len], prompt_state).all()
            is_auto_gen_mask[:prompt_len] = True
            
        target_values = torch.full((seq_len,), v)
        
        not_auto_gen_mask = ~is_auto_gen_mask
        
        # prep for causal masking
        return positive_state[:-1], target_policies[1:], target_values[1:], not_auto_gen_mask[1:]
    
    
    def decomposeGuidedExplorations(self, example_objects):
        gold_strings = [s['gold'] for s in example_objects]
        
        prompt_strings = [s['prompt'] for s in example_objects]
        prompt_states = self.env.strings_to_state(prompt_strings)
        
        decomp_strings = []
        matched_target_objects = []
        
        for example_object in example_objects:
            gold_string = example_object['gold']
            gold_state = self.env.strings_to_state([gold_string])[0]
            
            prompt_string = example_object['prompt']
            prompt_state = self.env.strings_to_state([prompt_string])[0]
        
            is_auto_gen_mask = self.env.singleAutoGeneratedMask(gold_state)
            
            for seq_idx in range(prompt_state.shape[0], gold_state.shape[0]):
                if is_auto_gen_mask[seq_idx] == False:
                    decomp_string = self.env.to_hash(gold_state[:seq_idx].unsqueeze(0))[0]
                    decomp_strings.append(decomp_string)
                    matched_target_objects.append(example_object)
        
        return self.env.strings_to_state(decomp_strings), matched_target_objects