import matplotlib
matplotlib.use('agg')
import matplotlib.pyplot as plt
import numpy as np
import sys, os
from dsn.util.dsn_util import get_savedir
from dsn.util.systems import Linear2D, STGCircuit, V1Circuit
from dsn.util.plot_util import make_training_movie
import time

os.chdir("../")

print(sys.argv)

dir_str = str(sys.argv[1])
nlayers = int(sys.argv[2])
c_init_order = int(sys.argv[3])
K = int(sys.argv[4])
sigma_init = float(sys.argv[5])
random_seed = int(sys.argv[6])

if (K > 1):
    sigma0 = float(sys.argv[7])

if (dir_str in ['Linear2D', 'test']):
    fixed_params = {"tau": 1.0}
    omega = 1
    mu = np.array([0.0, 2 * np.pi * omega])
    Sigma = np.array([1.0, 1.0])
    behavior = {"type": "oscillation", "means": mu, "variances": Sigma}
    system = Linear2D(fixed_params, behavior)
elif (dir_str == 'STGCircuit'):
    T = 200
    mean = 0.525
    variance = (.025)**2
    dt = 0.025
    fft_start = 0
    w = 20
    fixed_params = {'g_synB':5e-9}
    behavior = {"type":"hubfreq",
                "mean":mean,
                "variance":variance}
    model_opts = {"dt":dt,
                  "T":T,
                  "fft_start":fft_start,
                  "w":w
                 }
    system = STGCircuit(fixed_params, behavior, model_opts)
elif (dir_str == 'V1Circuit'):
    fixed_params = {'h_FFE':0.0, \
                'h_FFP':0.0, \
                'h_LATE':0.0, \
                'h_LATP':0.0, \
                'h_LATS':0.0, \
                'h_LATV':0.0, \
                'tau':1.0, \
                'n':2.0, \
                's_0':30}
    behavior_type = "difference"

    c_vals=np.array([1.0])
    s_vals=np.array([5, 60])
    r_vals=np.array([0.0, 1.0])

    behavior = {'type':behavior_type, \
            'c_vals':c_vals, \
            's_vals':s_vals, \
            'r_vals':r_vals}

    model_opts = {"g_FF": "c", "g_LAT": "square", "g_RUN": "r"}
    T = 40
    dt = 0.25
    init_conds = np.expand_dims(np.array([1.0, 1.1, 1.2, 1.3]), 1)

    system = V1Circuit(fixed_params, behavior, model_opts, T, dt, init_conds)
else:
    raise NotImplementedError()
    

flow_type = "PlanarFlow"
mult_and_shift = "post"
arch_dict = {
    "D": system.D,
    "K": K,
    "sigma0":sigma0, 
    "flow_type": flow_type,
    "repeats": nlayers,
    "post_affine": True,
}

lr_order = -3

savedir = get_savedir(system, arch_dict, sigma_init, c_init_order, random_seed, dir_str)
fname = savedir + 'opt_info.npz'
movie_fname = savedir + 'training'

step = 1
start_time = time.time()
make_training_movie(fname, system, step, movie_fname)
end_time = time.time()

print('Took %.3f seconds to make the movie.' % (end_time - start_time))
