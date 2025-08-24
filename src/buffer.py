from pathlib import Path
import openroad as ord
import os
from collections import OrderedDict, defaultdict
from openroad import Tech, Design, Timing
import re
import json
from dataclasses import dataclass, field
from typing import Dict, List,Union, Tuple
import sys
import argparse
import time
import random
import csv
import math
import odb
from parsetxt import parse_sta_report
# ----------------------------------------------------------------------
# 1. 先找出「src 目錄」的絕對路徑，再推導 workspace 根目錄
# ----------------------------------------------------------------------
start_time = time.time()

parser = argparse.ArgumentParser(description="Run design optimization")
parser.add_argument('--design', type=str, required=True, help='Design name')
parser.add_argument('--wl', type=float, required=True, help='Wirelength weight')
parser.add_argument('--power', type=float, required=True, help='Power weight')
parser.add_argument('--timing', type=float, required=True, help='Timing weight')
args = parser.parse_args()

design_name    = args.design
WL_WEIGHT     = args.wl
POWER_WEIGHT  = args.power
TIMING_WEIGHT = args.timing

THIS_PY   = Path(__file__).resolve()          # no2.py
SRC_DIR   = THIS_PY.parent                    # solution/project_py/src
WORKSPACE = SRC_DIR.parent                    # solution/project_py
MAIN_DIR  = WORKSPACE.parent                  # solution
DESIGN_PATH = MAIN_DIR / "ICCAD25_PorbC"
# 1.1. 組出 testcase、lib、lef、def 的完整路徑
TESTCASE_DIR = DESIGN_PATH / "ICCAD25_testcases"
ASAP7_DIR = DESIGN_PATH / "ASAP7"
CELL_INFO_DIR = TESTCASE_DIR / design_name
LIB_DIR      = ASAP7_DIR / "LIB"
LEF_DIR      = ASAP7_DIR / "LEF" 
TECH_LEF_DIR      = ASAP7_DIR / "techlef" 
TECH_LEF_FILE = TECH_LEF_DIR / "asap7_tech_1x_201209.lef"
DEF_FILE     = CELL_INFO_DIR /  f"{design_name}.def"
SDC_FILE     = CELL_INFO_DIR /  f"{design_name}.sdc"
# DEF_FILE     = CELL_INFO_DIR / "aes_cipher_top.def"
# SDC_FILE     = CELL_INFO_DIR/ "aes_cipher_top.sdc"
RC_TCL       = ASAP7_DIR / "setRC.tcl"
# ----------------------------------------------------------------------
# 2) 讀 LEF ── 先 tech LEF，再 stdcell/其他
# ----------------------------------------------------------------------
tech   = ord.Tech()
db = ord.get_db()
# ----------------------------------------------------------------------
# 2.1) 先定义用来 parse size/Vt 的 helper，以及临时存储结构
# ----------------------------------------------------------------------
all_full_names = []
def parse_size(s: str) -> float:
    """把 'x2','xp5','xp67','x2p67' 转成 2.0, 0.5, 0.67, 2.67"""
    assert s.startswith('x'), f"Invalid size prefix: {s}"
    raw = s[1:]  # 拿掉开头的 'x'
    # 只保留数字和 p，小写化
    m = re.match(r'[0-9p]+', raw.lower())
    if not m:
        raise ValueError(f"Cannot parse drive-strength from `{s}`")
    body = m.group(0)
    if body.startswith('p'):
        return float('0.' + body[1:])
    if 'p' in body:
        a,b = body.split('p',1)
        return float(a + '.' + b)
    return float(body)

VT_ORDER    = ["SL","L","R","SRAM"]
cell_def_re = re.compile(r'^\s*cell\s*\(\s*([A-Za-z0-9_]+)\s*\)\s*\{')
pin_re      = re.compile(r'^\s*pin\s*\(\s*(\w+)\s*\)\s*\{.*direction\s*:\s*"output"')

cell_name_dict = OrderedDict()   # base -> fam_id (str)
tmp = defaultdict(lambda: {"sizes":set(),"dc_sizes": set(),"Vts":set(),"out_pin":None}) # tmp 里额外加一个 dc_sizes，用来记录哪些 size 有 DC variant
next_id = 0

# ----------------------------------------------------------------------
# 2.2) 读 Liberty 的同时 parse cell 定义
# ----------------------------------------------------------------------
tech = Tech()
for lib in sorted(LIB_DIR.glob("*.lib")):
    # skip sram_asap7* but not nldm
    if lib.name.startswith("sram_asap7") and "nldm" not in lib.name:
        print(f"[LIB]  skip {lib.name}")
        continue

    print(f"[LIB]  read {lib.name}")
    tech.readLiberty(str(lib))

    # 这里把这个 .lib 里的 cell (…) 定义都抓出来
    with lib.open() as f:
        curr = None
        for line in f:
            m = cell_def_re.match(line)
            if m:
                full = m.group(1)  # e.g. "AO21x2_ASAP7_75t_L"
                curr = full
                all_full_names.append(full)  # <— 新增：记录它
                # 1) 抽 VT ：最后一个 "_" 后面的那段
                vt = full.split('_')[-1]     # "L","R","SL" 或 "SRAM"

                # 2) 去掉 "_ASAP7..." 再抽 base/size
                core = full.split("_ASAP7",1)[0]  # "AO21x2"
                # 用正则拆出 base 和 size （不含 DC）
                m2 = re.match(
                    r"^(.+?)"               # base
                    r"(x\d*(?:p\d+)?f?)"    # size：x + 任意数量数字 + 可选 p+数字 + 可选 f
                    r"(DC)?$",
                    core, re.IGNORECASE
                )
                if not m2:
                    curr = None
                    continue
                base, size = m2.group(1), m2.group(2).lower()
                dc_flag     = bool(m2.group(3))
                # 3) 分配 fam_id
                if base not in cell_name_dict:
                    cell_name_dict[base] = str(next_id)
                    next_id += 1
                fid = cell_name_dict[base]

                # 4) 收 size 和 Vt
                tmp[fid]["sizes"].add(size)
                if dc_flag:
                    tmp[fid]["dc_sizes"].add(size)
                tmp[fid]["Vts"].add(vt)
                continue

            if curr:
                pm = pin_re.match(line)
                if pm and tmp[fid]["out_pin"] is None:
                    tmp[fid]["out_pin"] = pm.group(1)
                if line.strip() == "}":
                    curr = None

# ----------------------------------------------------------------------
# 2.3) 读完所有 .lib 后，组装最终的 cell_dict
# ----------------------------------------------------------------------
cell_dict = OrderedDict()
for base, fid in cell_name_dict.items():
    entry = tmp[fid]
    sizes = sorted(entry["sizes"], 
        key=lambda s: (
        parse_size(s),    # 主键：drive-strength 数值
        s.endswith('f')   # 次键：带 'f' 的排在前面（True>False）
    ), reverse=True)
    vts   = [v for v in VT_ORDER if v in entry["Vts"]]
    out_pin = entry["out_pin"] or "Y"

    cell_dict[fid] = {
        "name":    base,
        "sizes":   sizes,
        "dc_sizes": sorted(entry["dc_sizes"], key=parse_size, reverse=True),
        "Vt":      vts,
        "out_pin": f"/{out_pin}"
    }
    
full_name_dict = {}
for base, fid in cell_name_dict.items():
    info    = cell_dict[fid]
    sizes   = info["sizes"]
    dc_set  = set(info.get("dc_sizes", []))
    vts     = info["Vt"]
    sorted_fulls = []

    for size in sizes:
        # 常规版本
        for vt in vts:
            prefix = f"{base}{size}_ASAP7"
            sorted_fulls += [n for n in all_full_names 
                             if n.startswith(prefix) and n.split('_')[-1] == vt]
        # DC 版本
        if size in dc_set:
            for vt in vts:
                prefix_dc = f"{base}{size}DC_ASAP7"
                sorted_fulls += [n for n in all_full_names 
                                 if n.startswith(prefix_dc) and n.split('_')[-1] == vt]

    # 去重并保持顺序
    seen = set(); uniq = []
    for name in sorted_fulls:
        if name not in seen:
            seen.add(name); uniq.append(name)

    full_name_dict[base] = uniq

tech.readLef(str(TECH_LEF_FILE))
for lef in sorted(LEF_DIR.glob("*.lef")):
    print(f"[LEF]  read {lef.name}")
    tech.readLef(str(lef))           

design = ord.Design(tech)
design.readDef(str(DEF_FILE))
# ----------------------------------------------------------------------
# 2.4) 其他文件：SDC / set_rc 等
# ----------------------------------------------------------------------
design.evalTclString(f"read_sdc {SDC_FILE}")
design.evalTclString(f"source   {RC_TCL}")     # 沒有 SPEF 時，用 set_rc.tcl
design.evalTclString(f"estimate_parasitics -placement")

sta = tech.getSta()
wns = design.evalTclString("report_wns")
tns = design.evalTclString("report_tns")
design.evalTclString("report_power")
timing = Timing(design)  
corner = timing.getCorners()[0]  
block = design.getBlock()
db.beginEco(block)
# # ----------------------------------------------------------------------
@dataclass
class CellNode:
    name: str                             # instance 名稱
    old_name: str
    master: str                           # 使用的 library cell 名稱
    features: Dict[str, float]            # 你前面算好的特徵字典
    x : int
    y : int
    fanout_cells: List[str] = field(default_factory=list)  # 由本 cell 輸出連到的 cell 名稱列表
    fanin_cells:  List[str] = field(default_factory=list)  # 驅動本 cell 的前驅 cell 名稱列表
# # ----------------------------------------------------------------------
def build_cell_graph(inst,iterms,oterms,block, timing, corner, features,nodes_by_name):
    """回傳 nodes_by_name: Dict[str, CellNode]"""
    # 1) 先為每顆 instance 建立 CellNode（先不處理 fanin/fanout）
    name   = inst.getName()
    old_name   = inst.getName()
    master = inst.getMaster().getName()
    BBox = inst.getBBox()
    x0 = BBox.xMin()
    y0 = BBox.yMin()
    x1 = BBox.xMax()
    y1 = BBox.yMax()
    nodes_by_name[name] = CellNode(name=name, old_name =old_name,master=master, features=features,x = x0,y = y0)

    # 2) 掃每顆 cell 的輸出 pin，建立 fanout / fanin 關係
    for oterm in oterms:
        outputnet = oterm.getNet()
        outnet_inputpins = [it for it in outputnet.getITerms() if it.isInputSignal()]
        for pin in outnet_inputpins:
            nodes_by_name[name].fanout_cells.append(pin.getInst().getName())

    for iterm in iterms:
        inputnet = iterm.getNet()
        inputnet_outputpins = [it for it in inputnet.getITerms() if it.isOutputSignal()]
        for pin in inputnet_outputpins:
            nodes_by_name[name].fanin_cells.append(pin.getInst().getName())

    return nodes_by_name
# # ---------------------------第一次parse----------------------------------
worstpinslack = 0.0
features = {}
cellgraph: Dict[str, CellNode] = {}
for inst in block.getInsts():
    name = inst.getName()

    # 划分输入/输出 ITerm
    input_terms  = [it for it in inst.getITerms() if it.isInputSignal()]
    output_terms = [it for it in inst.getITerms() if it.isOutputSignal()]

    # 1) slack：所有输入 pin 的 min(slack_rise, slack_fall) 中的最小值
    total_n_slack = 0.0
    slacks = []
    endpoints = []
    for it in input_terms:
        # 跳过非 signal（VDD/VSS）
        if it.getNet().getSigType() != "SIGNAL":
            continue
        if timing.isEndpoint(it):
            endpoints.append(it)
        sr = timing.getPinSlack(it, timing.Rise, timing.Max)
        sf = timing.getPinSlack(it, timing.Fall, timing.Max)
        pin_slack = min(sr, sf)
        slacks.append(pin_slack)

    slack = min(slacks) if slacks else 0.0
    if slack < worstpinslack:
            worstpinslack = slack
    
    # 1.1) tns
    for pin in endpoints:
        slack_r = timing.getPinSlack(pin, timing.Rise, timing.Max)
        slack_f = timing.getPinSlack(pin, timing.Fall, timing.Max)
        worst_slack = min(slack_r, slack_f)
        if worst_slack < 0:
            total_n_slack += worst_slack
    # 2) in_slew
    in_slews = [timing.getPinSlew(it) for it in input_terms]
    in_slew  = max(in_slews) if in_slews else 0.0

    # 3) out_slew
    out_slews = [timing.getPinSlew(ot) for ot in output_terms]
    out_slew  = max(out_slews) if out_slews else 0.0

    # 4) arc_delay = max(arrival @ outputs) - max(arrival @ inputs)
    in_arr  = [timing.getPinArrival(it, timing.Rise) for it in input_terms]
    out_arr = [timing.getPinArrival(ot, timing.Rise) for ot in output_terms]
    arc_delay = (max(out_arr) - max(in_arr)) if in_arr and out_arr else 0.0

    # 5) nom_delay：这里简单用第一个输入 pin 的到达时间近似
    nom_delay = timing.getPinArrival(input_terms[0], timing.Rise) if input_terms else 0.0

    # 6) cell_cap：所有 portCap 平均
    ports = input_terms + output_terms
    caps = [timing.getPortCap(p, corner, timing.Max) for p in ports]
    cell_cap = sum(caps)/len(caps) if caps else 0.0

    # 7) drv_load：所有输出 net 的电容和
    drv_load = sum(timing.getNetCap(ot.getNet(), corner, timing.Max) for ot in output_terms)

    # 8) fanin_cap：所有输入 net 的电容平均
    in_caps = [timing.getNetCap(it.getNet(), corner, timing.Max) for it in input_terms]
    fanin_cap = sum(in_caps)/len(in_caps) if in_caps else 0.0

    # 9) sibling_cap：同 driver 下其它 net 的电容之和
    sibling_cap = 0.0
    for ot in output_terms:
        net = ot.getNet()
        sibling_cap += timing.getNetCap(net, corner, timing.Max)
    

    features[name] = {
        'slack':       slack,
        'tns':       total_n_slack,
        'in_slew':     in_slew,
        'out_slew':    out_slew,
        'arc_delay':   arc_delay,
        'nom_delay':   nom_delay,
        'cell_cap':    cell_cap, 
        'drv_load':    drv_load,
        'fanin_cap':   fanin_cap,
        'sibling_cap': sibling_cap
    }
    cellgraph = build_cell_graph(inst,input_terms,output_terms,block, timing, corner, features[name],cellgraph)
print(worstpinslack)
# # ---------------------------function areas------------------------------------
def compute_tns_from_graph(cellgraph):
    return sum(node.features['tns']
               for node in cellgraph.values() )
def compute_fake_tns_from_graph(cellgraph):
    return sum(node.features['slack']
               for node in cellgraph.values() if node.features['slack'] < 0.0 )
def compute_power(block,timing,corner):
    static_p = sum(timing.staticPower(block.findInst(n), corner)
               for n in cellgraph)
    dyn_p    = sum(timing.dynamicPower(block.findInst(n), corner)
               for n in cellgraph)
    return static_p + dyn_p  
def cost_function(cellgraph,block,timing,corner,initial_tns,initial_power,alpha,gamma): #alpha for tns,gamma for power 到時候繳交時要改吃run.sh的參數
    # power = compute_power(block,timing,corner)/initial_power
    # tns =  abs(compute_tns_from_graph(cellgraph)/initial_tns)
    # return (alpha * tns + gamma * power)/(alpha+gamma) 
    return compute_tns_from_graph(cellgraph)
def cost_function2(cellgraph,block,timing,corner,initial_tns,initial_power,alpha,gamma,iter,ppower):
    if iter % 10 == 0: #alpha for tns,gamma for power 到時候繳交時要改吃run.sh的參數
        power = compute_power(block,timing,corner)/initial_power
        tns =  abs(compute_tns_from_graph(cellgraph)/initial_tns)
        return (alpha * tns + gamma * power)/(alpha+gamma) , power
    else:
        tns =  abs(compute_tns_from_graph(cellgraph)/initial_tns)
        return (alpha * tns + gamma * ppower)/(alpha+gamma) , ppower
def update_full_slacks(cellgraph: Dict[str, CellNode],
                  block, timing, corner) -> None:
    for inst in block.getInsts():
        name = inst.getName()
        if name not in cellgraph:
            bb = inst.getBBox()
            cellgraph[name] = CellNode(
                name=name,
                old_name=name,
                master=inst.getMaster().getName(),
                features={
                    'slack': 0.0, 'tns': 0.0,
                    'in_slew': 0.0, 'out_slew': 0.0,
                    'arc_delay': 0.0, 'nom_delay': 0.0,
                    'cell_cap': 0.0, 'drv_load': 0.0,
                    'fanin_cap': 0.0, 'sibling_cap': 0.0
                },
                x=bb.xMin(), y=bb.yMin()
            )
        BBox = inst.getBBox()
        x0 = BBox.xMin()
        y0 = BBox.yMin()
        # 只算 SIGNAL 的 input pin slack
        input_terms = [
            it for it in inst.getITerms()
            if it.isInputSignal() and it.getNet().getSigType()=="SIGNAL"
        ]
        output_terms = [ot for ot in inst.getITerms() if ot.isOutputSignal()]
        slacks = []
        endpoints = []
        total_n_slack = 0.0
        for it in input_terms:
            if timing.isEndpoint(it):
                endpoints.append(it)
            sr = timing.getPinSlack(it, timing.Rise, timing.Max)
            sf = timing.getPinSlack(it, timing.Fall, timing.Max)
            slacks.append(min(sr, sf))
        for pin in endpoints:
            slack_r = timing.getPinSlack(pin, timing.Rise, timing.Max)
            slack_f = timing.getPinSlack(pin, timing.Fall, timing.Max)
            worst_slack = min(slack_r, slack_f)
            if worst_slack < 0:
                total_n_slack += worst_slack
        in_arr  = [timing.getPinArrival(it, timing.Rise) for it in input_terms]
        out_arr = [timing.getPinArrival(ot, timing.Rise) for ot in output_terms]
        arc_delay = (max(out_arr) - max(in_arr)) if in_arr and out_arr else 0.0
        in_slews = [timing.getPinSlew(it) for it in input_terms]
        in_slew  = max(in_slews) if in_slews else 0.0
        out_slews = [timing.getPinSlew(ot) for ot in output_terms]
        out_slew  = max(out_slews) if out_slews else 0.0

        cellgraph[name].features['in_slew'] = in_slew
        cellgraph[name].features['out_slew'] = out_slew
        
        cellgraph[name].features['slack'] = min(slacks) if slacks else 0.0
        cellgraph[name].features['tns'] = total_n_slack
        cellgraph[name].features['arc_delay'] = arc_delay
        cellgraph[name].x = x0
        cellgraph[name].y = y0
def get_instance_centers(design) -> Dict[str, Tuple[float,float]]:
    """
    返回字典:inst_name -> (x_center, y_center)
    坐标单位默认是 DBU,如果 to_micron=True 会转换成 μm。
    """
    blk = design.getBlock()
    centers = {}
    for inst in blk.getInsts():
        box = inst.getBBox()
        # 取 BBox 中心
        x = 0.5 * (box.xMin() + box.xMax())
        y = 0.5 * (box.yMin() + box.yMax())
        
        centers[inst.getName()] = (x, y)
    return centers
def compute_displacements(before: Dict[str, Tuple[float,float]],after:  Dict[str, Tuple[float,float]]) -> Dict[str, Tuple[float,float]]:
    """
    返回 inst_name -> (dx, dy) 的位移字典，只对在 before 和 after 中都出现的 inst 计算。
    """
    disp = {}
    for name, (x0, y0) in before.items():
        if name in after:
            x1, y1 = after[name]
            disp[name] = (x1 - x0, y1 - y0)
    return disp
# # ---------------------------function areas------------------------------------
before_centers = get_instance_centers(design)
site = design.getBlock().getRows()[0].getSite()
# # ----------------------------------------------------------------------


# # ----------------------------------------------------------------------
def _inst_center(inst):
    bb = inst.getBBox()
    return 0.5 * (bb.xMin() + bb.xMax()), 0.5 * (bb.yMin() + bb.yMax())
def _inst_size(inst):
    bb = inst.getBBox()
    return (bb.xMax() - bb.xMin()), (bb.yMax() - bb.yMin())
def _snap_to_row_site(block, x, y):
    rows = list(block.getRows())
    if not rows:
        return int(x), int(y)

    first = rows[0]
    site  = first.getSite()

    # 取 site 寬度，x 對齊到最近的 site 柵格
    site_w = site.getWidth()
    x0, y0 = first.getOrigin()   # ← 這裡用 getOrigin() 取 (x, y)

    xi = int(round((x - x0) / site_w) * site_w + x0)

    # y 對齊到「最近」那一排（不是永遠對齊第一排）
    def row_y(row):
        _, ry = row.getOrigin()
        return ry

    nearest_row = min(rows, key=lambda r: abs(y - row_y(r)))
    yi = int(row_y(nearest_row))

    return xi, yi
def _set_origin(inst, ox, oy):
    # 優先用 OpenDB API，若環境不支援則 fallback 用 Tcl 的 place_inst
    try:
        inst.setLocation(int(ox), int(oy))
        try:
            inst.setPlacementStatus(odb.dbPlacementStatus.PLACED)
        except Exception:
            pass
    except Exception:
        iname = inst.getName()
        design.evalTclString(f"place_inst -name {{{iname}}} -origin {{{int(ox)} {int(oy)}}}")
def _move_pair_toward_each_other(block, a_inst, b_inst, fraction=0.20):
    # 跳過固定 / 鎖死的 cell（保守處理）
    try:
        if hasattr(a_inst, "isFixed") and a_inst.isFixed():
            return
        if hasattr(b_inst, "isFixed") and b_inst.isFixed():
            return
    except Exception:
        pass

    ax, ay = _inst_center(a_inst)
    bx, by = _inst_center(b_inst)
    dx, dy = (bx - ax), (by - ay)

    # 新中心點（往彼此靠近 fraction）
    nax, nay = ax + fraction * dx, ay + fraction * dy
    nbx, nby = bx - fraction * dx, by - fraction * dy

    aw, ah = _inst_size(a_inst)
    bw, bh = _inst_size(b_inst)

    # 轉成 origin 並對齊格點
    # aox, aoy = _snap_to_row_site(block, nax - 0.5 * aw, nay - 0.5 * ah)
    # box, boy = _snap_to_row_site(block, nbx - 0.5 * bw, nby - 0.5 * bh)
    nax_o, nay_o = nax - 0.5*aw, nay - 0.5*ah
    nbx_o, nby_o = nbx - 0.5*bw, nby - 0.5*bh

    # 如果你要等 legalizer 再對齊，這裡可以先不 snap
    _set_origin(a_inst, nax_o, nay_o)
    _set_origin(b_inst, nbx_o, nby_o)  
def get_driver_and_sinks_from_net(net):
    drivers, sinks = [], []
    for it in net.getITerms():
        if it.isOutputSignal():   # 這顆 cell 在此 net 上是 driver
            drivers.append(it)
        elif it.isInputSignal():
            sinks.append(it)
    # 注意：頂層 I/O 是 BTerms
    # bterms = list(net.getBTerms())  # 可能作為端點
    return drivers, sinks
# ---------- 主要流程：每條 path 取前兩個 net，把兩端 inst 互相靠近 ----------
# 去重：避免同一對 inst 在多個 net 被重複推動
# _seen_pairs = set()

# # 走訪每一條 path
# top100 = list(worst_paths_nets_dict.items())[:100]
# for path_idx, path_nets in top100:
#     # 取出該 path 的前兩個 net（你前面已經依 slew 排序過）
#     top2 = list(path_nets.items())[:2]  # [(net_name, info), ...]

#     for net_name, info in top2:
#         a_inst = info['driver_inst']
#         b_inst = info['sinker_inst']
#         if a_inst is None or b_inst is None:
#             continue
#         # 用 frozenset 去重（(A,B) 與 (B,A) 視為同一對）
#         key = frozenset((a_inst.getName(), b_inst.getName()))
#         if key in _seen_pairs:
#             continue
#         _seen_pairs.add(key)

#         _move_pair_toward_each_other(block, a_inst, b_inst, fraction=0.001)
#         update_full_slacks(cellgraph,block,timing,corner)
#         tns = compute_tns_from_graph(cellgraph)
# ---------- 主要流程：每條 path 取前兩個 net，把兩端 inst 互相靠近 ----------
# design.evalTclString("detailed_placement")  # 合法化實例位置
# design.evalTclString("estimate_parasitics -placement")
# update_full_slacks(cellgraph,block,timing,corner)
# tns = compute_tns_from_graph(cellgraph)
# design.evalTclString("report_wns")
# design.evalTclString("report_tns")

# # --------------------------------buffer list--------------------------------------
# def insert_buffer5_in_clk_net(net, odb, block, buffer_master_list, buffer_idx,
    #                          buffer_name_idx, _inst_center, _inst_size):
    # """
    # 在 net 上做「扇出分割」：將所有 sink 分成每組最多 5 個，
    # 每組插入一顆 buffer，buffer 輸出接該組 sinks，buffer 輸入仍接在原 net 上。
    # 回傳更新後的 buffer_name_idx。
    # """
    # # 1) 蒐集 sinks / drivers（僅使用 ITerms；若需要也可擴充 BTerms）
    # sinks = []
    # drivers = []
    # for it in list(net.getITerms()):  # 轉成 list 避免遍歷中修改連線
    #     if it.isInputSignal():
    #         sinks.append(it)
    #     elif it.isOutputSignal():
    #         drivers.append(it)

    # # 沒有 sink 就不做事
    # if not sinks:
    #     return buffer_name_idx

    # # 2) 依空間位置排序後「每 5 個一組」分組（簡單且效果通常不錯）
    # #    這邊用 instance center 的 x 做排序，也可以改成 y 或 k-means 聚類
    # def iterm_center(it):
    #     inst = it.getInst()
    #     return _inst_center(inst)  # (cx, cy)

    # sinks_sorted = sorted(sinks, key=lambda it: iterm_center(it)[0])

    # def chunk(lst, n):
    #     for i in range(0, len(lst), n):
    #         yield lst[i:i+n]

    # sink_groups = list(chunk(sinks_sorted, 60))

    # # 3) 每組建立一顆 buffer：輸入接原 net、輸出接新 net，再把該組 sinks 轉接到新 net
    # buf_master = buffer_master_list[buffer_idx]  # 你給的 master（通常是 BUFx/CLKBUF）
    # sig_type = net.getSigType()                  # 保留 CLOCK / SIGNAL 屬性

    # for group in sink_groups:
    #     # 3.1) 決定 buffer 擺放位置：取該組 sinks 所屬 cell 的幾何中心
    #     xs, ys = [], []
    #     for it in group:
    #         cx, cy = iterm_center(it)
    #         xs.append(cx); ys.append(cy)
    #     if xs and ys:
    #         gx = int(sum(xs) / len(xs))
    #         gy = int(sum(ys) / len(ys))
    #     else:
    #         # fallback：用原 net 連線的所有 cell 的中心平均
    #         all_cx = []; all_cy = []
    #         for it in net.getITerms():
    #             cx, cy = _inst_center(it.getInst())
    #             all_cx.append(cx); all_cy.append(cy)
    #         if not all_cx:
    #             continue
    #         gx = int(sum(all_cx)/len(all_cx))
    #         gy = int(sum(all_cy)/len(all_cy))

    #     # 3.2) 建立 buffer instance（名稱與 net 名稱都用遞增 index 確保唯一）
    #     buf_name = f"buffer{buffer_name_idx}"
    #     new_buf = odb.dbInst_create(block, buf_master, buf_name)
    #     dx, dy = _inst_size(new_buf)  # cell 寬高（DBU）
    #     # 放在該組中心（約略置中），你也可以改成 gx, gy 直接放或靠近 driver
    #     new_buf.setLocation(gx - dx // 2, gy - dy // 2)
    #     new_buf.setPlacementStatus("PLACED")

    #     # 3.3) 取得 buffer 的輸入/輸出腳位
    #     buf_inputs  = [t for t in new_buf.getITerms() if t.isInputSignal()]
    #     buf_outputs = [t for t in new_buf.getITerms() if t.isOutputSignal()]
    #     if not buf_inputs or not buf_outputs:
    #         # master 不是標準的 1in/1out，略過本組
    #         new_buf.destroy(new_buf)  # 清乾淨
    #         continue

    #     # 3.4) 新建一條 net 當作 buffer 輸出網，並標成與原 net 相同 SigType（如 CLOCK）
    #     out_net_name = f"net_buffer{buffer_name_idx}"
    #     out_net = odb.dbNet_create(block, out_net_name)
    #     out_net.setSigType(sig_type)

    #     # 3.5) 連線：buffer 輸出 -> out_net；buffer 輸入 -> 原 net
    #     for bo in buf_outputs:
    #         bo.connect(out_net)
    #     for bi in buf_inputs:
    #         bi.connect(net)

    #     # 3.6) 把本組 sinks 從原 net 轉接到 out_net
    #     for it in group:
    #         it.disconnect()
    #         it.connect(out_net)

    #     # 下一顆 buffer 的編號
    #     buffer_name_idx += 1

    # return buffer_name_idx
timing.makeEquivCells()
design.evalTclString(f"estimate_parasitics -placement") 
update_full_slacks(cellgraph, block, timing, corner)
initial_tns = compute_tns_from_graph(cellgraph)
initial_power = compute_power(block,timing,corner)
print("First TNS =", initial_tns)
print("First power =", initial_power)
# ======================================================================
# ============== 模擬退火 (Simulated Annealing) 主程式 ===============
# ======================================================================

# --- 1. SA 參數設定 ---
# 這些參數需要根據 design 的複雜度進行調整 (tuning)
T_INITIAL = 1e-7          # 初始溫度，設為 1.0 因為我們的成本已經正規化
T_MIN = 1e-8             # 終止溫度
ALPHA = 0.98             # 降溫速率 (Cooling rate)
STEPS_PER_TEMP = 50     # 每個溫度下要嘗試的步數 (迭代次數)

# 設定隨機種子以重現結果
SEED = random.randint(0, 2**31 - 1)
random.seed(SEED)
print(f"Random seed: {SEED}")

current_tns = initial_tns
current_power = initial_power
current_cost = cost_function(cellgraph,block,timing,corner,initial_tns,initial_power,TIMING_WEIGHT,POWER_WEIGHT)

# 記錄整個過程中找到的最佳解
best_cost = current_cost
best_eco_map = {} # 儲存最佳解的 cell sizing 方案
best_tns = current_tns
best_power = current_power

# print(f"Initial TNS: {initial_tns:.2f} ps")
# print(f"Initial Power: {initial_power*1000:.4f} mW")
print(f"Initial Cost: {current_cost:.4f}")

# --- 3. 演算法主迴圈 ---
temp = T_INITIAL
iteration = 0

while temp > T_MIN:
    accepted_moves = 0
    
    print(f"\n--- Temperature: {temp:.6f} ---")
    update_full_slacks(cellgraph, block, timing, corner)
    neg_nodes = [n for n in cellgraph.values() if n.features['slack'] < 0.0]
    if not neg_nodes:
        print("No negative slack nodes found. Annealing might stop early.")
        break
    for step in range(STEPS_PER_TEMP):
        # a. 產生鄰近狀態 (Generate a Neighbor)
        # 策略：從有負 slack 的 cell 中隨機選一個來改，讓搜尋更有效率
        
            
        node_to_change = random.choice(neg_nodes[:150])
        inst = block.findInst(node_to_change.name)
        if not inst: continue
        
        old_master = inst.getMaster()
        old_master_name = old_master.getName()
        equiv_cells = timing.equivCells(old_master)
        equivCells_masters_names = [e.getName() for e in equiv_cells]
        idx = equivCells_masters_names.index(old_master_name)

        if len(equiv_cells) <= 1: continue
        
        # 3) 构造 upsizing 候选（往后找更大 drive‑strength）
        # cand_masters_names = []
        # for j in (idx-3,idx-2,idx-1,idx+3,idx+2,idx+1,idx+4):
        #     if 0 <= j < len(equiv_cells) :
        #         cand_masters_names.append(equivCells_masters_names[j])

        # # 如果没有更强的就跳过
        # if not cand_masters_names:
        #     a += 1
        #     continue

        # # 隨機選擇一個不同的 master
        # new_master_name = random.choice(cand_masters_names)
        # new_master = None
        # for equiv_master in equiv_cells:
        #     if new_master_name == equiv_master.getName():
        #         new_master = equiv_master
        #         break

        # if new_master is None:
        #     # 沒找到，保守處理：跳過
        #     continue
        new_master = random.choice(equiv_cells)
        new_master_name = new_master.getName()
        # 確保新舊 master 不同
        while new_master.getName() == old_master.getName():
            new_master = random.choice(equiv_cells)
        # b. 評估新狀態
        inst.swapMaster(new_master)
        design.evalTclString("estimate_parasitics -placement") # <<<<< 關鍵！
        update_full_slacks(cellgraph, block, timing, corner)
        new_tns = compute_tns_from_graph(cellgraph)
        new_power = compute_power(block,timing,corner)
        new_cost = cost_function(cellgraph,block,timing,corner,initial_tns,initial_power,TIMING_WEIGHT,POWER_WEIGHT)
        
        delta_E = new_cost - current_cost
        print(f"de: {delta_E}")
        print(f"cc:{current_cost}")

        # c. Metropolis 接受準則
        if delta_E < 0 or random.random() < math.exp(-delta_E / temp):
            # 接受新狀態
            current_cost = new_cost
            current_tns = new_tns
            current_power = new_power
            accepted_moves += 1
            
            # 更新目前的 eco map (這裡簡化為只記錄最後一次的變化)
            # 在真實應用中，需要更複雜的 map 來追蹤所有變化
            best_eco_map[inst.getName()] = new_master.getName()

            # 如果這個新狀態是至今為止最好的，就記錄下來
            if current_cost < best_cost:
                best_tns = current_tns
                best_power = current_power
                best_cost = current_cost
                # best_eco_map_snapshot = best_eco_map.copy() # 建立快照
                print(f"  ---> New best found! Cost: {best_cost:.4f} (TNS: {best_tns}, Power: {best_power} mW)")
        else:
            # 不接受，恢復原狀
            inst.swapMaster(old_master)
            # 為了狀態一致性，恢復後也應重新估算。但為求速度，也可省略此步
            # design.evalTclString("estimate_parasitics -placement") 

    # d. 降溫
    temp *= ALPHA
    iteration += 1
    
    # 輸出目前溫度的統計數據
    print(f"  Accepted {accepted_moves}/{STEPS_PER_TEMP} moves. Current cost: {best_cost:.4f}")

    if not neg_nodes: break # 如果沒有負 slack cell 了，可以提前結束

# --- 4. 恢復到找到的最佳狀態 ---
print("\n=== Simulated Annealing Finished. Restoring best found state... ===")

# ----------------------------------------------------------------------
design.evalTclString(f"report_checks -path_delay max -fields {{slew cap input fanout net}} -format full_clock_expanded -slack_max 0.000 -group_path_count 1000000 > {design_name}.setup.rpt")
rpt = f"{design_name}.setup.rpt"
out_json = f"{design_name}.parsed.json"
timing_paths = parse_sta_report(rpt) #rpt總路徑
print(f"Parsed {len(timing_paths)} violated path(s) written to parsed_paths_detailed.txt and parsed_paths.json")
for timing_path in timing_paths:
    cells = [c for c in timing_path["cells"] if c.get("delay") is not None]
    # clk_cells = [c for c in timing_path["cells"] if c["input_pin"] == "CLK"]
    cells_sorted = sorted(cells, key=lambda c: c["delay"], reverse=True)
    for c in timing_path["cells"]:
        pin = str(c.get("input_pin","")).strip()
        if pin.upper().startswith("CLK"):
            clk_net_name = c.get("input_net")
            break
    if clk_net_name:
        break  # 找到就不必繼續
    timing_path["cells"] = cells_sorted
# clk_net_name = clk_cells[0]['input_net']
print(clk_net_name)
clk_net = block.findNet(clk_net_name)
with open(out_json, "w", encoding="utf-8") as f_json:
        # ensure_ascii=False 保留中文，indent=2 美化输出
        json.dump(timing_paths, f_json, indent=2, ensure_ascii=False)

def get_iterm(block, inst_name, pin_name):
    inst = block.findInst(inst_name)
    if inst is None:
        return None
    for it in inst.getITerms():                      # dbITerm
        if it.getMTerm().getName() == pin_name:      # mterm名：如 "A","Y","CLK","QN"
            return it
    return None
worst_paths_nets_dict = {}
idx = 0
for timing_path1 in timing_paths:
    worst_path_nets_dict = {}
    for net_name in timing_path1['net'].keys():
        if timing_path1['net'][net_name]['sink'] is None or timing_path1['net'][net_name]['driver'] is None :
            continue
        net = block.findNet(net_name)
        slew = timing_path1['net'][net_name]['driver']['slew']
        driver_inst = block.findInst(timing_path1['net'][net_name]['driver']['inst'])
        driver_pin = get_iterm(block,
                      timing_path1['net'][net_name]['driver']['inst'],
                      timing_path1['net'][net_name]['driver']['pin'])
        sinker_inst = block.findInst(timing_path1['net'][net_name]['sink']['inst'])
        sinker_pin = get_iterm(block,
                      timing_path1['net'][net_name]['sink']['inst'],
                      timing_path1['net'][net_name]['sink']['pin'])

        worst_path_nets_dict[net_name] = {
            'net' : net,
            'slew' : slew,
            'driver_inst' : driver_inst,
            'driver_pin' : driver_pin,
            'sinker_inst' : sinker_inst,
            'sinker_pin' : sinker_pin
        }
    worst_path_nets_dict = dict(sorted(worst_path_nets_dict.items(), key=lambda item: item[1]['slew'], reverse=True))
    worst_paths_nets_dict[f"{idx}"] = worst_path_nets_dict
    idx += 1
# # ----------------------------------------------------------------------
def insert_buffer5_in_clk_net(net, odb, block, buffer_master_list, buffer_idx,
                             buffer_name_idx, _inst_center, _inst_size,max_per_group):
    """
    在 net 上做「扇出分割」：將所有 sink 分成每組最多 5 個，
    每組插入一顆 buffer，buffer 輸出接該組 sinks，buffer 輸入仍接在原 net 上。
    回傳更新後的 buffer_name_idx。
    """
    # 1) 蒐集 sinks / drivers（僅使用 ITerms；若需要也可擴充 BTerms）
    sinks = []
    drivers = []
    for it in list(net.getITerms()):  # 轉成 list 避免遍歷中修改連線
        if it.isInputSignal():
            sinks.append(it)
        elif it.isOutputSignal():
            drivers.append(it)
    if not sinks: # 沒有 sink 就不做事
        return buffer_name_idx

    # 2) 依空間位置排序後「每 5 個一組」分組（簡單且效果通常不錯）
    def iterm_center(it):
        inst = it.getInst()
        return _inst_center(inst)  # (cx, cy)

    sinks_sorted = sorted(sinks, key=lambda it: iterm_center(it)[0])#    這邊用 instance center 的 x 做排序，也可以改成 y 或 k-means 聚類
    if len(sinks) <= max_per_group:
        return buffer_name_idx  # 夠小，不必切
    def chunk(lst, n):
        for i in range(0, len(lst), n):
            yield lst[i:i+n] # return sinks[]
    sink_groups = list(chunk(sinks_sorted, max_per_group))

    # 3) 每組建立一顆 buffer：輸入接原 net、輸出接新 net，再把該組 sinks 轉接到新 net
    buf_master = buffer_master_list[buffer_idx]  # 你給的 master（通常是 BUFx/CLKBUF）
    sig_type = net.getSigType()                  # 保留 CLOCK / SIGNAL 屬性

    for group in sink_groups:
        # 3.1) 決定 buffer 擺放位置：取該組 sinks 所屬 cell 的幾何中心
        xs, ys = [], []
        for it in group:
            cx, cy = iterm_center(it)
            xs.append(cx); ys.append(cy)
        if xs and ys:
            gx = int(sum(xs) / len(xs))
            gy = int(sum(ys) / len(ys))
        else:
            # fallback：用原 net 連線的所有 cell 的中心平均
            all_cx = []; all_cy = []
            for it in net.getITerms():
                cx, cy = _inst_center(it.getInst())
                all_cx.append(cx); all_cy.append(cy)
            if not all_cx:
                continue
            gx = int(sum(all_cx)/len(all_cx))
            gy = int(sum(all_cy)/len(all_cy))

        # 3.2) 建立 buffer instance（名稱與 net 名稱都用遞增 index 確保唯一）
        buf_name = f"buffer{buffer_name_idx}"
        new_buf = odb.dbInst_create(block, buf_master, buf_name)
        dx, dy = _inst_size(new_buf)  # cell 寬高（DBU）
        # 放在該組中心（約略置中），你也可以改成 gx, gy 直接放或靠近 driver
        new_buf.setLocation(gx - dx // 2, gy - dy // 2)
        new_buf.setPlacementStatus("PLACED")

        # 3.3) 取得 buffer 的輸入/輸出腳位
        buf_inputs  = [t for t in new_buf.getITerms() if t.isInputSignal()]
        buf_outputs = [t for t in new_buf.getITerms() if t.isOutputSignal()]
        # if not buf_inputs or not buf_outputs:
        #     # master 不是標準的 1in/1out，略過本組
        #     new_buf.destroy(new_buf)  # 清乾淨
        #     continue

        # 3.4) 新建一條 net 當作 buffer 輸出網，並標成與原 net 相同 SigType（如 CLOCK）
        out_net_name = f"net_buffer{buffer_name_idx}"
        out_net = odb.dbNet_create(block, out_net_name)
        out_net.setSigType(sig_type)

        # 3.5) 連線：buffer 輸出 -> out_net；buffer 輸入 -> 原 net
        for bo in buf_outputs:
            bo.connect(out_net)
        for bi in buf_inputs:
            bi.connect(net)

        # 3.6) 把本組 sinks 從原 net 轉接到 out_net
        for it in group:
            it.disconnect()
            it.connect(out_net)

        # 下一顆 buffer 的編號
        buffer_name_idx += 1
    buffer_name_idx = insert_buffer5_in_clk_net(net, odb, block, buffer_master_list, buffer_idx,
                             buffer_name_idx, _inst_center, _inst_size,max_per_group)

    return buffer_name_idx
db = ord.get_db()# Get OpenDB
libs = db.getLibs()# Get all cell libraries from different files (if multiple .lib files are read)
buffer_master_list = [] #所有可用buffer type list
timing.makeEquivCells()
for lib in libs:
    lib_name = lib.getName()# Get library name
    lib_masters = lib.getMasters()  # Get all library cells in that library
    for master in lib_masters:
        libcell_name = master.getName()# Get the name of the library cell
        if design.isBuffer(master):
            buffer_master_list.append(master)
buffer_idx = max(0,int(len(buffer_master_list)-13))
equiv_cells = timing.equivCells(buffer_master_list[0])
buffer_master_list = equiv_cells 
nets = block.getNets()
nets_dict = {}
for net in nets:
    net_name = net.getName()
    net_ITerms = net.getITerms()
    net_cap = net.getTotalCapacitance() # 電容
    net_res = net.getTotalResistance() # 電阻
    netRouteLength = design.getNetRoutedLength(net) # 線長
    outputPins = []
    net_ITerms = net.getITerms()
    for ITerm in net_ITerms:
        if (ITerm.isInputSignal()):
            outputPins.append(ITerm)
    fanOut = len(outputPins)
    nets_dict[net_name] = {
        'net':   net,
        'cap':   net_cap,
        'res':   net_res,
        'length': netRouteLength,
        'fanout': fanOut
    }
sorted_with_length_nets = sorted(nets_dict.items(),key=lambda item: item[1]['fanout'],reverse=True)   # fanout排序的nets list
buffer_name_idx = 1 
buffer_name_idx = insert_buffer5_in_clk_net(clk_net, odb, block, buffer_master_list, buffer_idx,
                             buffer_name_idx, _inst_center, _inst_size,max_per_group=30)
# for name,sorted_with_length_net_dict in sorted_with_length_nets[:20]:
#     old_buffer_net = sorted_with_length_net_dict['net']
#     net_ITerms = old_buffer_net.getITerms()
#     center_x_list = []
#     center_y_list = []
#     net_sink_pins = []
#     net_driver_pins = []
#     for net_ITerm in net_ITerms:
#         cell = net_ITerm.getInst()
#         center_x,center_y = _inst_center(cell)
#         center_x_list.append(center_x)
#         center_y_list.append(center_y)
#         if net_ITerm.isInputSignal() is True:
#             net_sink_pins.append(net_ITerm)
#         if net_ITerm.isOutputSignal() is True:
#             net_driver_pins.append(net_ITerm)
#     if len(center_x_list) == 0:
#         continue
#     if len(center_y_list) == 0:
#         continue
#     x_center = int(sum(center_x_list)/len(center_x_list))
#     y_center = int(sum(center_y_list)/len(center_y_list))
#     new_buffer_name = f"buffer{buffer_name_idx}"
#     buffer_name_idx += 1
#     new_buffer_master = buffer_master_list[buffer_idx] #master
#     new_buffer = odb.dbInst_create(block, new_buffer_master,  f"buffer{buffer_name_idx}")#後面是name
#     dx,dy = _inst_size(new_buffer)
#     new_buffer.setLocation(x_center - dx//2,y_center - dy//2)
#     new_buffer.setPlacementStatus("PLACED")

#     new_buffer_output_pins = [c for c in new_buffer.getITerms() if c.isOutputSignal()]
#     new_buffer_input_pins  = [c for c in new_buffer.getITerms() if c.isInputSignal()]
#     new_buffer_net = odb.dbNet_create(block, f"net_buffer{buffer_name_idx}")#後面是name;net
#     for new_buffer_output_pin in new_buffer_output_pins:
#         new_buffer_output_pin.connect(new_buffer_net)
#     for new_buffer_input_pin in new_buffer_input_pins:
#         new_buffer_input_pin.connect(old_buffer_net)
#     for net_sink_pin in net_sink_pins:
#         net_sink_pin.disconnect()
#         net_sink_pin.connect(new_buffer_net)
# # --------------------------------buffer list--------------------------------------
# # ----------------------------detailed placement-------------------------------------
# design.evalTclString("improve_placement") 
max_disp_x = int(design.micronToDBU(4) / site.getWidth())
max_disp_y = int(design.micronToDBU(4) / site.getHeight())
design.getOpendp().detailedPlacement(max_disp_x, max_disp_y, "dpl_failures.txt",)
# # ----------------------------detailed placement-------------------------------------

after_centers = get_instance_centers(design)
displacements = compute_displacements(before_centers, after_centers)
# design.evalTclString("estimate_parasitics -placement")
update_full_slacks(cellgraph,block,timing,corner)
tns = compute_tns_from_graph(cellgraph)
design.evalTclString("report_wns")
design.evalTclString("report_tns")
design.evalTclString("report_power")
print("[After move] TNS:\n", tns)
total_abs_dx = sum(abs(dx) for dx, dy in displacements.values())
total_abs_dy = sum(abs(dy) for dx, dy in displacements.values())
total = total_abs_dx +total_abs_dy
print(f"Total |Δx| = {0.001*total_abs_dx:.3f} μm, Total |Δy| = {0.001*total_abs_dy:.3f} μm,Total displacement = {0.001*total:.3f} μm")
design.evalTclString("report_tns")

ending_time = time.time()
elapsed_time = ending_time - start_time
print(f"Total elapsed time: {elapsed_time:.2f} seconds")
    