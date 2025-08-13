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
design.evalTclString(f"report_checks -path_delay max -fields {{slew cap input fanout net}} -format full_clock_expanded -slack_max 0.000 -group_path_count 1000000 > {design_name}.setup.rpt")
rpt = f"{design_name}.setup.rpt"
 

sta = tech.getSta()
wns = design.evalTclString("report_wns")
tns = design.evalTclString("report_tns")
design.evalTclString("report_power")
timing = Timing(design)  
corner = timing.getCorners()[0]  
block = design.getBlock()
db.beginEco(block)
# ----------------------------------------------------------------------
  # 改成你的報告檔名
out_json = f"{design_name}.parsed.json"
timing_paths = parse_sta_report(rpt) #rpt總路徑
print(f"Parsed {len(timing_paths)} violated path(s) written to parsed_paths_detailed.txt and parsed_paths.json")
for timing_path in timing_paths:
    cells = [c for c in timing_path["cells"] if c.get("delay") is not None]
    cells_sorted = sorted(cells, key=lambda c: c["delay"], reverse=True)
    timing_path["cells"] = cells_sorted
with open(out_json, "w", encoding="utf-8") as f_json:
        # ensure_ascii=False 保留中文，indent=2 美化输出
        json.dump(timing_paths, f_json, indent=2, ensure_ascii=False)
# # ----------------------------------------------------------------------
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
    tns =  compute_tns_from_graph(cellgraph)/initial_tns
    return alpha * tns 
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
# # --------------------------------buffer list--------------------------------------
def _inst_center(inst):
    bb = inst.getBBox()
    return 0.5 * (bb.xMin() + bb.xMax()), 0.5 * (bb.yMin() + bb.yMax())
def _inst_size(inst):
    bb = inst.getBBox()
    return (bb.xMax() - bb.xMin()), (bb.yMax() - bb.yMin())
db = ord.get_db()# Get OpenDB
libs = db.getLibs()# Get all cell libraries from different files (if multiple .lib files are read)
buffer_master_list = [] #所有可用buffer type list
for lib in libs:
    lib_name = lib.getName()# Get library name
    lib_masters = lib.getMasters()  # Get all library cells in that library
    for master in lib_masters:
        libcell_name = master.getName()# Get the name of the library cell
        if design.isBuffer(master):
            buffer_master_list.append(master)
buffer_idx = int(len(buffer_master_list)/2)
nets = block.getNets()
nets_dict = {}
# net_sink_pins = []
# net_driver_pins = []
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
for name,sorted_with_length_net_dict in sorted_with_length_nets[:50]:
    old_buffer_net = sorted_with_length_net_dict['net']
    net_ITerms = old_buffer_net.getITerms()
    center_x_list = []
    center_y_list = []
    net_sink_pins = []
    net_driver_pins = []
    for net_ITerm in net_ITerms:
        cell = net_ITerm.getInst()
        center_x,center_y = _inst_center(cell)
        center_x_list.append(center_x)
        center_y_list.append(center_y)
        if net_ITerm.isInputSignal() is True:
            net_sink_pins.append(net_ITerm)
        if net_ITerm.isOutputSignal() is True:
            net_driver_pins.append(net_ITerm)
    x_center = int(sum(center_x_list)/len(center_x_list))
    y_center = int(sum(center_y_list)/len(center_y_list))

    new_buffer_name = f"buffer{buffer_name_idx}"
    buffer_name_idx += 1
    new_buffer_master = buffer_master_list[buffer_idx] #master
    new_buffer = odb.dbInst_create(block, new_buffer_master,  f"buffer{buffer_name_idx}")#後面是name
    dx,dy = _inst_size(new_buffer)
    new_buffer.setLocation(x_center - dx,y_center - dy)
    new_buffer.setPlacementStatus("PLACED")

    new_buffer_output_pins = [c for c in new_buffer.getITerms() if c.isOutputSignal()]
    new_buffer_input_pins  = [c for c in new_buffer.getITerms() if c.isInputSignal()]
    new_buffer_net = odb.dbNet_create(block, f"net_buffer{buffer_name_idx}")#後面是name;net
    for new_buffer_output_pin in new_buffer_output_pins:
        new_buffer_output_pin.connect(new_buffer_net)
    for new_buffer_input_pin in new_buffer_input_pins:
        new_buffer_input_pin.connect(old_buffer_net)
    for net_sink_pin in net_sink_pins:
        net_sink_pin.disconnect()
        net_sink_pin.connect(new_buffer_net)

# # ----------------------------------------------------------------------
# def get_iterm(block, inst_name, pin_name):
#     inst = block.findInst(inst_name)
#     if inst is None:
#         return None
#     for it in inst.getITerms():                      # dbITerm
#         if it.getMTerm().getName() == pin_name:      # mterm名：如 "A","Y","CLK","QN"
#             return it
#     return None
# # worst_paths_nets_dict = {}
# # idx = 0
# # for timing_path1 in timing_paths:
#     # worst_path_nets_dict = {}
#     # for net_name in timing_path1['net'].keys():
#     #     if timing_path1['net'][net_name]['sink'] is None or timing_path1['net'][net_name]['driver'] is None :
#     #         continue
#     #     net = block.findNet(net_name)
#     #     slew = timing_path1['net'][net_name]['driver']['slew']
#     #     driver_inst = block.findInst(timing_path1['net'][net_name]['driver']['inst'])
#     #     driver_pin = get_iterm(block,
#     #                   timing_path1['net'][net_name]['driver']['inst'],
#     #                   timing_path1['net'][net_name]['driver']['pin'])
#     #     sinker_inst = block.findInst(timing_path1['net'][net_name]['sink']['inst'])
#     #     sinker_pin = get_iterm(block,
#     #                   timing_path1['net'][net_name]['sink']['inst'],
#     #                   timing_path1['net'][net_name]['sink']['pin'])

#     #     worst_path_nets_dict[net_name] = {
#     #         'net' : net,
#     #         'slew' : slew,
#     #         'driver_inst' : driver_inst,
#     #         'driver_pin' : driver_pin,
#     #         'sinker_inst' : sinker_inst,
#     #         'sinker_pin' : sinker_pin
#     #     }
#     # worst_path_nets_dict = dict(sorted(worst_path_nets_dict.items(), key=lambda item: item[1]['slew'], reverse=True))
#     # worst_paths_nets_dict[f"{idx}"] = worst_path_nets_dict
#     # idx += 1
# # # ----------------------------------------------------------------------
# def _inst_center(inst):
#     bb = inst.getBBox()
#     return 0.5 * (bb.xMin() + bb.xMax()), 0.5 * (bb.yMin() + bb.yMax())
# def _inst_size(inst):
#     bb = inst.getBBox()
#     return (bb.xMax() - bb.xMin()), (bb.yMax() - bb.yMin())
# def _snap_to_row_site(block, x, y):
#     rows = list(block.getRows())
#     if not rows:
#         return int(x), int(y)

#     first = rows[0]
#     site  = first.getSite()

#     # 取 site 寬度，x 對齊到最近的 site 柵格
#     site_w = site.getWidth()
#     x0, y0 = first.getOrigin()   # ← 這裡用 getOrigin() 取 (x, y)

#     xi = int(round((x - x0) / site_w) * site_w + x0)

#     # y 對齊到「最近」那一排（不是永遠對齊第一排）
#     def row_y(row):
#         _, ry = row.getOrigin()
#         return ry

#     nearest_row = min(rows, key=lambda r: abs(y - row_y(r)))
#     yi = int(row_y(nearest_row))

#     return xi, yi
# def _set_origin(inst, ox, oy):
#     # 優先用 OpenDB API，若環境不支援則 fallback 用 Tcl 的 place_inst
#     try:
#         inst.setLocation(int(ox), int(oy))
#         try:
#             inst.setPlacementStatus(odb.dbPlacementStatus.PLACED)
#         except Exception:
#             pass
#     except Exception:
#         iname = inst.getName()
#         design.evalTclString(f"place_inst -name {{{iname}}} -origin {{{int(ox)} {int(oy)}}}")
# def _move_pair_toward_each_other(block, a_inst, b_inst, fraction=0.20):
#     # 跳過固定 / 鎖死的 cell（保守處理）
#     try:
#         if hasattr(a_inst, "isFixed") and a_inst.isFixed():
#             return
#         if hasattr(b_inst, "isFixed") and b_inst.isFixed():
#             return
#     except Exception:
#         pass

#     ax, ay = _inst_center(a_inst)
#     bx, by = _inst_center(b_inst)
#     dx, dy = (bx - ax), (by - ay)

#     # 新中心點（往彼此靠近 fraction）
#     nax, nay = ax + fraction * dx, ay + fraction * dy
#     nbx, nby = bx - fraction * dx, by - fraction * dy

#     aw, ah = _inst_size(a_inst)
#     bw, bh = _inst_size(b_inst)

#     # 轉成 origin 並對齊格點
#     # aox, aoy = _snap_to_row_site(block, nax - 0.5 * aw, nay - 0.5 * ah)
#     # box, boy = _snap_to_row_site(block, nbx - 0.5 * bw, nby - 0.5 * bh)
#     nax_o, nay_o = nax - 0.5*aw, nay - 0.5*ah
#     nbx_o, nby_o = nbx - 0.5*bw, nby - 0.5*bh

#     # 如果你要等 legalizer 再對齊，這裡可以先不 snap
#     _set_origin(a_inst, nax_o, nay_o)
#     _set_origin(b_inst, nbx_o, nby_o)  
# def get_driver_and_sinks_from_net(net):
#     drivers, sinks = [], []
#     for it in net.getITerms():
#         if it.isOutputSignal():   # 這顆 cell 在此 net 上是 driver
#             drivers.append(it)
#         elif it.isInputSignal():
#             sinks.append(it)
#     # 注意：頂層 I/O 是 BTerms
#     # bterms = list(net.getBTerms())  # 可能作為端點
#     return drivers, sinks
# ---------- 主要流程：每條 path 取前兩個 net，把兩端 inst 互相靠近 ----------
# 去重：避免同一對 inst 在多個 net 被重複推動
# _seen_pairs = set()

# # 走訪每一條 path
# top500 = list(worst_paths_nets_dict.items())[:100]
# for path_idx, path_nets in top500:
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
# 合法化 + 重新估 parasitics + 報告 TNS/WNS
design.evalTclString("detailed_placement")  # 合法化實例位置
# design.evalTclString("estimate_parasitics -placement")
update_full_slacks(cellgraph,block,timing,corner)
tns = compute_tns_from_graph(cellgraph)
design.evalTclString("report_wns")
design.evalTclString("report_tns")
design.evalTclString("report_power")
print("[After move] TNS:\n", tns)
design.evalTclString("report_tns")

ending_time = time.time()
elapsed_time = ending_time - start_time
print(f"Total elapsed time: {elapsed_time:.2f} seconds")
    