# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD 3-Clause license found in the
# LICENSE file in the root directory of this source tree.
"""
A simple module swap UX for a float8 version of `torch.nn.Linear`.
"""

import dataclasses
import enum

from typing import Optional

import torch

from float8_experimental.config import Float8LinearConfig, TensorScalingType

from float8_experimental.float8_dynamic_utils import (
    cast_to_float8_e4m3_dynamic,
    cast_to_float8_e5m2_dynamic_bw,
)

from float8_experimental.float8_tensor import (
    Float8Tensor,
    GemmInputRole,
    LinearMMConfig,
    ScaledMMConfig,
    to_fp8_no_autograd,
)

from float8_experimental.float8_utils import (
    amax_history_to_scale,
    e4m3_dtype,
    e5m2_dtype,
    tensor_to_amax,
)

from float8_experimental.fsdp_utils import (
    WeightWithDelayedFloat8CastTensor,
    WeightWithDynamicFloat8CastTensor,
)


def _maybe_initialize_amaxes_scales_for_float8_cast(
    x,
    cur_amax,
    amax_history,
    scale,
    scale_fn_name,
    float8_dtype,
    is_initialized,
    reduce_amax,
):
    """
    If x is about to be cast to `float8` and the amax buffers are not initialized,
    initializes them inplace.
    """
    if is_initialized:
        return
    with torch.no_grad():
        # Note: we need to enable distributed reduction here in order
        # to match numerics between single GPU and multi GPU code for
        # activations and gradients
        new_amax = tensor_to_amax(x, reduce_amax=reduce_amax)
        cur_amax.fill_(new_amax)
        amax_history[0] = new_amax
        new_scale = amax_history_to_scale(
            amax_history, float8_dtype, x.dtype, scale_fn_name
        )
        scale.copy_(new_scale)


@torch._dynamo.allow_in_graph
class NoopFwToFloat8E5M2Bw(torch.autograd.Function):
    """
    Forward: no-op
    Backward: convert to float8_e5m2, initialize if needed
    """

    @staticmethod
    def forward(
        ctx,
        tensor,
        fp8_amax_grad_output,
        fp8_amax_history_grad_output,
        fp8_scale_grad_output,
        scale_fn_name,
        is_amax_initialized,
        linear_mm_config: LinearMMConfig,
    ):
        ctx.save_for_backward(
            fp8_amax_grad_output, fp8_amax_history_grad_output, fp8_scale_grad_output
        )
        ctx.scale_fn_name = scale_fn_name
        ctx.is_amax_initialized = is_amax_initialized
        ctx.linear_mm_config = linear_mm_config
        return tensor

    @staticmethod
    def backward(ctx, go):
        (
            fp8_amax_grad_output,
            fp8_amax_history_grad_output,
            fp8_scale_grad_output,
        ) = ctx.saved_tensors
        scale_fn_name = ctx.scale_fn_name
        is_amax_initialized = ctx.is_amax_initialized

        _maybe_initialize_amaxes_scales_for_float8_cast(
            go,
            fp8_amax_grad_output,
            fp8_amax_history_grad_output,
            fp8_scale_grad_output,
            scale_fn_name,
            e5m2_dtype,
            is_amax_initialized,
            reduce_amax=True,
        )

        fp8_amax_grad_output.fill_(tensor_to_amax(go))

        res = to_fp8_no_autograd(
            go,
            fp8_scale_grad_output,
            e5m2_dtype,
            linear_mm_config=ctx.linear_mm_config,
            gemm_input_role=GemmInputRole.DL_DY,
        )
        empty_grads = None, None, None, None, None, None
        return res, *empty_grads


class Float8Linear(torch.nn.Linear):
    """
    Note: this is **not** a public API and is only intended to be used
    inside of this repository. Please file an issue if you would benefit
    from this being a public API.

    A wrapper around a `torch.nn.Linear` module which does fp8 compute, and tracks
    scales in way friendly to delayed scaling.
    """

    def __init__(self, *args, **kwargs):
        """
        Additional arguments on top of `torch.nn.Linear`'s arguments:
        * `config`: Float8LinearConfig
        """

        # Amax scales should always be kept as float32.
        self.always_float32_buffers = set()
        config = kwargs.pop("config")
        emulate = config.emulate
        super().__init__(*args, **kwargs)

        # Defines the scaling behavior of input, weight, grad_output
        self.scaling_type_input = config.cast_config_input.scaling_type
        self.scaling_type_weight = config.cast_config_weight.scaling_type
        self.scaling_type_grad_output = config.cast_config_grad_output.scaling_type
        # Convenience flag to skip code related to delayed scaling
        self.has_any_delayed_scaling = (
            self.scaling_type_input is TensorScalingType.DELAYED
            or self.scaling_type_weight is TensorScalingType.DELAYED
            or self.scaling_type_grad_output is TensorScalingType.DELAYED
        )

        self.config = config

        self.create_buffers()

        self.linear_mm_config = LinearMMConfig(
            # output
            ScaledMMConfig(
                emulate,
                self.config.gemm_config_output.use_fast_accum,
                False,
                self.config.pad_inner_dim,
            ),
            # grad_input
            ScaledMMConfig(
                emulate,
                self.config.gemm_config_grad_input.use_fast_accum,
                False,
                self.config.pad_inner_dim,
            ),
            # grad_weight
            ScaledMMConfig(
                emulate,
                self.config.gemm_config_grad_weight.use_fast_accum,
                False,
                self.config.pad_inner_dim,
            ),
        )

        # Note: is_amax_initialized is not a buffer to avoid data dependent
        # control flow visible to dynamo
        # TODO(future PR): add serialization for this flag
        self.is_amax_initialized = not self.config.enable_amax_init

        # Syncing of amaxes and scales happens outside of this function. This
        # flag is here to enforce that the user does not forget to do this.
        self.amax_and_scale_synced = not self.config.enable_amax_init

        # This is needed to properly handle autocast in the amax/scale
        # update function for torch.float16
        self.last_seen_input_dtype = None

        # pre_forward and post_forward are currently broken with FSDP
        # and torch.compile, this option can disable them
        # Note that when using `self.config.enable_pre_and_post_forward = False`,
        # it's recommended to also set `self.config.enable_amax_init = False`.
        # Otherwise, the amax buffer would never be marked as initialized and
        # would be initialized in every iteration.
        self.enable_pre_and_post_forward = self.config.enable_pre_and_post_forward

    def create_buffers(self):
        # Default values for history buffers, see above TODO
        history_len = self.config.delayed_scaling_config.history_len
        device = self.weight.device
        # TODO(future PR): dtype values below don't have the other float8
        # flavors, fix it
        default_input = torch.finfo(torch.float8_e4m3fn).max
        default_weight = torch.finfo(torch.float8_e4m3fn).max
        default_grad_output = torch.finfo(torch.float8_e5m2).max

        # Note: for now, create all the buffers if any are needed, to postpone
        # the work to make the scale and amax syncing and history calculation
        # handle a heterogeneous setup. We can do that work later if benchmarks
        # show it is worth doing.
        if self.has_any_delayed_scaling:
            self.register_always_float32_buffer(
                "fp8_amax_input", torch.tensor([default_input], device=device)
            )
            self.register_always_float32_buffer(
                "fp8_amax_history_input", torch.zeros(history_len, device=device)
            )
            self.register_always_float32_buffer(
                "fp8_scale_input", torch.tensor([1.0], device=device)
            )
            self.register_always_float32_buffer(
                "fp8_amax_weight", torch.tensor([default_weight], device=device)
            )
            self.register_always_float32_buffer(
                "fp8_amax_history_weight", torch.zeros(history_len, device=device)
            )
            self.register_always_float32_buffer(
                "fp8_scale_weight", torch.tensor([1.0], device=device)
            )
            self.register_always_float32_buffer(
                "fp8_amax_grad_output",
                torch.tensor([default_grad_output], device=device),
            )
            self.register_always_float32_buffer(
                "fp8_amax_history_grad_output", torch.zeros(history_len, device=device)
            )
            self.register_always_float32_buffer(
                "fp8_scale_grad_output", torch.tensor([1.0], device=device)
            )

    def register_always_float32_buffer(
        self, name: str, tensor: Optional[torch.Tensor], persistent: bool = True
    ) -> None:
        self.register_buffer(name=name, tensor=tensor, persistent=persistent)
        self.always_float32_buffers.add(name)

    def _apply(self, fn, recurse=True):
        ret = super()._apply(fn, recurse)
        self.convert_amax_buffer_to_float32()
        return ret

    def convert_amax_buffer_to_float32(self):
        for key in self.always_float32_buffers:
            if self._buffers[key] is not None:
                self._buffers[key] = self._buffers[key].to(torch.float32)

    def cast_x_to_float8(
        self, x: torch.Tensor, is_amax_initialized: bool
    ) -> torch.Tensor:
        # Duplicate the autocast logic for F.linear, so that the output
        # of our module has the right original precision
        if torch.is_autocast_enabled():
            # For now, hardcode to GPU's autocast dtype
            # if we need CPU support in the future, we can add it
            autocast_dtype = torch.get_autocast_gpu_dtype()
            x = x.to(autocast_dtype)

        if self.scaling_type_input is TensorScalingType.DELAYED:
            scale_fn_name = self.config.delayed_scaling_config.scale_fn_name
            _maybe_initialize_amaxes_scales_for_float8_cast(
                x,
                self.fp8_amax_input,
                self.fp8_amax_history_input,
                self.fp8_scale_input,
                scale_fn_name,
                e4m3_dtype,
                is_amax_initialized,
                reduce_amax=True,
            )
            x_fp8 = Float8Tensor.to_float8(
                x,
                self.fp8_scale_input,
                e4m3_dtype,
                self.fp8_amax_input,
                linear_mm_config=self.linear_mm_config,
                gemm_input_role=GemmInputRole.X,
            )
        else:
            assert self.scaling_type_input is TensorScalingType.DYNAMIC
            x_fp8 = cast_to_float8_e4m3_dynamic(x, self.linear_mm_config)
        return x_fp8

    def cast_w_to_float8(
        self, w: torch.Tensor, is_amax_initialized: bool
    ) -> torch.Tensor:
        if self.scaling_type_weight is TensorScalingType.DELAYED:
            if isinstance(self.weight, Float8Tensor):  # cast by FSDP
                w_fp8 = self.weight
            else:
                scale_fn_name = self.config.delayed_scaling_config.scale_fn_name
                _maybe_initialize_amaxes_scales_for_float8_cast(
                    w,
                    self.fp8_amax_weight,
                    self.fp8_amax_history_weight,
                    self.fp8_scale_weight,
                    scale_fn_name,
                    e4m3_dtype,
                    is_amax_initialized,
                    reduce_amax=False,
                )

                w_fp8 = Float8Tensor.to_float8(
                    w,
                    self.fp8_scale_weight,
                    e4m3_dtype,
                    self.fp8_amax_weight,
                    linear_mm_config=self.linear_mm_config,
                    gemm_input_role=GemmInputRole.W,
                )
        else:
            assert self.scaling_type_weight is TensorScalingType.DYNAMIC
            if isinstance(self.weight, Float8Tensor):  # cast by FSDP
                w_fp8 = self.weight
            else:
                w_fp8 = cast_to_float8_e4m3_dynamic(
                    self.weight, self.linear_mm_config, gemm_input_role=GemmInputRole.W
                )
        return w_fp8

    def cast_y_to_float8_in_bw(self, y: torch.Tensor) -> torch.Tensor:
        if self.scaling_type_grad_output is TensorScalingType.DELAYED:
            scale_fn_name = self.config.delayed_scaling_config.scale_fn_name
            y = NoopFwToFloat8E5M2Bw.apply(
                y,
                self.fp8_amax_grad_output,
                self.fp8_amax_history_grad_output,
                self.fp8_scale_grad_output,
                scale_fn_name,
                self.is_amax_initialized,
                self.linear_mm_config,
            )
        else:
            assert self.scaling_type_grad_output is TensorScalingType.DYNAMIC
            y = cast_to_float8_e5m2_dynamic_bw(y, self.linear_mm_config)
        return y

    def float8_pre_forward(self, x):
        if not self.enable_pre_and_post_forward:
            return
        if (
            self.is_amax_initialized
            and (not self.amax_and_scale_synced)
            and torch.is_grad_enabled()
        ):
            raise AssertionError(
                "amaxes and scales not synced, please call `sync_float8_amax_and_scale_history` before forward"
            )
        self.last_seen_input_dtype = x.dtype

    def float8_post_forward(self):
        if not self.enable_pre_and_post_forward:
            return
        # Ensure that calling forward again will fail until the user syncs
        # amaxes and scales
        self.is_amax_initialized = True
        self.amax_and_scale_synced = False

    def forward(self, input: torch.Tensor) -> torch.Tensor:
        if self.has_any_delayed_scaling:
            self.float8_pre_forward(input)

        x_fp8 = self.cast_x_to_float8(input, self.is_amax_initialized)
        w_fp8 = self.cast_w_to_float8(self.weight, self.is_amax_initialized)

        y = torch.matmul(x_fp8, w_fp8.t())

        # Cast gradY to float8_e5m2 during backward
        y = self.cast_y_to_float8_in_bw(y)

        if self.bias is not None:
            y = y + self.bias.to(y.dtype)

        if self.has_any_delayed_scaling:
            self.float8_post_forward()
        return y

    def scaling_repr(self):
        # add scaling settings without using too many characters
        # example: "x:del,w:del,dldy:dyn"
        return f"x:{self.scaling_type_input.short_str()},w:{self.scaling_type_weight.short_str()},dldy:{self.scaling_type_grad_output.short_str()}"

    def extra_repr(self):
        s = f'{super().extra_repr()}, scaling="{self.scaling_repr()}"'
        return s

    @classmethod
    def from_float(
        cls,
        mod,
        config: Optional[Float8LinearConfig] = None,
    ):
        """
        Create an nn.Linear with fp8 compute from a regular nn.Linear

        Args:
            mod (torch.nn.Linear): nn.Linear to convert
            config (Optional[Float8LinearConfig]): configuration for conversion to float8
        """
        if config is None:
            config = Float8LinearConfig()
        with torch.device("meta"):
            new_mod = cls(
                mod.in_features,
                mod.out_features,
                bias=False,
                config=config,
            )
        new_mod.weight = mod.weight
        new_mod.bias = mod.bias
        # need to create buffers again when moving from meta device to
        # real device
        new_mod.create_buffers()

        # If FSDP float8 all-gather is on, wrap the weight in a float8-aware
        # tensor subclass. This must happen last because:
        # 1. weight needs to be on the correct device to create the buffers
        # 2. buffers need to be already created for the delayed scaling version
        #    of the weight wrapper to be initialized
        if config.enable_fsdp_float8_all_gather:
            if config.cast_config_weight.scaling_type is TensorScalingType.DYNAMIC:
                new_mod.weight = torch.nn.Parameter(
                    WeightWithDynamicFloat8CastTensor(
                        new_mod.weight,
                        new_mod.linear_mm_config,
                    )
                )
            else:
                assert (
                    config.cast_config_weight.scaling_type is TensorScalingType.DELAYED
                )
                new_mod.weight = torch.nn.Parameter(
                    WeightWithDelayedFloat8CastTensor(
                        new_mod.weight,
                        new_mod.fp8_amax_weight,
                        new_mod.fp8_amax_history_weight,
                        new_mod.fp8_scale_weight,
                        new_mod.linear_mm_config,
                        new_mod.is_amax_initialized,
                    )
                )

        return new_mod
