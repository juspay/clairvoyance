"""Build the Noizzen engine from the DPDFNet ONNX files (docs/NOISE_FILTER.md).

    uv run --no-project --with onnx==1.19.1 --with numpy==2.2.6 \\
        python app/ai/voice/noise_filter/noizzen/engine/build.py <model_dir> [--out DIR] [--meta]

For every <stem>.onnx in model_dir it writes, into --out (default: model_dir):
  <stem>.weights          64-byte header + every float weight (pre-transposed where the maths reads
                          it that way, 64-byte aligned) + the model's initial recurrent state
  <stem>.<variant>.so     the engine for this model, compiled for each CPU variant of this machine
                          (x86-64-v3 and x86-64-v2 on x86, arm64 on ARM); the loader picks one
  <stem>.meta.json        with --meta only: where each node's output lands in the debug buffer

The ONNX graph is turned into straight-line C: one kernel call per node with every shape, stride
and buffer offset fixed here. View ops alias their input, every other tensor gets a slot in one
scratch area planned by liveness, so the engine allocates nothing per frame. Any unsupported
op, attribute or shape fails the build instead of producing a wrong engine.
"""

import argparse
import json
import math
import os
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

import numpy as np
import onnx
from onnx import helper, numpy_helper, shape_inference

HERE = Path(__file__).resolve().parent
VIEWS = {"Reshape", "Squeeze", "Unsqueeze", "Flatten", "Identity"}
EW = {
    "Add": "ew_add",
    "Sub": "ew_sub",
    "Mul": "ew_mul",
    "Div": "ew_div",
    "Pow": "ew_pow",
}
UNARY = {
    "Relu": "u_relu",
    "Sigmoid": "u_sigmoid",
    "Tanh": "u_tanh",
    "Sqrt": "u_sqrt",
    "Log": "u_log",
}
HEADER = 64


def numel(shape):
    return int(np.prod(shape)) if len(shape) else 1


def strides(shape):
    out, acc = [], 1
    for d in reversed(shape):
        out.append(acc)
        acc *= d
    return out[::-1]


def carr(values, ctype="int"):
    return "(const %s[]){%s}" % (ctype, ",".join(str(int(v)) for v in values))


class Program:
    """One ONNX model turned into C source, a weights payload and a scratch plan."""

    def __init__(self, path: Path):
        self.model = onnx.load(str(path))
        inferred = shape_inference.infer_shapes(self.model, strict_mode=True)
        self.shape, self.dtype = {}, {}
        for vi in (
            list(inferred.graph.value_info)
            + list(inferred.graph.input)
            + list(inferred.graph.output)
        ):
            dims = vi.type.tensor_type.shape.dim
            if any(not d.HasField("dim_value") for d in dims):
                raise ValueError(
                    f"{vi.name}: dynamic shape; the engine needs fixed shapes"
                )
            self.shape[vi.name] = [d.dim_value for d in dims]
            self.dtype[vi.name] = vi.type.tensor_type.elem_type
        self.inits = {
            i.name: numpy_helper.to_array(i) for i in self.model.graph.initializer
        }
        for name, arr in self.inits.items():
            self.shape[name] = list(arr.shape)
        self.nodes = list(self.model.graph.node)
        inputs = [i.name for i in self.model.graph.input if i.name not in self.inits]
        outputs = [o.name for o in self.model.graph.output]
        if inputs != ["spec", "state_in"] or outputs != ["spec_e", "state_out"]:
            raise ValueError(
                f"expected (spec, state_in) -> (spec_e, state_out), got {inputs} -> {outputs}"
            )
        for name, elem in self.dtype.items():
            if name not in self.inits and elem != onnx.TensorProto.FLOAT:
                raise ValueError(f"non-float tensor {name} flows at runtime")
        self.meta = {p.key: p.value for p in self.model.metadata_props}

    def initial_state(self) -> np.ndarray:
        """Zero state seeded with the normaliser values DPDFNet keeps in its metadata."""
        state = np.zeros(numel(self.shape["state_in"]), np.float32)
        if "erb_norm_init" in self.meta and len(self.shape["state_in"]) == 1:
            erb = [float(v) for v in self.meta["erb_norm_init"].split(",")]
            spec = [float(v) for v in self.meta["spec_norm_init"].split(",")]
            erb_n, spec_n = int(self.meta["erb_norm_state_size"]), int(
                self.meta["spec_norm_state_size"]
            )
            if len(erb) != erb_n or len(spec) != spec_n or erb_n + spec_n > state.size:
                raise ValueError("normaliser metadata does not fit the state")
            state[:erb_n] = erb
            state[erb_n : erb_n + spec_n] = spec
        return state

    # ------------------------------------------------------------ weights
    def build_weights(self):
        """Float weights, pre-transposed for Gemm (B) and GRU (W, R); shapes/indices stay in code."""
        derived, transposed_use = {}, set()
        for n in self.nodes:
            if n.op_type == "Gemm":
                derived[n.input[1] + "^T"] = np.ascontiguousarray(
                    self.inits[n.input[1]].T
                )
                transposed_use.add((n.name, 1))
            if n.op_type == "GRU":
                for j in (1, 2):  # [dirs, 3H, X] -> [dirs, X, 3H]
                    derived[n.input[j] + "^T"] = np.ascontiguousarray(
                        np.transpose(self.inits[n.input[j]], (0, 2, 1))
                    )
                    transposed_use.add((n.name, j))
        plain = {
            x
            for n in self.nodes
            for j, x in enumerate(n.input)
            if (n.name, j) not in transposed_use
        }
        tensors = [
            (k, v)
            for k, v in self.inits.items()
            if k in plain and v.dtype == np.float32
        ]
        tensors += list(derived.items()) + [("__init_state__", self.initial_state())]
        self.woff, chunks, pos = {}, [], 0
        for name, arr in tensors:
            pad = (-pos) % 16
            if pad:
                chunks.append(np.zeros(pad, np.float32))
                pos += pad
            self.woff[name] = pos
            flat = np.ascontiguousarray(arr, dtype=np.float32).reshape(-1)
            chunks.append(flat)
            pos += flat.size
        self.payload = np.concatenate(chunks).astype("<f4").tobytes()
        self.crc = zlib.crc32(self.payload) & 0xFFFFFFFF
        self.n_floats = len(self.payload) // 4

    def header(self) -> bytes:
        bins = self.shape["spec"][-2]
        win = int(self.meta.get("window_length", (bins - 1) * 2))
        hop = int(self.meta.get("hop_length", win // 2))
        rate = int(self.meta.get("sample_rate", 0))
        if win != (bins - 1) * 2:
            raise ValueError(f"window {win} does not match {bins} spectrum bins")
        state = self.initial_state().size
        h = struct.pack(
            "<8sIIIIIIII",
            b"NOIZZEN1",
            1,
            self.n_floats,
            self.crc,
            state,
            self.woff["__init_state__"],
            rate,
            win,
            hop,
        )
        return h + b"\0" * (HEADER - len(h))

    # ------------------------------------------------------------ scratch plan
    def plan(self):
        self.storage, size, first, last = {}, {}, {}, {}

        def new(n_floats, at):
            sid = len(size)
            size[sid], first[sid], last[sid] = n_floats, at, at
            return sid

        for i, n in enumerate(self.nodes):
            for x in n.input:
                if x and x in self.storage:
                    last[self.storage[x]] = max(last[self.storage[x]], i)
            if n.op_type in VIEWS:
                if numel(self.shape[n.input[0]]) != numel(self.shape[n.output[0]]):
                    raise ValueError(f"{n.name}: view changes the element count")
                if n.input[0] in self.storage:
                    self.storage[n.output[0]] = self.storage[n.input[0]]
                continue
            for o in n.output:
                if o:
                    self.storage[o] = new(numel(self.shape[o]), i)
            if n.op_type == "GRU":
                hidden, steps = self._attrs(n)["hidden_size"], self.shape[n.input[0]][0]
                self.storage[f"__tmp_{i}"] = new(steps * 3 * hidden + 5 * hidden, i)
                for k, name in enumerate(n.output[:2]):
                    if not name:
                        self.storage[f"__unused_{i}_{k}"] = new(
                            [steps * 2 * hidden, 2 * hidden][k], i
                        )
        for o in ("spec_e", "state_out"):
            last[self.storage[o]] = len(self.nodes)
        placed, self.arena = [], 0
        for sid in sorted(size, key=lambda s: -size[s]):
            need = (size[sid] + 15) // 16 * 16
            clashes = sorted(
                (o, e)
                for s2, o, e in placed
                if not (last[s2] < first[sid] or last[sid] < first[s2])
            )
            off = 0
            for o, e in clashes:
                if off + need <= o:
                    break
                off = max(off, e)
            placed.append((sid, off, off + need))
            self.arena = max(self.arena, off + need)
        self.offset = {sid: o for sid, o, _ in placed}

    def ptr(self, name):
        if name in self.woff:
            return f"(W+{self.woff[name]})"
        if name == "spec":
            return "IN_spec"
        if name == "state_in":
            return "IN_state"
        if name in self.storage:
            return f"(S+{self.offset[self.storage[name]]})"
        for n in self.nodes:  # a view of a weight or graph input
            if name in n.output and n.op_type in VIEWS:
                return self.ptr(n.input[0])
        raise KeyError(name)

    @staticmethod
    def _attrs(n):
        return {a.name: helper.get_attribute_value(a) for a in n.attribute}

    def _ints(self, name):
        return [int(v) for v in self.inits[name].reshape(-1)]

    def _bcast(self, name, out_shape):
        s = [1] * (len(out_shape) - len(self.shape[name])) + list(self.shape[name])
        st = strides(s)
        return [
            0 if s[k] == 1 and out_shape[k] != 1 else st[k]
            for k in range(len(out_shape))
        ]

    # ------------------------------------------------------------ code
    def emit(self):  # noqa: C901 - one branch per supported op
        code, self.dbg, dbg_off = [], {}, 0
        for i, n in enumerate(self.nodes):
            op, a, ins, outs = n.op_type, self._attrs(n), list(n.input), list(n.output)
            out = self.ptr(outs[0]) if outs and outs[0] else None
            c = f"/* {i} {op} */ "
            if op in VIEWS:
                c += "/* view */"
            elif op in EW:
                os_ = self.shape[outs[0]] or [1]
                fn = EW[op]
                if op == "Pow":
                    if ins[1] not in self.inits or self.inits[ins[1]].size != 1:
                        raise ValueError(
                            f"{n.name}: only a constant exponent is supported"
                        )
                    if float(self.inits[ins[1]].reshape(-1)[0]) == 2.0:
                        fn = "ew_sq"
                sa = self._bcast(ins[0], os_) if self.shape[outs[0]] else [0]
                sb = self._bcast(ins[1], os_) if self.shape[outs[0]] else [0]
                c += f"{fn}({out},{self.ptr(ins[0])},{self.ptr(ins[1])},{len(os_)},{carr(os_)},{carr(sa, 'long')},{carr(sb, 'long')});"
            elif op in UNARY:
                c += f"{UNARY[op]}({out},{self.ptr(ins[0])},{numel(self.shape[outs[0]])});"
            elif op == "ReduceSum":
                ish = self.shape[ins[0]]
                r = len(ish)
                if (
                    a.get("keepdims", 1) != 0
                    or a.get("noop_with_empty_axes", 0)
                    or len(ins) < 2
                ):
                    raise ValueError(
                        f"{n.name}: only ReduceSum with axes and keepdims=0"
                    )
                axes = sorted({ax % r for ax in self._ints(ins[1])})
                oshape = [ish[k] for k in range(r) if k not in axes]
                ost, ostr, j = strides(oshape), [], 0
                for k in range(r):
                    ostr.append(0 if k in axes else ost[j])
                    j += k not in axes
                c += f"reduce_sum({out},{numel(oshape)},{self.ptr(ins[0])},{r},{carr(ish)},{carr(ostr, 'long')});"
            elif op == "LayerNormalization":
                if (
                    a.get("axis", -1) not in (-1, len(self.shape[ins[0]]) - 1)
                    or len(outs) > 1
                    and any(outs[1:])
                ):
                    raise ValueError(
                        f"{n.name}: only last-axis LayerNormalization with one output"
                    )
                d = self.shape[ins[0]][-1]
                bias = self.ptr(ins[2]) if len(ins) > 2 and ins[2] else "NULL"
                eps = float(a.get("epsilon", 1e-5))
                c += f"layernorm({out},{self.ptr(ins[0])},{self.ptr(ins[1])},{bias},{numel(self.shape[ins[0]]) // d},{d},{eps!r}f);"
            elif op == "Gemm":
                if (
                    a.get("transB", 0) != 1
                    or a.get("transA", 0)
                    or a.get("alpha", 1.0) != 1.0
                    or a.get("beta", 1.0) != 1.0
                ):
                    raise ValueError(f"{n.name}: only Gemm with transB=1, alpha=beta=1")
                m, k = self.shape[ins[0]]
                nn = self.shape[ins[1]][0]
                bias = "NULL"
                if len(ins) > 2 and ins[2]:
                    if numel(self.shape[ins[2]]) != nn:
                        raise ValueError(f"{n.name}: bias must have N elements")
                    bias = self.ptr(ins[2])
                c += f"gemm_kt({out},{self.ptr(ins[0])},{self.ptr(ins[1] + '^T')},{bias},{m},{k},{nn});"
            elif op == "MatMul":
                sa_, sb_ = self.shape[ins[0]], self.shape[ins[1]]
                if ins[1] not in self.inits:
                    raise ValueError(f"{n.name}: only MatMul by a weight")
                if len(sb_) == 2:
                    k, nn = sb_
                    c += f"gemm_kt({out},{self.ptr(ins[0])},{self.ptr(ins[1])},NULL,{numel(sa_) // k},{k},{nn});"
                else:
                    m, k, nn = sa_[-2], sa_[-1], sb_[-1]
                    ba, bb = list(sa_[:-2]), list(sb_[:-2])
                    r = max(len(ba), len(bb))
                    ba, bb = [1] * (r - len(ba)) + ba, [1] * (r - len(bb)) + bb
                    bo = [max(x, y) for x, y in zip(ba, bb)]
                    if any(x not in (1, y) for x, y in zip(ba, bo)) or any(
                        x not in (1, y) for x, y in zip(bb, bo)
                    ):
                        raise ValueError(f"{n.name}: batch dims do not broadcast")
                    sta, stb, aoffs, boffs = strides(ba), strides(bb), [], []
                    for idx in np.ndindex(*bo):
                        aoffs.append(
                            sum(
                                (v if ba[d] > 1 else 0) * sta[d]
                                for d, v in enumerate(idx)
                            )
                            * m
                            * k
                        )
                        boffs.append(
                            sum(
                                (v if bb[d] > 1 else 0) * stb[d]
                                for d, v in enumerate(idx)
                            )
                            * k
                            * nn
                        )
                    c += (
                        f"{{ static const long ao[]={{{','.join(map(str, aoffs))}}}, bo[]={{{','.join(map(str, boffs))}}};"
                        f" for (int b=0;b<{len(aoffs)};b++) gemm_kt({out}+(long)b*{m * nn},{self.ptr(ins[0])}+ao[b],"
                        f"{self.ptr(ins[1])}+bo[b],NULL,{m},{k},{nn}); }}"
                    )
            elif op == "Conv":
                if (
                    a.get("auto_pad", b"NOTSET") not in (b"NOTSET", "NOTSET")
                    or len(self.shape[ins[0]]) != 4
                ):
                    raise ValueError(f"{n.name}: only explicit-pad 2-D Conv")
                bn, ch, h, w = self.shape[ins[0]]
                mo, _, kh, kw = self.shape[ins[1]]
                _, _, oh, ow = self.shape[outs[0]]
                st, dl, pd = (
                    list(a.get("strides", [1, 1])),
                    list(a.get("dilations", [1, 1])),
                    list(a.get("pads", [0, 0, 0, 0])),
                )
                g = a.get("group", 1)
                if bn != 1:
                    raise ValueError(f"{n.name}: batch must be 1")
                bias = self.ptr(ins[2]) if len(ins) > 2 and ins[2] else "NULL"
                if (
                    kh == kw == 1
                    and g == 1
                    and st == [1, 1]
                    and pd == [0, 0, 0, 0]
                    and h * w >= 64
                ):
                    c += f"gemm_kt({out},{self.ptr(ins[1])},{self.ptr(ins[0])},NULL,{mo},{ch},{h * w});"  # W[M,C] x X[C,HW]
                    if bias != "NULL":
                        c += f" add_rowbias({out},{bias},{mo},{h * w});"
                else:
                    c += (
                        f"conv2d({out},{self.ptr(ins[0])},{self.ptr(ins[1])},{bias},{ch},{h},{w},{mo},{kh},{kw},{oh},{ow},"
                        f"{st[0]},{st[1]},{pd[0]},{pd[1]},{dl[0]},{dl[1]},{g});"
                    )
            elif op == "GRU":
                if (
                    a.get("direction") not in (b"bidirectional", "bidirectional")
                    or a.get("linear_before_reset") != 1
                ):
                    raise ValueError(
                        f"{n.name}: only bidirectional GRU with linear_before_reset=1"
                    )
                if any(
                    k in a
                    for k in (
                        "activations",
                        "activation_alpha",
                        "activation_beta",
                        "clip",
                        "layout",
                    )
                ):
                    raise ValueError(
                        f"{n.name}: only default GRU activations and layout"
                    )
                steps, batch, isz = self.shape[ins[0]]
                hidden = a["hidden_size"]
                if batch != 1 or len(ins) < 4 or not ins[3]:
                    raise ValueError(f"{n.name}: batch must be 1 and B given")
                if len(ins) > 4 and ins[4]:
                    raise ValueError(f"{n.name}: sequence_lens is not supported")
                y = self.ptr(outs[0]) if outs[0] else self.ptr(f"__unused_{i}_0")
                yh = (
                    self.ptr(outs[1])
                    if len(outs) > 1 and outs[1]
                    else self.ptr(f"__unused_{i}_1")
                )
                h0 = self.ptr(ins[5]) if len(ins) > 5 and ins[5] else "NULL"
                c += (
                    f"gru_bidir({y},{yh},{self.ptr(ins[0])},{self.ptr(ins[1] + '^T')},{self.ptr(ins[2] + '^T')},"
                    f"{self.ptr(ins[3])},{h0},{steps},{isz},{hidden},{self.ptr(f'__tmp_{i}')});"
                )
            elif op == "Slice":
                ish = self.shape[ins[0]]
                r = len(ish)
                st0 = strides(ish)
                starts, ends = self._ints(ins[1]), self._ints(ins[2])
                axes = (
                    self._ints(ins[3])
                    if len(ins) > 3 and ins[3]
                    else list(range(len(starts)))
                )
                steps = (
                    self._ints(ins[4]) if len(ins) > 4 and ins[4] else [1] * len(starts)
                )
                oshape, sstr, soff = list(ish), list(st0), 0
                for s, e, ax, stp in zip(starts, ends, axes, steps):
                    ax %= r
                    d = ish[ax]
                    if stp > 0:
                        s = min(max(s + d if s < 0 else s, 0), d)
                        e = min(max(e + d if e < 0 else e, 0), d)
                        length = max(0, math.ceil((e - s) / stp))
                    elif stp < 0:
                        s = min(max(s + d if s < 0 else s, -1), d - 1)
                        e = min(max(e + d if e < 0 else e, -1), d - 1)
                        length = max(0, math.ceil((s - e) / -stp))
                    else:
                        raise ValueError(f"{n.name}: zero step")
                    soff += s * st0[ax]
                    sstr[ax] = st0[ax] * stp
                    oshape[ax] = length
                if oshape != self.shape[outs[0]]:
                    raise ValueError(
                        f"{n.name}: slice shape {oshape} != {self.shape[outs[0]]}"
                    )
                c += f"copy_nd({out},{carr(strides(oshape), 'long')},{self.ptr(ins[0])}+{soff},{carr(sstr, 'long')},{r},{carr(oshape)});"
            elif op == "Split":
                ish = self.shape[ins[0]]
                r = len(ish)
                ax = a.get("axis", 0) % r
                if len(ins) > 1 and ins[1]:
                    sizes = self._ints(ins[1])
                elif "split" in a:
                    sizes = list(a["split"])
                else:
                    sizes = [ish[ax] // len(outs)] * len(outs)
                st0, cum, parts = strides(ish), 0, []
                for o, sz in zip(outs, sizes):
                    osh = self.shape[o]
                    if osh[ax] != sz:
                        raise ValueError(f"{n.name}: split sizes do not match")
                    parts.append(
                        f"copy_nd({self.ptr(o)},{carr(strides(osh), 'long')},{self.ptr(ins[0])}+{cum * st0[ax]},"
                        f"{carr(st0, 'long')},{r},{carr(osh)});"
                    )
                    cum += sz
                c += " ".join(parts)
            elif op == "Concat":
                osh = self.shape[outs[0]]
                r = len(osh)
                ax = a["axis"] % r
                ost, cum, parts = strides(osh), 0, []
                for x in ins:
                    ish = self.shape[x]
                    parts.append(
                        f"copy_nd({out}+{cum * ost[ax]},{carr(ost, 'long')},{self.ptr(x)},{carr(strides(ish), 'long')},{r},{carr(ish)});"
                    )
                    cum += ish[ax]
                if cum != osh[ax]:
                    raise ValueError(f"{n.name}: concat sizes do not add up")
                c += " ".join(parts)
            elif op == "Transpose":
                ish = self.shape[ins[0]]
                r = len(ish)
                perm = list(a.get("perm", list(range(r))[::-1]))
                osh = [ish[p] for p in perm]
                st0 = strides(ish)
                c += f"copy_nd({out},{carr(strides(osh), 'long')},{self.ptr(ins[0])},{carr([st0[p] for p in perm], 'long')},{r},{carr(osh)});"
            elif op == "Gather":
                ish = self.shape[ins[0]]
                r = len(ish)
                ax = a.get("axis", 0) % r
                if ins[1] not in self.inits or self.inits[ins[1]].ndim != 0:
                    raise ValueError(f"{n.name}: only a constant scalar Gather index")
                k = int(self.inits[ins[1]]) % ish[ax]
                st0 = strides(ish)
                osh = [ish[j] for j in range(r) if j != ax]
                sst = [st0[j] for j in range(r) if j != ax]
                if not osh:
                    c += f"{out}[0] = {self.ptr(ins[0])}[{k * st0[ax]}];"
                else:
                    c += f"copy_nd({out},{carr(strides(osh), 'long')},{self.ptr(ins[0])}+{k * st0[ax]},{carr(sst, 'long')},{r - 1},{carr(osh)});"
            elif op == "Pad":
                if (
                    a.get("mode") not in (b"reflect", "reflect")
                    or len(ins) > 3
                    and ins[3]
                ):
                    raise ValueError(f"{n.name}: only reflect Pad over all axes")
                ish, osh = self.shape[ins[0]], self.shape[outs[0]]
                r = len(ish)
                pads = self._ints(ins[1])
                if any(p < 0 for p in pads) or any(
                    pads[j] >= ish[j] or pads[r + j] >= ish[j]
                    for j in range(r)
                    if pads[j] or pads[r + j]
                ):
                    raise ValueError(
                        f"{n.name}: reflect pads must be smaller than their axis"
                    )
                c += f"pad_reflect({out},{self.ptr(ins[0])},{r},{carr(ish)},{carr(osh)},{carr(pads[:r])});"
            else:
                raise ValueError(f"{n.name}: unsupported op {op}")
            for o in outs:
                if o and o in self.shape:
                    count = numel(self.shape[o])
                    c += f" if (DBG) memcpy(DBG+{dbg_off},{self.ptr(o)},{count}*sizeof(float));"
                    self.dbg[o] = [dbg_off, self.shape[o], i, op]
                    dbg_off += count
            code.append("    " + c)
        code.append(
            f"    memcpy(OUT_spec,{self.ptr('spec_e')},{numel(self.shape['spec_e'])}*sizeof(float));"
        )
        code.append(
            f"    memcpy(OUT_state,{self.ptr('state_out')},{numel(self.shape['state_out'])}*sizeof(float));"
        )
        body = "\n".join(code)
        return f"""/* Generated by build.py: {len(self.nodes)} nodes. Do not edit. */
#define NOIZZEN_N_FLOATS {self.n_floats}u
#define NOIZZEN_CRC {self.crc}u
#define NOIZZEN_STATE {self.initial_state().size}
#define NOIZZEN_STATE_OFF {self.woff['__init_state__']}
#define NOIZZEN_ARENA {self.arena}
#define NOIZZEN_SPEC {numel(self.shape['spec'])}
#define NOIZZEN_DBG {dbg_off}
static void noizzen_run(const float *restrict W, const float *IN_spec, const float *IN_state, float *OUT_spec,
                     float *OUT_state, float *restrict S, float *DBG) {{
{body}
}}
"""


def cpu_variants():
    """Library variants to build on this machine; the loader picks the best one the CPU runs."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return [
            ("x86-64-v3", ["-march=x86-64-v3"]),
            ("x86-64-v2", ["-march=x86-64-v2"]),
        ]
    if machine in ("arm64", "aarch64"):
        return [("arm64", [])]
    raise RuntimeError(f"unsupported CPU architecture: {machine}")


def compile_library(cc: str, gen_c: Path, out: Path, flags: list, crc: int):
    cmd = [
        cc,
        "-O3",
        "-fno-trapping-math",
        "-std=gnu11",
        "-fPIC",
        "-shared",
        "-fvisibility=hidden",
        "-fstack-protector-strong",
        "-D_FORTIFY_SOURCE=2",
        "-Wall",
        "-Wextra",
        "-Wno-unused-parameter",
        "-Wno-unused-function",
        f"-I{HERE}",
        f'-DNOIZZEN_GEN="{gen_c}"',
        *flags,
        str(HERE / "runtime.c"),
        "-o",
        str(out),
        "-lm",
    ]
    if sys.platform.startswith("linux"):
        cmd += ["-Wl,-z,relro", "-Wl,-z,now", "-Wl,-z,noexecstack"]
    elif (
        sys.platform == "darwin"
    ):  # dyld reuses a loaded image with the same install name
        cmd += [f"-Wl,-install_name,@rpath/{out.stem}.{crc:08x}.so"]
    subprocess.run(cmd, check=True)


def build(onnx_path: Path, out_dir: Path, cc: str, write_meta: bool):
    prog = Program(onnx_path)
    prog.build_weights()
    prog.plan()
    source = prog.emit()
    stem = onnx_path.stem
    weights = out_dir / f"{stem}.weights"
    tmp = weights.with_suffix(".weights.tmp")
    tmp.write_bytes(prog.header() + prog.payload)
    os.replace(
        tmp, weights
    )  # never leave a half-written weights file under the real name
    with tempfile.TemporaryDirectory() as work:
        gen_c = Path(work) / f"{stem}.gen.c"
        gen_c.write_text(source)
        for variant, flags in cpu_variants():
            lib = out_dir / f"{stem}.{variant}.so"
            lib_tmp = Path(work) / lib.name
            compile_library(cc, gen_c, lib_tmp, flags, prog.crc)
            shutil.copy2(lib_tmp, lib.with_suffix(".so.tmp"))
            os.replace(lib.with_suffix(".so.tmp"), lib)
    if write_meta:
        meta = {
            "nodes": len(prog.nodes),
            "crc": prog.crc,
            "arena_floats": prog.arena,
            "dbg": prog.dbg,
            "dbg_floats": sum(numel(v[1]) for v in prog.dbg.values()),
        }
        (out_dir / f"{stem}.meta.json").write_text(json.dumps(meta))
    print(
        f"{stem}: {len(prog.nodes)} nodes, weights {prog.n_floats * 4 / 2**20:.2f} MB, scratch {prog.arena * 4 / 1024:.0f} KB, "
        f"crc {prog.crc:08x}, libraries: {', '.join(v for v, _ in cpu_variants())}"
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("model_dir", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument(
        "--meta", action="store_true", help="also write <stem>.meta.json (verification)"
    )
    args = ap.parse_args()
    cc = os.environ.get("CC", "cc")
    if not shutil.which(cc):
        sys.exit(f"no C compiler ({cc}) on PATH")
    models = sorted(args.model_dir.glob("*.onnx"))
    if not models:
        sys.exit(f"no .onnx models in {args.model_dir}")
    out = args.out or args.model_dir
    out.mkdir(parents=True, exist_ok=True)
    for path in models:
        build(path, out, cc, args.meta)


if __name__ == "__main__":
    main()
