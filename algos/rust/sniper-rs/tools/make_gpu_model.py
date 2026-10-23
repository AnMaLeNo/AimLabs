"""Greffe du pré/post-traitement sur le graphe YOLO11-seg pour sniper-rs (le GPU fait ce que le CPU ferait).

  --u8      entrée `images_u8` (1,256,256,3) uint8 NHWC = le crop RGB tel quel ; cast + /255 + transposition
            NCHW dans le graphe (4× moins d'octets à envoyer, plus de conversion f32 côté CPU)
  --decode  sorties réduites : `det` (1,5,K) = cx,cy,w,h,conf des K meilleures « person » (TopK) et
            `mask_logits` (K, R*64) = logits du masque de ces K détections sur R lignes autour du réticule
            (MatMul coefficients × protos). Au lieu de 623 Ko + 524 Ko à rapatrier : ~70 Ko.

Usage : python make_gpu_model.py in.onnx out.onnx [--u8] [--decode] [--k 32] [--rows 16] [--row0 24]
"""
import argparse

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--u8", action="store_true")
    ap.add_argument("--decode", action="store_true")
    ap.add_argument("--k", type=int, default=32)
    ap.add_argument("--rows", type=int, default=16)
    ap.add_argument("--row0", type=int, default=24)
    a = ap.parse_args()
    m = onnx.load(a.src)
    g = m.graph
    nodes_pre, nodes_post, inits = [], [], []
    ci = lambda name, v: inits.append(numpy_helper.from_array(np.array(v, dtype=np.int64), name))

    if a.u8:
        side = [d.dim_value for d in g.input[0].type.tensor_type.shape.dim][2]
        orig = g.input[0].name
        new_in = helper.make_tensor_value_info("images_u8", TensorProto.UINT8, [1, side, side, 3])
        inits.append(numpy_helper.from_array(np.array(1.0 / 255.0, dtype=np.float32), "inv255"))
        nodes_pre += [
            helper.make_node("Cast", ["images_u8"], ["u8_f"], to=TensorProto.FLOAT),
            helper.make_node("Mul", ["u8_f", "inv255"], ["u8_n"]),
            helper.make_node("Transpose", ["u8_n"], [orig], perm=[0, 3, 1, 2]),
        ]
        g.input.remove(g.input[0])
        g.input.insert(0, new_in)

    if a.decode:
        o0, o1 = g.output[0].name, g.output[1].name
        ci("ax0", [0]); ci("ax1", [1]); ci("ax2", [2])
        ci("s0", [0]); ci("e4", [4]); ci("s4", [4]); ci("e5", [5]); ci("s84", [84]); ci("e116", [116])
        ci("k", [a.k]); ci("r0", [a.row0]); ci("r1", [a.row0 + a.rows])
        ci("sh_a", [32, a.k]); ci("sh_p", [32, a.rows * 64])
        nodes_post += [
            helper.make_node("Slice", [o0, "s4", "e5", "ax1"], ["score3"]),          # (1,1,N) person
            helper.make_node("Squeeze", ["score3", "ax1"], ["score"]),               # (1,N)
            helper.make_node("TopK", ["score", "k"], ["top_v", "top_i"], axis=1, largest=1),  # (1,K)
            helper.make_node("Squeeze", ["top_i", "ax0"], ["idx"]),                  # (K,)
            helper.make_node("Squeeze", ["top_v", "ax0"], ["conf_k"]),
            helper.make_node("Slice", [o0, "s0", "e4", "ax1"], ["box_all"]),         # (1,4,N)
            helper.make_node("Gather", ["box_all", "idx"], ["box_k"], axis=2),       # (1,4,K)
            helper.make_node("Unsqueeze", ["conf_k", "ax0"], ["conf_1k"]),           # (1,K)
            helper.make_node("Unsqueeze", ["conf_1k", "ax0"], ["conf_11k"]),         # (1,1,K)
            helper.make_node("Concat", ["box_k", "conf_11k"], ["det"], axis=1),      # (1,5,K)
            helper.make_node("Slice", [o0, "s84", "e116", "ax1"], ["coef_all"]),     # (1,32,N)
            helper.make_node("Gather", ["coef_all", "idx"], ["coef_k"], axis=2),     # (1,32,K)
            helper.make_node("Reshape", ["coef_k", "sh_a"], ["coef_2d"]),            # (32,K)
            helper.make_node("Transpose", ["coef_2d"], ["coef_t"], perm=[1, 0]),     # (K,32)
            helper.make_node("Slice", [o1, "r0", "r1", "ax2"], ["proto_rows"]),      # (1,32,R,64)
            helper.make_node("Reshape", ["proto_rows", "sh_p"], ["proto_2d"]),       # (32,R*64)
            helper.make_node("MatMul", ["coef_t", "proto_2d"], ["mask_logits"]),     # (K,R*64)
        ]
        while len(g.output):
            g.output.remove(g.output[0])
        g.output.extend([
            helper.make_tensor_value_info("det", TensorProto.FLOAT, [1, 5, a.k]),
            helper.make_tensor_value_info("mask_logits", TensorProto.FLOAT, [a.k, a.rows * 64]),
        ])

    # pré-traitement en tête, post-traitement en queue : l'ordre topologique doit rester valide
    new_nodes = nodes_pre + list(g.node) + nodes_post
    del g.node[:]
    g.node.extend(new_nodes)
    g.initializer.extend(inits)
    onnx.checker.check_model(m)
    onnx.save(m, a.dst)
    print("écrit", a.dst, "·", len(g.node), "nœuds · entrées", [i.name for i in g.input], "· sorties", [o.name for o in g.output])


if __name__ == "__main__":
    main()
