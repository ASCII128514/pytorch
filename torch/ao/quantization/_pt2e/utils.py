import torch
from torch.fx import (
    GraphModule,
    Node,
)
from torch.nn.utils.fusion import fuse_conv_bn_weights
# TODO[jerryzh168]: move this to a more general util function
from torch.ao.quantization.fx.prepare import (
    _is_activation_post_process_node,
)
import operator
from typing import Dict, Tuple


def _get_tensor_constant_from_node(node, m):
    if node is None:
        return None
    assert node.op == "get_attr"
    return getattr(m, node.target)

def _get_all_arguments(orig_args, orig_kwargs, args_schema):
    all_args = []
    for i, schema in enumerate(args_schema):
        if schema.name in orig_kwargs:
            all_args.append(orig_kwargs[schema.name])
        elif not schema.kwarg_only and i < len(orig_args):
            all_args.append(orig_args[i])
        else:
            all_args.append(schema.default_value)
    return all_args

def _fold_bn_weights_into_conv_node(
    conv_node: Node,
    conv_weight_node: Node,
    conv_bias_node: Node,
    bn_node: Node,
    m: GraphModule
) -> None:
    # conv args: input, weight, bias, stride, padding, dilation, transposed, ...
    conv_w = _get_tensor_constant_from_node(conv_weight_node, m)
    conv_b = _get_tensor_constant_from_node(conv_bias_node, m)
    transpose = conv_node.args[6]

    # eval bn args: input, weight, bias, running mean, running var, momentum, eps
    # train bn args: input, weight, bias, running mean, running var, training, momentum, eps
    bn_args_schema = bn_node.target._schema.arguments  # type: ignore[union-attr]
    bn_args = _get_all_arguments(bn_node.args, bn_node.kwargs, bn_args_schema)
    bn_w = _get_tensor_constant_from_node(bn_args[1], m)
    bn_b = _get_tensor_constant_from_node(bn_args[2], m)
    bn_rm = _get_tensor_constant_from_node(bn_args[3], m)
    bn_rv = _get_tensor_constant_from_node(bn_args[4], m)
    if bn_node.target == torch.ops.aten._native_batch_norm_legit_no_training.default:
        eps_arg_index = 6
    elif bn_node.target == torch.ops.aten._native_batch_norm_legit.default:
        eps_arg_index = 7
    else:
        raise ValueError("BN node target is unexpected ", bn_node.target)
    bn_eps = bn_args[eps_arg_index]

    fused_weight, fused_bias = fuse_conv_bn_weights(conv_w, conv_b, bn_rm, bn_rv, bn_eps, bn_w, bn_b, transpose=transpose)

    # update the weight and bias for conv
    conv_args = list(conv_node.args)
    # calling data since the fused_weight and fused_bias are nn.Parameter
    weight_attr_name = conv_weight_node.target
    setattr(m, weight_attr_name, fused_weight)  # type: ignore[arg-type]
    if conv_bias_node is not None:
        bias_attr_name = conv_bias_node.target
    else:
        bias_attr_name = weight_attr_name + "_bias"
        with m.graph.inserting_before(conv_node):
            get_bias_node = m.graph.get_attr(bias_attr_name)
        # NOTE: here we assume the bias of conv is not quantized!
        conv_args[2] = get_bias_node
    setattr(m, bias_attr_name, fused_bias)  # type: ignore[arg-type]
    conv_node.args = tuple(conv_args)

    # native_batch_norm has 3 outputs, we expect getitem calls on the output
    # and we want to replace the uses of getitem 0 with the output of conv
    #
    # Before:
    # conv -> bn - (first output) -> users1
    #          \ - (second output) -> users2
    #          \ - (third output) -> users3
    # After:
    # conv -> (first output) -> users1
    #       bn -
    #          \ - (second output) -> users2
    #          \ - (third output) -> users3
    # if users2 and users3 are empty then bn will be removed through dead code elimination

    for user in bn_node.users:
        if user.op != "call_function" or user.target != operator.getitem or user.args[1] != 0:
            continue
        user.replace_all_uses_with(conv_node)

# fuse conv bn weights, inplace modification of the graph_module and graph
def _fuse_conv_bn_(m: GraphModule) -> None:
    for n in m.graph.nodes:
        if n.op != "call_function" or n.target != torch.ops.aten._native_batch_norm_legit_no_training.default:
            continue
        bn_node = n
        n = bn_node.args[0]
        if n.op != "call_function" or n.target != torch.ops.aten.convolution.default:
            continue
        conv_node = n
        conv_weight_node = conv_node.args[1]
        conv_bias_node = conv_node.args[2]
        _fold_bn_weights_into_conv_node(conv_node, conv_weight_node, conv_bias_node, bn_node, m)

    m.graph.eliminate_dead_code()
    m.recompile()

# TODO: remove hack when we have better support for pattern matching
# move around the observer for addmm
def _rearrange_weight_observer_for_decomposed_linear(
    model: GraphModule,
) -> None:
    """
    Linear is decomposed to `t - addmm` (w/ bias) or `t - mm` (w/o bias)
    before:
         weight - t - observer \
           input - observer - addmm/mm
    after:
         weight - observer - t \
           input - observer - addmm/mm
    """
    aten = torch.ops.aten
    op_to_weight_obs_index = {
        aten.addmm.default : 2,
        aten.mm.default : 1,
    }
    named_modules = dict(model.named_modules(remove_duplicate=False))
    for node in model.graph.nodes:
        if node.target not in (aten.addmm.default, aten.mm.default):
            continue
        root_node = node
        maybe_weight_obs = root_node.args[op_to_weight_obs_index[root_node.target]]
        if not _is_activation_post_process_node(maybe_weight_obs, named_modules):
            continue
        transpose_node = maybe_weight_obs.args[0]
        if transpose_node.target != torch.ops.aten.t.default:
            continue
        # swap the order of transpose and observation

        maybe_weight_obs.replace_input_with(transpose_node, transpose_node.args[0])
        # remove the transpose node
        with model.graph.inserting_after(maybe_weight_obs):
            args = list(transpose_node.args)
            args[0] = maybe_weight_obs
            new_transpose_node = model.graph.create_node(
                "call_function",
                torch.ops.aten.t.default,
                tuple(args),
                transpose_node.kwargs
            )
        root_node.replace_input_with(maybe_weight_obs, new_transpose_node)

    model.graph.eliminate_dead_code()
    model.graph.lint()
    model.recompile()

def _get_node_name_to_scope(model: GraphModule) -> Dict[str, Tuple[str, type]]:
    # TODO: move this information to fx node itself
    node_name_to_scope: Dict[str, Tuple[str, type]] = {}
    for n in model.graph.nodes:
        nn_module_stack = n.meta.get("nn_module_stack", None)
        current_scope = ("", type(None))
        if nn_module_stack:
            bt = list(nn_module_stack.values())[-1]
            current_scope = (bt[0].split(".")[-1], bt[1])
        node_name_to_scope[n.name] = current_scope
    return node_name_to_scope
