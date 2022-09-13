import argparse
import os
import torch
import torch.distributed as dist
import torch.multiprocessing as mp
import torch.nn as nn
import torch.fx as fx

from torch.distributed._shard import shard_module
from torch.distributed._shard.sharded_optim import (
    ShardedOptimizer,
    named_params_with_sharded_tensor,
)
from torch.distributed._shard.sharding_plan import ShardingPlan
from torch.distributed._shard.sharding_spec import ChunkShardingSpec


class Schedule():

    def __init__(self, mod: nn.Module, world_size: int, rank: int) -> None:
        self.mod = mod
        self.world_size = world_size
        self.rank = rank
        self.tensors = {}

        gm: fx.GraphModule = fx.symbolic_trace(self.mod)
        for module in gm.named_modules():
            self.tensors[module] = Tensor(module)

    def __getitem__(self, name: str):
        return self.tensors[name]


class Tensor():
    def __init__(self, name: str):
        self.name = name
        self.spec = None

    def partition(self, axis):
        placements = [f"rank:{idx}/cuda:{idx}" for idx in range(self.world_size)]
        self.spec = ChunkShardingSpec(
            dim=axis,
            placements=placements,
        )


def create_schedule(mod: nn.Module):
    return Schedule(mod)


def build(sch: Schedule, rank: int):
    # The result from the second nn.linear layer needs aggregation by dim 0.
    output_spec = ChunkShardingSpec(
        dim=0,
        placements=[f"rank:{idx}/cuda:{idx}" for idx in range(sch.world_size)],
    )
    sharding_plan = {t+".weight": sch[t].spec for t in sch.tensors}
    print(sharding_plan)
    module_sharding_plan = ShardingPlan(
        # Specify the sharding plan for the component of each module.
        plan=sharding_plan,
        # Specify the sharding plan for the output of one particular module.
        # e.g., the output of the second nn layer in the example of Megatron-LM.
        output_plan={
            "dense_2": output_spec,
        },
        # Specify to get the tensor stored on the local shard if the output
        # is a sharded tensor.
        return_local_tensor=["dense_2"],
    )
    # Shard the module based on created plan.
    shard_module(sch.mod, module_sharding_plan)
    # Create a optimizer for the sharded module.
    opt = ShardedOptimizer(
        dict(named_params_with_sharded_tensor(sch.mod)),
        torch.optim.SGD, # SGD is only demo purpose, one can use other optims.
        # lr=lr,
    )
    return sch.mod.cuda(rank), opt
