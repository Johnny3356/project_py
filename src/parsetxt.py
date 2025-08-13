import re
import json
from collections import OrderedDict
from typing import List, Dict, Optional

def extract_path_blocks(lines: List[str]) -> List[List[str]]:
    """
    从 STA 报告中抽取每条路径的 block（以 Startpoint: 开头）
    """
    blocks = []
    current = []
    in_path = False
    for line in lines:
        if line.strip().startswith("Startpoint:"):
            if in_path and current:
                blocks.append(current)
            current = [line]
            in_path = True
        elif in_path:
            current.append(line)
    if in_path and current:
        blocks.append(current)
    return blocks

# def parse_cells_from_block(block: List[str]) -> List[Dict]:
#     """
#     解析一个路径 block 中的所有 cell，并去重（同一 instance 只保留一次，保留输出端信息）
#     只解析 data arrival time 之前的行，跳过 clock、latency、required-time 等行
#     """
#     # 1) 找 header 行
#     header_idx = 0
#     for idx, line in enumerate(block):
#         if "Fanout" in line and "Delay" in line and "Time" in line and "Description" in line:
#             header_idx = idx
#             break

#     # 2) 找到 “data arrival time” 之前的切点
#     cutoff_idx = len(block)
#     for idx, line in enumerate(block):
#         if "data arrival time" in line.lower():
#             cutoff_idx = idx
#             break

#     # 3) 用 OrderedDict 去重
#     cells_dict = OrderedDict()
#     suffix_re = re.compile(r'([\^v])\s+([^\s/]+/[^\s]+)\s*\(([^)]+)\)')

#     # 4) 逐行解析
#     for line in block[header_idx+1 : cutoff_idx]:
#         m = suffix_re.search(line)
#         if not m:
#             continue
#         direction = m.group(1)           # "^" or "v"
#         full_inst = m.group(2)           # e.g. "cellX/Y"
#         cell_type = m.group(3)           # e.g. "INVx2_ASAP7_75t_SL"
#         inst_name, pin = full_inst.split("/", 1)

#         # fanout~arrival 五个数值就在匹配位置之前
#         nums = line[: m.start()].strip().split()
#         if len(nums) < 5:
#             continue
#         fanout  = int(nums[0])
#         cap     = float(nums[1])
#         slew    = float(nums[2])
#         delay   = float(nums[3])
#         arrival = float(nums[4])

#         # 去重：第一次见写入，之后遇到输出端(direction=="^")再更新
#         if inst_name not in cells_dict or direction == "^":
#             cells_dict[inst_name] = {
#                 "instance":  inst_name,
#                 "cell_type": cell_type,
#                 "pin":       pin,
#                 "direction": direction,
#                 "fanout":    fanout,
#                 "cap":       cap,
#                 "slew":      slew,
#                 "delay":     delay,
#                 "arrival":   arrival,
#                 "raw":       line.rstrip()
#             }

#     return list(cells_dict.values())
# ---- 共同使用的 regex 與小工具 ----
def _is_clock_net(netname: str) -> bool:
    if not netname:
        return False
    n = netname.strip().lower()
    # 依你專案命名可再擴充；這裡先把典型的 clk/clk_*/*_clk 都排除
    return n == "clk" or n.startswith("clk") or n.endswith("clk")
def build_net_map(cells: List[Dict]) -> Dict[str, Dict]:
    """
    建立「除了 clock 之外，路徑上所有 net」的對應：
      net_name -> {
        "driver": {"inst": ..., "pin": ..., "slew": ...},
        "sink":   {"inst": ..., "pin": ..., "slew": ...}
      }
    演算法：
      1) 先收集所有出現在 cells 的 input_net 與 output_net（聯集）
      2) 移除 clock 類型（_is_clock_net）
      3) 對每個 net，找 driver = output_net == net 的那顆 cell
                      找  sink  = input_net  == net 的那顆 cell（通常是下一顆）
    """
    # 1) 收集所有 net
    nets_set = []
    seen = set()
    for c in cells:
        for k in ("input_net", "output_net"):
            n = c.get(k)
            if not n or _is_clock_net(n):
                continue
            if n not in seen:
                seen.add(n)
                nets_set.append(n)

    # 2) 建立查找表（提升效率）
    by_output = {}  # net -> cell dict（driver）
    by_input  = {}  # net -> cell dict（sink）
    for c in cells:
        on = c.get("output_net")
        if on and not _is_clock_net(on) and on not in by_output:
            by_output[on] = c
        inn = c.get("input_net")
        if inn and not _is_clock_net(inn) and inn not in by_input:
            by_input[inn] = c

    # 3) 組裝結果（保持 nets_set 出現順序）
    net_map = OrderedDict()
    for netname in nets_set:
        drv_cell = by_output.get(netname)
        sink_cell = by_input.get(netname)

        driver = None
        if drv_cell:
            driver = {
                "inst": drv_cell.get("instance"),
                "pin":  drv_cell.get("output_pin"),
                "slew": drv_cell.get("output_slew"),
            }

        sink = None
        if sink_cell:
            sink = {
                "inst": sink_cell.get("instance"),
                "pin":  sink_cell.get("input_pin"),
                "slew": sink_cell.get("input_slew"),
            }

        net_map[netname] = {"driver": driver, "sink": sink}

    return net_map

_inst_row = re.compile(r'([\\^v])\s+([^\s/]+/[^\s]+)\s*\(([^)]+)\)\s*$')
_net_row  = re.compile(r'^\s*([^\s]+)\s*\(net\)\s*$', re.IGNORECASE)
_num_tok  = re.compile(r'[-+]?\d+(?:\.\d+)?')

def _to_float(s: Optional[str]) -> Optional[float]:
    if s is None:
        return None
    try:
        return float(s)
    except Exception:
        return None

def _is_clockish_pin(pin: str) -> bool:
    u = pin.upper()
    # 依你的設計可再擴充
    bad = ("CLK", "CK", "CLKN", "RESET", "SET", "RN", "SN", "SE", "TE")
    return any(b in u for b in bad)

def parse_cells_from_block(block: List[str]) -> List[Dict]:
    # 1) header & cutoff
    header_idx = None
    cutoff_idx = len(block)
    for i, line in enumerate(block):
        if header_idx is None and all(k in line for k in ("Fanout", "Slew", "Delay", "Time", "Description")):
            header_idx = i
            continue
        if "data arrival time" in line.lower():
            cutoff_idx = i
            break
    if header_idx is None:
        return []

    cells_by_inst: "OrderedDict[str, Dict]" = OrderedDict()
    order: List[str] = []

    # 狀態
    data_started = False               # 已進入 data path？
    pending_output_inst = None         # 剛見到輸出腳的 inst（下一個 net 給它）
    current_net = None                 # 目前 data net（供一串 sinks）
    sinks_seen = set()                 # current_net 下已看過 input 的 inst
    last_net = None                    # 最近一次看到的 (net)（包含 clock net；僅起跑前允許使用）

    for line in block[header_idx + 1 : cutoff_idx]:
        s = line.rstrip("\n")

        # (A) inst/pin 列
        m = _inst_row.search(s)
        if m:
            caret, full, cell_type = m.groups()
            inst, pin = full.split("/", 1)

            # 右往左取 Time, Delay, Slew, (Cap), (Fanout)
            left = s[:m.start()].rstrip()
            toks = _num_tok.findall(left)
            time_v  = _to_float(toks[-1]) if len(toks) >= 1 else None
            delay   = _to_float(toks[-2]) if len(toks) >= 2 else None
            slew    = _to_float(toks[-3]) if len(toks) >= 3 else None
            cap     = _to_float(toks[-4]) if len(toks) >= 4 else None
            fanout  = int(float(toks[-5])) if len(toks) >= 5 else None

            # 建或更
            if inst not in cells_by_inst:
                order.append(inst)
                cells_by_inst[inst] = {
                    "instance":   inst,
                    "cell_type":  cell_type,
                    "fanout":     fanout,
                    "cap":        cap,
                    "slew":       None,    # 保留舊欄位；請改用 input_slew/output_slew
                    "delay":      delay,
                    "arrival":    time_v,
                    "input_pin":  None, "input_net":  None, "input_slew":  None,
                    "output_pin": None, "output_net": None, "output_slew": None,
                    "raw":        []
                }
            else:
                g = cells_by_inst[inst]
                g["cell_type"] = cell_type
                g["fanout"] = fanout if fanout is not None else g["fanout"]
                g["cap"]    = cap    if cap    is not None else g["cap"]
                g["delay"]  = delay  if delay  is not None else g["delay"]
                g["arrival"]= time_v if time_v is not None else g["arrival"]

            cells_by_inst[inst]["raw"].append(s)

            # --------- 核心修正：處理起點 FF 的 /CLK 作為 input ----------
            if not data_started and _is_clockish_pin(pin):
                # 只有在 data 尚未起跑時，允許 clock 腳吃 "上一個 (net)" 作為 input_net
                if last_net is not None:
                    cells_by_inst[inst]["input_pin"]  = pin
                    cells_by_inst[inst]["input_net"]  = last_net    # 例如 "clk"
                    cells_by_inst[inst]["input_slew"] = slew
                    last_net = None  # 用過就清
                # 不啟動 data，也不設 pending_output_inst；等同一顆 /QN 來啟動
                continue
            # ---------------------------------------------------------

            # 非 clock 腳（或 data 已啟動後的任何腳）
            if not data_started:
                # 這是資料路徑的第一個輸出腳（例：/QN）
                cells_by_inst[inst]["output_pin"]  = pin
                cells_by_inst[inst]["output_slew"] = slew
                pending_output_inst = inst
                data_started = True
            else:
                if current_net is None:
                    # 沒有活躍 data net → 新的 driver 輸出腳
                    cells_by_inst[inst]["output_pin"]  = pin
                    cells_by_inst[inst]["output_slew"] = slew
                    pending_output_inst = inst
                else:
                    # 有活躍 data net
                    if inst not in sinks_seen:
                        # 先來 input（吃 current_net）
                        cells_by_inst[inst]["input_pin"]  = pin
                        cells_by_inst[inst]["input_net"]  = current_net
                        cells_by_inst[inst]["input_slew"] = slew
                        sinks_seen.add(inst)
                    else:
                        # 再來 output
                        cells_by_inst[inst]["output_pin"]  = pin
                        cells_by_inst[inst]["output_slew"] = slew
                        pending_output_inst = inst
            continue

        # (B) net 列
        n = _net_row.search(s)
        if n:
            net_name = n.group(1)

            if data_started:
                # 只有在已啟動資料路徑時，(net) 才可能是 data net
                if pending_output_inst and pending_output_inst in cells_by_inst:
                    # 這個 net 是剛剛那個輸出腳的 output_net，同時成為新的 current_net
                    cells_by_inst[pending_output_inst]["output_net"] = net_name
                    current_net = net_name
                    sinks_seen.clear()
                    pending_output_inst = None
                else:
                    # data 已啟動，但沒有 pending driver：這種 (net) 多半是訊息行，忽略
                    pass
            else:
                # data 尚未起跑：這裡的 (net) 可能是 clock net
                last_net = net_name   # 先記下，等下一行若是 /CLK 就能當 input_net
            continue

        # 其它行忽略

    return [cells_by_inst[name] for name in order]



def parse_single_block(block: List[str]) -> Dict:
    """
    将一个路径 block 解析成 dict，包含 startpoint、endpoint、slack、cells 等信息
    """
    text = "".join(block)
    startpoint = ""
    endpoint = ""
    for line in block:
        if line.strip().startswith("Startpoint:"):
            startpoint = line.split("Startpoint:")[1].strip()
        if line.strip().startswith("Endpoint:"):
            endpoint = line.split("Endpoint:")[1].strip()

    # 提取 slack
    slack_value = None
    violated = False
    for line in reversed(block):
        m = re.search(r'([+-]?\d+\.\d+)\s+slack', line, flags=re.IGNORECASE)
        if m:
            slack_value = float(m.group(1))
            if "violated" in line.lower():
                violated = True
            break

    cells = parse_cells_from_block(block)
    net_map = build_net_map(cells)
    return {
        "startpoint": startpoint,
        "endpoint":   endpoint,
        "slack":      slack_value,
        "violated":   violated or (slack_value is not None and slack_value < 0),
        "cells":      cells,
        "net":        net_map,
        "raw_body":   text
    }

def parse_sta_report(path: str) -> List[Dict]:
    """
    解析整个 STA 报告，返回所有 slack < 0 的路径列表
    """
    with open(path, encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    blocks = extract_path_blocks(lines)
    parsed = [parse_single_block(b) for b in blocks]
    violated_paths = [p for p in parsed if p["violated"]]
    return violated_paths



