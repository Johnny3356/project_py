from pathlib import Path
import openroad as ord
import os
from collections import OrderedDict, defaultdict
from openroad import Tech, Design, Timing
import re
import json
from dataclasses import dataclass, field
from typing import Dict, List,Union, Tuple
# ----------------------------------------------------------------------
# 1. 先找出「src 目錄」的絕對路徑，再推導 workspace 根目錄
# ----------------------------------------------------------------------
THIS_PY   = Path(__file__).resolve()          # /mnt/c/.../iccad_c/src/run_rl.py
SRC_DIR   = THIS_PY.parent                    # /mnt/c/.../iccad_c/src
WORKSPACE = SRC_DIR.parent                    # /mnt/c/.../iccad_c

# 1.1. 組出 testcase、lib、lef、def 的完整路徑
TESTCASE_DIR = WORKSPACE / "ICCAD25_PorbC" / "ASAP7"
LIB_DIR      = TESTCASE_DIR / "LIB"
LEF_DIR      = TESTCASE_DIR / "LEF" 
TECH_LEF_DIR      = TESTCASE_DIR / "techlef" 
TECH_LEF_FILE = TECH_LEF_DIR / "asap7_tech_1x_201209.lef"
DEF_FILE     = WORKSPACE / "ICCAD25_PorbC" / "aes_cipher_top" / "aes_cipher_top.def"
SDC_FILE     = WORKSPACE / "ICCAD25_PorbC" / "aes_cipher_top" / "aes_cipher_top.sdc"
RC_TCL       = WORKSPACE / "ICCAD25_PorbC" / "ASAP7" / "setRC.tcl"
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

VT_ORDER    = ["SL","R","L","SRAM"]
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
    slacks = []
    for it in input_terms:
        # 跳过非 signal（VDD/VSS）
        if it.getNet().getSigType() != "SIGNAL":
            continue
        sr = timing.getPinSlack(it, timing.Rise, timing.Max)
        sf = timing.getPinSlack(it, timing.Fall, timing.Max)
        slacks.append(min(sr, sf))
    slack = min(slacks) if slacks else 0.0
    if slack < worstpinslack:
            worstpinslack = slack

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
    return sum(node.features['slack']
               for node in cellgraph.values()
               if node.features['slack'] < 0.0)
def compute_power(block,timing,corner):
    static_p = sum(timing.staticPower(block.findInst(n), corner)
               for n in cellgraph)
    dyn_p    = sum(timing.dynamicPower(block.findInst(n), corner)
               for n in cellgraph)
    return static_p + dyn_p  # 單位：瓦    
    
# def compute_tns_from_sub_graph(cellnode,block,cell_dict,cell_name_dict,cellgraph):
#     allslack = cellnode.features['slack']
#     update_full_slacks(cellgraph,block,timing,corner)
#     for fanout_cell_name in fanout_cells:
#         inst =  cellgraph[fanout_cell_name]
#         # new_slack = update_cell_slack(block,inst.name,cell_dict,cell_name_dict)
#         allslack += inst.features['slack']
#     for fanin_cell_name in fanin_cells:
#         inst =  block.findInst(fanin_cell_name)
#         # new_slack = update_cell_slack(block,cellnode.name,cell_dict,cell_name_dict)
#         allslack += inst.features['slack']
#     return allslack

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
        for it in input_terms:
            sr = timing.getPinSlack(it, timing.Rise, timing.Max)
            sf = timing.getPinSlack(it, timing.Fall, timing.Max)
            slacks.append(min(sr, sf))
        cellgraph[name].features['slack'] = min(slacks) if slacks else 0.0

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
# # --------------------------貪婪greeeeeeeeeedy----------------------------------
# 2. 篩選出所有 slack < 0 的節點，並按 slack 越負越前排序
N = 15
neg_nodes = [node for node in cellgraph.values() if node.features['slack'] < 0.0]
neg_nodes.sort(key=lambda n: n.features['slack'])
for i, node in enumerate(neg_nodes[:N]):
    inst_name = node.name
    inst = block.findInst(inst_name)
    if not inst:
        continue
    old_master = inst.getMaster()
    old_master_name = old_master.getName()
    equivCells_masters = timing.equivCells(old_master)
    equivCells_masters_names = [e.getName() for e in equivCells_masters]
    idx = equivCells_masters_names.index(old_master_name)
    if idx + 3 < len(equivCells_masters):
        inst.swapMaster(equivCells_masters[idx+3]) 
# # ----------------------------------------------------------------------
# 3. 對最嚴重的前 N 顆做 sizing（這裡示範 N=50，可視需求調整）
update_full_slacks(cellgraph, block, timing, corner)
seed_tns = compute_tns_from_graph(cellgraph)
print("First TNS =", seed_tns)

for epoch in range(8):
    print(f"\n=== Sizing ROUND {epoch+1}/8 ===")
    a = 0
    b = 0
    # 1) 先刷新所有 node slack
    update_full_slacks(cellgraph, block, timing, corner)

    # 2) 重新找出负 slack 并排序
    neg_nodes = [n for n in cellgraph.values() if n.features['slack'] < 0.0]
    neg_nodes.sort(key=lambda n: n.features['slack'])
    for i, node in enumerate(neg_nodes[:N]):
        inst_name = node.name
        inst = block.findInst(inst_name)
        if not inst:
            continue
        old_master = inst.getMaster()
        old_master_name = old_master.getName()
        equivCells_masters = timing.equivCells(old_master)
        equivCells_masters_names = [e.getName() for e in equivCells_masters]
        # ========================== 使用 timing.equivCells 的邏輯 ==========================
        idx = equivCells_masters_names.index(old_master_name)

        # 3) 构造 upsizing 候选（往后找更大 drive‑strength）
        cand_masters_names = []
        for j in (idx-1,idx+3,idx+6):
            if 0 <= j < len(equivCells_masters_names) :
                cand_masters_names.append(equivCells_masters_names[j])

        # 如果没有更强的就跳过
        if not cand_masters_names:
            a += 1
            continue
        # ==============================================================================

        best_tns_so_far = seed_tns
        best_master_name_to_swap = None

        for new_master_name in cand_masters_names:
            # 需要從 cell name (string) 找到 master object
            for equivCells_master in equivCells_masters:
                if new_master_name == equivCells_master.getName():
                    new_master = equivCells_master
    
            # 暫時應用 Sizing
            inst.swapMaster(new_master)
            update_full_slacks(cellgraph,block,timing,corner)
            design.evalTclString("report_tns")
            # new_tns = float(design.evalTclString("report_tns").split()[0])
            new_tns = compute_tns_from_graph(cellgraph)

            if new_tns > best_tns_so_far:
                best_tns_so_far = new_tns
                best_master_name_to_swap = new_master_name
                best_master_to_swap = new_master

            # 復原狀態
            inst.swapMaster(old_master)

        # 做出最終決定
        if best_master_name_to_swap:
            improvement = best_tns_so_far - seed_tns
            
            # 永久應用最佳的 Sizing
            best_master = best_master_to_swap
            inst.swapMaster(best_master)
            update_full_slacks(cellgraph,block,timing,corner)
            
            print(f"[{i+1}/{N}] Sizing {inst_name}: {old_master_name} -> {best_master_name_to_swap}, New TNS: {best_tns_so_far:.4f}, ΔTNS: +{improvement:.4f}")

            # 更新下一次迭代的基準 TNS
            seed_tns = best_tns_so_far
            
        else:
            print(f"[{i+1}/{N}] Sizing {inst_name}: No improvement found.")
            b += 1
            
    N -= 2
    print(a,b)
# # --------------------------貪婪greeeeeeeeeedy結束----------------------------------
# # --------------------------report---------------------------------- 
update_full_slacks(cellgraph, block, timing, corner)
print("Final TNS =", compute_tns_from_graph(cellgraph))
design.evalTclString("report_tns")
design.evalTclString("report_wns")  
# # --------------------------moveeeeeeeeee---------------------------------- 
# neg_nodes = [n for n in cellgraph.values() if n.features['slack'] < 0.0]
# neg_nodes.sort(key=lambda n: n.features['slack'])
# for i, node in enumerate(neg_nodes[:N]):
#     inst_name = node.name
#     inst = block.findInst(inst_name)
#     if not inst:
#         continue
#     box = inst.getBBox()
#     # 取 BBox 中心
#     inst_x = 0.5 * (box.xMin() + box.xMax())
#     inst_y = 0.5 * (box.yMin() + box.yMax())
#     x_in_lists = []
#     y_in_lists = []
#     for in_node in node.fanin_cells:
#         in_node_inst_name = in_node.name
#         in_node_inst = block.findInst(in_node_inst_name)
#         box = inst.getBBox()
#         in_node_inst_x = 0.5 * (box.xMin() + box.xMax())
#         in_node_inst_y = 0.5 * (box.yMin() + box.yMax())
#         x_in_lists.append(in_node_inst_x)
#         y_in_lists.append(in_node_inst_y)
#     ava_xin = sum(x_in_inst for x_in_inst in x_in_insts)/len(x_in_insts)
#     ava_yin = sum(y_in_inst for y_in_inst in y_in_insts)/len(y_in_insts)
#     delta_x = inst_x - ava_xin  
#     delta_y = inst_y - ava_yin  
#     box.xMin() -= delta_x/10
#     box.xMax() -= delta_x/10
#     box.yMin() -= delta_y/10
#     box.yMax() -= delta_y/10
# # ---------------------------moveeeeeeee-------------------------------------
# # ---------------------------buffer-------------------------------------
# design.evalTclString("estimate_parasitics -placement")
# design.evalTclString("repair_design -match_cell_footprint  -max_wire_length 10 ") 
# # ---------------------------buffer-------------------------------------    
# # ----------------------------detailed placement-------------------------------------
before_centers = get_instance_centers(design)
site = design.getBlock().getRows()[0].getSite()
max_disp_x = int(design.micronToDBU(4) / site.getWidth())
max_disp_y = int(design.micronToDBU(4) / site.getHeight())
design.getOpendp().detailedPlacement(max_disp_x, max_disp_y, "dpl_failures.txt",)
design.getOpendp().reportLegalizationStats()
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
print(f"Total |Δx| = {total_abs_dx:.3f} μm, Total |Δy| = {total_abs_dy:.3f} μm")
design.evalTclString("report_power")
# # ---------------------------------final report-------------------------------------
# # -----------------------------write def-----------------------------------------
db.endEco(block)
design.writeDef("final.def")
# design.getDb().writeEco(block,"eco_changelist.eco")