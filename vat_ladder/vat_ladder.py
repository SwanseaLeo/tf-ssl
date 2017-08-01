# -----------------------------
# IMPORTS
# -----------------------------
import IPython
import tensorflow as tf
import input_data
import math
import os
import csv
from tqdm import tqdm
import argparse
import numpy as np  # needed only to set seed for data input
from tensorflow.contrib import layers as layers
import time
# from vat_layers import kl_divergence_with_logit


def fclayer(input,
            size_out,
            wts_init=layers.xavier_initializer(),
            bias_init=tf.truncated_normal_initializer(stddev=1e-6),
            reuse=None,
            scope=None,
            activation=None):
    return layers.fully_connected(
        inputs=input,
        num_outputs=size_out,
        activation_fn=activation,
        normalizer_fn=None,
        normalizer_params=None,
        weights_initializer=wts_init,
        weights_regularizer=None,
        biases_initializer=bias_init,
        biases_regularizer=None,
        reuse=reuse,
        variables_collections=None,
        outputs_collections=None,
        trainable=True,
        scope=scope
    )

# -----------------------------
# -----------------------------
# PARAMETER PARSING
# -----------------------------
# -----------------------------

def parse_argstring(argstring, dtype=float, sep='-'):
    return list(map(dtype, argstring.split(sep)))

def get_cli_params():
    parser = argparse.ArgumentParser()
    parser.add_argument('--id', default='ladder')
    # parser.add_argument('--train_flag', action='store_true')
    parser.add_argument('--decay_start_epoch', default=100, type=int)
    parser.add_argument('--end_epoch', default=150, type=int)
    # parser.add_argument('--print_interval', default=50, type=int)
    # parser.add_argument('--save_epochs', default=None, type=float)
    parser.add_argument('--num_labeled', default=100, type=int)

    parser.add_argument('--labeled_batch_size', default=100, type=int)
    # parser.add_argument('--unlabeled_batch_size', default=250, type=int)

    parser.add_argument('--initial_learning_rate', default=0.002, type=float)

    # parser.add_argument('--gamma_flag', action='store_true')

    # Specify encoder layers
    parser.add_argument('--encoder_layers',
                        default='784-1000-500-250-250-250-10')

    # Weight to apply to supervised cost in total loss
    # parser.add_argument('--sc_weight', default=1, type=float)

    # Standard deviation of the Gaussian noise to inject at each level
    parser.add_argument('--encoder_noise_sd', default=0.3, type=float)

    # Default RC cost corresponds to the gamma network
    parser.add_argument('--rc_weights', default='2000-20-0.2-0.2-0.2-0.2-0.2')

    # Specify form of combinator (A)MLP
    # parser.add_argument('--combinator_layers', default='4-1')
    parser.add_argument('--combinator_sd', default=0.025, type=float)

    parser.add_argument('--which_gpu', default=0, type=int)
    parser.add_argument('--write_to', default=None)
    parser.add_argument('--seed', default=1, type=int)

    # by default use the unlabeled batch epochs
    # parser.add_argument('--use_labeled_epochs', action='store_true')

    # only used if train_flag is false
    parser.add_argument('--train_step', default=None, type=int)
    parser.add_argument('--verbose', action='store_true') # for testing

    # option to not save the model at all
    parser.add_argument('--do_not_save', action='store_true')

    # weight of vat cost
    parser.add_argument('--vat_weight', default=0.1, type=float)

    # description to print
    parser.add_argument('--description', default=None)

    params = parser.parse_args()
    # params.write_to = 'logs/' + params.id + '.results' if params.write_to is None else params.write_to

    return params

def process_cli_params(params):
    # Specify base structure
    encoder_layers = parse_argstring(params.encoder_layers, dtype=int)
    rc_weights = parse_argstring(params.rc_weights, dtype=float)
    rc_weights = dict(zip(range(len(rc_weights)), rc_weights))
    params.encoder_layers = encoder_layers
    params.rc_weights = rc_weights
    return params


def count_trainable_params():
    trainables = tf.trainable_variables()
    return np.sum([np.prod(var.get_shape()) for var in trainables])

def order_param_settings(params):
    param_dict = vars(params)
    param_list = []
    for k in sorted(param_dict.keys()):
        param_list.append(str(k) + ": " + str(param_dict[k]))

    return param_list


PARAMS = process_cli_params(get_cli_params())

# norm length for (virtual) adversarial training
EPSILON = 8.0
# the number of power iterations
NUM_POWER_ITERATIONS = 1
# small constant for finite difference
XI = 1e-6
# Weight of vat wrt other losses
ALPHA = PARAMS.vat_weight

# Set GPU device to use
os.environ["CUDA_DEVICE_ORDER"]="PCI_BUS_ID"
os.environ["CUDA_VISIBLE_DEVICES"]=str(PARAMS.which_gpu)

# Set seeds
np.random.seed(PARAMS.seed)
tf.set_random_seed(PARAMS.seed)

# Set layer sizes for encoders
layer_sizes = PARAMS.encoder_layers

num_layers = len(layer_sizes) - 1  # number of layers

num_epochs = PARAMS.end_epoch
num_labeled = PARAMS.num_labeled

print("===  Loading Data ===")
mnist = input_data.read_data_sets("MNIST_data", n_labeled=num_labeled,
                                  one_hot=True, verbose=True)
num_examples = mnist.train.num_examples

starter_learning_rate = PARAMS.initial_learning_rate

# epoch after which to begin learning rate decay
decay_after = PARAMS.decay_start_epoch
batch_size = PARAMS.labeled_batch_size
num_iter = (num_examples//batch_size) * num_epochs  # number of loop iterations



# -----------------------------
# -----------------------------
# LADDER SETUP
# -----------------------------
# -----------------------------

inputs = tf.placeholder(tf.float32, shape=(None, layer_sizes[0]))
outputs = tf.placeholder(tf.float32)


def bias_init(inits, size, name):
    return tf.Variable(inits * tf.ones([size]), name=name)

def wts_init(shape, name):
    # effectively a Xavier initializer
    return tf.Variable(tf.random_normal(shape), name=name) / \
           math.sqrt(shape[0])

shapes = list(zip(layer_sizes[:-1], layer_sizes[1:]))  # shapes of linear layers

weights = {'W': [wts_init(s, "W") for s in shapes],  # Encoder weights
           'V': [wts_init(s[::-1], "V") for s in shapes],  # Decoder weights
           # batch normalization parameter to shift the normalized value
           'beta': [bias_init(0.0, layer_sizes[l + 1], "beta") for l in range(num_layers)],
           # batch normalization parameter to scale the normalized value
           'gamma': [bias_init(1.0, layer_sizes[l + 1], "beta") for l in range(num_layers)]}

# scaling factor for noise used in corrupted encoder
noise_std = PARAMS.encoder_noise_sd

# hyperparameters that denote the importance of each layer
denoising_cost = PARAMS.rc_weights

# Lambdas for extracting labeled/unlabeled, etc.
join = lambda l, u: tf.concat([l, u], 0)
labeled = lambda x: tf.slice(x, [0, 0], [batch_size, -1]) if x is not None else x
unlabeled = lambda x: tf.slice(x, [batch_size, 0], [-1, -1]) if x is not None else x
split_lu = lambda x: (labeled(x), unlabeled(x))

# Boolean training flag
TRAIN_FLAG = tf.placeholder(tf.bool)


# -----------------------------
# -----------------------------
# BATCH NORMALIZATION SETUP
# -----------------------------
# -----------------------------
ewma = tf.train.ExponentialMovingAverage(decay=0.99)  # to calculate the moving averages of mean and variance
bn_assigns = []  # this list stores the updates to be made to average mean and variance

def batch_normalization(batch, mean=None, var=None):
    if mean is None or var is None:
        mean, var = tf.nn.moments(batch, axes=[0])
    return (batch - mean) / tf.sqrt(var + tf.constant(1e-10))

# average mean and variance of all layers
running_mean = [tf.Variable(tf.constant(0.0, shape=[l]), trainable=False) for l in layer_sizes[1:]]
running_var = [tf.Variable(tf.constant(1.0, shape=[l]), trainable=False) for l in layer_sizes[1:]]


def update_batch_normalization(batch, l):
    "batch normalize + update average mean and variance of layer l"
    mean, var = tf.nn.moments(batch, axes=[0])
    assign_mean = running_mean[l-1].assign(mean)
    assign_var = running_var[l-1].assign(var)
    bn_assigns.append(ewma.apply([running_mean[l-1], running_var[l-1]]))
    with tf.control_dependencies([assign_mean, assign_var]):
        return (batch - mean) / tf.sqrt(var + 1e-10)

# -----------------------------
# -----------------------------
# ENCODER
# -----------------------------
# -----------------------------
def encoder(inputs, noise_std, is_training=TRAIN_FLAG, update_batch_stats=True):
    """
    is_training has to be a placeholder TF boolean
    Note: if is_training is false, update_batch_stats is false, since the
    update is only called in the training setting
    """
    h = inputs + tf.random_normal(tf.shape(inputs)) * noise_std  # add noise to input
    d = {}  # to store the pre-activation, activation, mean and variance for each layer
    # The data for labeled and unlabeled examples are stored separately
    d['labeled'] = {'z': {}, 'm': {}, 'v': {}, 'h': {}}
    d['unlabeled'] = {'z': {}, 'm': {}, 'v': {}, 'h': {}}
    d['labeled']['z'][0], d['unlabeled']['z'][0] = split_lu(h)

    for l in range(1, num_layers+1):
        print("Layer ", l, ": ", layer_sizes[l-1], " -> ", layer_sizes[l])
        d['labeled']['h'][l-1], d['unlabeled']['h'][l-1] = split_lu(h)
        z_pre = tf.matmul(h, weights['W'][l-1])  # pre-activation
        z_pre_l, z_pre_u = split_lu(z_pre)  # split labeled and unlabeled examples

        m, v = tf.nn.moments(z_pre_u, axes=[0])

        # if training:
        def training_batch_norm():
            # Training batch normalization
            # batch normalization for labeled and unlabeled examples is performed separately
            if noise_std > 0:
                # Corrupted encoder
                # batch normalization + noise
                z = join(batch_normalization(z_pre_l), batch_normalization(z_pre_u, m, v))
                z += tf.random_normal(tf.shape(z_pre)) * noise_std
            else:
                # Clean encoder
                # batch normalization + update the average mean and variance using batch mean and variance of labeled examples
                bn_l = update_batch_normalization(z_pre_l, l) if \
                    update_batch_stats else batch_normalization(z_pre_l)
                bn_u = batch_normalization(z_pre_u, m, v)
                z = join(bn_l, bn_u)
            return z

        # else:
        def eval_batch_norm():
            # Evaluation batch normalization
            # obtain average mean and variance and use it to normalize the batch
            mean = ewma.average(running_mean[l-1])
            var = ewma.average(running_var[l-1])
            z = batch_normalization(z_pre, mean, var)
            # Instead of the above statement, the use of the following 2 statements containing a typo
            # consistently produces a 0.2% higher accuracy for unclear reasons.
            # m_l, v_l = tf.nn.moments(z_pre_l, axes=[0])
            # z = join(batch_normalization(z_pre_l, m_l, mean, var), batch_normalization(z_pre_u, mean, var))
            return z

        # perform batch normalization according to value of boolean "training" placeholder:
        z = tf.cond(is_training, training_batch_norm, eval_batch_norm)
        # z = training_batch_norm() if is_training else eval_batch_norm()

        if l == num_layers:
            # use softmax activation in output layer
            h = tf.nn.softmax(weights['gamma'][l-1] * (z + weights["beta"][l-1]))
        else:
            # use ReLU activation in hidden layers
            h = tf.nn.relu(z + weights["beta"][l-1])

        d['labeled']['z'][l], d['unlabeled']['z'][l] = split_lu(z)
        d['unlabeled']['m'][l], d['unlabeled']['v'][l] = m, v  # save mean and variance of unlabeled examples for decoding
    d['labeled']['h'][l], d['unlabeled']['h'][l] = split_lu(h)

    return h, d


print( "=== Corrupted Encoder === ")
logits_corr, corr_stats = encoder(inputs, noise_std, is_training=TRAIN_FLAG,
                                  update_batch_stats=False)

print( "=== Clean Encoder ===")
logits_clean, clean_stats = encoder(inputs, 0.0, is_training=TRAIN_FLAG,
                                    update_batch_stats=True)  # 0.0 -> do not add noise

# -----------------------------
# -----------------------------
# VAT FUNCTIONS
# -----------------------------
# -----------------------------

def logsoftmax(x):
    xdev = x - tf.reduce_max(x, 1, keep_dims=True)
    lsm = xdev - tf.log(tf.reduce_sum(tf.exp(xdev), 1, keep_dims=True))
    return lsm

def kl_divergence_with_logit(q_logit, p_logit):
    q = tf.nn.softmax(q_logit)
    qlogq = tf.reduce_mean(tf.reduce_sum(q * logsoftmax(q_logit), 1))
    qlogp = tf.reduce_mean(tf.reduce_sum(q * logsoftmax(p_logit), 1))
    return qlogq - qlogp

# vat encoder is clean encoder but without updating batch norm
# def logit(x, is_training=TRAIN_FLAG, update_batch_stats=False, stochastic=True,
#           seed=1234):
#     noise_std = 0.0 if stochastic is False else PARAMS.encoder_noise_sd
#     print("=== VAT PASS ===")
#     logits, _ = encoder(x, noise_std, is_training=is_training,
#                        update_batch_stats=update_batch_stats)
#     return logits

def forward(x, is_training=TRAIN_FLAG, update_batch_stats=False, seed=1234):

    def training_logit():
        print("=== VAT Clean Pass === ")
        logit,_ = encoder(x, 0.0,
                          is_training=is_training,
                          update_batch_stats=update_batch_stats)
        return logit

    def testing_logit():
        print("=== VAT Corrupted Pass ===")
        logit, _ = encoder(x, PARAMS.encoder_noise_sd,
                           is_training=is_training,
                           update_batch_stats=update_batch_stats)
        return logit

    # return tf.cond(is_training, training_logit, testing_logit)
    return training_logit()

def get_normalized_vector(d):
    # IPython.embed()
    d_dims = len(d.get_shape()) - 1
    axes = [range(1, d_dims)] if d_dims > 1 else [1]
    d /= (1e-12 + tf.reduce_max(tf.abs(d), axis=axes, keep_dims=True))
    d /= tf.sqrt(1e-6 + tf.reduce_sum(tf.pow(d, 2.0), axis=axes,
                                      keep_dims=True))
    return d


def generate_virtual_adversarial_perturbation(x, logit, is_training=TRAIN_FLAG):
    d = tf.random_normal(shape=tf.shape(x))

    for k in range(NUM_POWER_ITERATIONS):
        d = XI * get_normalized_vector(d)
        logit_p = logit
        print("=== Power Iteration: {} ===".format(k))
        logit_m = forward(x + d, update_batch_stats=False, is_training=is_training)
        dist = kl_divergence_with_logit(logit_p, logit_m)
        grad = tf.gradients(dist, [d], aggregation_method=2)[0]
        d = tf.stop_gradient(grad)

    return EPSILON * get_normalized_vector(d)


def virtual_adversarial_loss(x, logit, is_training=TRAIN_FLAG, name="vat_loss"):
    print("=== VAT Pass: Generating VAT perturbation ===")
    r_vadv = generate_virtual_adversarial_perturbation(x, logit, is_training=is_training)
    logit = tf.stop_gradient(logit)
    logit_p = logit
    print("=== VAT Pass: Computing VAT Loss (KL Divergence)")
    logit_m = forward(x + r_vadv, update_batch_stats=False, is_training=is_training)
    loss = kl_divergence_with_logit(logit_p, logit_m)
    return tf.identity(loss, name=name)


# -----------------------------
# -----------------------------
# RECOMBINATION FUNCTIONS
# -----------------------------
# -----------------------------

def amlp_combinator(z_c, u, size):
    uz = tf.multiply(z_c, u)
    x = tf.stack([z_c, u, uz], axis=-1)
    print(size)
    # print(z_c.get_shape, u.get_shape, uz.get_shape)

    h = fclayer(x, size_out=4, wts_init=tf.random_normal_initializer(
        stddev=PARAMS.combinator_sd), reuse=None) #, scope='combinator_hidden')

    o = fclayer(h, size_out=1, wts_init=tf.random_normal_initializer(
        stddev=PARAMS.combinator_sd), reuse=None,
                activation=tf.nn.relu) #, scope='combinator_out')

    return tf.squeeze(o)


def gauss_combinator(z_c, u, size):
    "gaussian denoising function proposed in the original paper"
    wi = lambda inits, name: tf.Variable(inits * tf.ones([size]), name=name)
    a1 = wi(0., 'a1')
    a2 = wi(1., 'a2')
    a3 = wi(0., 'a3')
    a4 = wi(0., 'a4')
    a5 = wi(0., 'a5')

    a6 = wi(0., 'a6')
    a7 = wi(1., 'a7')
    a8 = wi(0., 'a8')
    a9 = wi(0., 'a9')
    a10 = wi(0., 'a10')

    mu = a1 * tf.sigmoid(a2 * u + a3) + a4 * u + a5
    v = a6 * tf.sigmoid(a7 * u + a8) + a9 * u + a10

    z_est = (z_c - mu) * v + mu
    return z_est


# Choose recombination function
combinator = gauss_combinator


# -----------------------------
# -----------------------------
# DECODER
# -----------------------------
# -----------------------------

# IPython.embed()
print( "=== Decoder ===")
# Decoder
z_est = {}
d_cost = []  # to store the denoising cost of all layers
for l in range(num_layers, -1, -1):
    print("Layer ", l, ": ", layer_sizes[l+1] if l+1 < len(layer_sizes) else
    None, " -> ", layer_sizes[l], ", denoising cost: ", denoising_cost[l])
    z, z_c = clean_stats['unlabeled']['z'][l], corr_stats['unlabeled']['z'][l]
    m, v = clean_stats['unlabeled']['m'].get(l, 0), clean_stats['unlabeled']['v'].get(l, 1 - 1e-10)
    # print(l)
    if l == num_layers:
        u = unlabeled(logits_corr)
    else:
        u = tf.matmul(z_est[l+1], weights['V'][l])

    u = batch_normalization(u)

    z_est[l] = combinator(z_c, u, layer_sizes[l])

    z_est_bn = (z_est[l] - m) / v
    # append the cost of this layer to d_cost
    d_cost.append((tf.reduce_mean(tf.reduce_sum(tf.square(z_est_bn - z), 1)) / layer_sizes[l]) * denoising_cost[l])


# -----------------------------
# -----------------------------
# PUTTING IT ALL TOGETHER
# -----------------------------
# -----------------------------

# vat cost
# ul_x = unlabeled(inputs)
# ul_logit = unlabeled(logits_corr)
# ul_logit = forward(ul_x, is_training=True, update_batch_stats=False)
vat_loss = PARAMS.vat_weight * virtual_adversarial_loss(inputs, logits_corr)

# calculate total unsupervised cost by adding the denoising cost of all layers
u_cost = tf.add_n(d_cost)

y_N = labeled(logits_corr)
cost = -tf.reduce_mean(tf.reduce_sum(outputs*tf.log(y_N), 1))  # supervised cost

loss = cost + u_cost + vat_loss # total cost

pred_cost = -tf.reduce_mean(tf.reduce_sum(outputs * tf.log(logits_clean), 1))  # cost used for prediction

correct_prediction = tf.equal(tf.argmax(logits_clean, 1), tf.argmax(outputs, 1))  # no of correct predictions
accuracy = tf.reduce_mean(tf.cast(correct_prediction, "float")) * tf.constant(100.0)

learning_rate = tf.Variable(starter_learning_rate, trainable=False)
train_step = tf.train.AdamOptimizer(learning_rate).minimize(loss)

# add the updates of batch normalization statistics to train_step
bn_updates = tf.group(*bn_assigns)
with tf.control_dependencies([train_step]):
    train_step = tf.group(bn_updates)

saver = tf.train.Saver()

# -----------------------------
# -----------------------------

print("===  Starting Session ===")
sess = tf.Session()

i_iter = 0

# -----------------------------
# Resume from checkpoint
ckpt_dir = "checkpoints/" + PARAMS.id + "/"
ckpt = tf.train.get_checkpoint_state(ckpt_dir)  # get latest checkpoint (if any)
if ckpt and ckpt.model_checkpoint_path:
    # if checkpoint exists, restore the parameters and set epoch_n and i_iter
    saver.restore(sess, ckpt.model_checkpoint_path)
    epoch_n = int(ckpt.model_checkpoint_path.split('-')[1])
    i_iter = (epoch_n+1) * (num_examples//batch_size)
    print("Restored Epoch ", epoch_n)
else:
    # no checkpoint exists. create checkpoints directory if it does not exist.
    if not os.path.exists(ckpt_dir):
        os.makedirs(ckpt_dir)
    init = tf.global_variables_initializer()
    sess.run(init)

# -----------------------------
# Write logs to appropriate directory
log_dir = "logs/" + PARAMS.id
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

desc_file = log_dir + "/" + "description"
with open(desc_file, 'a') as f:
    if PARAMS.description is not None:
        print(PARAMS.description, file=f, flush=True)
    print(*order_param_settings(PARAMS), sep='\n', file=f, flush=True)
    print("Trainable parameters: ", count_trainable_params(), file=f,
          flush=True)

log_file = log_dir + "/" + "train_log"


# -----------------------------
print("=== Training ===")

[init_acc, init_loss] = sess.run([accuracy, loss], feed_dict={
    inputs: mnist.train.labeled_ds.images, outputs:
        mnist.train.labeled_ds.labels,
    TRAIN_FLAG: False})
print("Initial Train Accuracy: ", init_acc, "%")
print("Initial Train Loss: ", init_loss)

[init_acc] = sess.run([accuracy], feed_dict={
    inputs: mnist.test.images, outputs: mnist.test.labels, TRAIN_FLAG: False})
print("Initial Test Accuracy: ", init_acc, "%")
# print("Initial Test Loss: ", init_loss)


start = time.time()
for i in tqdm(range(i_iter, num_iter)):
    images, labels = mnist.train.next_batch(batch_size)

    _ = sess.run(
        [train_step],
        feed_dict={inputs: images, outputs: labels, TRAIN_FLAG: True})


    if (i > 1) and ((i+1) % (num_iter//num_epochs) == 0):
        now = time.time() - start
        epoch_n = i//(num_examples//batch_size)
        if (epoch_n+1) >= decay_after:
            # decay learning rate
            # learning_rate = starter_learning_rate * ((num_epochs - epoch_n) / (num_epochs - decay_after))
            ratio = 1.0 * (num_epochs - (epoch_n+1))  # epoch_n + 1 because learning rate is set for next epoch
            ratio = max(0., ratio / (num_epochs - decay_after))
            sess.run(learning_rate.assign(starter_learning_rate * ratio))
        saver.save(sess, 'checkpoints/model.ckpt', epoch_n)
        # print "Epoch ", epoch_n, ", Accuracy: ", sess.run(accuracy, feed_dict={inputs: mnist.test.images, outputs:mnist.test.labels, training: False}), "%"

        with open(log_file, 'a') as train_log:
            # write test accuracy to file "train_log"
            # train_log_w = csv.writer(train_log)
            log_i = [now, epoch_n] + sess.run(
                [accuracy],
                feed_dict={inputs: mnist.test.images, outputs: mnist.test.labels, TRAIN_FLAG: False}
            ) + sess.run(
                [loss, cost, u_cost, vat_loss],
                feed_dict={inputs: images, outputs: labels, TRAIN_FLAG: True})
            # train_log_w.writerow(log_i)
            print(*log_i, sep=',', flush=True, file=train_log)

print("Final Accuracy: ", sess.run(accuracy, feed_dict={
    inputs: mnist.test.images, outputs: mnist.test.labels, TRAIN_FLAG: False}),
      "%")

sess.close()
