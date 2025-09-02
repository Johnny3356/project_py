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
wns = design.evalTclString("report_wns")
tns = design.evalTclString("report_tns")
design.evalTclString("report_power")
timing = Timing(design)  
corner = timing.getCorners()[0]  
block = design.getBlock()
site = design.getBlock().getRows()[0].getSite()
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
    fanin_cap = sum(in_caps) if in_caps else 0.0

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
    tns =  abs(compute_tns_from_graph(cellgraph)/initial_tns)
    # return (alpha * tns + gamma * power)/(alpha+gamma) 
    return tns
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
# # --------------------------------buffer list--------------------------------------
before_centers = get_instance_centers(design)
timing.makeEquivCells()
design.evalTclString(f"estimate_parasitics -placement") 
update_full_slacks(cellgraph, block, timing, corner)
initial_tns = compute_tns_from_graph(cellgraph)
initial_power = compute_power(block,timing,corner)
print("First TNS =", initial_tns)
print("First power =", initial_power)

# ----------------------------------------------------------------------
design.evalTclString(f"report_checks -path_delay max -fields {{slew cap input fanout net}} -format full_clock_expanded -slack_max 0.000 -group_path_count 1000000 > {design_name}.setup.rpt")
rpt = f"{design_name}.setup.rpt"
out_json = f"{design_name}.parsed.json"
timing_paths = parse_sta_report(rpt) #rpt總路徑
print(f"Parsed {len(timing_paths)} violated path(s) written to parsed_paths_detailed.txt and parsed_paths.json")
clk_net_name = None                    # >>> NEW: 先初始化，避免 NameError
found = False                          # >>> NEW: 外層是否已找到
net_criticality = {}
for timing_path in timing_paths:
    # Part A: 建立完整的 net_criticality 字典 (每次迴圈都執行)
    for net_name, net_info in timing_path["net"].items():
        if net_info.get('driver') is None or net_info.get('sink') is None or net_info['driver'].get('inst') is None or net_info['sink'].get('inst') is None:
            continue
        net_criticality[net_name] = net_criticality.get(net_name, 0) + 1
for timing_path in timing_paths:
    cells = [c for c in timing_path["cells"] if c.get("delay") is not None]
    # clk_cells = [c for c in timing_path["cells"] if c["input_pin"] == "CLK"]
    cells_sorted = sorted(cells, key=lambda c: c["delay"], reverse=True)
    for c in timing_path["cells"]:
        pin = str(c.get("input_pin","")).strip()
        if pin.upper().startswith("CLK"):
            clk_net_name = c.get("input_net")
            found = True
            break
    if found:                         # >>> NEW: 若已找到，就連外層一併跳出
        break
    timing_path["cells"] = cells_sorted
sorted_critical_nets = sorted(net_criticality.items(), key=lambda item: item[1], reverse=True)
print(clk_net_name)
clk_net = block.findNet(clk_net_name)
clk_outputPins = []
clk_net_ITerms = clk_net.getITerms()
for ITerm in clk_net_ITerms:
    if (ITerm.isInputSignal()):
      clk_outputPins.append(ITerm) 
clk_net_fanOut = len(clk_outputPins)
clk_group = int(clk_net_fanOut/100)
if clk_group < 5:
    clk_group = 5
with open(out_json, "w", encoding="utf-8") as f_json:
        # ensure_ascii=False 保留中文，indent=2 美化输出
        json.dump(timing_paths, f_json, indent=2, ensure_ascii=False)
bterms = block.getBTerms()
for btt in bterms:
    if btt.getName() == clk_net_name:
        print(btt.getName())
        one, clk_x ,clk_y = btt.getFirstPinLocation()
        print(clk_x,clk_y)
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
def insert_buffer_chain_in_clk_net(net, odb, block, buffer_master_list, buffer_idx,
                             buffer_name_idx, _inst_center, _inst_size, new_buffer_name_list,clk_x,clk_y):
   
    # -------------------- 基本收集 --------------------
    sink = None
    driver = None
    driver_y = None
    driver_x = None
    for it in list(net.getITerms()):  # 轉成 list 避免遍歷中修改連線
        if it.isInputSignal():
            sink = it
        elif it.isOutputSignal():
            driver = it
            print("clkn:", it.getName())
    if not sink:
        return buffer_name_idx
    if not driver:
        print("no clk")
        driver_x = clk_x
        driver_y = clk_y

    # -------------------- 分群（依 x 排序，每 F 個一組） --------------------
    def iterm_center(it):
        inst = it.getInst()
        return _inst_center(inst)  # (cx, cy)
    sink_x , sink_y = iterm_center(sink)
    gx = int((sink_x + driver_x) / 2)
    gy = int((sink_y + driver_y) / 2)
    # -------------------- 每群放一顆 buffer，必要時遞迴繼續切 --------------------
    buf_master = buffer_master_list[buffer_idx]  # （可改：愈靠 root 用愈大顆）
    sig_type = net.getSigType()                  # 保留 CLOCK / SIGNAL 屬性

    buf_name = f"clk_buffer{buffer_name_idx}"
    new_buf = odb.dbInst_create(block, buf_master, buf_name)
    new_buffer_name_list.append(buf_name)
    dx, dy = _inst_size(new_buf)  # cell 寬高（DBU）
    new_buf.setLocation(gx - dx // 2, gy - dy // 2)
    new_buf.setPlacementStatus("PLACED")

    # 3.3) 腳位
    buf_inputs  = [t for t in new_buf.getITerms() if t.isInputSignal()]
    buf_outputs = [t for t in new_buf.getITerms() if t.isOutputSignal()]

    # 3.4) 新建本群輸出 net：附帶 _L 與 _F 以承載層數與固定 fanout
    #     下一層 level = curr_level + 1
    out_net_name = f"net_buffer{buffer_name_idx}"  # <<< CHANGED
    out_net = odb.dbNet_create(block, out_net_name)
    out_net.setSigType(sig_type)

    # 3.5) 連線：buffer 輸出 -> out_net；buffer 輸入 -> 原 net
    for bo in buf_outputs:
        bo.connect(out_net)
    for bi in buf_inputs:
        bi.connect(net)

    # 3.6) 把本組 sinks 從原 net 轉接到 out_net
    
    sink.disconnect()
    sink.connect(out_net)

    buffer_name_idx += 1

    return buffer_name_idx
def insert_buffer5_in_clk_net(net, odb, block, buffer_master_list, buffer_idx,
                             buffer_name_idx, _inst_center, _inst_size,max_per_group,new_buffer_name_list,clk_x,clk_y,insert_buffer_chain_in_clk_net):
    # 1) 蒐集 sinks / drivers（僅使用 ITerms；若需要也可擴充 BTerms）
    sinks = []
    drivers = []
    for it in list(net.getITerms()):  # 轉成 list 避免遍歷中修改連線
        if it.isInputSignal():
            sinks.append(it)
        elif it.isOutputSignal():
            drivers.append(it)
            print("clkn:",it.getName())
    if not sinks: # 沒有 sink 就不做事
        return buffer_name_idx
    if not drivers:
        print("no clk")
        driver_x = clk_x
        driver_y = clk_y

    # 2) 依空間位置排序後「每 5 個一組」分組（簡單且效果通常不錯）
    def iterm_center(it):
        inst = it.getInst()
        return _inst_center(inst)  # (cx, cy)

    sinks_sorted = sorted(sinks, key=lambda it: iterm_center(it)[0])#    這邊用 instance center 的 x 做排序，也可以改成 y 或 k-means 聚類
    if len(sinks) <= max_per_group:
        print("too small")
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
            gx = int((sum(xs) + driver_x) / (len(xs) + 1))
            gy = int((sum(ys) + driver_y) / (len(ys) + 1))
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
        buf_name = f"clk_buffer{buffer_name_idx}"
        new_buf = odb.dbInst_create(block, buf_master, buf_name)
        new_buffer_name_list.append(buf_name)
        dx, dy = _inst_size(new_buf)  # cell 寬高（DBU）
        # 放在該組中心（約略置中），你也可以改成 gx, gy 直接放或靠近 driver
        new_buf.setLocation(gx - dx // 2, gy - dy // 2)
        new_buf.setPlacementStatus("PLACED")

        # 3.3) 取得 buffer 的輸入/輸出腳位
        buf_inputs  = [t for t in new_buf.getITerms() if t.isInputSignal()]
        buf_outputs = [t for t in new_buf.getITerms() if t.isOutputSignal()]

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
    
    buffer_name_idx = insert_buffer5_in_clk_net(net, odb, block, buffer_master_list, buffer_idx,buffer_name_idx, _inst_center, _inst_size, max_per_group, new_buffer_name_list,clk_x,clk_y,insert_buffer_chain_in_clk_net)
    return buffer_name_idx
def insert_buffer_kmeans_in_clk_net(net, odb, block, buffer_master_list, buffer_idx,
                             buffer_name_idx, _inst_center, _inst_size,
                             max_per_group, new_buffer_list, clk_x, clk_y,
                             insert_buffer_chain_in_clk_net):
    # 1) 蒐集 sinks / drivers（僅使用 ITerms；若需要也可擴充 BTerms）
    sinks = []
    drivers = []
    for it in list(net.getITerms()):  # 轉成 list 避免遍歷中修改連線
        if it.isInputSignal():
            sinks.append(it)
        elif it.isOutputSignal():
            drivers.append(it)
            print("clkn:", it.getName())
    if not sinks:  # 沒有 sink 就不做事
        return buffer_name_idx

    # 取得 driver 位置（必要：後面 gx/gy 混合 driver & 群中心）
    if not drivers:
        print("no clk")
        driver_x, driver_y = clk_x, clk_y

    # ----------------------- 分群：改為 K-Means -----------------------
    # 內嵌版極簡 K-Means，避免外部依賴；只在本函式內使用
    def _kmeans(points, k, max_iter=30):
        # points: List[(x,y)]
        import random
        if not points or k <= 0:
            return [0] * len(points)
        k = min(k, len(points))
        C = random.sample(points, k)  # 隨機初始化
        for _ in range(max_iter):
            # 指派
            labels = []
            for (x, y) in points:
                jmin = 0
                dmin = (x - C[0][0])**2 + (y - C[0][1])**2
                for j in range(1, k):
                    d = (x - C[j][0])**2 + (y - C[j][1])**2
                    if d < dmin:
                        dmin = d
                        jmin = j
                labels.append(jmin)
            # 更新
            sums = [(0.0, 0.0, 0) for _ in range(k)]
            sx, sy, n = 0.0, 0.0, 0
            for (x, y), lab in zip(points, labels):
                sx0, sy0, n0 = sums[lab]
                sums[lab] = (sx0 + x, sy0 + y, n0 + 1)
                sx += x; sy += y; n += 1
            newC = []
            fallback = (sx / n, sy / n) if n else (0.0, 0.0)
            for (sx0, sy0, n0) in sums:
                if n0 == 0:
                    newC.append(fallback)  # 空群：用整體質心補
                else:
                    newC.append((sx0 / n0, sy0 / n0))
            moved = sum(abs(newC[j][0] - C[j][0]) + abs(newC[j][1] - C[j][1]) for j in range(k))
            C = newC
            if moved < 1e-6:
                break
        # 最終標籤
        labels = []
        for (x, y) in points:
            jmin = 0
            dmin = (x - C[0][0])**2 + (y - C[0][1])**2
            for j in range(1, k):
                d = (x - C[j][0])**2 + (y - C[j][1])**2
                if d < dmin:
                    dmin = d
                    jmin = j
            labels.append(jmin)
        return labels

    def _kmeans_multi_restart(points, k, max_iterations=100, num_restarts=10):
        if not points or k <= 0 or k > len(points):
            return None

        best_wcss = float('inf')
        best_labels = []
        best_centroids = []

        for _ in range(num_restarts):
            # --- 1. 初始化 (每次都重新隨機) ---
            centroids = random.sample(points, k)
            
            # --- 2. K-Means 核心迭代 (與你原來的版本相同) ---
            current_labels = []
            for _ in range(max_iterations):
                # ... (此處省略你原有的 指派/更新/收斂 迴圈邏輯) ...
                # ... (請將你 _kmeans 函式中的 for _ in range(max_iter) 迴圈完整複製到此處) ...
                # --- Start of inner K-Means loop ---
                labels = []
                for point_idx, point in enumerate(points):
                    min_dist_sq = float('inf')
                    closest_centroid_idx = -1
                    for centroid_idx, centroid in enumerate(centroids):
                        dist_sq = (point[0] - centroid[0])**2 + (point[1] - centroid[1])**2
                        if dist_sq < min_dist_sq:
                            min_dist_sq = dist_sq
                            closest_centroid_idx = centroid_idx
                    labels.append(closest_centroid_idx)

                sums = [(0.0, 0.0, 0) for _ in range(k)]
                sx, sy, n = 0.0, 0.0, 0
                for (x, y), lab in zip(points, labels):
                    sx0, sy0, n0 = sums[lab]
                    sums[lab] = (sx0 + x, sy0 + y, n0 + 1)
                    sx += x; sy += y; n += 1
                new_centroids = []
                fallback = (sx / n, sy / n) if n else (0.0, 0.0)
                for (sx0, sy0, n0) in sums:
                    if n0 == 0: new_centroids.append(fallback)
                    else: new_centroids.append((sx0 / n0, sy0 / n0))
                
                moved = sum(abs(new_centroids[j][0] - centroids[j][0]) + abs(new_centroids[j][1] - centroids[j][1]) for j in range(k))
                centroids = new_centroids
                current_labels = labels # 記錄當前的標籤
                if moved < 1e-6: break
                # --- End of inner K-Means loop ---

            # --- 3. 計算該次執行的 WCSS ---
            current_wcss = 0
            for point_idx, point in enumerate(points):
                assigned_centroid = centroids[current_labels[point_idx]]
                current_wcss += (point[0] - assigned_centroid[0])**2 + (point[1] - assigned_centroid[1])**2

            # --- 4. 比較並儲存最佳結果 ---
            if current_wcss < best_wcss:
                best_wcss = current_wcss
                best_labels = current_labels
                best_centroids = centroids
        
        # print(f"Best WCSS found after {num_restarts} restarts: {best_wcss}")
        return best_labels
    # 若總數本來就不超過上限，維持你原本的行為：直接返回（不插）
    if len(sinks) <= max_per_group:
        print("too small")
        return buffer_name_idx

    # 1) 取所有 sink 的座標
    points = [_inst_center(it.getInst()) for it in sinks]

    # 2) 群數 k：讓平均每群 ~ max_per_group
    import math
    k = max(1, min(len(points), math.ceil(len(points) / max_per_group)))

    # 3) K-Means 標籤
    labels = _kmeans(points, k)

    # 4) 依標籤分群
    tmp_groups = [[] for _ in range(k)]
    for it, lab in zip(sinks, labels):
        tmp_groups[lab].append(it)

    # 5) 後處理：若某群仍 > max_per_group，按 x 再切片（保證上限）
    def iterm_center(it):
        inst = it.getInst()
        return _inst_center(inst)  # (cx, cy)

    sink_groups = []
    for g in tmp_groups:
        if len(g) <= max_per_group:
            sink_groups.append(g)
        else:
            g_sorted = sorted(g, key=lambda t: iterm_center(t)[0])
            for i in range(0, len(g_sorted), max_per_group):
                sink_groups.append(g_sorted[i:i + max_per_group])

    # ----------------------- 後續建立 buffer 的流程不變 -----------------------
    buf_master = buffer_master_list[buffer_idx]  # 你給的 master（通常是 BUFx/CLKBUF）
    
    sig_type = net.getSigType()                  # 保留 CLOCK / SIGNAL 屬性

    for group in sink_groups:
        # 3.1) 決定 buffer 擺放位置：取該組 sinks 的幾何中心（含 driver 做加權平均）
        xs, ys = [], []
        for it in group:
            cx, cy = iterm_center(it)
            xs.append(cx); ys.append(cy)
        if xs and ys:
            gx = int((sum(xs) + driver_x) / (len(xs) + 1))
            gy = int((sum(ys) + driver_y) / (len(ys) + 1))
        else:
            # fallback：用原 net 連線的所有 cell 的中心平均
            all_cx = []; all_cy = []
            for it in net.getITerms():
                cx, cy = _inst_center(it.getInst())
                all_cx.append(cx); all_cy.append(cy)
            if not all_cx:
                continue
            gx = int(sum(all_cx) / len(all_cx))
            gy = int(sum(all_cy) / len(all_cy))

        # 3.2) 建立 buffer instance（名稱與 net 名稱都用遞增 index 確保唯一）
        buf_name = f"clk_buffer{buffer_name_idx}"
        new_buf = odb.dbInst_create(block, buf_master, buf_name)
        buf_master_name = new_buf.getMaster().getName()
        new_buffer_list.append(new_buf)
        dx, dy = _inst_size(new_buf)  # cell 寬高（DBU）
        new_buf.setLocation(gx - dx // 2, gy - dy // 2)
        new_buf.setPlacementStatus("PLACED")

        # 3.3) 取得 buffer 的輸入/輸出腳位
        buf_inputs  = [t for t in new_buf.getITerms() if t.isInputSignal()]
        buf_outputs = [t for t in new_buf.getITerms() if t.isOutputSignal()]

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

    # 保留你原本的遞迴（此時原 net 已無 sinks，遞迴會立即返回，不會造成重複插入）
    buffer_name_idx = insert_buffer_kmeans_in_clk_net(
        net, odb, block, buffer_master_list, buffer_idx, buffer_name_idx,
        _inst_center, _inst_size, max_per_group, new_buffer_list,
        clk_x, clk_y, insert_buffer_chain_in_clk_net
    )
    return buffer_name_idx
def insert_inverter_pair(net, odb, block, inv_master_list, inv_idx, _inst_center, _inst_size,clk_x,clk_y, inv_name_idx):
    print("old net name:",net.getName())
    sinks = []
    drivers = []
    for it in list(net.getITerms()):  # 轉成 list 避免遍歷中修改連線
        if it.isInputSignal():
            sinks.append(it)
        elif it.isOutputSignal():
            drivers.append(it)
            print("driver:", it.getName())
    if not sinks:  # 沒有 sink 就不做事
        return inv_name_idx
    if not drivers:
        print("no driver")
        for btt in bterms:
            if btt.getName() == net.getName():
                print(btt.getName())
                one, btt_x ,btt_y = btt.getFirstPinLocation()
                print(btt_x,btt_y)
        driver_x, driver_y = btt_x, btt_y
    # 1. 建立兩個 inverter instances
    inv_master = inv_master_list[inv_idx]
    inv1_inst = odb.dbInst_create(block, inv_master, f"inv1_no.{inv_name_idx}")
    inv2_inst = odb.dbInst_create(block, inv_master, f"inv2_no.{inv_name_idx}")
    
    # ... (設定位置和 placement status) ...
    def iterm_center(it):
        inst = it.getInst()
        return _inst_center(inst)  # (cx, cy)
    xs, ys = [], []
    
    for it in net.getITerms():
        cx, cy = iterm_center(it)
        xs.append(cx); ys.append(cy)
    if xs and ys:
        if not drivers:
            gx = int((sum(xs) + driver_x) / (len(xs) + 1))
            gy = int((sum(ys) + driver_y) / (len(ys) + 1))
        else:
            gx = int((sum(xs)) / (len(xs)))
            gy = int((sum(ys)) / (len(ys)))
    inv1_inst.setLocation(gx, gy)
    inv1_inst.setPlacementStatus("PLACED")
    # (可以為 inv2 設置一個稍微偏移的位置)
    inv2_inst.setLocation(gx + inv_master.getWidth(), gy)
    inv2_inst.setPlacementStatus("PLACED")

    # 2. 取得新 inverters 的 pins
    inv1_inputs  = [t for t in inv1_inst.getITerms() if t.isInputSignal()]
    inv1_outputs = [t for t in inv1_inst.getITerms() if t.isOutputSignal()]
    inv2_inputs  = [t for t in inv2_inst.getITerms() if t.isInputSignal()]
    inv2_outputs = [t for t in inv2_inst.getITerms() if t.isOutputSignal()]

    # 3. 建立中間的 net + 連線
    intermediate_net = odb.dbNet_create(block, f"inv_mid_net{inv_name_idx}")
    inv1_outputs[0].connect(intermediate_net)
    inv2_inputs[0].connect(intermediate_net)
    
    # a. 原 net 的 sinks 全部斷開
    for iterm in sinks:
        if iterm.isInputSignal():
            iterm.disconnect()

    # b. 連接 inv1_input
    inv1_inputs[0].connect(net)
    
    # c. 將 inv2 的輸出作為新的 driver，重新連接所有 sinks
    new_inv_net = odb.dbNet_create(block, f"new_inv_net{inv_name_idx}")
    inv2_outputs[0].connect(new_inv_net)
    for iterm in sinks:
        if iterm.isInputSignal():
            iterm.connect(new_inv_net) # 連接到 inv2 所在的 net
            
    print(f"Inserted inverter pair {inv1_inst.getName()} -> {inv2_inst.getName()}")
    inv_name_idx += 1
    return inv_name_idx
db = ord.get_db()
libs = db.getLibs()# Get all cell libraries from different files (if multiple .lib files are read)
buffer_master_list = [] #所有可用buffer type list
inv_master_list = []
timing.makeEquivCells()
for lib in libs:
    lib_name = lib.getName()# Get library name
    lib_masters = lib.getMasters()  # Get all library cells in that library
    for master in lib_masters:
        libcell_name = master.getName()# Get the name of the library cell
        if design.isBuffer(master):
            buffer_master_list.append(master)
        if design.isInverter(master):
            inv_master_list.append(master)
buffer_idx = max(0,int(len(buffer_master_list)-13))
inv_idx = max(0,int(len(inv_master_list)-13))
equiv_cells = timing.equivCells(buffer_master_list[0])
inv_equiv_cells = timing.equivCells(inv_master_list[0])

iec_name = []
for iec in equiv_cells:
    iec_name.append(iec.getName())
print(iec_name)
print(len(iec_name))
buffer_master_list = equiv_cells 
inverter_master_list = inv_equiv_cells
buffer_idx_master_name =  buffer_master_list[buffer_idx].getName()
inv_idx_master_name =  inv_master_list[inv_idx].getName()
buffer_name_idx = 1 
inv_name_idx = 1
new_buffer_list = []
buffer_name_idx = insert_buffer_kmeans_in_clk_net(clk_net, odb, block, buffer_master_list, buffer_idx,
                            buffer_name_idx, _inst_center, _inst_size,clk_group,new_buffer_list,clk_x,clk_y,insert_buffer_chain_in_clk_net)
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
sorted_with_length_nets = sorted(nets_dict.items(),key=lambda item: item[1]['length'],reverse=True)   # fanout排序的nets list
sorted_critical_nets = sorted(net_criticality.items(), key=lambda item: item[1], reverse=True)

inserted_net = [] 

# for name,sorted_with_length_net_dict in sorted_with_length_nets[:20]:
#     old_buffer_net = sorted_with_length_net_dict['net']
#     print(old_buffer_net.getName())
#     if old_buffer_net == clk_net:
#         continue
#     fi = False
#     for inn in inserted_net:
#         if old_buffer_net == clk_net:
#             fi = True
#             break
#     if fi is True: continue
#     inv_name_idx = insert_inverter_pair(old_buffer_net, odb, block, inv_master_list, inv_idx, _inst_center, _inst_size,clk_x,clk_y, inv_name_idx)
    # net_ITerms = old_buffer_net.getITerms()
    # center_x_list = []
    # center_y_list = []
    # net_sink_pins = []
    # net_driver_pins = []
    # for net_ITerm in net_ITerms:
    #     cell = net_ITerm.getInst()
    #     center_x,center_y = _inst_center(cell)
    #     center_x_list.append(center_x)
    #     center_y_list.append(center_y)
    #     if net_ITerm.isInputSignal() is True:
    #         net_sink_pins.append(net_ITerm)
    #     if net_ITerm.isOutputSignal() is True:
    #         net_driver_pins.append(net_ITerm)
    # if len(center_x_list) == 0:
    #     continue
    # if len(center_y_list) == 0:
    #     continue
    # x_center = int(sum(center_x_list)/len(center_x_list))
    # y_center = int(sum(center_y_list)/len(center_y_list))
    # new_buffer_name = f"data_buffer{buffer_name_idx}"
    # new_buffer_master = buffer_master_list[buffer_idx] #master
    # new_buffer = odb.dbInst_create(block, new_buffer_master,  f"data_buffer{buffer_name_idx}")#後面是name
    # new_buffer_name_list.append(f"data_buffer{buffer_name_idx}")
    # buffer_name_idx += 1
    # dx,dy = _inst_size(new_buffer)
    # new_buffer.setLocation(x_center - dx//2,y_center - dy//2)
    # new_buffer.setPlacementStatus("PLACED")

    # new_buffer_output_pins = [c for c in new_buffer.getITerms() if c.isOutputSignal()]
    # new_buffer_input_pins  = [c for c in new_buffer.getITerms() if c.isInputSignal()]
    # new_buffer_net = odb.dbNet_create(block, f"net_buffer{buffer_name_idx}")#後面是name;net
    # for new_buffer_output_pin in new_buffer_output_pins:
    #     new_buffer_output_pin.connect(new_buffer_net)
    # for new_buffer_input_pin in new_buffer_input_pins:
    #     new_buffer_input_pin.connect(old_buffer_net)
    # for net_sink_pin in net_sink_pins:
    #     net_sink_pin.disconnect()
    #     net_sink_pin.connect(new_buffer_net)

design.evalTclString("estimate_parasitics -placement")
update_full_slacks(cellgraph,block,timing,corner)
print("after buffer tns:")
design.evalTclString("report_tns")
# # --------------------------------buffer list--------------------------------------

# # ----------------------------detailed placement-------------------------------------
# design.evalTclString("improve_placement") 
max_disp_x = int(design.micronToDBU(8) / site.getWidth())
max_disp_y = int(design.micronToDBU(8) / site.getHeight())
design.getOpendp().detailedPlacement(max_disp_x, max_disp_y, "dpl_failures.txt",)
design.getOpendp().reportLegalizationStats()
# # ----------------------------detailed placement-------------------------------------
design.writeDef(f"{design_name}.sol.def")
with open(f"{design_name}.sol.changelist", "w") as f:
    for new_buffer in new_buffer_list:
        new_buffer_load_pins = [pin for pin in new_buffer.getITerms() if pin.isOutputSignal() is True] #也有可能是output
        new_buffer_load_pins_name = [pin.getName() for pin in new_buffer_load_pins]
        library_cell_name = new_buffer.getMaster().getName()
        new_buffer_name = new_buffer.getName()
        new_buffer_net_name =  new_buffer_load_pins[0].getNet().getName()
        f.write(f"insert_buffer {new_buffer_load_pins_name[0]} {library_cell_name} {new_buffer_name} {new_buffer_net_name}\n")
# # ------------------------------------------------------------------------------------
after_centers = get_instance_centers(design)
displacements = compute_displacements(before_centers, after_centers)
design.evalTclString("estimate_parasitics -placement")
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
    