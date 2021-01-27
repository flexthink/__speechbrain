# Basics of multi-GPU

SpeechBrain provides two different ways of using multiple gpus while training or
inferring. For further information on how it is implemented and what are the
differences, please consider reading or detailed tutorial : [amazing multi-gpu tutorial](#)

## Multi-GPU training using Data Parallel
The common pattern for using multi-GPU training over a single machine with Data Parallel is:

```
> cd recipes/<dataset>/<task>/
> python experiment.py params.yaml --data_parallel_backend=True --data_parallel_count=2
```

Important: the batch size for each GPU process will be: `batch_size / data_parallel_count`. So you should consider changing the batch_size value according to you need.

## Multi-GPU training using Distributed Data Parallel (DDP)
To use DDP, you should consider using `torch.distributed.launch` for setting the subprocess with the right Unix variables `local_rank` and `rank`. The `local_rank` variable allows to set the right `device` argument for each DDP subprocess, the `rank` variable (which is unique for each subprocess) will be used for registering the subprocess rank to the DDP group. In that way, **we can manage multi-GPU training over multiple machines**.

The common pattern for using multi-GPU training with DDP (on a single machine with 4 GPUs):
```
cd recipes/<dataset>/<task>/
python -m torch.distributed.launch --nproc_per_node=4 experiment.py hyperparams.yaml --distributed_launch=True --distributed_backend='nccl'
```
Try to switch DDP backend if you have issues with `nccl`.

### With multiple machines (suppose you have 2 servers with 2 GPUs):
```
# Machine 1
cd recipes/<dataset>/<task>/
python -m torch.distributed.launch --nproc_per_node=2 --nnodes=2 --node=0 --master_addr machine_1_adress --master_port 5555 experiment.py hyperparams.yaml --distributed_launch=True --distributed_backend='nccl'

# Machine 2
cd recipes/<dataset>/<task>/
python -m torch.distributed.launch --nproc_per_node=2 --nnodes=2 --node=1 --master_addr machine_1_adress --master_port 5555 experiment.py hyperparams.yaml --distributed_launch=True --distributed_backend='nccl'
```
Machine 1 will have 2 subprocess (subprocess1: with `local_rank=0`, `rank=0`, and subprocess2: with `local_rank=1`, `rank=1`).
Machine 2 will have 2 subprocess (subprocess1: with `local_rank=0`, `rank=2`, and subprocess2: with `local_rank=1`, `rank=3`).

In this way, the current DDP group will contain 4 GPUs.