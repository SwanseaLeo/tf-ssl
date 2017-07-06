import tensorflow as tf
import tensorflow.contrib.layers as layers
from collections import OrderedDict


class NoisyBNLayer(object):

    def __init__(self, scope_name, size, noise_sd=None, decay=0.99, var_ep=1e-5, reuse=None):
        self.scope = scope_name
        self.size = size
        self.noise_sd = noise_sd
        self.decay = decay
        self.var_ep = var_ep
        with tf.variable_scope(scope_name, reuse=reuse):
            self.scale = tf.get_variable('NormScale', initializer=tf.ones([size]))
            self.beta = tf.get_variable('NormOffset', initializer=tf.zeros([size]))
            self.pop_mean = tf.get_variable('PopulationMean', initializer=tf.zeros([size]), trainable=False)
            self.pop_var = tf.get_variable('PopulationVariance', initializer=tf.fill([size], 1e-2), trainable=False)
        self.batch_mean, self.batch_var = None, None

    def normalize(self, x, training=True):
        eps = self.var_ep
        if training:
            batch_mean, batch_var = tf.nn.moments(x, [0])
            self.batch_mean, self.batch_var = batch_mean, batch_var
            train_mean_op = tf.assign(self.pop_mean,
                                      self.pop_mean * self.decay + batch_mean * (1 - self.decay))
            train_var_op = tf.assign(self.pop_var,
                                     self.pop_var * self.decay + batch_var * (1 - self.decay))

            with tf.control_dependencies([train_mean_op, train_var_op]):
                return tf.divide(tf.subtract(x, batch_mean), tf.add(tf.sqrt(batch_var), eps))

        else:
            return tf.divide(tf.subtract(x, self.pop_mean), tf.add(tf.sqrt(self.pop_var), eps))

    def normalize_from_saved_stats(self, x, training=True):
        if training:
            return tf.divide(tf.subtract(x, self.batch_mean), tf.add(tf.sqrt(self.batch_var), self.var_ep))
        else:
            return tf.divide(tf.subtract(x, self.pop_mean), tf.add(tf.sqrt(self.pop_var), self.var_ep))

    def add_noise(self, x):
        if self.noise_sd is not None:
            noise = tf.random_normal(shape=tf.shape(x), mean=0.0, stddev=self.noise_sd)
            return x + noise
        else:
            return x

    def apply_shift_scale(self, x, shift=True, scale=True):
        if shift:
            x = tf.add(x, self.beta)
        if scale:
            x = tf.multiply(self.scale, x)
        return x


class Encoder(object):
    def __init__(self, x, y, layer_sizes, noise_sd=None, reuse=None, training=True):
        """

        :param scope:
        :param x:
        :param y:
        :param layer_sizes:
        :param noise_sd: only a single scalar for all levels at this point
        """
        self.scope = 'enc'
        self.reuse = reuse
        self.n_layers = len(layer_sizes)
        self.layer_sizes = layer_sizes
        self.last = self.n_layers-1

        self.x = x
        self.z_pre = OrderedDict()
        self.z = OrderedDict()
        self.h = OrderedDict()
        self.y = y


        self.bn_layers = [NoisyBNLayer(scope_name=self.scope+'_bn'+str(i),
                                       size=layer_sizes[i],
                                       noise_sd=noise_sd,
                                       reuse=reuse) for i in range(self.n_layers)]

        # self.wts_init = layers.xavier_initializer()
        # self.bias_init = tf.truncated_normal_initializer(stddev=1e-6)

        self.loss, self.predict = self.build(training)


    def build(self, training=True):
        bn = self.bn_layers[0]
        self.h[0] = bn.add_noise(bn.normalize(self.x, training))

        for l in range(1, self.n_layers):
            size_out = self.layer_sizes[l]
            bn = self.bn_layers[l]
            self.z_pre[l] = fclayer(self.h[l-1], size_out, reuse=self.reuse, scope=self.scope+str(l))

            if l == self.n_layers - 1:
                self.z[l] = bn.add_noise(bn.normalize(self.z_pre[l], training))
                self.h[l] = tf.nn.softmax(logits=bn.apply_shift_scale(self.z[l], shift=True, scale=True))
            else:
                # Do not need to apply scaling to RELU
                self.z[l] = bn.add_noise(bn.normalize(self.z_pre[l], training))
                self.h[l] = tf.nn.relu(bn.apply_shift_scale(self.z[l], shift=True, scale=False))

        loss = tf.nn.softmax_cross_entropy_with_logits(labels=self.y, logits=self.z[self.n_layers - 1])

        predict = tf.argmax(self.h[self.n_layers-1], axis=-1)

        return loss, predict


class Combinator(object):
    def __init__(self, inputs, layer_sizes=(2,2,2), stddev=0.006, scope='com'):
        """
        :param inputs:
        :param layer_sizes: Hidden layers
        :param stddev: Standard deviation of weight initializer
        """
        self.wts_init = tf.random_normal_initializer(stddev=stddev)
        self.bias_init = tf.truncated_normal_initializer(stddev=1e-6)
        self.reuse = None
        self.scope = scope
        self.outputs = self.build(inputs, layer_sizes)


    def build(self, inputs, layer_sizes):
        ls = layer_sizes
        output = inputs
        last = len(ls) - 1

        for l, size_out in enumerate(ls):
            output = fclayer(output, size_out, self.wts_init, self.bias_init, self.reuse, self.scope+str(l))

            if l < last:
                output = lrelu(output)
            else:
                output = tf.squeeze(output)

        return output


def fclayer(input,
            size_out,
            wts_init=layers.xavier_initializer(),
            bias_init=tf.truncated_normal_initializer(stddev=1e-6),
            reuse=None,
            scope=None):
    return layers.fully_connected(
        inputs=input,
        num_outputs=size_out,
        activation_fn=None,
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


def lrelu(x, alpha=0.1):
    return tf.maximum(x, alpha*x)


class Decoder(object):
    def __init__(self, noisy, clean, scope='dec'):

        self.noisy = noisy
        self.clean = clean
        self.scope = scope
        self.rc_cost = 0
        u_l = tf.expand_dims(tf.cast(self.noisy.predict, tf.float32), axis=-1)  # label, with dim matching
        self.build(u_l)

    def build(self, u_l):

        for l in range(self.noisy.n_layers-1, -1, -1):
            u_l, rc_cost = self.compute_rc_cost(l, u_l)
            self.rc_cost += rc_cost

    def compute_rc_cost(self, l, v_l):
        noisy, clean = self.noisy, self.clean

        # Use decoder weights to upsample the signal from above
        size_out = noisy.layer_sizes[l]
        u_l = fclayer(v_l, size_out, scope=self.scope + str(l))

        # Unbatch-normalized activations from parallel layer in noisy encoder
        z_l = noisy.z[l]

        # Unbatch-normalized target activations from parallel layer in clean encoder
        target_z = clean.z[l]

        # Augmented multiplicative term
        uz_l = tf.multiply(u_l, z_l)

        inputs = tf.stack([u_l, z_l, uz_l], axis=-1)
        combinator = Combinator(inputs, layer_sizes=(2, 2, 1), stddev=0.025, scope='com' + str(l) + '_')
        recons = combinator.outputs
        rc_cost = tf.reduce_sum(tf.square(noisy.bn_layers[l].normalize_from_saved_stats(recons) - target_z), axis=-1)

        return v_l, rc_cost


class GammaDecoder(Decoder):
    def build(self, u_l):
        _, self.rc_cost = self.compute_rc_cost(self.noisy.n_layers-1, u_l)