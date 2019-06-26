#!/bin/bash
for nlayers in 10
do
  for K in 10
  do
    for c_init in 0
    do
      for sigma_init in 1.0 3.0 5.0
      do
        for rs in {1..2}
        do
          for sigma0 in 0.1
          do
            sbatch train_V1_circuit.sh 5 $nlayers $K $c_init $sigma_init $rs $sigma0
            sbatch train_V1_circuit.sh 60 $nlayers $K $c_init $sigma_init $rs $sigma0
          done
        done
      done
    done
  done
done
