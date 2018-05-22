import tensorflow as tf
from tensorflow.contrib.rnn import LSTMBlockCell, MultiRNNCell, OutputProjectionWrapper, ResidualWrapper
from tensorflow.contrib.seq2seq import BasicDecoder, AttentionWrapper
from text.symbols import symbols
from util.infolog import log
from .attention import LocationSensitiveAttention
from .helpers import TacoTestHelper, TacoTrainingHelper
from .modules import conv_and_lstm, postnet
from .rnn_wrappers import PrenetCell, FrameProjectionCell, TacotronDecoderCell
from tensorflow.python.ops import rnn_cell_impl

class Tacotron():
  def __init__(self, hparams):
    self._hparams = hparams


  def initialize(self, inputs, input_lengths, mel_targets=None, linear_targets=None):
    '''Initializes the model for inference.

    Sets "mel_outputs", "linear_outputs", and "alignments" fields.

    Args:
      inputs: int32 Tensor with shape [N, T_in] where N is batch size, T_in is number of
        steps in the input time series, and values are character IDs
      input_lengths: int32 Tensor with shape [N] where N is batch size and values are the lengths
        of each sequence in inputs.
      mel_targets: float32 Tensor with shape [N, T_out, M] where N is batch size, T_out is number
        of steps in the output time series, M is num_mels, and values are entries in the mel
        spectrogram. Only needed for training.
      linear_targets: float32 Tensor with shape [N, T_out, F] where N is batch_size, T_out is number
        of steps in the output time series, F is num_freq, and values are entries in the linear
        spectrogram. Only needed for training.
    '''
    with tf.variable_scope('inference') as scope:
      is_training = linear_targets is not None
      batch_size = tf.shape(inputs)[0]
      hp = self._hparams

      # Embeddings
      embedding_table = tf.get_variable(
        'embedding', [len(symbols), hp.embedding_dim], dtype=tf.float32,
        initializer=tf.truncated_normal_initializer(stddev=0.5))
      embedded_inputs = tf.nn.embedding_lookup(embedding_table, inputs)           # [N, T_in, 512]

      # Encoder
      encoder_outputs = conv_and_lstm(
        embedded_inputs,
        input_lengths,
        conv_layers=hp.encoder_conv_layers,
        conv_width=hp.encoder_conv_width,
        conv_channels=hp.encoder_conv_channels,
        lstm_units=hp.encoder_lstm_units,
        is_training=is_training,
        scope='encoder')                                                         # [N, T_in, 512]

      # Decoder prenet
      decoder_prenet = PrenetCell(is_training, scope='decoder_prenet')           # [N, T_in, 256]

      # Attention
      attention_mechanism = LocationSensitiveAttention(hp.attention_depth, encoder_outputs)

      # Decoder (layers specified bottom to top):
      decoder_lstm = MultiRNNCell([
        LSTMBlockCell(hp.decoder_lstm_units),
        LSTMBlockCell(hp.decoder_lstm_units)
      ], state_is_tuple=True)                                                    # [N, T_in, 1024]

      # Project onto r mel spectrograms (predict r outputs at each RNN step):
      frame_projection = FrameProjectionCell(hp.num_mels * hp.outputs_per_step)  # [N, T_in, M*r]

      # Decoder wrapper
      decoder_cell = TacotronDecoderCell(
        decoder_prenet,
        attention_mechanism,
        decoder_lstm,
        frame_projection)                                                        # [N, T_in, M*r]

      if is_training:
        helper = TacoTrainingHelper(inputs, mel_targets, hp.num_mels, hp.outputs_per_step)
      else:
        helper = TacoTestHelper(batch_size, hp.num_mels, hp.outputs_per_step)

      decoder_init_state = decoder_cell.zero_state(batch_size=batch_size, dtype=tf.float32)
      (multi_decoder_outputs, _), final_decoder_state, _ = tf.contrib.seq2seq.dynamic_decode(
        BasicDecoder(decoder_cell, helper, decoder_init_state),
        maximum_iterations=hp.max_iters)                                        # [N, T_out/r, M*r]

      # Reshape outputs to be one output per entry                                [N, T_out, M]
      decoder_outputs = tf.reshape(multi_decoder_outputs, [batch_size, -1, hp.num_mels])

      # Postnet: predicts a residual
      postnet_outputs = postnet(
        decoder_outputs,
        layers=hp.postnet_conv_layers,
        conv_width=hp.postnet_conv_width,
        channels=hp.postnet_conv_channels,
        is_training=is_training)
      mel_outputs = decoder_outputs + postnet_outputs

      # Convert to linear using a similar architecture as the encoder:
      expand_outputs = conv_and_lstm(
        mel_outputs,
        None,
        conv_layers=hp.expand_conv_layers,
        conv_width=hp.expand_conv_width,
        conv_channels=hp.expand_conv_channels,
        lstm_units=hp.expand_lstm_units,
        is_training=is_training,
        scope='expand')                                                        # [N, T_in, 512]
      linear_outputs = tf.layers.dense(expand_outputs, hp.num_freq)            # [N, T_out, F]

      # Grab alignments from the final decoder state:
      alignments = tf.transpose(final_decoder_state.alignment_history.stack(), [1, 2, 0])

      self.inputs = inputs
      self.input_lengths = input_lengths
      self.decoder_outputs = decoder_outputs
      self.mel_outputs = mel_outputs
      self.linear_outputs = linear_outputs
      self.linear_targets = linear_targets
      self.alignments = alignments
      self.mel_targets = mel_targets
      log('Initialized Tacotron model. Dimensions: ')
      log('  embedding:               %d' % embedded_inputs.shape[-1])
      log('  encoder out:             %d' % encoder_outputs.shape[-1])
      log('  decoder cell out:        %d' % decoder_cell.output_size)
      log('  decoder out (%d frames):  %d' % (hp.outputs_per_step, decoder_outputs.shape[-1]))
      log('  decoder out (1 frame):   %d' % mel_outputs.shape[-1])
      log('  expand out:              %d' % expand_outputs.shape[-1])
      log('  linear out:              %d' % linear_outputs.shape[-1])


  def add_loss(self):
    '''Adds loss to the model. Sets "loss" field. initialize must have been called.'''
    with tf.variable_scope('loss') as scope:
      hp = self._hparams

      # Compute loss of predictions before postnet
      self.decoder_loss = tf.losses.mean_squared_error(self.mel_targets, self.decoder_outputs)
      # Compute loss after postnet
      self.mel_loss = tf.losses.mean_squared_error(self.mel_targets, self.mel_outputs)

      # Prioritize loss for frequencies under 2000 Hz.
      l1 = tf.abs(self.linear_targets - self.linear_outputs)
      n_priority_freq = int(2000 / (hp.sample_rate * 0.5) * hp.num_freq)
      self.linear_loss = 0.5 * tf.reduce_mean(l1) + 0.5 * tf.reduce_mean(l1[:,:,0:n_priority_freq])

      # Compute the regularization weight
      reg_weight_scaler = 1. / (2 * hp.max_abs_value) if hp.symmetric_mels else 1. / (hp.max_abs_value)
      reg_weight = 1e-6 * reg_weight_scaler

      # Get all trainable variables
      all_vars = tf.trainable_variables()
      self.regularization_loss = tf.add_n([tf.nn.l2_loss(v) for v in all_vars
        if not('bias' in v.name or 'Bias' in v.name)]) * reg_weight

      self.loss = self.decoder_loss + self.mel_loss + self.linear_loss + self.regularization_loss


  def add_optimizer(self, global_step):
    '''Adds optimizer. Sets "gradients" and "optimize" fields. add_loss must have been called.

    Args:
      global_step: int32 scalar Tensor representing current global step in training
    '''
    with tf.variable_scope('optimizer') as scope:
      hp = self._hparams
      self.learning_rate = tf.train.exponential_decay(
        hp.initial_learning_rate, global_step, hp.learning_rate_decay_halflife, 0.5)
      optimizer = tf.train.AdamOptimizer(self.learning_rate, hp.adam_beta1, hp.adam_beta2)
      gradients, variables = zip(*optimizer.compute_gradients(self.loss))
      self.gradients = gradients
      clipped_gradients, _ = tf.clip_by_global_norm(gradients, 1.0)

      # Add dependency on UPDATE_OPS; otherwise batchnorm won't work correctly. See:
      # https://github.com/tensorflow/tensorflow/issues/1122
      with tf.control_dependencies(tf.get_collection(tf.GraphKeys.UPDATE_OPS)):
        self.optimize = optimizer.apply_gradients(zip(clipped_gradients, variables),
          global_step=global_step)
