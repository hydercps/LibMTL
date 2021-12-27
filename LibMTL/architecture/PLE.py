import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np

from LibMTL.architecture.abstract_arch import AbsArchitecture

class _transform_resnet_PLE(nn.Module):
    def __init__(self, resnet_network, task_name, img_size, num_experts, device):
        super(_transform_resnet_PLE, self).__init__()
        
        self.num_experts = num_experts
        self.img_size = img_size
        self.task_name = task_name
        self.task_num = len(task_name)
        self.device = device
        self.forward_task = None
        
        self.shared_conv = nn.Sequential(resnet_network.conv1, resnet_network.bn1, 
                                         resnet_network.relu, resnet_network.maxpool)
        self.specific_layer, self.shared_layer = nn.ModuleDict({}), nn.ModuleDict({})
        for i in range(4):
            self.specific_layer[str(i)] = nn.ModuleDict({})
            for task in self.task_name:
                self.specific_layer[str(i)][task] = nn.ModuleList([eval('resnet_network.layer'+str(i+1))]*self.num_experts[task])
            self.shared_layer[str(i)] = nn.ModuleList([eval('resnet_network.layer'+str(i+1))]*self.num_experts['share'])
        
        input_size = []
        with torch.no_grad():
            x = torch.rand([int(s) for s in self.img_size]).unsqueeze(0).to(self.device)
            x = self.shared_conv(x)
            input_size.append(x.size().numel())
            for i in range(3):
                x = self.shared_layer[str(i)][0](x)
                input_size.append(x.size().numel())
        self.gate_specific = nn.ModuleDict({task: nn.ModuleList([self._gate_layer(input_size[i], 
                                                                   self.num_experts['share']+\
                                                                   self.num_experts[task]) \
                                                   for i in range(4)]) for task in self.task_name})
    
    def _gate_layer(self, in_channel, out_channel):
        return nn.Sequential(nn.Linear(in_channel, out_channel), nn.Softmax(dim=-1))
    
    def forward(self, inputs):
        s_rep = self.shared_conv(inputs)
        gate_rep = {task: s_rep for task in self.task_name}
        for i in range(4):
            for task in self.task_name:
                if self.forward_task is not None and task != self.forward_task:
                    continue
                experts_shared_rep = torch.stack([e(gate_rep[task]) for e in self.shared_layer[str(i)]])
                experts_specific_rep = torch.stack([e(gate_rep[task]) for e in self.specific_layer[str(i)][task]])
                selector = self.gate_specific[task][i](torch.flatten(gate_rep[task], start_dim=1))
                gate_rep[task] = torch.einsum('ij..., ji -> j...', 
                                           torch.cat([experts_shared_rep, experts_specific_rep], dim=0),
                                           selector)
        if self.forward_task is None:
            return gate_rep
        else:
            return gate_rep[self.forward_task]
            

class PLE(AbsArchitecture):
    r"""Progressive Layered Extraction (PLE).
    
    This method is proposed in `Progressive Layered Extraction (PLE): A Novel Multi-Task Learning (MTL) Model for Personalized Recommendations (ACM RecSys 2020 Best Paper) <https://dl.acm.org/doi/10.1145/3383313.3412236>`_ \
    and implemented by us. 

    Args:
        img_size (list): The size of input data. For example, [3, 244, 244] for input images with size 3x224x224.
        num_experts (list): The numbers of experts shared for all tasks and specific to each task, respectively. Each expert is the encoder network.

    .. warning::
            :class:`PLE` is only supported with ResNet-based encoder.

    """
    def __init__(self, task_name, encoder, decoders, rep_grad, multi_input, device, **kwargs):
        super(PLE, self).__init__(task_name, encoder, decoders, rep_grad, multi_input, device, **kwargs)
        
        self.img_size = self.kwargs['img_size']
        self.input_size = np.array(self.img_size, dtype=int).prod()
        self.num_experts = {task: self.kwargs['num_experts'][tn+1] for tn, task in enumerate(self.task_name)}
        self.num_experts['share'] = self.kwargs['num_experts'][0]
        try: 
            callable(eval('encoder.layer1'))
            self.encoder = _transform_resnet_PLE(encoder.to(device), task_name, self.img_size, 
                                                 self.num_experts, device)
        except:
            self.encoder.resnet_network = _transform_resnet_PLE(self.encoder.resnet_network.to(device), 
                                                                task_name, self.img_size, 
                                                                self.num_experts, device)
            
    def forward(self, inputs, task_name=None):
        out = {}
        if self.multi_input:
            try:
                callable(eval('self.encoder.resnet_network'))
                self.encoder.resnet_network.forward_task = task_name
            except:
                self.encoder.forward_task = task_name
        gate_rep = self.encoder(inputs)
        for tn, task in enumerate(self.task_name):
            if task_name is not None and task != task_name:
                continue
            ss_rep = gate_rep[task] if isinstance(gate_rep, dict) else gate_rep
            ss_rep = self._prepare_rep(ss_rep, task, same_rep=False)
            out[task] = self.decoders[task](ss_rep)
        return out
    
    def get_share_params(self):
        try:
            callable(eval('self.encoder.resnet_network'))
            r = self.encoder.resnet_network
        except:
            r = self.encoder
        p = []
        p += r.shared_conv.parameters()
        p += r.shared_layer.parameters()
        return p

    def zero_grad_share_params(self):
        try:
            callable(eval('self.encoder.resnet_network'))
            r = self.encoder.resnet_network
        except:
            r = self.encoder
        r.shared_conv.zero_grad()
        r.shared_layer.zero_grad()