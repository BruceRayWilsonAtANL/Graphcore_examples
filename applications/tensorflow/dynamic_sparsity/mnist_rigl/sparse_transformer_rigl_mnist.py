# Copyright (c) 2020 Graphcore Ltd. All rights reserved.
import os
import json
import time
import logging
import tempfile
import numpy as np
from tqdm import tqdm
from functools import partial
import tensorflow.compat.v1 as tf
from tensorflow.python.ipu import utils, ipu_compiler, scopes, loops
from tensorflow.python.ipu.ipu_infeed_queue import IPUInfeedQueue
from tensorflow.python.ipu.ipu_outfeed_queue import IPUOutfeedQueue
from tensorflow.python.ipu.config import IPUConfig

from sparse_mnist import build_optimizer
os.sys.path.append("../")  # dynamic_sparsity
from ipu_sparse_ops import optimizers  # noqa: E402
from ipu_sparse_ops.model_baseclass import SparseModelOptions  # noqa: E402
from ipu_sparse_ops.transformer.transformer_baseclass import TransformerOptions   # noqa: E402
from ipu_sparse_ops.transformer.transformer_dynsparse import DynsparseTransformer  # noqa: E402

tf.disable_eager_execution()
tf.disable_v2_behavior()

logger = logging.getLogger(__file__)


def get_program_options():
    parser = TransformerOptions()
    SparseModelOptions.add_all_arguments(parser)
    parser.add_argument("--mode", choices=['train', 'test', 'all'],
                        default="all", help="Choices are [training, test, all]")
    parser.add_argument("--train-checkpoint-path", type=str,
                        help="Path to which to save the trained model or load model from.")
    parser.add_argument("--nepochs", default=1, type=int, help="Number of training epochs")
    parser.add_argument("--steps-per-epoch", type=int, default=5, help="Number of times to run prune and grow every epoch")
    parser.add_argument("--optimizer", type=str, default="Adam", choices=["GradientDescent", "Momentum", "Adam"],
                        help="Which optimizer to use.")

    def parse_optimizer_arg(arg):
        name, value = arg.split('=')
        return (name, json.loads(value))

    parser.add_argument("--optimizer-arg", type=parse_optimizer_arg, action="append",
                        help="Extra argument for the chosen optimizer of the form argname=value. "
                        "Example: `use_nesterov=false`. "
                        "Can be input multiple times.")

    default_settings = dict(
        batch_size=2,
        nepochs=1,
        num_shards=1,
        sparsity=0.90,
        prune_ratio=0.30,
        dtype=tf.float32,
        source_sequence_length=28,
        target_vocab_length=10,
        hidden_length=96,
        ff_length=48,
        attention_heads=16,
        qkv_length=32,
        log_level="DEBUG",
        regrow_type="rigl",
        train_checkpoint_path=tempfile.mkdtemp(),
        mode="all"
    )
    parser.set_defaults(**default_settings)
    opts = parser.parse_args()
    logging.basicConfig(level=logging.getLevelName(opts.log_level),
                        format='%(asctime)s %(name)s %(levelname)s %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S')
    return opts


def forward_pass(opts, transformer, lr, iterations_per_step, is_training,
                 outfeed, png_outfeed, source, target):
    # Input may require padding for some block-sizes as
    B, S, H = source.shape.as_list()
    if (H % opts.block_size) != 0:
        pad_size = (1 + (H // opts.block_size)) * opts.block_size - H
        source = tf.pad(source, [[0, 0], [0, 0], [0, pad_size]])
    transformer.embedding_length = source.shape.as_list()[-1]

    with tf.variable_scope("counter", reuse=tf.AUTO_REUSE, use_resource=True):
        itr_counter = tf.get_variable("iterations", [], tf.int32)
        mod_itrs = tf.math.floormod(itr_counter, iterations_per_step)
        last_itr = tf.equal(mod_itrs, 0)
        increment_counter = tf.assign_add(itr_counter, 1)

    with tf.variable_scope("transformer", reuse=tf.AUTO_REUSE, use_resource=True):
        transformer.compute_dense_grad = last_itr

        # Add position embeddings to prevent permutation invariance
        x = transformer.position_encoder(source, transformer.source_sequence_length)

        # Project image to hidden dimension (to enable skip connects)
        with transformer.namescope("embedding_projection"):
            x = transformer.sparseLinear(x, transformer.sparsity,
                                         transformer.hidden_length, last_itr, use_bias=True)

        # Use a single encoder layer  x [B, S, H]
        x = transformer.encoder_layer(x, mask=None, debug_name="encoder_layer0")

        # Each token position then produces logits independently.
        # The model logits is the sum over all output tokens
        with transformer.namescope("output"):
            # The output dimension may need to be padded to conform to block size
            T = transformer.target_vocab_length
            output_pad_size = 0
            if (T % opts.block_size) != 0:
                output_pad_size = (1 + (T // opts.block_size)) * opts.block_size - T

            # Compute output
            x = transformer.sparseLinear(x, transformer.sparsity, T + output_pad_size, last_itr, use_bias=False)

            # Remove padding
            if output_pad_size > 0:
                x = x[:, :, :T]

            model_output = tf.reduce_sum(x, axis=1)  # [B, S, 10] -> [B, 10]

    with tf.variable_scope("metrics", reuse=tf.AUTO_REUSE, use_resource=True):
        predictions = tf.argmax(model_output, axis=1, output_type=tf.int32)
        acc, acc_op = tf.metrics.accuracy(target, predictions, name="accuracy")
        # Sparse softmax can lead to NaNs very easily in float16
        logits = model_output if model_output.dtype == tf.float32 else tf.cast(model_output, tf.float32)
        loss = tf.nn.sparse_softmax_cross_entropy_with_logits(labels=target, logits=logits)
        mean_loss = tf.reduce_mean(loss, name='train_loss')

    if is_training:
        with tf.variable_scope("training", reuse=tf.AUTO_REUSE, use_resource=True):
            optimizer_class, optimizer_kwargs = build_optimizer(opts.optimizer, opts.optimizer_arg)
            optimizer = optimizers.SparseOptimizer(optimizer_class)
            optimizer = optimizer(learning_rate=lr, **optimizer_kwargs,
                                  sparse_layers=transformer.sparse_layers.values(),
                                  prune_and_grow_outfeed=png_outfeed,
                                  dense_gradient_condition=last_itr)
            train_op = optimizer.minimize(loss)

        metrics = {'mean_loss': mean_loss, 'acc': acc, 'iteration': itr_counter}

        # Only stream back final metrics:
        def true_fn():
            with tf.control_dependencies([outfeed.enqueue(metrics), acc_op]):
                return tf.no_op()

        with tf.control_dependencies([train_op]):
            condition = tf.cond(last_itr, true_fn, tf.no_op)
            output = tf.group(increment_counter, condition)

    else:
        # At inference time stream back the loss and accuracy
        with tf.control_dependencies([acc_op]):
            mean_loss = tf.reduce_mean(loss, name='test_loss')
        output = outfeed.enqueue({'mean_loss': mean_loss, 'acc': acc, 'iteration': itr_counter})
    return output


def learning_rate_schedule(epoch, opts):
    progress = epoch / opts.nepochs
    lr_scale = 2e-3 / opts.batch_size
    if progress < 0.25:
        return 1 * lr_scale
    if progress < 0.50:
        return 0.5 * lr_scale
    if progress < 0.75:
        return 0.30 * lr_scale
    else:
        return 0.10 * lr_scale


def run_training(opts, transformer, x_train, y_train):
    # Calculate dataset length
    num_train = len(y_train)
    batches_per_epoch = num_train // opts.batch_size
    batches_per_step = batches_per_epoch // (opts.steps_per_epoch)
    total_steps = (opts.steps_per_epoch) * opts.nepochs
    logging.info(f"Batches per epoch: {batches_per_epoch} Batches per step: {batches_per_step}")

    if not batches_per_epoch % (opts.steps_per_epoch) == 0:
        raise ValueError(f"IPU steps per epoch {opts.steps_per_epoch} must divide batches per epoch {batches_per_epoch} exactly.")

    # Construct the training graph
    training_graph = tf.Graph()
    with training_graph.as_default():
        with tf.device("cpu"):
            input_shape = [None, *x_train.shape[1:]]
            place_x = tf.placeholder(dtype=opts.dtype, shape=input_shape, name="input")
            place_y = tf.placeholder(dtype=tf.int32, shape=[None], name="label")
            lr_placeholder = tf.placeholder(opts.dtype, shape=[])

            # Create dataset and IPU feeds:
            dataset = tf.data.Dataset.from_tensor_slices((place_x, place_y))
            dataset = dataset.shuffle(buffer_size=len(y_train), reshuffle_each_iteration=True, seed=opts.random_seed).cache()
            dataset = dataset.repeat().batch(opts.batch_size, drop_remainder=True)

            # Queues for streaming from host to device and back
            train_infeed = IPUInfeedQueue(dataset, feed_name="train_infeed")
            train_outfeed = IPUOutfeedQueue(feed_name="train_outfeed")
            png_outfeed = IPUOutfeedQueue(feed_name="prune_and_grow_outfeed")

            # Helper function
            def loop_builder(iterations, builder_func, infeed):
                return loops.repeat(iterations, builder_func, [], infeed)

            # Compile the forward and backward pass for training
            with scopes.ipu_scope("/device:IPU:0"):
                train_loop = partial(forward_pass, opts, transformer, lr_placeholder, batches_per_step, True, train_outfeed, png_outfeed)
                train_loop = partial(loop_builder, batches_per_step, train_loop, train_infeed)
                train_loop = ipu_compiler.compile(train_loop, inputs=[])
                transformer.buildSparsityUpdateOps()

            # Metrics
            with tf.device("cpu"):
                metrics_vars = tf.get_collection(tf.GraphKeys.LOCAL_VARIABLES, scope="metrics")
                metrics_initializer = tf.variables_initializer(var_list=metrics_vars)
                saver = tf.train.Saver(max_to_keep=5)

                # These ops are declared here so that the graph can be frozen afterwards
                global_initializer = tf.global_variables_initializer()
                train_outfeed_dequeue = train_outfeed.dequeue()
                png_outfeed_dequeue = png_outfeed.dequeue()

    # Setup and acquire an IPU device:
    config = IPUConfig()
    config.auto_select_ipus = opts.num_shards
    config.configure_ipu_system()

    logpath = os.path.join(opts.train_checkpoint_path, "train")
    summary_writer = tf.summary.FileWriter(logpath)

    # Run the model:
    training_graph.finalize()  # no more new ops added from here on out
    with tf.Session(graph=training_graph) as sess:
        logger.info(f"Creating training session")
        sess.run(global_initializer)
        sess.run(train_infeed.initializer, feed_dict={place_x: x_train, place_y: y_train})

        progress = tqdm(range(opts.nepochs), bar_format='{desc} Epoch: {n_fmt}/{total_fmt} {bar}')
        for e in progress:
            for i in range(opts.steps_per_epoch):
                # Train the model
                sess.run(metrics_initializer)
                dt = time.perf_counter()
                sess.run(train_loop, feed_dict={lr_placeholder: learning_rate_schedule(e, opts)})
                dt = time.perf_counter() - dt
                session_outputs = sess.run(train_outfeed_dequeue)
                logger.debug(f"Train outputs: {session_outputs}")

                # Calculate avg throughput
                num_tokens = transformer.source_sequence_length * batches_per_step * opts.batch_size
                throughput = num_tokens / dt
                desc = f"Loss {session_outputs['mean_loss'][-1]:.5f} " \
                       f"Accuracy {session_outputs['acc'][-1]:.5f} " \
                       f"Iteration: {session_outputs['iteration'][-1]}"
                progress.set_description(desc + f" Throughput {throughput:.1f} token/s")

                # Perform pruning (if using RigL the dense grads from session_outputs are used)
                step = 1 + i + e * (opts.steps_per_epoch)
                if transformer.prune_ratio is not None:
                    t0 = time.perf_counter()
                    png_results = sess.run(png_outfeed_dequeue)
                    t1 = time.perf_counter()
                    for k in png_results:
                        png_results[k] = png_results[k][-1]
                    logger.debug(f"Prune and grow outputs: {png_results.keys()}")
                    logger.info(f"Downloaded the prune and grow data from Device to Host in {t1-t0:0.3f} seconds")

                    transformer.syncPruneAndRegrowOnHost(opts.cosine_prune_schedule, step, total_steps, png_results)
                    transformer.streamSparsityFromHostToDevice()

            # Save at the end of each epoch
            logger.info(f"Saving model")
            saver.save(sess, os.path.join(opts.train_checkpoint_path, 'model.ckpt'))


def run_testing(opts, transformer, x_test, y_test):
    batches_per_epoch = len(y_test) // opts.batch_size
    testing_graph = tf.Graph()
    with testing_graph.as_default():
        with tf.device("cpu"):
            input_shape = [None, *x_test.shape[1:]]
            place_x = tf.placeholder(dtype=opts.dtype, shape=input_shape, name="input")
            place_y = tf.placeholder(dtype=tf.int32, shape=[None], name="label")

            # Create dataset and IPU feeds:
            dataset = tf.data.Dataset.from_tensor_slices((place_x, place_y)).cache()
            dataset = dataset.batch(opts.batch_size, drop_remainder=True)
            test_infeed = IPUInfeedQueue(dataset, feed_name="test_infeed")
            test_outfeed = IPUOutfeedQueue(feed_name="test_outfeed")

            # Helper function
            def loop_builder(iterations, builder_func, infeed):
                return loops.repeat(iterations, builder_func, [], infeed)

            # Compile the forward pass for testing
            with scopes.ipu_scope("/device:IPU:0"):
                test_loop = partial(forward_pass, opts, transformer, None, batches_per_epoch, False, test_outfeed, None)
                test_loop = partial(loop_builder, batches_per_epoch, test_loop, test_infeed)
                test_loop = ipu_compiler.compile(test_loop, inputs=[])

            # Metrics
            with tf.device("cpu"):
                metrics_vars = tf.get_collection(tf.GraphKeys.LOCAL_VARIABLES, scope="metrics")
                metrics_initializer = tf.variables_initializer(var_list=metrics_vars)
                saver = tf.train.Saver()

                test_outfeed_dequeue = test_outfeed.dequeue()

    # Setup and acquire an IPU device:
    config = IPUConfig()
    config.auto_select_ipus = opts.num_shards
    config.configure_ipu_system()

    logpath = os.path.join(opts.train_checkpoint_path, "test")
    checkpoint = tf.train.latest_checkpoint(opts.train_checkpoint_path)
    summary_writer = tf.summary.FileWriter(logpath)

    testing_graph.finalize()  # no more new ops added from here on out
    with tf.Session(graph=testing_graph) as sess:
        logger.info(f"Testing...")
        # The sparsity will also  be streamed from the checkpoint
        # The host and device sparsity are not in sync here
        saver.restore(sess, checkpoint)
        sess.run(test_infeed.initializer, feed_dict={place_x: x_test, place_y: y_test})
        sess.run(metrics_initializer)

        # Run inference (whole dataset in one session call)
        dt = time.perf_counter()
        sess.run(test_loop)
        dt = time.perf_counter() - dt
        session_outputs = sess.run(test_outfeed_dequeue)

        # Test set performance
        throughput = transformer.source_sequence_length * len(y_test) / dt
        test_loss = session_outputs['mean_loss'].mean()
        test_acc = session_outputs['acc'][-1]
        desc = f"Test loss: {test_loss:.8f} Test accuracy: {test_acc:.8f}"
        logger.info(desc + f" Throughput {throughput:.1f} token/s")

    # Regression tests
    accuracy_threshold = 0.85
    assert test_acc >= accuracy_threshold, f"Test accuracy ({test_acc:3.2f}) is below threshold of ({accuracy_threshold:3.2f})"
    print("All asserts pass.")


def run_mnist(opts):
    if opts.random_seed is not None:
        utils.reset_ipu_seed(opts.random_seed)

    # MNIST
    numpy_dtype = opts.dtype.as_numpy_dtype
    (x_train, y_train), (x_test, y_test) = tf.keras.datasets.mnist.load_data()
    x_train, x_test = x_train / 255.0, x_test / 255.0
    x_train, x_test = x_train.astype(numpy_dtype), x_test.astype(numpy_dtype)
    y_train, y_test = y_train.astype(np.int32), y_test.astype(np.int32)

    # Create a transformer object (does not build a graph until called)
    if opts.mode in ["all", "train"]:
        training_transformer = DynsparseTransformer(opts)
        run_training(opts, training_transformer, x_train, y_train)

    if opts.mode in ["all", "test"]:
        testing_transformer = DynsparseTransformer(opts)
        run_testing(opts, testing_transformer, x_test, y_test)


if __name__ == '__main__':
    opts = get_program_options()
    run_mnist(opts)
