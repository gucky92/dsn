# Copyright 2018 Sean Bittner, Columbia University
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# ==============================================================================
import tensorflow as tf
import numpy as np
import time
import csv
from datetime import datetime
import scipy.stats
import sys
import os
import datetime
import io
from sklearn.metrics import pairwise_distances
from dsn.util.dsn_util import (
    setup_param_logging,
    initialize_adam_parameters,
    computeMoments,
    getEtas,
    approxKL,
    get_savedir,
    compute_R2,
    check_convergence,
)
from tf_util.tf_util import (
    density_network,
    log_grads,
    count_params,
    AL_cost,
    memory_extension,
    get_initdir,
    check_init,
    load_nf_init,
)
from dsn.util.dsn_util import initialize_gauss_nf


def train_dsn(
    system,
    n,
    arch_dict,
    k_max=10,
    sigma_init=10.0,
    c_init_order=0,
    lr_order=-3,
    random_seed=0,
    min_iters=1000,
    max_iters=5000,
    check_rate=100,
    dir_str="general",
    savedir=None,
    entropy=True,
    db=False,
):
    """Trains a degenerate solution network (DSN).

        Args:
            system (obj): Instance of tf_util.systems.system.
            n (int): Batch size.
            arch_dict (dict): Specifies structure of approximating density network.
            k_max (int): Number of augmented Lagrangian iterations.
            c_init (float): Augmented Lagrangian trade-off parameter initialization.
            lr_order (float): Adam learning rate is 10^(lr_order).
            check_rate (int): Log diagonstics at every check_rate iterations.
            max_iters (int): Maximum number of training iterations.
            random_seed (int): Tensorflow random seed for initialization.

        """
    print('train_dsn start')
    print(system.behavior)
    # Learn a single (K=1) distribution with a DSN.
    K = 1

    # set initialization of AL parameter c and learning rate
    lr = 10 ** lr_order
    c_init = 10 ** c_init_order

    # save tensorboard summary in intervals
    TB_SAVE_EVERY = 50
    MODEL_SAVE_EVERY = 5000
    tb_save_params = False

    # Optimization hyperparameters:
    # If stop_early is true, test if parameter gradients over the last COST_GRAD_LAG
    # samples are significantly different than zero in each dimension.
    stop_early = False
    COST_GRAD_LAG = 100
    ALPHA = 0.05

    # Look for model initialization.  If not found, optimize the init.
    initdir = initialize_nf(system, arch_dict, sigma_init, random_seed)
    print('done initializing', initdir)

    # Reset tf graph, and set random seeds.
    tf.reset_default_graph()
    tf.set_random_seed(random_seed)
    np.random.seed(0)

    # Load nf initialization
    W = tf.placeholder(tf.float64, shape=(None, None, system.D), name="W")

    # Create model save directory if doesn't exist.
    if (savedir is None):
        savedir = get_savedir(
            system, arch_dict, sigma_init, lr_order, c_init_order, random_seed, dir_str
        )
    if not os.path.exists(savedir):
        print("Making directory %s" % savedir)
        os.makedirs(savedir)

    # Construct density network parameters.
    if (system.has_support_map):
        support_mapping = system.support_mapping
    else:
        support_mapping = None

    Z, sum_log_det_jacobian, flow_layers = density_network(
        W, arch_dict, support_mapping, initdir=initdir
    )

    with tf.name_scope("Entropy"):
        p0 = tf.reduce_prod(tf.exp((-tf.square(W)) / 2.0) / np.sqrt(2.0 * np.pi), axis=2)
        base_log_q_z = tf.log(p0)
        log_q_z = base_log_q_z - sum_log_det_jacobian
        H = -tf.reduce_mean(log_q_z)
        tf.summary.scalar("H", H)

    all_params = tf.trainable_variables()
    nparams = len(all_params)

    with tf.name_scope("system"):
        # Compute system-specific sufficient statistics and log base measure on samples.
        T_x = system.compute_suff_stats(Z)
        mu = system.compute_mu()
        print('T_x', T_x)
        print('mu', mu)
        T_x_mu_centered = system.center_suff_stats_by_mu(T_x)
        I_x = None

    # Declare ugmented Lagrangian optimization hyperparameter placeholders.
    with tf.name_scope("AugLagCoeffs"):
        Lambda = tf.placeholder(dtype=tf.float64, shape=(system.num_suff_stats,))
        c = tf.placeholder(dtype=tf.float64, shape=())

    print('system.num_suff_stats')
    print(system.num_suff_stats)
    print('Lambda')
    print(Lambda)
    print('T_x')
    print(T_x.shape)
    print('I_x')
    print(I_x)
    # Augmented Lagrangian cost function.
    print("Setting up augmented lagrangian gradient graph.")
    with tf.name_scope("AugLagCost"):
        cost, cost_grads, R_x = AL_cost(H, T_x_mu_centered, Lambda, c, \
                                      all_params, entropy=entropy, I_x=I_x)
        tf.summary.scalar("cost", cost)
        for i in range(system.num_suff_stats):
            tf.summary.scalar('R_%d' % (i+1), R_x[i])

    # Compute gradient of density network params (theta) wrt cost.
    grads_and_vars = []
    for i in range(len(all_params)):
        grads_and_vars.append((cost_grads[i], all_params[i]))

    # Add inputs and outputs of NF to saved tf model.
    tf.add_to_collection("W", W)
    tf.add_to_collection("Z", Z)
    saver = tf.train.Saver()

    # Tensorboard logging
    summary_writer = tf.summary.FileWriter(savedir)
    if tb_save_params:
        setup_param_logging(all_params)

    summary_op = tf.summary.merge_all()

    config = tf.ConfigProto()
    # Allow the full trace to be stored at run time.
    run_options = tf.RunOptions(trace_level=tf.RunOptions.FULL_TRACE)

    num_diagnostic_checks = k_max * (max_iters // check_rate) + 1
    nparam_vals = count_params(all_params)
    if (db):
        COST_GRAD_LOG_LEN = num_diagnostic_checks
        param_vals = np.zeros((COST_GRAD_LOG_LEN, nparam_vals))
    else:
        COST_GRAD_LOG_LEN = 2*COST_GRAD_LAG
        param_vals = None

    # Cyclically record gradients in a 2*COST_GRAD_LAG logger
    cost_grad_vals = np.zeros((COST_GRAD_LOG_LEN, nparam_vals))
    # Keep track of cost, entropy, and constraint violation throughout training.
    costs = np.zeros((num_diagnostic_checks,))
    Hs = np.zeros((num_diagnostic_checks,))
    R2s = np.zeros((num_diagnostic_checks,))
    mean_T_xs = np.zeros((num_diagnostic_checks, system.num_suff_stats))

    # Keep track of AL parameters throughout training.
    cs = []
    lambdas = []
    epoch_inds = [0]

    # Take snapshots of z and log density throughout training.
    nsamps = 1000
    if (db):
        Zs = np.zeros((num_diagnostic_checks, nsamps, system.D))
        log_q_zs = np.zeros((num_diagnostic_checks, nsamps))
        T_xs = np.zeros((num_diagnostic_checks, nsamps, system.num_suff_stats))
    else:
        Zs = np.zeros((k_max + 1, nsamps, system.D))
        log_q_zs = np.zeros((k_max + 1, nsamps))
        T_xs = np.zeros((k_max + 1, nsamps, system.num_suff_stats))

    gamma = 0.25
    num_norms = 100
    norms = np.zeros((num_norms,))
    new_norms = np.zeros((num_norms,))

    _c = c_init
    _lambda = np.zeros((system.num_suff_stats,))
    check_it = 0
    with tf.Session(config=config) as sess:
        print("training DSN for %s" % system.name)
        init_op = tf.global_variables_initializer()
        sess.run(init_op)
        summary_writer.add_graph(sess.graph)

        # Log initial state of the DSN.
        w_i = np.random.normal(np.zeros((K, nsamps, system.D)), 1.0)
        feed_dict = {W: w_i, Lambda: _lambda, c: _c}
        cost_i, _cost_grads, _Z, _T_x, _H, _log_q_z, summary = sess.run(
            [cost, cost_grads, Z, T_x, H, log_q_z, summary_op], feed_dict
        )

        summary_writer.add_summary(summary, 0)
        log_grads(_cost_grads, cost_grad_vals, 0)
        if (db):
            _params = sess.run(all_params)
            log_grads(_params, param_vals, 0)

        mean_T_xs[0, :] = np.mean(_T_x[0], 0)
        Hs[0] = _H
        costs[0] = cost_i
        check_it += 1

        Zs[0, :, :] = _Z[0, :, :]
        log_q_zs[0, :] = _log_q_z[0, :]
        T_xs[0, :, :] = _T_x[0]

        optimizer = tf.contrib.optimizer_v2.AdamOptimizer(learning_rate=lr)
        train_step = optimizer.apply_gradients(grads_and_vars)

        total_its = 1
        for k in range(k_max):
            print("AL iteration %d" % (k + 1))
            cs.append(_c)
            lambdas.append(_lambda)

            # Reset the optimizer so momentum from previous epoch of AL optimization
            # does not effect optimization in the next epoch.
            initialize_adam_parameters(sess, optimizer, all_params)

            for j in range(num_norms):
                w_j = np.random.normal(np.zeros((1, n, system.D)), 1.0)
                feed_dict.update({W: w_j})
                _T_x_mu_centered = sess.run(T_x_mu_centered, feed_dict)
                _R = np.mean(_T_x_mu_centered[0], 0)
                norms[j] = np.linalg.norm(_R)

            i = 0
            wrote_graph = False
            has_converged = False
            convergence_it = 0
            while i < max_iters:
                cur_ind = total_its + i

                w_i = np.random.normal(np.zeros((K, n, system.D)), 1.0)
                feed_dict = {W: w_i, Lambda: _lambda, c: _c}

                # Log diagnostics for W draw before gradient step
                if np.mod(cur_ind + 1, check_rate) == 0:
                    feed_dict = {W: w_i, Lambda: _lambda, c: _c}
                    _H, _T_x, _Z, _log_q_z = sess.run([H, T_x, Z, log_q_z], feed_dict)
                    print(42 * "*")
                    print("it = %d " % (cur_ind + 1))
                    print("H", _H, "cost", cost_i)
                    sys.stdout.flush()
                    
                    Hs[check_it] = _H
                    mean_T_xs[check_it] = np.mean(_T_x[0], 0)

                    if (db):
                        Zs[check_it, :, :] = _Z[0, :, :]
                        log_q_zs[check_it, :] = _log_q_z[0, :]
                        T_xs[check_it, :, :] = _T_x[0]

                    if stop_early:
                        has_converged = check_convergence(
                            cost_grad_vals, cur_ind % COST_GRAD_LOG_LEN, COST_GRAD_LAG, ALPHA
                        )

                    if has_converged:
                        print("has converged!!!!!!")
                        sys.stdout.flush()
                        convergence_it = cur_ind
                        break

                    np.savez(
                        savedir + "opt_info.npz",
                        costs=costs,
                        cost_grad_vals=cost_grad_vals,
                        param_vals=param_vals,
                        Hs=Hs,
                        R2s=R2s,
                        mean_T_xs=mean_T_xs,
                        fixed_params=system.fixed_params,
                        behavior=system.behavior,
                        mu=system.mu,
                        it=cur_ind,
                        Zs=Zs,
                        cs=cs,
                        lambdas=lambdas,
                        log_q_zs=log_q_zs,
                        T_xs=T_xs,
                        convergence_it=convergence_it,
                        check_rate=check_rate,
                        epoch_inds=epoch_inds,
                    )

                    print(42 * "*")

                if np.mod(cur_ind, check_rate) == 0:
                    start_time = time.time()

                if np.mod(cur_ind, TB_SAVE_EVERY) == 0:
                    # Create a fresh metadata object:
                    run_metadata = tf.RunMetadata()
                    ts, cost_i, _cost_grads, summary = sess.run([train_step, cost, cost_grads, summary_op], 
                                       feed_dict,
                                       options=run_options,
                                       run_metadata=run_metadata)
                    summary_writer.add_summary(summary, cur_ind)
                    if (not wrote_graph and i>20): # In case a GPU needs to warm up for optims
                        assert(min_iters >= 20 and TB_SAVE_EVERY >= 20)
                        print("writing graph stuff for AL iteration %d" % (k+1))
                        summary_writer.add_run_metadata(run_metadata, 
                                                        "train_step_{}".format(cur_ind),
                                                        cur_ind)
                        wrote_graph = True
                else:
                    ts, cost_i, _cost_grads = sess.run([train_step, cost, cost_grads], feed_dict)
                if np.mod(cur_ind + 1, check_rate) == 0:
                    costs[check_it] = cost_i
                    check_it += 1

                if np.mod(cur_ind, check_rate) == 0:
                    end_time = time.time()
                    print("iteration took %.4f seconds." % (end_time - start_time))

                log_grads(_cost_grads, cost_grad_vals, cur_ind % COST_GRAD_LOG_LEN)

                if (db):
                    _params = sess.run(all_params)
                    log_grads(_params, param_vals, cur_ind % COST_GRAD_LOG_LEN)


                if np.mod(i, MODEL_SAVE_EVERY) == 0:
                    print("saving model at iter", i)
                    saver.save(sess, savedir + "model")

                sys.stdout.flush()
                i += 1
            w_k = np.random.normal(np.zeros((K, nsamps, system.D)), 1.0)
            feed_dict = {W: w_k, Lambda: _lambda, c: _c}
            _H, _T_x, _Z, _log_q_z = sess.run([H, T_x, Z, log_q_z], feed_dict)

            if (not db):
                Zs[k + 1, :, :] = _Z[0, :, :]
                log_q_zs[k + 1, :] = _log_q_z[0, :]
                T_xs[k + 1, :, :] = _T_x[0]
            _T_x_mu_centered = sess.run(T_x_mu_centered, feed_dict)
            _R = np.mean(_T_x_mu_centered[0], 0)
            _lambda = _lambda + _c * _R

            # save all the hyperparams
            if not os.path.exists(savedir):
                print("Making directory %s" % savedir)
                os.makedirs(savedir)
            # saveParams(params, savedir);
            # save the model
            print("saving to", savedir)
            saver.save(sess, savedir + "model")

            total_its += i
            epoch_inds.append(total_its - 1)


            # If optimizing for feasible set and on f.s., quit.
            if (system.behavior["type"] == "feasible"):
                is_feasible = system.behavior["is_feasible"](_T_x[0])
                if is_feasible:
                    print('On the feasible set.  Initialization complete.')
                    break
                else:
                    print('Not on safe part of feasible set yet.')

            # do the hypothesis test to figure out whether or not we should update c
            feed_dict = {Lambda: _lambda, c: _c}

            for j in range(num_norms):
                w_j = np.random.normal(np.zeros((1, n, system.D)), 1.0)
                feed_dict.update({W: w_j})
                _T_x_mu_centered = sess.run(T_x_mu_centered, feed_dict)
                _R = np.mean(_T_x_mu_centered[0], 0)
                new_norms[j] = np.linalg.norm(_R)

            t, p = scipy.stats.ttest_ind(new_norms, gamma * norms, equal_var=False)
            # probabilistic update based on p value
            u = np.random.rand(1)
            print("t", t, "p", p)
            if u < 1 - p / 2.0 and t > 0:
                print(u, "not enough! c updated")
                _c = 4 * _c
            else:
                print(u, "same c")

        final_thetas = {};
        for i in range(nparams):
            final_thetas.update({all_params[i].name:sess.run(all_params[i])});

        np.savez(
                savedir + "theta.npz",
                theta=final_thetas
            )

    print("saving to %s  ..." % savedir)
    sys.stdout.flush()
                    
    np.savez(
        savedir + "opt_info.npz",
        costs=costs,
        cost_grad_vals=cost_grad_vals,
        param_vals=param_vals,
        Hs=Hs,
        R2s=R2s,
        mean_T_xs=mean_T_xs,
        fixed_params=system.fixed_params,
        behavior=system.behavior,
        mu=system.mu,
        it=cur_ind,
        Zs=Zs,
        cs=cs,
        lambdas=lambdas,
        log_q_zs=log_q_zs,
        T_xs=T_xs,
        convergence_it=convergence_it,
        check_rate=check_rate,
        epoch_inds=epoch_inds,
    )

    if (system.behavior["type"] == "feasible"):
        return costs, _Z, is_feasible
    else:
        return costs, _Z


def initialize_nf(system, arch_dict, sigma_init, random_seed, 
                  min_iters=50000):

    print('init nf start', system.behavior["type"])
    # Inequality case: Start in the feasible set of the bounds.
    if ("bounds" in system.behavior.keys()):
        # Check for feasible set initialization first
        behavior = system.behavior
        feasible_behavior = {"type":"feasible", \
                             "means":system.behavior["feasible_means"], \
                             "variances":system.behavior["feasible_variances"], \
                             "is_feasible":system.behavior["is_feasible"]
                            }
        system.behavior = feasible_behavior
        system.mu = system.compute_mu()
        system.T_x_labels = system.get_T_x_labels()
        system.num_suff_stats = len(system.T_x_labels)
        system.behavior_str = system.get_behavior_str()

        initdir = get_initdir(system, 
                              arch_dict, 
                              sigma_init, 
                              random_seed,
                              init_type="feas")
        
        initialized = check_init(initdir)
        if (not initialized):
            n = 1000
            k_max = 20
            min_iters = 2500
            max_iters = 5000
            lr_order=-3
            c_init_order = 0
            check_rate = 100
            is_feasible = False
            while (not is_feasible):
                _, _, is_feasible = train_dsn(system,
                                              n,
                                              arch_dict,
                                              k_max,
                                              sigma_init,
                                              c_init_order,
                                              lr_order,
                                              random_seed,
                                              min_iters,
                                              max_iters,
                                              check_rate,
                                              dir_str=None,
                                              savedir=initdir,
                                              entropy=False
                                              )
                max_iters = 2*max_iters

        system.behavior = behavior
    else:
        initdir = get_initdir(system, 
                              arch_dict, 
                              sigma_init, 
                              random_seed)
        initialized = check_init(initdir)
        if (not initialized):
            initialize_gauss_nf(system.D, 
                                arch_dict, 
                                sigma_init,
                                random_seed, 
                                initdir,
                                mu=system.density_network_init_mu,
                                bounds=system.density_network_bounds)
    return initdir




