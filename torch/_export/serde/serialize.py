import dataclasses
from enum import Enum
import io
import json
import logging
import operator
import typing
from typing import Any, cast, Dict, List, Optional, Tuple, Union

import torch
from torch.fx.experimental.symbolic_shapes import is_concrete_int
from torch._subclasses.fake_tensor import FakeTensor, FakeTensorMode
import torch._export.exported_program as ep
from torch.utils._pytree import pytree_to_str, str_to_pytree
from .schema import (   # type: ignore[attr-defined]
    Argument,
    BackwardSignature,
    CallSpec,
    Device,
    ExportedProgram,
    Graph,
    GraphModule,
    GraphSignature,
    Layout,
    MemoryFormat,
    NamedArgument,
    Node,
    ScalarType,
    SymInt,
    SymIntArgument,
    TensorArgument,
    TensorMeta,
    TensorValue,
    _Union,
)


__all__ = [
    "serialize",
    "GraphModuleSerializer",
    "ExportedProgramSerializer",
    "GraphModuleDeserializer",
    "ExportedProgramDeserializer",
]


log = logging.getLogger(__name__)


class SerializeError(RuntimeError):
    pass

def _reverse_map(d):
    return {v.value: k for k, v in d.items()}


_TORCH_TO_SERIALIZE_DTYPE = {
    torch.uint8: ScalarType.BYTE,
    torch.int8: ScalarType.CHAR,
    torch.int16: ScalarType.SHORT,
    torch.int32: ScalarType.INT,
    torch.int64: ScalarType.LONG,
    torch.float16: ScalarType.HALF,
    torch.float32: ScalarType.FLOAT,
    torch.float64: ScalarType.DOUBLE,
    torch.complex32: ScalarType.COMPLEXHALF,
    torch.complex64: ScalarType.COMPLEXFLOAT,
    torch.complex128: ScalarType.COMPLEXDOUBLE,
    torch.bool: ScalarType.BOOL,
    torch.bfloat16: ScalarType.BFLOAT16
}


_SERIALIZE_TO_TORCH_DTYPE = _reverse_map(_TORCH_TO_SERIALIZE_DTYPE)


_TORCH_TO_SERIALIZE_LAYOUT = {
    torch.sparse_coo: Layout.SparseCoo,
    torch.sparse_csr: Layout.SparseCsr,
    torch.sparse_csc: Layout.SparseCsc,
    torch.sparse_bsr: Layout.SparseBsr,
    torch.sparse_bsc: Layout.SparseBsc,
    torch._mkldnn: Layout._mkldnn,  # type: ignore[attr-defined]
    torch.strided: Layout.Strided,
}


_SERIALIZE_TO_TORCH_LAYOUT = _reverse_map(_TORCH_TO_SERIALIZE_LAYOUT)


_TORCH_TO_SERIALIZE_MEMORY_FORMAT = {
    torch.contiguous_format: MemoryFormat.ContiguousFormat,
    torch.channels_last: MemoryFormat.ChannelsLast,
    torch.channels_last_3d: MemoryFormat.ChannelsLast3d,
    torch.preserve_format: MemoryFormat.PreserveFormat,
}


_SERIALIZE_TO_TORCH_MEMORY_FORMAT = _reverse_map(_TORCH_TO_SERIALIZE_MEMORY_FORMAT)


_SYM_INT_OPS = {
    operator.mul,
    operator.add,
    operator.sub,
    operator.floordiv,
    operator.mod,
}

def deserialize_device(d: Device) -> torch.device:
    if d.index is None:
        return torch.device(type=d.type)  # type: ignore[call-overload]
    return torch.device(type=d.type, index=d.index)


def serialize_sym_int(s: Union[int, torch.SymInt]) -> SymInt:
    if isinstance(s, int):
        return SymInt.create(as_int=s)
    elif isinstance(s, torch.SymInt):
        if is_concrete_int(s):
            return SymInt.create(as_int=int(s))
        else:
            return SymInt.create(as_symbol=str(s))
    else:
        raise SerializeError(
            f"SymInt should be either symbol or int, got `{s}` of type `{type(s)}`"
        )


def serialize_tensor_meta(t: torch.Tensor) -> TensorMeta:
    """
    Extract a TensorMeta describing `t`.
    """
    return TensorMeta(
        dtype=_TORCH_TO_SERIALIZE_DTYPE[t.dtype],
        sizes=[serialize_sym_int(s) for s in t.shape],
        requires_grad=t.requires_grad,
        device=Device(type=t.device.type, index=t.device.index),
        strides=[serialize_sym_int(s) for s in t.stride()],
        storage_offset=0,
        layout=_TORCH_TO_SERIALIZE_LAYOUT[t.layout],
    )


def deserialize_tensor_meta(tensor_meta: TensorMeta, fake_tensor_mode: FakeTensorMode) -> FakeTensor:
    with fake_tensor_mode:
        return cast(
            FakeTensor,
            torch.empty_strided(
                tuple([val.as_int for val in tensor_meta.sizes]),
                tuple([val.as_int for val in tensor_meta.strides]),
                device=deserialize_device(tensor_meta.device),
                dtype=_SERIALIZE_TO_TORCH_DTYPE[tensor_meta.dtype],
            ),
        )


def serialize_metadata(node: torch.fx.Node) -> Dict[str, str]:
    ret = {}
    if stack_trace := node.meta.get("stack_trace"):
        ret["stack_trace"] = stack_trace
    module_fqn = node.meta.get("module_fqn")
    # Need an explicit None check instead of walrus operator, because
    # module_fqn can be the empty string if the node belongs to the root.
    # The walrus operator returns False on an empty string :(
    if module_fqn is not None:
        ret["module_fqn"] = module_fqn
    # TODO(angelayi) add nn_module_stack and source_fn
    return ret


def deserialize_metadata(metadata) -> Dict[str, str]:
    ret = {}
    if stack_trace := metadata.get("stack_trace"):
        ret["stack_trace"] = stack_trace
    # Need an explicit None check instead of walrus operator, because
    # module_fqn can be the empty string if the node belongs to the root.
    # The walrus operator returns False on an empty string :(
    module_fqn = metadata.get("module_fqn")
    if module_fqn is not None:
        ret["module_fqn"] = module_fqn
    # TODO(angelayi) add nn_module_stack and source_fn
    return ret



def serialize_operator(target) -> str:
    if isinstance(target, str):
        return target
    elif target in _SYM_INT_OPS:
        return f"{target.__module__}.{target.__name__}"
    elif isinstance(target, torch._ops.HigherOrderOperator):
        return target.__name__
    else:
        return str(target)


def deserialize_operator(serialized_target: str):
    target = torch.ops
    for name in serialized_target.split("."):
        if not hasattr(target, name):
            log.warning(f"Could not find operator {serialized_target}. Returning fake operator.")  # noqa: G004

            # Create a random fake placeholder op
            def fake_op(x):
                return x
            fake_op.__name__ = serialized_target
            return fake_op
        else:
            target = getattr(target, name)
    return target


def serialize_call_spec(call_spec: ep.CallSpec) -> CallSpec:
    return CallSpec(
        in_spec=pytree_to_str(call_spec.in_spec),
        out_spec=pytree_to_str(call_spec.out_spec),
    )


def deserialize_call_spec(call_spec: CallSpec) -> ep.CallSpec:
    return ep.CallSpec(
        in_spec=str_to_pytree(call_spec.in_spec),
        out_spec=str_to_pytree(call_spec.out_spec),
    )


def serialize_signature(sig: ep.ExportGraphSignature) -> GraphSignature:
    if bw_sig := sig.backward_signature:
        backward_signature = BackwardSignature(
            gradients_to_parameters=bw_sig.gradients_to_parameters,
            gradients_to_user_inputs=bw_sig.gradients_to_user_inputs,
            loss_output=bw_sig.loss_output,
        )
    else:
        backward_signature = None

    graph_signature = GraphSignature(
        inputs_to_parameters=sig.inputs_to_parameters,  # type: ignore[arg-type]
        inputs_to_buffers=sig.inputs_to_buffers,  # type: ignore[arg-type]
        user_inputs=sig.user_inputs,  # type: ignore[arg-type]
        user_outputs=sig.user_outputs,  # type: ignore[arg-type]
        buffers_to_mutate=sig.buffers_to_mutate,  # type: ignore[arg-type]
        backward_signature=backward_signature,
    )
    return graph_signature


def deserialize_signature(sig: GraphSignature) -> ep.ExportGraphSignature:
    backward_signature = None
    if bw_sig := sig.backward_signature:
        backward_signature = ep.ExportBackwardSignature(
            gradients_to_parameters=dict(bw_sig.gradients_to_parameters),
            gradients_to_user_inputs=dict(bw_sig.gradients_to_user_inputs),
            loss_output=bw_sig.loss_output,
        )
    return ep.ExportGraphSignature(
        parameters=list(sig.inputs_to_parameters.values()),  # type: ignore[arg-type]
        buffers=list(sig.inputs_to_buffers.values()),  # type: ignore[arg-type]
        user_inputs=list(sig.user_inputs),  # type: ignore[arg-type]
        user_outputs=list(sig.user_outputs),  # type: ignore[arg-type]
        inputs_to_buffers=dict(sig.inputs_to_buffers),  # type: ignore[arg-type]
        inputs_to_parameters=dict(sig.inputs_to_parameters),  # type: ignore[arg-type]
        buffers_to_mutate=dict(sig.buffers_to_mutate),  # type: ignore[arg-type]
        backward_signature=backward_signature,
    )


def serialize_state_dict(state_dict: Dict[str, Any]) -> bytes:
    buffer = io.BytesIO()
    state_dict = dict(state_dict)
    for name in state_dict:
        # This is a workaround for backend's tensor deserialization problem:
        # unpickleTensor() always create a tensor on the device where it was originally saved
        # This behavior is bad for multi-gpu training, as we wish to directly load the tensor
        # on the designated device.
        # For now, we simply move the tensor to cpu before saving.
        # TODO: this should be fixed by deserialization instead.
        state_dict[name] = state_dict[name].cpu()
    torch.save(state_dict, buffer)
    return buffer.getvalue()


def deserialize_state_dict(serialized: bytes) -> Dict[str, torch.Tensor]:
    if len(serialized) == 0:
        return {}
    buffer = io.BytesIO(serialized)
    buffer.seek(0)
    return torch.load(buffer)


def _is_single_tensor_return(target: torch._ops.OpOverload) -> bool:
    returns = target._schema.returns
    return len(returns) == 1 and isinstance(returns[0].real_type, torch.TensorType)


class GraphModuleSerializer:
    def __init__(self, graph_signature: ep.ExportGraphSignature, call_spec: ep.CallSpec):
        self.inputs: List[Argument] = []
        self.outputs: List[Argument] = []
        self.nodes: List[Node] = []
        self.tensor_values: Dict[str, TensorValue] = {}
        self.sym_int_values: Dict[str, SymInt] = {}
        self.graph_signature = graph_signature
        self.call_spec = call_spec

    def handle_placeholder(self, node: torch.fx.Node):
        assert node.op == "placeholder"
        self.inputs.append(Argument.create(as_tensor=TensorArgument(name=node.name)))

        self.tensor_values[node.name] = TensorValue(
            meta=serialize_tensor_meta(node.meta["val"])
        )

    def handle_output(self, node: torch.fx.Node):
        assert node.op == "output"
        assert len(node.args) == 1, "FX.Node's args should have one arg"
        node_args = node.args[0]
        assert isinstance(node_args, tuple)
        self.outputs = [self.serialize_input(arg) for arg in node_args]

    def handle_call_function(self, node: torch.fx.Node):
        assert node.op == "call_function"

        # getitem has been handled in the producer node, skip it here
        if node.target is operator.getitem:
            return

        if node.target in _SYM_INT_OPS:
            assert len(node.kwargs) == 0
            meta_val = node.meta["val"]
            ex_node = Node(
                target=serialize_operator(node.target),
                inputs=self.serialize_sym_int_op_inputs(node.args),
                outputs=[Argument.create(as_sym_int=self.serialize_sym_int_output(node.name, meta_val))],
                metadata=serialize_metadata(node),
            )
        elif isinstance(node.target, torch._ops.OpOverload):
            ex_node = Node(
                target=serialize_operator(node.target),
                inputs=self.serialize_inputs(node.target, node.args, node.kwargs),
                outputs=self.serialize_outputs(node),
                # TODO: create a new tensor_values here, meta might have faketensor info
                metadata=serialize_metadata(node),
            )
        else:
            # TODO(angelayi) Higher order ops
            raise SerializeError(f"Serializing {node.target} is not supported")

        self.nodes.append(ex_node)

    def handle_get_attr(self, node):
        pass

    def serialize_sym_int_op_inputs(self, args) -> List[NamedArgument]:
        serialized_args = []
        args_names = ["a", "b"]
        for args_name, arg in zip(args_names, args):
            serialized_args.append(
                NamedArgument(name=args_name, arg=self.serialize_input(arg))
            )
        return serialized_args

    def serialize_inputs(
        self, target: torch._ops.OpOverload, args, kwargs
    ) -> List[NamedArgument]:
        assert isinstance(target, torch._ops.OpOverload)
        serialized_args = []
        for i, schema_arg in enumerate(target._schema.arguments):
            if schema_arg.name in kwargs:
                serialized_args.append(
                    NamedArgument(
                        name=schema_arg.name,
                        arg=self.serialize_input(kwargs[schema_arg.name]),
                    )
                )
            elif not schema_arg.kwarg_only and i < len(args):
                serialized_args.append(
                    NamedArgument(
                        name=schema_arg.name,
                        arg=self.serialize_input(args[i]),
                    )
                )
            else:
                serialized_args.append(
                    NamedArgument(
                        name=schema_arg.name,
                        arg=self.serialize_input(schema_arg.default_value),
                    )
                )

        return serialized_args

    def is_sym_int_arg(self, arg) -> bool:
        return isinstance(arg, int) or (
            isinstance(arg, torch.fx.Node) and arg.name in self.sym_int_values
        )

    def serialize_input(self, arg) -> Argument:
        if isinstance(arg, torch.fx.Node):
            if arg.op == "get_attr":
                return Argument.create(as_tensor=TensorArgument(name=str(arg.target)))
            elif self.is_sym_int_arg(arg):
                return Argument.create(as_sym_int=SymIntArgument.create(asName=arg.name))
            else:
                return Argument.create(as_tensor=TensorArgument(name=arg.name))
        elif isinstance(arg, bool):
            return Argument.create(as_bool=arg)
        elif isinstance(arg, str):
            return Argument.create(as_string=arg)
        elif isinstance(arg, int):
            return Argument.create(as_int=arg)
        elif isinstance(arg, float):
            return Argument.create(as_float=arg)
        elif arg is None:
            return Argument.create(as_none=())
        elif isinstance(arg, (list, tuple)):
            # Must check bool first, as bool is also treated as int
            if all(isinstance(a, bool) for a in arg):
                return Argument.create(as_bools=list(arg))
            elif all(isinstance(a, int) for a in arg):
                return Argument.create(as_ints=list(arg))
            elif all(isinstance(a, float) for a in arg):
                return Argument.create(as_floats=list(arg))
            elif all(self.is_sym_int_arg(a) for a in arg):
                # list of sym_ints
                values = []
                for a in arg:
                    if isinstance(a, torch.fx.Node):
                        values.append(SymIntArgument.create(as_name=a.name))
                    elif isinstance(a, int):
                        values.append(SymIntArgument.create(as_int=a))
                return Argument.create(as_sym_ints=values)
            elif all(isinstance(a, torch.fx.Node) for a in arg):
                # list of tensors
                return Argument.create(
                    as_tensors=[TensorArgument(name=a.name) for a in arg],
                )
            else:
                raise SerializeError(f"Unsupported list/tuple argument type: {type(arg)}")
        elif isinstance(arg, torch.dtype):
            return Argument.create(as_scalar_type=_TORCH_TO_SERIALIZE_DTYPE[arg])
        elif isinstance(arg, torch.device):
            return Argument.create(as_device=Device(type=arg.type, index=arg.index))
        elif isinstance(arg, torch.memory_format):
            return Argument.create(as_memory_format=_TORCH_TO_SERIALIZE_MEMORY_FORMAT[arg])
        elif isinstance(arg, torch.layout):
            return Argument.create(as_layout=_TORCH_TO_SERIALIZE_LAYOUT[arg])
        else:
            raise SerializeError(f"Unsupported argument type: {type(arg)}")

    def serialize_tensor_output(self, name, meta_val) -> TensorArgument:
        assert name not in self.tensor_values
        self.tensor_values[name] = TensorValue(meta=serialize_tensor_meta(meta_val))
        return TensorArgument(name=name)

    def serialize_sym_int_output(self, name, meta_val) -> SymIntArgument:
        assert name not in self.sym_int_values
        self.sym_int_values[name] = serialize_sym_int(meta_val)
        return SymIntArgument.create(as_name=name)

    def serialize_outputs(self, node: torch.fx.Node) -> List[Argument]:
        """For a given node, return the dataclass representing its output values.

        [NOTE: Multiple outputs] We handle aggregates differently than FX. For
        FX, it looks like:

            x = call_function("multiple_return", ...)
            element0 = call_function(getitem, x, 0)
            foo = call_function("use_output", element0)

        We do not want the intermediate `getitem` call, so our serialized thing looks like:

            element0, element1, element2 = call_function("multiple_return", ...)
            foo = call_function("use_output", element0)

        We want names to be consistent across these two schemes, so that we can
        mostly reuse the names coming from FX. This function computes a mapping from
        the FX representation to our representation, preserving the names.
        """
        assert node.op == "call_function" and isinstance(node.target, torch._ops.OpOverload)

        meta_val = node.meta["val"]

        assert isinstance(node.target, torch._ops.OpOverload)
        returns = node.target._schema.returns

        # Check single value return
        if _is_single_tensor_return(node.target):
            return [Argument.create(as_tensor=self.serialize_tensor_output(node.name, meta_val))]
        elif len(returns) == 1 and isinstance(returns[0].real_type, torch.SymIntType):  # type: ignore[attr-defined]
            return [Argument.create(as_sym_int=self.serialize_sym_int_output(node.name, meta_val))]

        # There are a two possibilities at this point:
        # - This operator returns a list of Tensors.
        # - This operator returns multiple Tensors.
        #
        # Either way, start by gathering a list of TensorArguments with the correct names.
        # For consistent naming with FX, consult the downstream `getitem` node and
        # make sure our outputs have the same name.
        idx_to_name = {}
        for user in node.users:
            assert user.target is operator.getitem, f"User node {user} of {node} is incorrect"
            idx_to_name[user.args[1]] = user.name

        for idx, _ in enumerate(meta_val):
            # FX does not emit a getitem node for any outputs that are unused.
            # However, we need a name for them so that the number of outputs will
            # correctly match the schema. Just assign a dummy name.
            if idx not in idx_to_name:
                idx_to_name[idx] = f"{node.name}_unused_{idx}"

        arg_list = []
        for i, element_meta_val in enumerate(meta_val):
            arg_list.append(
                self.serialize_tensor_output(idx_to_name[i], element_meta_val)
            )

        # Then, pack the return value differently depending on what the return type is.
        if len(returns) == 1:
            return_type = returns[0].real_type
            assert isinstance(return_type, torch.ListType) and isinstance(
                return_type.getElementType(), torch.TensorType
            ), "Only tensors and lists of tensors supported"

            return [Argument.create(as_tensors=arg_list)]
        else:
            assert all(
                isinstance(ret.real_type, torch.TensorType) for ret in returns
            ), f"Multiple returns can only have tensor returns, got: {[ret.real_type for ret in returns]}"

            return [Argument.create(as_tensor=arg) for arg in arg_list]

    def serialize(self, graph_module: torch.fx.GraphModule) -> GraphModule:
        for node in graph_module.graph.nodes:
            try:
                self.node = node
                getattr(self, f"handle_{node.op}")(node)
            except Exception as e:
                if not isinstance(e, SerializeError):
                    raise SerializeError(f"Failed serializing node {node}") from e

        graph = Graph(
            inputs=self.inputs,
            nodes=self.nodes,
            tensor_values=self.tensor_values,
            sym_int_values=self.sym_int_values,
            outputs=self.outputs,
        )

        return GraphModule(
            graph=graph,
            signature=serialize_signature(self.graph_signature),
            call_spec=serialize_call_spec(self.call_spec),
        )


class ExportedProgramSerializer:
    def __init__(self, opset_version: Optional[Dict[str, int]] = None):
        self.opset_version: Dict[str, int] = (
            {} if opset_version is None else opset_version
        )

    def serialize(self, exported_program: ep.ExportedProgram) -> Tuple[ExportedProgram, bytes]:
        serialized_graph_module = (
            GraphModuleSerializer(
                exported_program.graph_signature,
                exported_program.call_spec
            ).serialize(exported_program.graph_module)
        )
        return (
            ExportedProgram(
                graph_module=serialized_graph_module,
                opset_version=self.opset_version
            ),
            serialize_state_dict(exported_program.state_dict),
        )


class GraphModuleDeserializer:
    def __init__(self):
        self.serialized_name_to_node: Dict[str, torch.fx.Node] = {}
        self.serialized_name_to_meta: Dict[str, FakeTensor] = {}
        self.graph = torch.fx.Graph()
        self.fake_tensor_mode = FakeTensorMode()

    def deserialize(
        self, serialized_graph_module: GraphModule,
    ) -> Tuple[torch.fx.GraphModule, ep.ExportGraphSignature, ep.CallSpec]:
        graph = self.graph
        serialized_graph = serialized_graph_module.graph

        # Handle the tensor metas.
        for name, tensor_value in serialized_graph.tensor_values.items():
            meta_val = deserialize_tensor_meta(tensor_value.meta, self.fake_tensor_mode)
            self.serialized_name_to_meta[name] = meta_val

        # Inputs: convert to placeholder nodes in FX.
        for input in serialized_graph.inputs:
            placeholder_node = graph.placeholder(input.as_tensor.name)
            self.sync_serialized_node(input.as_tensor.name, placeholder_node)

        # Nodes: convert to call_function nodes.
        for serialized_node in serialized_graph.nodes:
            target = deserialize_operator(serialized_node.target)

            # For convenience: if this node returns a single tensor, name the
            # newly-created node after it. This ensures that these tensor values
            # have names that are consistent with serialized.
            name = (
                serialized_node.outputs[0].value.name
                if _is_single_tensor_return(target)
                else None  # FX will generate a name for us.
            )
            args, kwargs = self.deserialize_inputs(target, serialized_node)

            fx_node = graph.create_node("call_function", target, args, kwargs, name)

            self.deserialize_outputs(serialized_node, fx_node)

            fx_node.meta.update(deserialize_metadata(serialized_node.metadata))

        # Outputs: convert to a single `output` node.
        outputs = []
        for output in serialized_graph.outputs:
            assert isinstance(output.value, TensorArgument)
            outputs.append(self.serialized_name_to_node[output.value.name])

        graph.output(tuple(outputs))

        sig = deserialize_signature(serialized_graph_module.signature)
        call_spec = deserialize_call_spec(serialized_graph_module.call_spec)
        return torch.fx.GraphModule({}, graph), sig, call_spec

    def sync_serialized_node(self, name: str, fx_node: torch.fx.Node):
        self.serialized_name_to_node[name] = fx_node
        fx_node.meta["val"] = self.serialized_name_to_meta[name]

    def deserialize_inputs(self, target: torch._ops.OpOverload, serialized_node: Node):
        schema_args = target._schema.arguments
        actual_args = {
            input.name: self.deserialize_input(input.arg) for input in serialized_node.inputs
        }
        args = []
        kwargs = {}
        for schema_arg in schema_args:
            is_positional = not schema_arg.has_default_value()
            if is_positional:
                args.append(actual_args[schema_arg.name])
            else:
                if schema_arg.name in actual_args:
                    kwargs[schema_arg.name] = actual_args[schema_arg.name]
        return tuple(args), kwargs

    def deserialize_input(self, value: Argument) -> Any:
        type_ = value.type
        if type_ == Argument.fields().as_none.name:
            # None should converted as None, but is encoded as bool in serialized
            # Convert serialized object to torch equivalent
            return None
        elif type_ == Argument.fields().as_tensor.name:
            return self.serialized_name_to_node[value.as_tensor.name]
        elif type_ == Argument.fields().as_tensors.name:
            return [self.serialized_name_to_node[arg.name] for arg in value.as_tensors]
        elif type_ == Argument.fields().as_int.name:
            return value.as_int
        elif type_ == Argument.fields().as_ints.name:
            # convert from serialized.python.types.List to python list
            return list(value.as_ints)
        elif type_ == Argument.fields().as_float.name:
            return value.as_float
        elif type_ == Argument.fields().as_floats.name:
            # convert from serialized.python.types.List to python list
            return list(value.as_floats)
        elif type_ == Argument.fields().as_string.name:
            return str(value.as_string)
        elif type_ in {Argument.fields().as_sym_int.name, Argument.fields().as_sym_ints.name}:
            raise ValueError("Symints not yet supported")
        elif type_ == Argument.fields().as_scalar_type.name:
            return _SERIALIZE_TO_TORCH_DTYPE[value.as_scalar_type]
        elif type_ == Argument.fields().as_memory_format.name:
            return _SERIALIZE_TO_TORCH_MEMORY_FORMAT[value.as_memory_format]
        elif type_ == Argument.fields().as_layout.name:
            return _SERIALIZE_TO_TORCH_LAYOUT[value.as_layout]
        elif type_ == Argument.fields().as_device.name:
            return deserialize_device(value.as_device),
        elif type_ == Argument.fields().as_bool.name:
            return value.as_bool
        elif type_ == Argument.fields().as_bools.name:
            # convert from serialized.python.types.List to python list
            return list(value.as_bools)
        else:
            raise SerializeError("Unhandled argument type:", type_)

    def deserialize_outputs(self, serialized_node: Node, fx_node: torch.fx.Node) -> None:
        # Simple case for single tensor return.
        assert isinstance(fx_node.target, torch._ops.OpOverload)
        if _is_single_tensor_return(fx_node.target):
            return self.sync_serialized_node(serialized_node.outputs[0].as_tensor.name, fx_node)

        # Convert multiple return types to FX format.
        # In FX, each node only returns one value. So in order to represent
        # multiple return values, we have to emit a `getitem` node for each
        # return value.
        # This performs the inverse mapping of the `serialize_outputs` call in
        # serialization, see [NOTE: Multiple outputs]
        output_names = []
        if len(serialized_node.outputs) == 1:
            assert serialized_node.outputs[0].type == Argument.fields().as_tensors.name
            output_names = [arg.name for arg in serialized_node.outputs[0].as_tensors]
        else:
            for output in serialized_node.outputs:
                assert output.type == Argument.fields().as_tensor.name
                output_names.append(output.as_tensor.name)

        for idx, name in enumerate(output_names):
            individual_output = self.graph.create_node(
                "call_function",
                operator.getitem,
                (fx_node, idx),
                name=name,
            )
            self.sync_serialized_node(name, individual_output)
            # The derived `getitem` nodes should have the same stacktrace as the
            # original `fx_node`
            individual_output.meta.update(deserialize_metadata(serialized_node.metadata))

        # also update the metaval for `fx_node` to be a list(meta)
        fx_node.meta["val"] = [self.serialized_name_to_meta[name] for name in output_names]


class ExportedProgramDeserializer:
    def __init__(self, expected_opset_version: Optional[Dict[str, int]] = None):
        self.expected_opset_version: Dict[str, int] = (
            {} if expected_opset_version is None else expected_opset_version
        )

    def deserialize(
        self, serialized_exported_program: ExportedProgram, serialized_state_dict: bytes
    ) -> ep.ExportedProgram:
        graph_module, sig, call_spec = (
            GraphModuleDeserializer()
            .deserialize(serialized_exported_program.graph_module)
        )
        state_dict = deserialize_state_dict(serialized_state_dict)

        # TODO(angelyi): serialize constraints
        return ep.ExportedProgram(
            state_dict, graph_module.graph, sig, call_spec, state_dict, {}, {},
        )


class EnumEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Enum):
            return obj.value
        return super().default(obj)


def serialize(
    exported_program: ep.ExportedProgram,
    opset_version: Optional[Dict[str, int]] = None,
) -> Tuple[bytes, bytes]:
    serialized_exported_program, serialized_state_dict = (
        ExportedProgramSerializer(opset_version).serialize(exported_program)
    )
    json_program = json.dumps(
        dataclasses.asdict(serialized_exported_program), cls=EnumEncoder
    )
    json_bytes = json_program.encode('utf-8')
    return json_bytes, serialized_state_dict


def _dict_to_dataclass(cls, data):
    if isinstance(cls, type) and issubclass(cls, _Union):
        obj = cls(**data)
        field_type = cls.__annotations__[obj.type]
        setattr(obj, obj.type, _dict_to_dataclass(field_type, obj.value))
        return obj
    elif dataclasses.is_dataclass(cls):
        obj = cls(**data)  # type: ignore[assignment]
        for field in dataclasses.fields(cls):
            name = field.name
            new_field_obj = _dict_to_dataclass(field.type, getattr(obj, name))
            setattr(obj, name, new_field_obj)
        return obj
    elif isinstance(data, list):
        d_type = typing.get_args(cls)[0]
        return [
            _dict_to_dataclass(d_type, d)
            for d in data
        ]
    elif isinstance(data, dict):
        v_type = typing.get_args(cls)[1]
        return {
            k: _dict_to_dataclass(v_type, v)
            for k, v in data.items()
        }
    return data


def deserialize(
    exported_program_bytes: bytes,
    state_dict: bytes,
    expected_opset_version: Optional[Dict[str, int]] = None,
) -> ep.ExportedProgram:
    exported_program_str = exported_program_bytes.decode('utf-8')
    exported_program_dict = json.loads(exported_program_str)
    serialized_exported_program = _dict_to_dataclass(ExportedProgram, exported_program_dict)
    return (
        ExportedProgramDeserializer(expected_opset_version)
        .deserialize(serialized_exported_program, state_dict)
    )