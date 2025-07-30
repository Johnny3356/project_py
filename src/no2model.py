#標準化
#算法退火結構
#power要加到cost function
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

WL_WEIGHT     = args.wl
POWER_WEIGHT  = args.power
TIMING_WEIGHT = args.timing

THIS_PY   = Path(__file__).resolve()                   # /mnt/c/.../project_py/src/no2model.py
SRC_DIR   = THIS_PY.parent                             # /mnt/c/.../project_py/src
WORKSPACE = SRC_DIR.parent                             # /mnt/c/.../project_py
DESIGN_PATH = WORKSPACE / Path(args.design)            # /mnt/c/.../project_py/ICCAD25_PorbC

# 1.1. 組出 testcase、lib、lef、def 的完整路徑
TESTCASE_DIR = DESIGN_PATH / "ASAP7"
LIB_DIR      = TESTCASE_DIR / "LIB"
LEF_DIR      = TESTCASE_DIR / "LEF" 
TECH_LEF_DIR      = TESTCASE_DIR / "techlef" 
TECH_LEF_FILE = TECH_LEF_DIR / "asap7_tech_1x_201209.lef"
DEF_FILE     = DESIGN_PATH / "aes_cipher_top" / "aes_cipher_top.def"
SDC_FILE     = DESIGN_PATH / "aes_cipher_top" / "aes_cipher_top.sdc"
RC_TCL       = TESTCASE_DIR / "setRC.tcl"
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

# 写入 JSON
# Path("full_name_dict.json").write_text(json.dumps(full_name_dict, indent=2))
# # （可选）写 JSON 方便检查
# Path("cell_name_dict.json").write_text(json.dumps(cell_name_dict, indent=2))
# Path("cell_dict.json").write_text(json.dumps(cell_dict,      indent=2))

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
design.evalTclString(f"source   {RC_TCL}")     # 你沒有 SPEF 時，用 set_rc.tcl
design.evalTclString(f"estimate_parasitics -placement") 

sta = tech.getSta()
wns = design.evalTclString("report_wns")
tns = design.evalTclString("report_tns")

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
    fanout_cells: List[str] = field(default_factory=list)  # 由本 cell 輸出連到的 cell 名稱列表
    fanin_cells:  List[str] = field(default_factory=list)  # 驅動本 cell 的前驅 cell 名稱列表
# # ----------------------------------------------------------------------
def build_cell_graph(inst,iterms,oterms,block, timing, corner, features,nodes_by_name):
    """回傳 nodes_by_name: Dict[str, CellNode]"""
    # 1) 先為每顆 instance 建立 CellNode（先不處理 fanin/fanout）
    name   = inst.getName()
    old_name   = inst.getName()
    master = inst.getMaster().getName()
    nodes_by_name[name] = CellNode(name=name, old_name =old_name,master=master, features=features)

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
# # ----------------------------------------------------------------------
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
def update_full_slacks(cellgraph: Dict[str, CellNode],
                  block, timing, corner) -> None:
    for inst in block.getInsts():
        name = inst.getName()
        # 只算 SIGNAL 的 input pin slack
        input_terms = [
            it for it in inst.getITerms()
            if it.isInputSignal() and it.getNet().getSigType()=="SIGNAL"
        ]
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
        cellgraph[name].features['slack'] = min(slacks) if slacks else 0.0
        cellgraph[name].features['tns'] = total_n_slack

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
def compute_displacements(
    before: Dict[str, Tuple[float,float]],
    after:  Dict[str, Tuple[float,float]]
) -> Dict[str, Tuple[float,float]]:
    """
    返回 inst_name -> (dx, dy) 的位移字典，只对在 before 和 after 中都出现的 inst 计算。
    """
    disp = {}
    for name, (x0, y0) in before.items():
        if name in after:
            x1, y1 = after[name]
            disp[name] = (x1 - x0, y1 - y0)
    return disp

# 1. 先讓 STA 準備好等價 cell group
timing.makeEquivCells()
master_to_base_map = {}
for base_name, full_names in full_name_dict.items():
    for fn in full_names:
        master_to_base_map[fn] = base_name
Path("master_to_base_map.json").write_text(json.dumps(master_to_base_map,      indent=2))
# # ----------------------------------------------------------------------
# --------------------------模擬退火 (Simulated Annealing)----------------------------------
import math
import random

# 1. 模擬退火參數設定
T_initial      = 1e-10   # 初始溫度 (ps) - TNS 的數量級約為數千 ps，溫度要相對應   # 標準化
alpha          = 0.98   # 降溫速率
steps_per_temp = 10    # 每個溫度下的迭代次數
iterations     = 200  # 總迭代次數

# 2. 初始化狀態
print("\n=== Initializing Simulated Annealing ===")
update_full_slacks(cellgraph, block, timing, corner)
current_cost = abs(compute_tns_from_graph(cellgraph))
best_cost    = current_cost

# 儲存目前為止找到的最佳 cell master 指派
# 這樣我們才能在最後恢復到最佳狀態，而不是 SA 結束時的最後狀態
best_assignment = {inst.getName(): inst.getMaster() for inst in block.getInsts()}

print(f"Initial TNS: {-current_cost} ps")
print(f"Initial Cost (abs(TNS)): {current_cost}")

temp = T_initial
iteration = 0

# 3. 模擬退火主迴圈
while iteration < iterations:
    # 每次迭代開始時，顯示目前溫度和迭代次數
    print(f"\n=== Iteration {iteration + 1} / {iterations} ===")

    # 在每個溫度開始時，更新負 slack 節點列表
    neg_nodes = [n for n in cellgraph.values() if n.features['slack'] < 0.0]
    neg_nodes.sort(key=lambda n: n.features['slack'])
    
    if not neg_nodes:
        print("All timing violations resolved. Stopping early.")
        break
        
    accepted_moves = 0
    for i in range(steps_per_temp):
        # 3.1) 產生一個鄰近狀態 (隨機選擇一個 cell 並 sizing)
        # 從負 slack 的節點中隨機挑選，讓搜尋更有效率
        if i % 10 == 0:
            update_full_slacks(cellgraph, block, timing, corner)  # 建議同步
            neg_nodes = [n for n in cellgraph.values() if n.features['slack'] < 0.0]
            neg_nodes.sort(key=lambda n: n.features['slack'])
        node_to_change = random.choice(neg_nodes)
        inst_name = node_to_change.name
        inst = block.findInst(inst_name)
        
        if not inst:
            continue

        old_master = inst.getMaster()
        equiv_cells = timing.equivCells(old_master)
        equiv_cells_names = [e.getName() for e in equiv_cells]
        old_master_name = old_master.getName()
        idx = equiv_cells_names.index(old_master_name)

        # 如果沒有其他可替換的 cell，就跳過
        if len(equiv_cells) <= 1:
            continue
        
        # 隨機挑選一個新的 master
        new_master = random.choice(equiv_cells)
        new_master_name = new_master.getName()
        # 確保新舊 master 不同
        while new_master.getName() == old_master.getName():
            new_master = random.choice(equiv_cells)
        # while equiv_cells_names.index(new_master_name) < equiv_cells_names.index(old_master_name) :
        #     new_master = random.choice(equiv_cells)
        # 3.2) 計算成本變化 (ΔE)
        # 在交換前，TNS 就是目前的 current_cost
        
        # 執行交換
        inst.swapMaster(new_master)
        update_full_slacks(cellgraph, block, timing, corner)
        new_cost = abs(compute_tns_from_graph(cellgraph))
        # design.evalTclString("report_tns")
        delta_E = new_cost - current_cost

        # 3.3) 根據 Metropolis 準則決定是否接受新狀態
        # 如果是更優的解 (delta_E < 0)，或者以一定機率接受較差的解
        if delta_E < 0 or (temp > 0 and random.random() < math.exp(-delta_E / temp)):
            # 接受新狀態
            print('delta',delta_E)
            print('temp',temp)
            current_cost = new_cost
            accepted_moves += 1
            # 如果這個新狀態是至今為止最好的，就記錄下來
            if current_cost < best_cost:
                best_cost = current_cost
                if iteration > iterations - 10 and iteration < iterations:
                    best_assignment = {i.getName(): i.getMaster() for i in block.getInsts()}#可改
                print(f"  ---> New best found! TNS: {-best_cost} s")
                design.evalTclString("report_tns")
        else:
            # 不接受，恢復原狀
            inst.swapMaster(old_master)

    # 顯示目前進度
    print(f"Temp: {temp} | Current TNS: {-current_cost} s | Best TNS: {-best_cost} s | Accepted: {accepted_moves}/{steps_per_temp}")

    # 3.4) 降溫
    temp *= alpha
    iteration += 1

# 4. 恢復到找到的最佳狀態
print("\n=== Simulated Annealing Finished. Restoring best found state... ===")
for inst_name, best_master in best_assignment.items():
    inst = block.findInst(inst_name)
    if inst:
        inst.swapMaster(best_master)

# # --------------------------貪婪greeeeeeeeeedy結束----------------------------------
# # --------------------------report---------------------------------- 
update_full_slacks(cellgraph, block, timing, corner)
print("Final TNS =", compute_tns_from_graph(cellgraph))
design.evalTclString("report_tns")
design.evalTclString("report_wns")  
# # ---------------------------buffer-------------------------------------
# design.evalTclString("estimate_parasitics -placement")
# design.evalTclString("repair_design -match_cell_footprint  -max_wire_length 10 ") 
# # ---------------------------buffer-------------------------------------    
# # ----------------------------detailed placement-------------------------------------
before_centers = get_instance_centers(design)
site = design.getBlock().getRows()[0].getSite()
max_disp_x = int(design.micronToDBU(6) / site.getWidth())
max_disp_y = int(design.micronToDBU(6) / site.getHeight())
design.getOpendp().detailedPlacement(max_disp_x, max_disp_y, "dpl_failures.txt",)
after_centers = get_instance_centers(design)
displacements = compute_displacements(before_centers, after_centers)
# # ----------------------------detailed placement-------------------------------------
# # ---------------------------------final report-------------------------------------
# 最後一次 full STA／報告
update_full_slacks(cellgraph, block, timing, corner)
print("Final TNS =", compute_tns_from_graph(cellgraph))
design.evalTclString("report_tns")
design.evalTclString("report_wns")
total_abs_dx = sum(abs(dx) for dx, dy in displacements.values())
total_abs_dy = sum(abs(dy) for dx, dy in displacements.values())
print(f"Total |Δx| = {total_abs_dx} μm, Total |Δy| = {total_abs_dy} μm")
design.evalTclString("report_power")
# # ---------------------------------final report-------------------------------------
# # -----------------------------write def-----------------------------------------
db.endEco(block)
design.writeDef("final.def")
ending_time = time.time()
elapsed_time = ending_time - start_time
print(f"Total elapsed time: {elapsed_time:.2f} seconds")
# design.getDb().writeEco(block,"eco_changelist.eco")