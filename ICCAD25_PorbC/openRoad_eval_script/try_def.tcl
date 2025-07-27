set script_dir [file dirname [info script]]
set parent_dir [file normalize "$script_dir/.."]
set main_dir [file normalize "$script_dir/../.."]
puts "reading lib.."
foreach libFile [glob "$parent_dir/ASAP7/LIB/*nldm*.lib"] {
    puts "lib: $libFile"
    read_liberty $libFile
}
puts "reading lef.."
read_lef $parent_dir/ASAP7/techlef/asap7_tech_1x_201209.lef
foreach lef [glob "$parent_dir/ASAP7/LEF/*.lef"] {
    read_lef $lef
}
puts "reading def.."
read_def $parent_dir/aes_cipher_top/aes_cipher_top.def
read_sdc $parent_dir/aes_cipher_top/aes_cipher_top.sdc
source $parent_dir/ASAP7/setRC.tcl

write_def $main_dir/temp/placed_opt_before.def
estimate_parasitics -placement
repair_design
repair_timing -skip_pin_swap -skip_gate_cloning
puts "reporting timing.."
report_tns
report_wns
report_checks -path_delay max -fields {slew cap input nets fanout} -format full_clock_expanded -slack_max -0.000 -group_count 1000000 > setup.rpt
report_cell_usage -verbose
#report_instance -connections
puts "writing optimized def.."
write_def $main_dir/temp/placed_opt_after.def

report_power
exit