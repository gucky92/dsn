#!/bin/bash
for nlayers in 10 20
do
  for sigma_init in 1.0 10.0
  do
    for c_init in 0
    do
      for rs in {1..10}
      do
        sbatch train_V1_circuit.sh $nlayers $c_init $sigma_init $rs
      done
    done
  done
done
