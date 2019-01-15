"""Optimization related functions."""

import collections

import tensorflow as tf

from opennmt import optimizers
from opennmt.utils import decay
from opennmt.optimizers.mixed_precision_wrapper import get_loss_scale_from_params


def learning_rate_decay_fn(decay_type,
                           decay_params=None,
                           decay_step_duration=1,
                           start_decay_step=0,
                           minimum_learning_rate=0.0):
  """Returns the learning rate decay function.

  Args:
    decay_type: The type of decay. A function from ``tf.train`` or
      :mod:`opennmt.utils.decay` as a string.
    decay_params: Additional parameters for the decay function.
    decay_step_duration: The number of training steps that make 1 decay step.
    start_decay_step: Start decay after this many steps.
    minimum_learning_rate: Do not decay past this learning rate value.

  Returns:
    A function with signature
    ``(learning_rate, global_step) -> decayed_learning_rate``.

  Raises:
    ValueError: if :obj:`decay_type` can not be resolved.
  """
  if decay_params is None:
    decay_params = {}

  def _decay_fn(learning_rate, global_step):
    decay_op_name = None

    if decay_op_name is None:
      decay_op_name = getattr(tf.train, decay_type, None)
    if decay_op_name is None:
      decay_op_name = getattr(decay, decay_type, None)
    if decay_op_name is None:
      raise ValueError("Unknown decay function: {}".format(decay_type))

    # Map the training step to a decay step.
    step = tf.maximum(global_step - start_decay_step, 0)
    step //= decay_step_duration

    learning_rate = decay_op_name(learning_rate, step, **decay_params)
    return tf.maximum(learning_rate, minimum_learning_rate)

  return _decay_fn

def get_optimizer_class(classname):
  """Returns the optimizer class.

  Args:
    classname: The name of the optimizer class in ``tf.train``,
      ``tf.contrib.opt``, or ``opennmt.optimizers`` as a string.

  Returns:
    A class inheriting from ``tf.train.Optimizer``.

  Raises:
    ValueError: if :obj:`classname` can not be resolved.
  """
  optimizer_class = None

  if optimizer_class is None:
    optimizer_class = getattr(tf.train, classname, None)
  if optimizer_class is None:
    optimizer_class = getattr(tf.contrib.opt, classname, None)
  if optimizer_class is None:
    optimizer_class = getattr(optimizers, classname, None)
  if optimizer_class is None:
    raise ValueError("Unknown optimizer class: {}".format(classname))

  return optimizer_class

def optimize_loss(loss, params, mixed_precision=False, var_list=None):
  """Minimizes the loss.

  Args:
    loss: The loss to minimize.
    params: A dictionary of hyperparameters.
    mixed_precision: If ``True``, wraps the optimizer to maintain a float32 copy
      of the weights.
    var_list: The variables to update.

  Returns:
    The loss minimization op and a list of internal variables to initialize.
  """
  regularization = params.get("regularization")
  if regularization is not None:
    loss += regularization_penalty(
        regularization["type"], regularization["scale"], weights_list=var_list)

  global_step = tf.train.get_or_create_global_step()

  learning_rate = tf.constant(params["learning_rate"], dtype=tf.float32)
  if params.get("decay_type") is not None:
    decay_fn = learning_rate_decay_fn(
        params["decay_type"],
        decay_params=params.get("decay_params", {}),
        decay_step_duration=params.get("decay_step_duration", 1),
        start_decay_step=params.get("start_decay_steps", 0),
        minimum_learning_rate=params.get("minimum_learning_rate", 0))
    learning_rate = decay_fn(learning_rate, global_step)
  tf.summary.scalar("learning_rate", learning_rate)

  with tf.variable_scope("optim"):
    # Optimizer.
    optimizer_class = get_optimizer_class(params["optimizer"])
    optimizer_params = params.get("optimizer_params", {})
    if optimizer_class.__name__ == "AdafactorOptimizer":
      optimizer = optimizers.get_adafactor_optimizer_from_params(
          optimizer_class, optimizer_params, learning_rate=learning_rate)
    else:
      optimizer = optimizer_class(learning_rate, **optimizer_params)
    if mixed_precision:
      optimizer = optimizers.MixedPrecisionOptimizerWrapper(
          optimizer, loss_scale=get_loss_scale_from_params(params))

    # Gradients.
    gradients = optimizer.compute_gradients(
        loss, var_list=var_list, colocate_gradients_with_ops=True)
    _summarize_gradients_norm("global_norm/gradient_norm", gradients)
    if params.get("clip_gradients") is not None:
      gradients = _clip_gradients_by_norm(gradients, float(params["clip_gradients"]))
      _summarize_gradients_norm("global_norm/clipped_gradient_norm", gradients)

    return delayed_update(
        optimizer,
        gradients,
        global_step,
        accum_count=params.get("gradients_accum", 1))

def delayed_update(optimizer, grads_and_vars, global_step, accum_count=1):
  """Possibly delays the parameters update by first accumulating gradients.

  Args:
    optimizer: The optimizer.
    grads_and_vars: List of (gradient, variable) pairs.
    global_step: The training step that will be increased when the parameters
      are updated.
    accum_count: The number of times to accumulate gradients, as a constant or
      a ``tf.Tensor``.

  Returns:
    An operation that conditionally applies the gradients and a list of internal
    variables to initialize.
  """
  if not tf.contrib.framework.is_tensor(accum_count) and accum_count == 1:
    return optimizer.apply_gradients(grads_and_vars, global_step=global_step), []

  model_step = tf.Variable(0, trainable=False, collections=[], dtype=tf.int64)
  accum_grads = []
  accum_grads_and_vars = []
  for grad, var in grads_and_vars:
    accum_grad = tf.Variable(
        tf.zeros(var.shape, dtype=grad.dtype),
        trainable=False,
        collections=[])
    accum_grads.append(accum_grad)
    accum_grads_and_vars.append((accum_grad, var))

  def _accum_grads(accum_fn=tf.assign_add, apply_gradients=False):
    update_ops = [model_step.assign_add(1)]
    for accum_grad, (grad, _) in zip(accum_grads, grads_and_vars):
      with tf.control_dependencies([grad]):
        update_ops.append(accum_fn(accum_grad, grad))
    with tf.control_dependencies(update_ops):
      if apply_gradients:
        # Override the current name scope to create the optimizer slot variables
        # in the same scope as if the optimizer was called outside of tf.cond.
        # This is needed to ensure we can continue from a model trained without
        # gradient accumulation (and vice-versa).
        with tf.name_scope("%s/" % tf.get_variable_scope().name):
          return optimizer.apply_gradients(accum_grads_and_vars, global_step=global_step)
      else:
        return tf.no_op()

  update_op = tf.cond(
      tf.equal((model_step + 1) % accum_count, 0),
      true_fn=lambda: _accum_grads(apply_gradients=True),
      false_fn=lambda: tf.cond(
          tf.equal(model_step % accum_count, 0),
          true_fn=lambda: _accum_grads(accum_fn=tf.assign),
          false_fn=_accum_grads))
  extra_variables = accum_grads + [model_step]
  return update_op, extra_variables

def regularization_penalty(regularization_type, scale, weights_list=None):
  """Computes the weights regularization penalty.

  Args:
    regularization_type: The regularization type: ``l1``, ``l2``, or ``l1_l2``.
    scale: The regularization multiplier. If :obj:`regularization_type` is
      ``l1_l2``, this should be a list or tuple containing the L1 regularization
      scale and the L2 regularization scale.
    weights_list: The list of weights. Defaults to non bias variables.

  Returns:
    The regularization penalty.

  Raises:
    ValueError: if :obj:`regularization_type` is invalid or is ``l1_l2`` but
      :obj:`scale` is not a sequence.
  """
  def _is_bias(variable):
    return len(variable.shape.as_list()) == 1 and variable.name.endswith("bias:0")
  if weights_list is None:
    weights_list = tf.trainable_variables()
  weights_list = list(filter(lambda v: not _is_bias(v), weights_list))

  regularization_type = regularization_type.lower()
  if regularization_type == "l1":
    regularizer = tf.keras.regularizers.l1(l=float(scale))
  elif regularization_type == "l2":
    regularizer = tf.keras.regularizers.l2(l=float(scale))
  elif regularization_type == "l1_l2":
    if not isinstance(scale, collections.Sequence) or len(scale) != 2:
      raise ValueError("l1_l2 regularization requires 2 scale values")
    regularizer = tf.keras.regularizers.l1_l2(
        l1=float(scale[0]), l2=float(scale[1]))
  else:
    raise ValueError("invalid regularization type %s" % regularization_type)

  penalty = tf.add_n([regularizer(w) for w in weights_list])
  return penalty

def _clip_gradients_by_norm(grads_and_vars, clip_gradients):
  """Clips gradients by global norm."""
  gradients, variables = zip(*grads_and_vars)
  clipped_gradients, _ = tf.clip_by_global_norm(gradients, clip_gradients)
  return list(zip(clipped_gradients, variables))

def _summarize_gradients_norm(name, gradients):
  """Summarizes global norm of gradients."""
  tf.summary.scalar(name, tf.linalg.global_norm(list(zip(*gradients))[0]))
