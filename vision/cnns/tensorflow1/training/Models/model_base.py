# Copyright (c) 2020 Graphcore Ltd. All rights reserved.
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
from typing import Dict, List
import tensorflow as tf
from functools import partial
import warnings


def custom_dtype_getter(getter, name, dtype, trainable,
                        master_weight_filter_fn,
                        shape=None, *args, **kwargs):
    master_dtype = master_weight_filter_fn(name)
    if dtype != master_dtype and trainable:
        var = getter(
            name, shape, master_dtype, *args, trainable=trainable, **kwargs
        )
        return tf.cast(var, dtype=dtype, name=name + "_cast")
    else:
        return getter(name, shape, dtype, *args, trainable=trainable, **kwargs)


class ModelBase:
    def __init__(self, opts, is_training=True):
        dtypes = opts["precision"].split(".")
        self.dtype = tf.float16 if dtypes[0] == "16" else tf.float32
        self.master_weight_default_dtype = tf.float32 if dtypes[1] == "32" else tf.float16

        # Sometimes the argument is parsed as a string try and take corrective
        # action and warn the user.
        self._master_weight_dtype_overrides: Dict["dtype", List[str]] = {}
        for dtype, option in ((tf.float16, "force_weight_to_fp16"), (tf.float32, "force_weight_to_fp32")):
            value = opts.get(option, [])
            if isinstance(value, str):
                value = [value]
                warnings.warn(f"'{option}' was set with a string rather than a list of strings,"
                              f" filter interpreted as {value}")
            self._master_weight_dtype_overrides[dtype] = value

        self.custom_dtype_getter = partial(
            custom_dtype_getter,
            master_weight_filter_fn=self.master_weight_filter_fn,
        )

        # Apply dataset specific changes
        if opts["dataset"] == "imagenet":
            self.num_classes = 1000
        elif opts["dataset"] == "cifar-10":
            self.num_classes = 10
        elif opts["dataset"] == "cifar-100":
            self.num_classes = 100
        else:
            raise ValueError("Unknown Dataset {}".format(opts["dataset"]))

    def master_weight_filter_fn(self, name):
        output_dtype = None
        for dtype, variable_filters in self._master_weight_dtype_overrides.items():
            if any(variable_filter in name for variable_filter in variable_filters):
                if output_dtype is not None:
                    raise RuntimeError(
                        f"Attempting to force master weight of variable '{name}' "
                        "into both FP16 and FP32. Check the filters that were set with "
                        "--force-weight-to-fp32 and --force-weight-to-fp16")
                output_dtype = dtype

        return output_dtype if output_dtype is not None else self.master_weight_default_dtype

    def _build_function_list(self):
        raise NotImplementedError

    def build_whole_graph(self, x):
        fn_list = self._build_function_list()

        tf.add_to_collection("activations", x)
        with tf.variable_scope("all", use_resource=True, custom_getter=self.custom_dtype_getter):
            for fn in fn_list:
                x = fn(x)
        return x

    def first_stage(self, x, first_split_name):
        self.fn_list = self._build_function_list()
        if first_split_name not in [f.keywords["name"] for f in self.fn_list]:
            raise ValueError(
                "Couldn't find pipeline split called " + first_split_name
            )
        tf.add_to_collection("activations", x)
        with tf.variable_scope(
            "all", use_resource=True, custom_getter=self.custom_dtype_getter
        ):
            for fn in self.fn_list:
                if fn.keywords["name"] == first_split_name:
                    break
                x = fn(x)
        return x

    def later_stage(self, x, prev_split_name, end_split_name):
        if end_split_name is not None and end_split_name not in [
            fn.keywords["name"] for fn in self.fn_list
        ]:
            raise ValueError(
                "Couldn't find pipeline split called " + end_split_name
            )
        with tf.variable_scope(
            "all", use_resource=True, custom_getter=self.custom_dtype_getter
        ):
            first_stage = False
            for f in self.fn_list:
                if (not first_stage and f.keywords["name"] != prev_split_name):
                    continue
                first_stage = True
                if f.keywords["name"] == end_split_name:
                    break
                x = f(x)
        return x

    def __call__(self, x):
        return self.build_whole_graph(x)
