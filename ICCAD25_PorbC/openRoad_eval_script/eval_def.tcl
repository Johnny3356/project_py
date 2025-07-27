puts "reading lib.."
foreach libFile [glob "/mnt/c/Users/User202206/Desktop/project/iccad_c/ICCAD25_PorbC/ASAP7/LIB/*nldm*.lib"] {
    puts "lib: $libFile"
    read_liberty $libFile
}
puts "reading lef.."
read_lef /mnt/c/Users/User202206/Desktop/project/iccad_c/ICCAD25_PorbC/ASAP7/techlef/asap7_tech_1x_201209.lef
foreach lef [glob "/mnt/c/Users/User202206/Desktop/project/iccad_c/ICCAD25_PorbC/ASAP7/LEF/*.lef"] {
    read_lef $lef
}
puts "reading def.."
read_def /mnt/c/Users/User202206/Desktop/project/iccad_c/ICCAD25_PorbC/aes_cipher_top/aes_cipher_top.def
read_sdc /mnt/c/Users/User202206/Desktop/project/iccad_c/ICCAD25_PorbC/aes_cipher_top/aes_cipher_top.sdc
source /mnt/c/Users/User202206/Desktop/project/iccad_c/ICCAD25_PorbC/ASAP7/setRC.tcl
write_def /mnt/c/Users/User202206/Desktop/project/iccad_c/ICCAD25_PorbC/aes_cipher_top/placed_opt_before.def

estimate_parasitics -placement
# === 新增開始：呼叫 placement & timing 優化步驟 ===
# 全局佈局 (global placement)
puts "running global placement..."
#global_placement -timing_driven

# 精細佈局 (detailed placement)
puts "running detailed placement..."
#detailed_placement 

# 時序優化 (buffer 插入 / cell resize / cell move)
puts "running timing optimization..."
#repair_design
repair_timing -skip_buffer_removal -skip_pin_swap -skip_gate_cloning
# === 新增結束 ===

# (1) 输出最差 slack 数值（小数 3 位）
puts "=== Report Worst Slack (最差 slack) ==="
report_worst_slack -min -digits 3 

# (2) 输出 slack 分布 histogram（20 个区间）
puts "=== Report Slack Histogram ==="
report_timing_histogram -num_bins 20 -setup 

puts "writing optimized def.."
write_def /mnt/c/Users/User202206/Desktop/project/iccad_c/ICCAD25_PorbC/aes_cipher_top/placed_opt_after.def

puts "reporting timing.."
report_tns
report_wns
report_power
