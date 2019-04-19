#!/bin/bash
#SBATCH --account=stats
#SBATCH --job-name=SC_DSN
#SBATCH -c 1
#SBATCH --gres=gpu
#SBATCH --time=11:29:00
#SBATCH --mem-per-cpu=5gb

module load cuda90/toolkit
module load cuda90/blas
module load cudnn/7.0.5

source activate dsn_gpu
python3 train_SC_circuit_logbarrier.py $1 $2 $3
